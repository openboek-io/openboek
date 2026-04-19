"""Layer 2 — AI review of tax returns.

Sends financial summary to Ollama for contextual review.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.config import settings

logger = logging.getLogger(__name__)


async def run_ai_review(
    session: AsyncSession,
    entity_id: uuid.UUID,
    period_type: str = "btw_q",
    year: int | None = None,
    quarter: int | None = None,
    lang: str = "nl",
) -> dict:
    """Run AI review on a tax return.

    Returns dict with:
        - issues: list of potential problems
        - suggestions: list of improvements
        - risk_flags: list of items that may trigger scrutiny
        - confidence: "high" | "medium" | "low"
    """
    today = date.today()
    year = year or today.year
    quarter = quarter or ((today.month - 1) // 3 + 1)

    # Check Ollama availability
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
            if resp.status_code != 200:
                return _offline_result()
    except Exception:
        return _offline_result()

    # Gather financial summary
    summary = await _build_summary(session, entity_id, year, quarter, period_type)

    entity_type_label = "BTW" if period_type == "btw_q" else "IB/VPB"

    prompt = f"""You are an expert Dutch bookkeeper reviewing a {entity_type_label} return.
Period: {'Q' + str(quarter) if quarter else ''} {year}

Financial summary:
{json.dumps(summary, indent=2, ensure_ascii=False)}

Review for:
1. Common categorization errors (personal expenses in business accounts)
2. Missing deductions the taxpayer likely qualifies for
3. Suspicious entries that may trigger Belastingdienst scrutiny
4. BTW rubriek assignments that don't match transaction type
5. Unusual deviations from typical patterns

Respond in {'Dutch' if lang == 'nl' else 'English'} as a JSON object:
{{"issues": [{{"text": "...", "severity": "block|warning|info"}}],
  "suggestions": [{{"text": "...", "impact": "..."}}],
  "risk_flags": [{{"text": "...", "severity": "high|medium|low"}}],
  "confidence": "high|medium|low"}}

Return JSON only, no explanation."""

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=90,
            )
            if resp.status_code != 200:
                return _offline_result()

            raw = resp.json().get("response", "")
            return _parse_review(raw)

    except Exception as e:
        logger.warning("AI review failed: %s", e)
        return _offline_result()


async def _build_summary(
    session: AsyncSession,
    entity_id: uuid.UUID,
    year: int,
    quarter: int | None,
    period_type: str,
) -> dict:
    """Build a financial summary for AI review."""
    if period_type == "btw_q" and quarter:
        q_start = date(year, (quarter - 1) * 3 + 1, 1)
        q_end = date(year, quarter * 3 + 1, 1) if quarter < 4 else date(year + 1, 1, 1)
    else:
        q_start = date(year, 1, 1)
        q_end = date(year + 1, 1, 1)

    # P&L by account type
    result = await session.execute(
        text(
            """SELECT a.account_type, a.btw_code,
                      COALESCE(SUM(jl.debit), 0) as total_debit,
                      COALESCE(SUM(jl.credit), 0) as total_credit
               FROM journal_lines jl
               JOIN journal_entries je ON jl.entry_id = je.id
               JOIN accounts a ON jl.account_id = a.id
               WHERE je.entity_id = :eid
                 AND je.date >= :start AND je.date < :end
                 AND je.status IN ('posted', 'locked')
               GROUP BY a.account_type, a.btw_code"""
        ),
        {"eid": entity_id, "start": q_start, "end": q_end},
    )
    rows = result.all()

    summary = {"period": f"{'Q' + str(quarter) + ' ' if quarter else ''}{year}", "accounts": []}
    for row in rows:
        summary["accounts"].append({
            "type": row.account_type,
            "btw_code": row.btw_code,
            "debit": float(row.total_debit),
            "credit": float(row.total_credit),
        })

    # Transaction count
    tx_result = await session.execute(
        text(
            "SELECT COUNT(*) FROM journal_entries WHERE entity_id = :eid AND date >= :start AND date < :end AND status IN ('posted', 'locked')"
        ),
        {"eid": entity_id, "start": q_start, "end": q_end},
    )
    summary["transaction_count"] = tx_result.scalar() or 0

    return summary


def _parse_review(raw: str) -> dict:
    """Parse AI review response."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        try:
            result = json.loads(raw[start:end + 1])
            if isinstance(result, dict):
                return {
                    "issues": result.get("issues", []),
                    "suggestions": result.get("suggestions", []),
                    "risk_flags": result.get("risk_flags", []),
                    "confidence": result.get("confidence", "medium"),
                    "available": True,
                }
        except json.JSONDecodeError:
            pass

    return {
        "issues": [],
        "suggestions": [],
        "risk_flags": [],
        "confidence": "low",
        "available": True,
        "raw": raw[:500],
    }


def _offline_result() -> dict:
    return {
        "issues": [],
        "suggestions": [],
        "risk_flags": [],
        "confidence": "n/a",
        "available": False,
    }
