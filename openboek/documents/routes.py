"""Document upload, processing, and results routes.

Flow: Upload files -> auto-process everything -> show results.
No approval gate. Correction interface available after the fact.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.accounting.models import Account
from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.db import get_session
from openboek.documents.service import FILE_STORAGE_ROOT, process_bank_transactions
from openboek.entities.models import Entity
from openboek.tasks.queue import enqueue

router = APIRouter(tags=["documents"])


def _templates():
    from openboek.main import templates
    return templates


# ---------------------------------------------------------------------------
# Upload page
# ---------------------------------------------------------------------------

@router.get("/entities/{entity_id}/upload", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show the bulk upload page."""
    entity = await get_entity_for_user(entity_id, user, session)

    # Get recent batches for this entity
    recent = await session.execute(
        text("""SELECT batch_id, min(created_at) as started, count(*) as total,
                count(*) FILTER (WHERE review_status = 'auto_processed') as processed,
                count(*) FILTER (WHERE review_status = 'needs_review') as needs_review,
                count(*) FILTER (WHERE review_status = 'failed') as failed,
                count(*) FILTER (WHERE review_status = 'pending') as pending
            FROM documents WHERE entity_id = :eid AND batch_id IS NOT NULL
            GROUP BY batch_id ORDER BY min(created_at) DESC LIMIT 5"""),
        {"eid": str(entity_id)},
    )
    batches = [dict(r._mapping) for r in recent.fetchall()]

    return _templates().TemplateResponse(request, "documents/upload.html", {
        "entity": entity, "user": user, "lang": user.preferred_lang,
        "batches": batches,
    })


# ---------------------------------------------------------------------------
# File upload endpoint (multipart, multiple files)
# ---------------------------------------------------------------------------

