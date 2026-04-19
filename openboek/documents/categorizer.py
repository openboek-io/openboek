"""AI auto-categorization for documents and bank transactions.

Flow:
1. Check categorization_rules for exact match (instant, free)
2. If no rule, call Ollama for AI classification
3. Return category + confidence + account suggestion
4. After 3 user confirmations of same pattern, auto-create rule
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.config import settings

logger = logging.getLogger(__name__)

CONFIDENCE_AUTO = Decimal("0.85")

VALID_CATEGORIES = [
    "business_expense", "sales_income", "purchase_invoice",
    "salary", "tax_payment", "loan", "personal", "other",
]

CLASSIFY_PROMPT = """Je bent een Nederlandse boekhouder. Classificeer deze transactie.

Transactie:
- Tegenpartij: {counterparty}
- Bedrag: EUR {amount}
- Omschrijving: {description}
- IBAN tegenpartij: {iban}
- Richting: {direction}

Kies EXACT een categorie:
- business_expense (zakelijke uitgave: kantoor, reis, abonnement, inkoop)
- sales_income (omzet ontvangen van klant)
- purchase_invoice (inkoopfactuur betaald)
- salary (salaris of loonkosten)
- tax_payment (belasting betaald: BTW, IB, VPB)
- loan (lening aflossing of ontvangst)
- personal (prive, niet zakelijk)
- other (anders/onbekend)

Suggereer ook het meest waarschijnlijke grootboekrekening-type:
- Uitgaven: kantoorkosten, reiskosten, autokosten, telefoonkosten, verzekeringen, inkoop, overige kosten
- Inkomsten: omzet diensten, omzet producten, rente-inkomsten
- Belasting: BTW betaald, inkomstenbelasting, vennootschapsbelasting

