"""Document processing service — orchestrates OCR, categorization, and journal entry creation.

This is the core engine. It:
1. Processes uploaded scans via OCR (Ollama minicpm-v)
2. Processes bank files via MT940/ING CSV parsers
3. Runs AI categorization on everything
4. Auto-creates journal entries for ALL items (no approval gate)
5. Flags failures for later correction
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.accounting.models import (
    Account, AccountType, JournalEntry, JournalLine, JournalStatus,
)
from openboek.config import settings
from openboek.documents.categorizer import (
    CONFIDENCE_AUTO, CategorizationResult, categorize_transaction,
)

logger = logging.getLogger(__name__)

FILE_STORAGE_ROOT = Path("/home/nano/openboek/uploads")

# Enhanced OCR prompt for full invoice extraction
OCR_INVOICE_PROMPT = """Analyseer dit document (factuur, bon, of zakelijk document). Extraheer ALLE details.

Antwoord ALLEEN in JSON (geen uitleg, geen markdown fences):
{{
  "document_type": "sales_invoice|purchase_invoice|receipt|credit_note|other",
  "invoice_number": null,
  "invoice_date": "YYYY-MM-DD",
  "due_date": null,
  "vendor_name": null,
  "vendor_kvk": null,
  "vendor_btw_number": null,
  "vendor_iban": null,
  "customer_name": null,
  "customer_kvk": null,
  "from_entity": "vendor_name or null",
  "to_entity": "customer_name or null",
  "line_items": [{{"description": "", "quantity": 1, "unit_price": 0.00, "btw_rate": 21, "amount": 0.00}}],
  "subtotal_excl": 0.00,
  "btw_amounts": {{"21": 0.00, "9": 0.00, "0": 0.00}},
  "total_incl": 0.00,
  "currency": "EUR",
  "payment_status": "unknown|paid|unpaid",
  "category_hint": "office|travel|food|telecom|insurance|hosting|software|professional_services|other",
  "confidence": 0.0
}}

