"""Proactive AI Advisor — analyzes books and generates money-saving insights.

Runs on-demand or scheduled; stores insights in the insights table.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.accounting.models import (
    Account,
    AccountType,
    JournalEntry,
    JournalLine,
    JournalStatus,
)
from openboek.config import settings
from openboek.entities.models import Entity

logger = logging.getLogger(__name__)


async def run_advisor(
    session: AsyncSession,
    entity_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> list[dict]:
    """Run proactive analysis for an entity and store insights.

    Returns list of newly created insight dicts.
    """
    insights: list[dict] = []

    # Gather financial data
    today = date.today()
    year = today.year

    # Get entity info
    entity_result = await session.execute(
        select(Entity).where(Entity.id == entity_id)
    )
    entity = entity_result.scalar_one_or_none()
    if not entity:
        return []

    # 1. BTW deadline check
    quarter = (today.month - 1) // 3 + 1
    btw_deadline = date(year, quarter * 3 + 1, 30) if quarter < 4 else date(year + 1, 1, 31)
    if quarter == 4:
        btw_deadline = date(year + 1, 1, 31)
    else:
        import calendar
        deadline_month = quarter * 3 + 1
        _, last_day = calendar.monthrange(year, deadline_month)
        btw_deadline = date(year, deadline_month, min(30, last_day))

    days_until = (btw_deadline - today).days
    if 0 < days_until <= 30:
        insights.append({
            "category": "deadline",
            "title_nl": f"BTW-aangifte Q{quarter} {year} — deadline over {days_until} dagen",
            "title_en": f"BTW return Q{quarter} {year} — deadline in {days_until} days",
            "description_nl": f"De BTW-aangifte voor Q{quarter} {year} moet vóór {btw_deadline.strftime('%d-%m-%Y')} worden ingediend. Controleer of alle transacties zijn verwerkt.",
            "description_en": f"The BTW return for Q{quarter} {year} must be filed before {btw_deadline.strftime('%Y-%m-%d')}. Check that all transactions have been processed.",
            "risk_level": "safe",
            "legal_basis": "Art. 14 Wet OB 1968",
        })

    # 2. Unmatched bank transactions
    unmatched_count = await session.execute(
        text(
            """SELECT COUNT(*) FROM bank_transactions bt
               JOIN bank_accounts ba ON bt.bank_account_id = ba.id
               WHERE ba.entity_id = :eid AND bt.matched_entry_id IS NULL"""
        ),
        {"eid": entity_id},
    )
    n_unmatched = unmatched_count.scalar() or 0
    if n_unmatched > 0:
        insights.append({
            "category": "warning",
            "title_nl": f"{n_unmatched} banktransacties niet gematcht",
            "title_en": f"{n_unmatched} bank transactions unmatched",
            "description_nl": f"Er zijn {n_unmatched} banktransacties die nog niet aan boekingen zijn gekoppeld. Dit kan de BTW-aangifte beïnvloeden.",
            "description_en": f"There are {n_unmatched} bank transactions not yet matched to journal entries. This may affect your BTW return.",
            "risk_level": "safe",
        })

    # 3. Trial balance check (debit != credit means problem)
    balance_result = await session.execute(
        text(
            """SELECT COALESCE(SUM(jl.debit), 0) as total_debit, COALESCE(SUM(jl.credit), 0) as total_credit
               FROM journal_lines jl
               JOIN journal_entries je ON jl.entry_id = je.id
               WHERE je.entity_id = :eid AND je.status IN ('posted', 'locked')"""
        ),
        {"eid": entity_id},
    )
    bal = balance_result.one()
    if bal.total_debit != bal.total_credit:
        diff = abs(bal.total_debit - bal.total_credit)
        insights.append({
            "category": "warning",
            "title_nl": f"Proefbalans niet in evenwicht (verschil: €{diff:.2f})",
            "title_en": f"Trial balance out of balance (difference: €{diff:.2f})",
            "description_nl": "De totale debiteringen en crediteringen zijn niet gelijk. Dit duidt op een boekhoudkundige fout die moet worden gecorrigeerd.",
            "description_en": "Total debits and credits are not equal. This indicates a bookkeeping error that needs correction.",
            "risk_level": "safe",
            "impact_eur": float(diff),
        })

    # 4. Draft entries not posted
    draft_count_result = await session.execute(
        text(
            "SELECT COUNT(*) FROM journal_entries WHERE entity_id = :eid AND status = 'draft'"
        ),
        {"eid": entity_id},
    )
    n_draft = draft_count_result.scalar() or 0
    if n_draft > 5:
        insights.append({
            "category": "warning",
            "title_nl": f"{n_draft} conceptboekingen niet definitief gemaakt",
            "title_en": f"{n_draft} draft journal entries not yet posted",
            "description_nl": f"Er staan {n_draft} boekingen als concept. Deze worden niet meegenomen in rapporten en BTW-aangiftes tot ze definitief zijn.",
            "description_en": f"There are {n_draft} entries still in draft. These are excluded from reports and BTW returns until posted.",
            "risk_level": "safe",
        })

    # 5. Send to AI for deeper analysis if Ollama is available
    ai_insights = await _ai_analysis(session, entity_id, entity, year)
    insights.extend(ai_insights)

    # Store insights in database
    for insight in insights:
        try:
            await session.execute(
                text(
                    """INSERT INTO insights (id, entity_id, user_id, category, title_nl, title_en,
                       description_nl, description_en, impact_eur, risk_level, legal_basis, status)
                       VALUES (:id, :eid, :uid, :cat, :tnl, :ten, :dnl, :den, :impact, :risk, :legal, 'active')"""
                ),
                {
                    "id": uuid.uuid4(),
                    "eid": entity_id,
                    "uid": user_id,
                    "cat": insight.get("category", "other"),
                    "tnl": insight["title_nl"],
                    "ten": insight["title_en"],
                    "dnl": insight["description_nl"],
                    "den": insight["description_en"],
                    "impact": insight.get("impact_eur"),
                    "risk": insight.get("risk_level", "safe"),
                    "legal": insight.get("legal_basis"),
                },
            )
        except Exception as e:
            logger.warning("Failed to store insight: %s", e)

    return insights


async def _ai_analysis(
    session: AsyncSession,
    entity_id: uuid.UUID,
    entity: Entity,
    year: int,
) -> list[dict]:
    """Use Ollama to generate deeper insights based on financial data."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
            if resp.status_code != 200:
                return []
    except Exception:
        return []

    # Get P&L summary for context
    pl_result = await session.execute(
        text(
            """SELECT a.account_type, COALESCE(SUM(jl.debit), 0) as total_debit,
                      COALESCE(SUM(jl.credit), 0) as total_credit
               FROM journal_lines jl
               JOIN journal_entries je ON jl.entry_id = je.id
               JOIN accounts a ON jl.account_id = a.id
               WHERE je.entity_id = :eid AND je.status IN ('posted', 'locked')
                 AND EXTRACT(YEAR FROM je.date) = :year
               GROUP BY a.account_type"""
        ),
        {"eid": entity_id, "year": year},
    )
    pl_rows = pl_result.all()
    pl_summary = {row.account_type: {"debit": float(row.total_debit), "credit": float(row.total_credit)} for row in pl_rows}

    prompt = f"""Analyze this Dutch business entity's financial data for {year} and suggest 1-3 tax optimization insights.

Entity: {entity.name} (type: {entity.entity_type.value})
P&L summary by account type: {json.dumps(pl_summary)}

For each insight, respond in this exact JSON format:
[{{"title_nl": "...", "title_en": "...", "description_nl": "...", "description_en": "...", "category": "deduction|optimization|timing_advice", "risk_level": "safe|commonly_accepted", "legal_basis": "Art. X Wet Y", "impact_eur": null}}]

Focus on: KIA eligibility, zelfstandigenaftrek, werkruimte aftrek, BTW optimization, DGA salary optimization.
Only suggest what's relevant to the entity type. Return JSON array only, no explanation."""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=60,
            )
            if resp.status_code != 200:
                return []

            raw = resp.json().get("response", "")
            # Parse JSON from response
            raw = raw.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                raw = "\n".join(lines).strip()

            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1:
                parsed = json.loads(raw[start:end + 1])
                if isinstance(parsed, list):
                    return [i for i in parsed if isinstance(i, dict) and "title_nl" in i]

    except Exception as e:
        logger.warning("AI advisor analysis failed: %s", e)

    return []