Antwoord ALLEEN in JSON (geen uitleg, geen markdown):
{{"category": "...", "account_suggestion": "...", "confidence": 0.0}}"""


@dataclass
class CategorizationResult:
    category: str
    account_suggestion: str | None
    confidence: Decimal
    rule_id: str | None = None
    account_id: str | None = None


async def categorize_transaction(
    session: AsyncSession,
    entity_id: str,
    *,
    vendor_name: str | None = None,
    counterparty_name: str | None = None,
    counterparty_iban: str | None = None,
    amount: Decimal | None = None,
    description: str | None = None,
) -> CategorizationResult:
    """Categorize using rules first, then AI."""
    rule = await _match_rule(
        session, entity_id,
        vendor_name=vendor_name,
        counterparty_name=counterparty_name,
        counterparty_iban=counterparty_iban,
        description=description,
    )
    if rule:
        return rule

    return await _ai_classify(
        counterparty=counterparty_name or vendor_name or "",
        amount=amount or Decimal("0"),
        description=description or "",
        iban=counterparty_iban or "",
    )


async def _match_rule(
    session: AsyncSession,
    entity_id: str,
    *,
    vendor_name: str | None = None,
    counterparty_name: str | None = None,
    counterparty_iban: str | None = None,
    description: str | None = None,
) -> CategorizationResult | None:
    checks = []
    if counterparty_iban:
        checks.append(("counterparty_iban", counterparty_iban.strip()))
    if counterparty_name:
        checks.append(("counterparty_name", counterparty_name.strip().lower()))
    if vendor_name and vendor_name != counterparty_name:
        checks.append(("vendor_name", vendor_name.strip().lower()))

    for match_type, match_value in checks:
        result = await session.execute(
            text("""
                SELECT id, category, account_id, confidence
                FROM categorization_rules
                WHERE entity_id = :eid AND match_type = :mt AND lower(match_value) = :mv
                ORDER BY confidence DESC LIMIT 1
            """),
            {"eid": entity_id, "mt": match_type, "mv": match_value},
        )
        row = result.one_or_none()
        if row:
            await session.execute(
                text("UPDATE categorization_rules SET times_used = times_used + 1 WHERE id = :id"),
                {"id": row.id},
            )
            return CategorizationResult(
                category=row.category, account_suggestion=None,
                confidence=Decimal(str(row.confidence)),
                rule_id=str(row.id),
                account_id=str(row.account_id) if row.account_id else None,
            )

    if description:
        result = await session.execute(
            text("""
                SELECT id, category, account_id, confidence
                FROM categorization_rules
                WHERE entity_id = :eid AND match_type = 'description_contains'
                  AND lower(:desc) LIKE '%%' || lower(match_value) || '%%'
                ORDER BY confidence DESC LIMIT 1
            """),
            {"eid": entity_id, "desc": description},
        )
        row = result.one_or_none()
        if row:
            await session.execute(
                text("UPDATE categorization_rules SET times_used = times_used + 1 WHERE id = :id"),
                {"id": row.id},
            )
            return CategorizationResult(
                category=row.category, account_suggestion=None,
                confidence=Decimal(str(row.confidence)),
                rule_id=str(row.id),
                account_id=str(row.account_id) if row.account_id else None,
            )
    return None


async def _ai_classify(
    counterparty: str,
    amount: Decimal,
    description: str,
    iban: str,
) -> CategorizationResult:
    direction = "ontvangen (bij)" if amount >= 0 else "betaald (af)"
    prompt = CLASSIFY_PROMPT.format(
        counterparty=counterparty or "(onbekend)",
        amount=f"{abs(amount):.2f}",
        description=description or "(geen)",
        iban=iban or "(onbekend)",
        direction=direction,
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.ollama_url}/api/generate",
                json={"model": settings.ollama_model, "prompt": prompt, "stream": False, "options": {"temperature": 0.1}},
                timeout=60,
            )
            if resp.status_code != 200:
                logger.error("Ollama classify returned %d", resp.status_code)
                return CategorizationResult(category="other", account_suggestion=None, confidence=Decimal("0"))
            raw = resp.json().get("response", "")
            return _parse_ai_response(raw)
    except Exception as e:
        logger.error("AI categorization failed: %s", e)
        return CategorizationResult(category="other", account_suggestion=None, confidence=Decimal("0"))


def _parse_ai_response(raw: str) -> CategorizationResult:
    t = raw.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        t = "\n".join(lines).strip()
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(t[start:end + 1])
            cat = data.get("category", "other")
            if cat not in VALID_CATEGORIES:
                cat = "other"
            conf = min(max(float(data.get("confidence", 0)), 0), 1)
            return CategorizationResult(
                category=cat,
                account_suggestion=data.get("account_suggestion"),
                confidence=Decimal(str(round(conf, 2))),
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return CategorizationResult(category="other", account_suggestion=None, confidence=Decimal("0"))


async def record_confirmation(
    session: AsyncSession, entity_id: str, *,
    vendor_name: str | None = None,
    counterparty_name: str | None = None,
    counterparty_iban: str | None = None,
    category: str, account_id: str | None = None,
) -> None:
    """Record a correction/confirmation. Auto-create rule after 3."""
    match_type = match_value = None
    if counterparty_iban:
        match_type, match_value = "counterparty_iban", counterparty_iban.strip()
    elif counterparty_name:
        match_type, match_value = "counterparty_name", counterparty_name.strip().lower()
    elif vendor_name:
        match_type, match_value = "vendor_name", vendor_name.strip().lower()
    else:
        return

    await session.execute(
        text("""INSERT INTO categorization_confirmations
            (entity_id, match_type, match_value, category, account_id)
            VALUES (:eid, :mt, :mv, :cat, :aid)"""),
        {"eid": entity_id, "mt": match_type, "mv": match_value, "cat": category, "aid": account_id},
    )
    result = await session.execute(
        text("""SELECT count(*) FROM categorization_confirmations
            WHERE entity_id = :eid AND match_type = :mt AND lower(match_value) = lower(:mv) AND category = :cat"""),
        {"eid": entity_id, "mt": match_type, "mv": match_value, "cat": category},
    )
    if (result.scalar() or 0) >= 3:
        await session.execute(
            text("""INSERT INTO categorization_rules (entity_id, match_type, match_value, category, account_id)
                VALUES (:eid, :mt, :mv, :cat, :aid)
                ON CONFLICT (entity_id, match_type, match_value)
                DO UPDATE SET category = EXCLUDED.category, account_id = EXCLUDED.account_id, confidence = 1.00"""),
            {"eid": entity_id, "mt": match_type, "mv": match_value, "cat": category, "aid": account_id},
        )
        logger.info("Auto-created rule: %s=%s -> %s", match_type, match_value, category)