BELANGRIJK:
- Als je een veld NIET kunt lezen, gebruik null. Verzin GEEN data.
- invoice_date is het BELANGRIJKSTE veld. Zoek naar "Factuurdatum", "Datum", "Date", "Invoice date".
- Detecteer of dit een VERKOOP factuur is (wij zijn de afzender) of INKOOP factuur (wij zijn de ontvanger).
- Bedragen: gebruik punt als decimaalteken, geen duizendtallen-scheiding.
- confidence: 0.0-1.0 hoe zeker je bent van de totale extractie."""


async def process_scan_document(
    session: AsyncSession,
    doc_id: str,
    file_path: str,
    entity_id: str,
    entity_name: str,
    entity_kvk: str | None = None,
) -> dict[str, Any]:
    """Process a scanned document: OCR -> categorize -> create journal entry.

    Returns dict with status info.
    """
    import base64
    import httpx

    path = Path(file_path)
    if not path.exists():
        await _update_doc_status(session, doc_id, "failed", ocr_status="failed")
        return {"status": "failed", "error": "File not found"}

    # 1. Run OCR
    await _update_doc_status(session, doc_id, "pending", ocr_status="processing")

    # Strategy: pdftotext first (fast, accurate for digital PDFs), vision OCR as fallback
    ocr_result = None
    if path.suffix.lower() == ".pdf":
        import subprocess
        try:
            text_result = subprocess.run(
                ["pdftotext", str(path), "-"],
                capture_output=True, text=True, timeout=30,
            )
            extracted_text = text_result.stdout.strip()
            # If we got meaningful text (>50 chars), use text-based extraction via LLM
            if len(extracted_text) > 50:
                logger.info("Using pdftotext for %s (%d chars)", doc_id, len(extracted_text))
                ocr_result = await _run_text_extraction(extracted_text)
        except Exception as e:
            logger.warning("pdftotext failed for %s: %s", doc_id, e)

    # Fallback: vision OCR for scanned docs or if text extraction failed
    if ocr_result is None or ocr_result.get("error"):
        logger.info("Falling back to vision OCR for %s", doc_id)
        if path.suffix.lower() == ".pdf":
            import subprocess
            img_path = path.with_suffix(".png")
            try:
                subprocess.run(
                    ["pdftoppm", "-png", "-f", "1", "-l", "1", "-r", "200", "-singlefile", str(path), str(img_path.with_suffix(""))],
                    check=True, capture_output=True, timeout=30,
                )
                with open(img_path, "rb") as f:
                    image_b64 = base64.b64encode(f.read()).decode("utf-8")
                img_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("PDF conversion failed for %s: %s", doc_id, e)
                with open(path, "rb") as f:
                    image_b64 = base64.b64encode(f.read()).decode("utf-8")
        else:
            with open(path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode("utf-8")
        ocr_result = await _run_ocr(image_b64)
    ocr_error = ocr_result.get("error")

    if ocr_error:
        await session.execute(
            text("""UPDATE documents SET ocr_status = 'failed', ocr_result = :r,
                    review_status = 'failed' WHERE id = :id"""),
            {"r": json.dumps(ocr_result), "id": doc_id},
        )
        return {"status": "failed", "error": ocr_error}

    # 2. Determine direction (sales vs purchase)
    direction = _detect_direction(ocr_result, entity_name, entity_kvk)
    if direction == "sales":
        ocr_result["document_type"] = "sales_invoice"
    elif direction == "purchase":
        ocr_result["document_type"] = "purchase_invoice"

    # 3. Extract key fields
    vendor = ocr_result.get("vendor_name") or ocr_result.get("from_entity") or ""
    customer = ocr_result.get("customer_name") or ocr_result.get("to_entity") or ""
    total_incl = _to_decimal(ocr_result.get("total_incl"))
    total_excl = _to_decimal(ocr_result.get("subtotal_excl"))
    invoice_date = _parse_date(ocr_result.get("invoice_date"))
    vendor_iban = ocr_result.get("vendor_iban")

    # Calculate BTW
    btw_amounts = ocr_result.get("btw_amounts", {})
    btw_total = Decimal("0")
    for rate, amt in btw_amounts.items():
        v = _to_decimal(amt)
        if v:
            btw_total += v

    if total_incl and total_excl and not btw_total:
        btw_total = total_incl - total_excl
    elif total_incl and btw_total and not total_excl:
        total_excl = total_incl - btw_total
    elif total_excl and btw_total and not total_incl:
        total_incl = total_excl + btw_total

    # Use invoice date for historical import, fallback to today
    tx_date = invoice_date or date.today()

    # Determine the counterparty for categorization
    if direction == "sales":
        counterparty = customer
        amount = total_incl or Decimal("0")
    else:
        counterparty = vendor
        amount = -(total_incl or Decimal("0")) if total_incl else Decimal("0")

    # 4. Categorize
    cat_result = await categorize_transaction(
        session, entity_id,
        vendor_name=vendor if direction != "sales" else None,
        counterparty_name=counterparty,
        counterparty_iban=vendor_iban,
        amount=amount,
        description=ocr_result.get("category_hint", ""),
    )

    # Override category based on document type detection
    if direction == "sales":
        cat_result = CategorizationResult(
            category="sales_income",
            account_suggestion=cat_result.account_suggestion,
            confidence=max(cat_result.confidence, Decimal("0.80")),
            rule_id=cat_result.rule_id,
            account_id=cat_result.account_id,
        )
    elif direction == "purchase":
        cat_result = CategorizationResult(
            category="purchase_invoice",
            account_suggestion=cat_result.account_suggestion,
            confidence=max(cat_result.confidence, Decimal("0.80")),
            rule_id=cat_result.rule_id,
            account_id=cat_result.account_id,
        )

    # 5. Create journal entry
    je_id = None
    review_status = "auto_processed"
    try:
        je_id = await _create_journal_entry(
            session, entity_id=entity_id, tx_date=tx_date,
            description=f"{vendor or customer}: {ocr_result.get('invoice_number', '')}".strip(": "),
            reference=ocr_result.get("invoice_number"),
            total_incl=total_incl, total_excl=total_excl, btw_total=btw_total,
            category=cat_result.category, account_id=cat_result.account_id,
            account_suggestion=cat_result.account_suggestion,
            counterparty=counterparty,
        )
    except Exception as e:
        logger.error("Journal creation failed for doc %s: %s", doc_id, e)
        review_status = "needs_review"

    # 6. Update document record
    await session.execute(
        text("""UPDATE documents SET
            ocr_status = 'completed', ocr_result = :ocr,
            vendor_name = :vendor, transaction_date = :tdate,
            amount = :amt, amount_excl = :excl, btw_amount = :btw, btw_rate = :brate,
            description = :desc, counterparty_iban = :iban,
            category = :cat, ai_category = :aicat, ai_account_suggestion = :aisug,
            ai_confidence = :conf, rule_id = :rid, account_id = :aid,
            journal_entry_id = :jeid, review_status = :rstatus
            WHERE id = :id"""),
        {
            "ocr": json.dumps(ocr_result), "vendor": vendor or customer,
            "tdate": tx_date, "amt": total_incl, "excl": total_excl,
            "btw": btw_total if btw_total else None,
            "brate": _to_decimal(ocr_result.get("btw_rate")) if "btw_rate" in ocr_result else None,
            "desc": ocr_result.get("invoice_number", ""),
            "iban": vendor_iban, "cat": cat_result.category,
            "aicat": cat_result.category, "aisug": cat_result.account_suggestion,
            "conf": cat_result.confidence, "rid": cat_result.rule_id,
            "aid": cat_result.account_id, "jeid": je_id,
            "rstatus": review_status, "id": doc_id,
        },
    )

    return {
        "status": review_status,
        "journal_entry_id": str(je_id) if je_id else None,
        "category": cat_result.category,
        "confidence": float(cat_result.confidence),
    }


async def process_bank_transactions(
    session: AsyncSession,
    entity_id: str,
    user_id: str,
    batch_id: str,
    transactions: list,
    entity_name: str,
) -> dict[str, Any]:
    """Process parsed bank transactions: categorize all, create journal entries.

    Returns summary dict.
    """
    auto_count = 0
    review_count = 0
    failed_count = 0

    for tx in transactions:
        doc_id = str(uuid.uuid4())
        tx_date = tx.date if isinstance(tx.date, date) else date.today()

        # Categorize
        try:
            cat_result = await categorize_transaction(
                session, entity_id,
                counterparty_name=tx.counterparty_name,
                counterparty_iban=tx.counterparty_iban,
                amount=tx.amount,
                description=tx.description,
            )
        except Exception as e:
            logger.error("Categorization failed: %s", e)
            cat_result = CategorizationResult(
                category="other", account_suggestion=None, confidence=Decimal("0"),
            )

        # Create journal entry
        je_id = None
        review_status = "auto_processed"
        try:
            # Determine category from amount direction if AI unsure
            if cat_result.category == "other" and cat_result.confidence < Decimal("0.3"):
                if tx.amount > 0:
                    cat_result = CategorizationResult(
                        category="sales_income", account_suggestion="omzet",
                        confidence=Decimal("0.50"),
                    )
                else:
                    cat_result = CategorizationResult(
                        category="business_expense", account_suggestion="overige kosten",
                        confidence=Decimal("0.50"),
                    )

            je_id = await _create_journal_entry(
                session, entity_id=entity_id, tx_date=tx_date,
                description=f"{tx.counterparty_name}: {tx.description[:80]}".strip(": "),
                reference=tx.reference,
                total_incl=abs(tx.amount), total_excl=None, btw_total=None,
                category=cat_result.category, account_id=cat_result.account_id,
                account_suggestion=cat_result.account_suggestion,
                counterparty=tx.counterparty_name,
                is_bank=True, bank_amount=tx.amount,
            )
            auto_count += 1
        except Exception as e:
            logger.error("Journal creation failed for bank tx: %s", e)
            review_status = "needs_review"
            review_count += 1

        # Insert document record
        await session.execute(
            text("""INSERT INTO documents
                (id, entity_id, user_id, source, batch_id,
                 ocr_status, vendor_name, transaction_date, amount,
                 description, counterparty_iban,
                 category, ai_category, ai_account_suggestion, ai_confidence,
                 rule_id, account_id, journal_entry_id, review_status)
                VALUES (:id, :eid, :uid, 'bank', :bid,
                 'not_needed', :vendor, :tdate, :amt,
                 :desc, :iban,
                 :cat, :aicat, :aisug, :conf,
                 :rid, :aid, :jeid, :rstatus)"""),
            {
                "id": doc_id, "eid": entity_id, "uid": user_id, "bid": batch_id,
                "vendor": tx.counterparty_name, "tdate": tx_date, "amt": tx.amount,
                "desc": tx.description[:500] if tx.description else "",
                "iban": tx.counterparty_iban,
                "cat": cat_result.category, "aicat": cat_result.category,
                "aisug": cat_result.account_suggestion,
                "conf": cat_result.confidence,
                "rid": cat_result.rule_id, "aid": cat_result.account_id,
                "jeid": je_id, "rstatus": review_status,
            },
        )

    return {"auto_processed": auto_count, "needs_review": review_count, "failed": failed_count}


async def _run_text_extraction(text: str) -> dict[str, Any]:
    """Extract invoice data from PDF text using LLM (Gemma 4 on node3)."""
    import httpx

    prompt = f"""Analyseer deze factuur-tekst en extraheer ALLE details.