@router.post("/entities/{entity_id}/upload/files")
async def upload_files(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Accept multiple files, store them, dispatch processing.

    Handles:
    - Images/PDFs -> queue for OCR processing
    - CSV/MT940 -> parse immediately, create bank documents
    """
    entity = await get_entity_for_user(entity_id, user, session)
    form = await request.form()
    files = form.getlist("files")

    if not files:
        return JSONResponse({"error": "Geen bestanden ontvangen"}, status_code=400)

    batch_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    year = str(now.year)
    month = f"{now.month:02d}"

    scan_count = 0
    bank_count = 0
    errors = []

    for upload_file in files:
        if not hasattr(upload_file, "filename"):
            continue

        filename = upload_file.filename or "unknown"
        content = await upload_file.read()
        if not content:
            errors.append(f"{filename}: leeg bestand")
            continue

        ext = Path(filename).suffix.lower()
        mime = upload_file.content_type or ""

        # Detect bank files
        is_bank = False
        if ext in (".csv", ".sta", ".mt940", ".swi", ".txt"):
            try:
                text_content = content.decode("utf-8", errors="replace")
                is_bank = _detect_bank_file(text_content, ext)
            except Exception:
                pass

        if is_bank:
            # Parse bank file immediately
            try:
                text_content = content.decode("utf-8", errors="replace")
                transactions = _parse_bank_content(text_content, ext)
                if not transactions:
                    errors.append(f"{filename}: geen transacties gevonden")
                    continue

                result = await process_bank_transactions(
                    session, str(entity_id), str(user.id), batch_id,
                    transactions, entity.name,
                )
                bank_count += len(transactions)
            except Exception as e:
                errors.append(f"{filename}: {str(e)[:100]}")
                continue
        else:
            # Store file on disk, queue for OCR
            doc_id = str(uuid.uuid4())
            safe_ext = ext if ext in (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".bmp", ".heic") else ".bin"
            storage_dir = FILE_STORAGE_ROOT / str(entity_id) / year / month
            storage_dir.mkdir(parents=True, exist_ok=True)
            storage_path = storage_dir / f"{doc_id}{safe_ext}"

            with open(storage_path, "wb") as f:
                f.write(content)

            # Insert document record
            await session.execute(
                text("""INSERT INTO documents
                    (id, entity_id, user_id, source, batch_id,
                     original_filename, storage_path, mime_type, file_size_bytes,
                     ocr_status, review_status)
                    VALUES (:id, :eid, :uid, 'scan', :bid,
                     :fname, :path, :mime, :size,
                     'pending', 'pending')"""),
                {
                    "id": doc_id, "eid": str(entity_id), "uid": str(user.id),
                    "bid": batch_id, "fname": filename,
                    "path": str(storage_path), "mime": mime,
                    "size": len(content),
                },
            )

            # Enqueue OCR + processing task
            await enqueue(
                session, "process_document",
                payload={
                    "doc_id": doc_id,
                    "file_path": str(storage_path),
                    "entity_id": str(entity_id),
                    "entity_name": entity.name,
                    "entity_kvk": entity.kvk_number,
                },
                priority=5,
            )
            scan_count += 1

    return JSONResponse({
        "batch_id": batch_id,
        "scans_queued": scan_count,
        "bank_imported": bank_count,
        "errors": errors,
        "redirect": f"/entities/{entity_id}/upload/batch/{batch_id}",
    })


# ---------------------------------------------------------------------------
# Batch results page
# ---------------------------------------------------------------------------

@router.get("/entities/{entity_id}/upload/batch/{batch_id}", response_class=HTMLResponse)
async def batch_results(
    request: Request,
    entity_id: uuid.UUID,
    batch_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show results of a batch import — what was created, what failed."""
    entity = await get_entity_for_user(entity_id, user, session)

    # Summary stats
    stats = await session.execute(
        text("""SELECT
            count(*) as total,
            count(*) FILTER (WHERE review_status = 'auto_processed') as auto_ok,
            count(*) FILTER (WHERE review_status = 'needs_review') as needs_review,
            count(*) FILTER (WHERE review_status = 'failed') as failed,
            count(*) FILTER (WHERE review_status = 'pending') as pending,
            count(*) FILTER (WHERE source = 'scan') as scan_count,
            count(*) FILTER (WHERE source = 'bank') as bank_count,
            min(transaction_date) as date_from,
            max(transaction_date) as date_to,
            coalesce(sum(amount) FILTER (WHERE amount > 0), 0) as total_income,
            coalesce(sum(abs(amount)) FILTER (WHERE amount < 0 OR (amount > 0 AND category IN ('business_expense','purchase_invoice'))), 0) as total_expense
            FROM documents WHERE entity_id = :eid AND batch_id = :bid"""),
        {"eid": str(entity_id), "bid": batch_id},
    )
    summary = dict(stats.fetchone()._mapping)

    # All documents in batch, grouped by month
    docs_result = await session.execute(
        text("""SELECT d.*, je.status as je_status
            FROM documents d
            LEFT JOIN journal_entries je ON je.id = d.journal_entry_id
            WHERE d.entity_id = :eid AND d.batch_id = :bid
            ORDER BY d.transaction_date ASC NULLS LAST, d.created_at ASC"""),
        {"eid": str(entity_id), "bid": batch_id},
    )
    docs = [dict(r._mapping) for r in docs_result.fetchall()]

    # Group by month
    months = {}
    for d in docs:
        td = d.get("transaction_date")
        if td:
            key = td.strftime("%Y-%m") if hasattr(td, "strftime") else str(td)[:7]
        else:
            key = "onbekend"
        months.setdefault(key, []).append(d)

    return _templates().TemplateResponse(request, "documents/batch_results.html", {
        "entity": entity, "user": user, "lang": user.preferred_lang,
        "batch_id": batch_id, "summary": summary,
        "documents": docs, "months": months,
    })


# ---------------------------------------------------------------------------
# Batch status (HTMX polling for processing progress)
# ---------------------------------------------------------------------------

@router.get("/entities/{entity_id}/upload/batch/{batch_id}/status")
async def batch_status(
    request: Request,
    entity_id: uuid.UUID,
    batch_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return batch processing status as HTML partial (for HTMX polling)."""
    entity = await get_entity_for_user(entity_id, user, session)

    stats = await session.execute(
        text("""SELECT
            count(*) as total,
            count(*) FILTER (WHERE review_status = 'auto_processed') as auto_ok,
            count(*) FILTER (WHERE review_status = 'needs_review') as needs_review,
            count(*) FILTER (WHERE review_status = 'failed') as failed,
            count(*) FILTER (WHERE review_status = 'pending') as pending
            FROM documents WHERE entity_id = :eid AND batch_id = :bid"""),
        {"eid": str(entity_id), "bid": batch_id},
    )
    s = dict(stats.fetchone()._mapping)

    return _templates().TemplateResponse(request, "documents/_batch_progress.html", {
        "entity": entity, "batch_id": batch_id, "summary": s,
    })


# ---------------------------------------------------------------------------
# Single document detail / correction
# ---------------------------------------------------------------------------

@router.get("/entities/{entity_id}/upload/doc/{doc_id}", response_class=HTMLResponse)
async def document_detail(
    request: Request,
    entity_id: uuid.UUID,
    doc_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show single document detail with correction form."""
    entity = await get_entity_for_user(entity_id, user, session)

    doc_result = await session.execute(
        text("SELECT * FROM documents WHERE id = :id AND entity_id = :eid"),
        {"id": doc_id, "eid": str(entity_id)},
    )
    doc = doc_result.one_or_none()
    if not doc:
        return RedirectResponse(f"/entities/{entity_id}/upload", status_code=303)
    doc = dict(doc._mapping)

    # Parse OCR result
    ocr = {}
    if doc.get("ocr_result"):
        try:
            ocr = json.loads(doc["ocr_result"]) if isinstance(doc["ocr_result"], str) else doc["ocr_result"]
        except (json.JSONDecodeError, TypeError):
            pass

    # Get accounts
    accounts_result = await session.execute(
        select(Account).where(Account.entity_id == entity_id).order_by(Account.code)
    )
    accounts = list(accounts_result.scalars().all())

    return _templates().TemplateResponse(request, "documents/detail.html", {
        "entity": entity, "user": user, "lang": user.preferred_lang,
        "doc": doc, "ocr": ocr, "accounts": accounts,
    })


# ---------------------------------------------------------------------------
# Correction endpoint
# ---------------------------------------------------------------------------

@router.post("/entities/{entity_id}/upload/doc/{doc_id}/correct")
async def correct_document(
    request: Request,
    entity_id: uuid.UUID,
    doc_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Apply corrections to a document — update category, amount, re-create journal entry."""
    entity = await get_entity_for_user(entity_id, user, session)
    form = await request.form()

    category = form.get("category", "other")
    vendor_name = form.get("vendor_name", "")
    amount_str = form.get("amount", "0")
    amount_excl_str = form.get("amount_excl", "0")
    btw_str = form.get("btw_amount", "0")
    date_str = form.get("transaction_date", "")
    account_id = form.get("account_id") or None
    notes = form.get("notes", "")

    try:
        amount = Decimal(amount_str)
    except Exception:
        amount = Decimal("0")
    try:
        amount_excl = Decimal(amount_excl_str) if amount_excl_str else None
    except Exception:
        amount_excl = None
    try:
        btw_amount = Decimal(btw_str) if btw_str else None
    except Exception:
        btw_amount = None
    try:
        tx_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        tx_date = date.today()

    # Get existing doc
    doc_result = await session.execute(
        text("SELECT * FROM documents WHERE id = :id AND entity_id = :eid"),
        {"id": doc_id, "eid": str(entity_id)},
    )
    doc = doc_result.one_or_none()
    if not doc:
        return RedirectResponse(f"/entities/{entity_id}/upload", status_code=303)
    doc = dict(doc._mapping)

    # Delete old journal entry if exists
    old_je = doc.get("journal_entry_id")
    if old_je:
        await session.execute(text("DELETE FROM journal_lines WHERE entry_id = :id"), {"id": old_je})
        await session.execute(text("DELETE FROM journal_entries WHERE id = :id"), {"id": old_je})

    # Create new journal entry with corrected data
    from openboek.documents.service import _create_journal_entry

    accounts_result = await session.execute(
        select(Account).where(Account.entity_id == entity_id)
    )

    je_id = await _create_journal_entry(
        session, entity_id=str(entity_id), tx_date=tx_date,
        description=vendor_name, reference=doc.get("description") or "",
        total_incl=amount, total_excl=amount_excl, btw_total=btw_amount,
        category=category, account_id=account_id,
        account_suggestion=None, counterparty=vendor_name,
    )

    # Update document
    await session.execute(
        text("""UPDATE documents SET
            category = :cat, vendor_name = :vendor, amount = :amt,
            amount_excl = :excl, btw_amount = :btw, transaction_date = :td,
            account_id = :aid, journal_entry_id = :jeid,
            review_status = 'reviewed', reviewed_at = now(), notes = :notes
            WHERE id = :id"""),
        {
            "cat": category, "vendor": vendor_name, "amt": amount,
            "excl": amount_excl, "btw": btw_amount, "td": tx_date,
            "aid": account_id, "jeid": je_id,
            "notes": notes, "id": doc_id,
        },
    )

    # Record confirmation for rule learning
    from openboek.documents.categorizer import record_confirmation
    await record_confirmation(
        session, str(entity_id),
        vendor_name=vendor_name,
        counterparty_name=doc.get("vendor_name"),
        counterparty_iban=doc.get("counterparty_iban"),
        category=category, account_id=account_id,
    )

    # Redirect to batch or back to detail
    batch_id = doc.get("batch_id")
    if batch_id:
        return RedirectResponse(
            f"/entities/{entity_id}/upload/batch/{batch_id}?success=Document+gecorrigeerd",
            status_code=303,
        )
    return RedirectResponse(
        f"/entities/{entity_id}/upload/doc/{doc_id}?success=Gecorrigeerd",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Serve uploaded files
# ---------------------------------------------------------------------------

@router.get("/entities/{entity_id}/upload/file/{doc_id}")
async def serve_file(
    request: Request,
    entity_id: uuid.UUID,
    doc_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Serve an uploaded document file."""
    entity = await get_entity_for_user(entity_id, user, session)

    doc_result = await session.execute(
        text("SELECT storage_path, mime_type FROM documents WHERE id = :id AND entity_id = :eid"),
        {"id": doc_id, "eid": str(entity_id)},
    )
    doc = doc_result.one_or_none()
    if not doc or not doc.storage_path:
        return JSONResponse({"error": "not found"}, status_code=404)

    from fastapi.responses import FileResponse
    return FileResponse(doc.storage_path, media_type=doc.mime_type or "application/octet-stream")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_bank_file(content: str, ext: str) -> bool:
    """Detect if content is a bank statement file."""
    if ext in (".sta", ".mt940", ".swi"):
        return True
    if ext == ".csv":
        from openboek.banking.ing_csv import detect_ing_csv
        if detect_ing_csv(content):
            return True
    # MT940 content detection
    if ":20:" in content[:500] and ":60F:" in content[:2000]:
        return True
    return False


def _parse_bank_content(content: str, ext: str):
    """Parse bank file content into transactions."""
    # Try ING CSV first
    from openboek.banking.ing_csv import detect_ing_csv, parse_ing_csv
    if detect_ing_csv(content):
        return parse_ing_csv(content)

    # Try MT940
    if ext in (".sta", ".mt940", ".swi") or (":20:" in content[:500]):
        from openboek.banking.mt940 import parse_mt940
        return parse_mt940(content)

    # Generic CSV — try ING format as fallback
    if ext == ".csv":
        return parse_ing_csv(content)

    return []