Antwoord ALLEEN in JSON (geen uitleg, geen markdown):
{{
  "document_type": "sales_invoice|purchase_invoice|receipt|credit_note|other",
  "invoice_number": null,
  "invoice_date": "YYYY-MM-DD",
  "due_date": null,
  "vendor_name": null,
  "vendor_kvk": null,
  "vendor_btw_number": null,
  "vendor_iban": null,
  "customer_name": null,
  "total_incl": 0.00,
  "subtotal_excl": 0.00,
  "btw_amounts": {{"21": 0.00, "9": 0.00, "0": 0.00}},
  "line_items": [{{"description": "", "quantity": null, "unit_price": 0.00, "btw_rate": 21, "amount": 0.00}}],
  "payment_status": null,
  "currency": "EUR",
  "confidence": 0.95,
  "category_hint": "",
  "from_entity": null,
  "to_entity": null
}}

Belangrijk:
- Bedragen: gebruik punt als decimaalteken (88.27 niet 88,27)
- Lees het EXACTE bedrag van de factuur, niet een geschat bedrag
- Kijk naar "Totaalprijs incl. BTW" of "Total" voor het totaalbedrag

FACTUUR TEKST:
{text[:3000]}"""

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=120,
            )
            if resp.status_code != 200:
                return {"error": f"LLM returned status {resp.status_code}"}
            raw = resp.json().get("response", "")
            return _parse_ocr_json(raw)
    except httpx.ConnectError:
        return {"error": "LLM service unavailable"}


async def _run_ocr(image_b64: str) -> dict[str, Any]:
    """Run OCR via Ollama minicpm-v."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                "http://127.0.0.1:11434/api/generate",
                json={
                    "model": "minicpm-v:8b",
                    "prompt": OCR_INVOICE_PROMPT,
                    "images": [image_b64],
                    "stream": False,
                },
                timeout=180,
            )
            if resp.status_code != 200:
                return {"error": f"OCR returned status {resp.status_code}"}
            raw = resp.json().get("response", "")
            return _parse_ocr_json(raw)
    except httpx.ConnectError:
        return {"error": "OCR service unavailable (Ollama offline)"}
    except httpx.TimeoutException:
        return {"error": "OCR timed out"}
    except Exception as e:
        return {"error": str(e)}


def _parse_ocr_json(raw: str) -> dict[str, Any]:
    """Extract JSON from OCR response."""
    t = raw.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        t = "\n".join(lines).strip()
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end > start:
        try:
            result = json.loads(t[start:end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    return {"error": "Could not parse OCR result", "raw_text": raw[:500]}


def _detect_direction(
    ocr_result: dict, entity_name: str, entity_kvk: str | None,
) -> str:
    """Detect if this is a sales or purchase invoice based on entity name/KVK."""
    entity_lower = entity_name.lower() if entity_name else ""
    from_entity = (ocr_result.get("from_entity") or "").lower()
    to_entity = (ocr_result.get("to_entity") or "").lower()
    vendor = (ocr_result.get("vendor_name") or "").lower()
    customer = (ocr_result.get("customer_name") or "").lower()

    # Check KVK match
    if entity_kvk:
        vendor_kvk = ocr_result.get("vendor_kvk") or ""
        customer_kvk = ocr_result.get("customer_kvk") or ""
        if entity_kvk in vendor_kvk:
            return "sales"
        if entity_kvk in customer_kvk:
            return "purchase"

    # Check name match
    if entity_lower:
        if entity_lower in from_entity or entity_lower in vendor:
            return "sales"
        if entity_lower in to_entity or entity_lower in customer:
            return "purchase"

    # Default based on document_type field from OCR
    doc_type = ocr_result.get("document_type", "")
    if doc_type == "sales_invoice":
        return "sales"
    if doc_type in ("purchase_invoice", "receipt"):
        return "purchase"

    return "purchase"  # Default: assume we received it


async def _create_journal_entry(
    session: AsyncSession,
    *,
    entity_id: str,
    tx_date: date,
    description: str,
    reference: str | None,
    total_incl: Decimal | None,
    total_excl: Decimal | None,
    btw_total: Decimal | None,
    category: str,
    account_id: str | None,
    account_suggestion: str | None,
    counterparty: str,
    is_bank: bool = False,
    bank_amount: Decimal | None = None,
) -> str | None:
    """Create a journal entry with appropriate debit/credit lines.

    Returns journal entry ID or None.
    """
    if is_bank:
        amount = abs(bank_amount) if bank_amount else Decimal("0")
        is_income = bank_amount and bank_amount > 0
    else:
        amount = total_incl or Decimal("0")
        is_income = category in ("sales_income",)

    if amount <= 0 and not is_bank:
        return None  # No amount, can't create entry

    # Get accounts for this entity
    result = await session.execute(
        select(Account).where(Account.entity_id == entity_id)
    )
    accounts = list(result.scalars().all())
    by_code = {a.code: a for a in accounts}

    # Find matching account
    target_account = None
    if account_id:
        target_account = next((a for a in accounts if str(a.id) == account_id), None)

    if not target_account and account_suggestion:
        # Try to match suggestion to an account name
        suggestion_lower = account_suggestion.lower()
        for a in accounts:
            if suggestion_lower in a.name_nl.lower() or suggestion_lower in a.name_en.lower():
                target_account = a
                break

    # Fallback accounts based on category
    if not target_account:
        fallback_map = {
            "business_expense": ["4000", "4900", "4100"],  # General expenses
            "purchase_invoice": ["4000", "4900", "7000"],
            "sales_income": ["8000", "8010", "8100"],  # Revenue
            "salary": ["4200", "4210"],
            "tax_payment": ["1520", "1500"],
            "loan": ["0700", "0710"],
            "personal": ["0300", "0310"],  # Prive
            "other": ["4900", "4000"],
        }
        for code in fallback_map.get(category, ["4900"]):
            if code in by_code:
                target_account = by_code[code]
                break
        # Last resort: first expense or revenue account
        if not target_account:
            target_type = AccountType.expense if not is_income else AccountType.revenue
            target_account = next(
                (a for a in accounts if a.account_type == target_type), None
            )

    # Find contra accounts
    bank_account = by_code.get("1100") or by_code.get("1000")  # Bank / Kas
    debtors_account = by_code.get("1300") or by_code.get("1310")  # Debiteuren
    creditors_account = by_code.get("1600") or by_code.get("1610")  # Crediteuren
    btw_input_account = by_code.get("1510")  # BTW voorbelasting
    btw_output_account = by_code.get("1520")  # BTW afdracht

    if not target_account:
        logger.warning("No target account found for entity %s, category %s", entity_id, category)
        return None

    # Create journal entry
    je_id = str(uuid.uuid4())
    await session.execute(
        text("""INSERT INTO journal_entries (id, entity_id, date, reference, description, status)
            VALUES (:id, :eid, :d, :ref, :desc, 'draft')"""),
        {"id": je_id, "eid": entity_id, "d": tx_date, "ref": reference or "",
         "desc": description[:500] if description else ""},
    )

    if is_bank:
        # Bank transaction: bank account vs expense/revenue
        if is_income:
            # Money in: debit bank, credit revenue
            await _add_line(session, je_id, bank_account, debit=amount)
            await _add_line(session, je_id, target_account, credit=amount, desc=counterparty)
        else:
            # Money out: debit expense, credit bank
            await _add_line(session, je_id, target_account, debit=amount, desc=counterparty)
            await _add_line(session, je_id, bank_account, credit=amount)
    elif category == "sales_income":
        # Sales invoice: debit debiteuren, credit revenue + BTW
        contra = debtors_account or bank_account
        if contra:
            await _add_line(session, je_id, contra, debit=amount, desc=counterparty)
        if total_excl and btw_total and btw_output_account:
            await _add_line(session, je_id, target_account, credit=total_excl, desc=counterparty)
            await _add_line(session, je_id, btw_output_account, credit=btw_total, desc=f"BTW - {counterparty}")
        else:
            await _add_line(session, je_id, target_account, credit=amount, desc=counterparty)
    else:
        # Purchase/expense: debit expense + BTW input, credit crediteuren
        contra = creditors_account or bank_account
        if total_excl and btw_total and btw_input_account:
            await _add_line(session, je_id, target_account, debit=total_excl, desc=counterparty)
            await _add_line(session, je_id, btw_input_account, debit=btw_total, desc=f"BTW - {counterparty}")
        else:
            await _add_line(session, je_id, target_account, debit=amount, desc=counterparty)
        if contra:
            await _add_line(session, je_id, contra, credit=amount, desc=counterparty)

    return je_id


async def _add_line(
    session: AsyncSession, je_id: str, account: Account | None,
    debit: Decimal | None = None, credit: Decimal | None = None,
    desc: str = "",
) -> None:
    """Add a journal line."""
    if not account:
        return
    await session.execute(
        text("""INSERT INTO journal_lines (id, entry_id, account_id, debit, credit, description)
            VALUES (:id, :eid, :aid, :d, :c, :desc)"""),
        {
            "id": str(uuid.uuid4()), "eid": je_id, "aid": str(account.id),
            "d": debit or Decimal("0"), "c": credit or Decimal("0"), "desc": desc[:255],
        },
    )


async def _update_doc_status(
    session: AsyncSession, doc_id: str, review_status: str,
    ocr_status: str | None = None,
) -> None:
    if ocr_status:
        await session.execute(
            text("UPDATE documents SET review_status = :rs, ocr_status = :os WHERE id = :id"),
            {"rs": review_status, "os": ocr_status, "id": doc_id},
        )
    else:
        await session.execute(
            text("UPDATE documents SET review_status = :rs WHERE id = :id"),
            {"rs": review_status, "id": doc_id},
        )


def _to_decimal(val: Any) -> Decimal | None:
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except Exception:
        return None


def _parse_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val))
    except (ValueError, TypeError):
        return None
