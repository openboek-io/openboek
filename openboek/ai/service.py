"""AI Tax Consultant service — Ollama integration with tool calling."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
import yaml
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.accounting.models import (
    Account,
    AccountType,
    JournalEntry,
    JournalLine,
    JournalStatus,
)
from openboek.config import settings
from openboek.entities.models import Entity, EntityRelationship

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"


# ---------------------------------------------------------------------------
# Tax Knowledge Base loader
# ---------------------------------------------------------------------------

def load_tax_knowledge() -> str:
    """Load all YAML knowledge files into a combined text block for the system prompt."""
    parts = []
    for yaml_file in sorted(KNOWLEDGE_DIR.glob("*.yaml")):
        try:
            with open(yaml_file, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            parts.append(f"### {yaml_file.stem}\n```yaml\n{yaml.dump(data, default_flow_style=False, allow_unicode=True)}```")
        except Exception as e:
            logger.warning("Failed to load knowledge file %s: %s", yaml_file, e)
    return "\n\n".join(parts)


_tax_knowledge_cache: str | None = None


def get_tax_knowledge() -> str:
    global _tax_knowledge_cache
    if _tax_knowledge_cache is None:
        _tax_knowledge_cache = load_tax_knowledge()
    return _tax_knowledge_cache


# ---------------------------------------------------------------------------
# Tool implementations — query the database
# ---------------------------------------------------------------------------

class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        if isinstance(o, uuid.UUID):
            return str(o)
        return super().default(o)


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, cls=_DecimalEncoder, ensure_ascii=False)


async def tool_get_trial_balance(
    session: AsyncSession, entity_id: str, as_of: str | None = None
) -> dict:
    """Get trial balance for an entity."""
    eid = uuid.UUID(entity_id)
    query = (
        select(
            Account.code,
            Account.name_nl,
            Account.name_en,
            Account.account_type,
            func.coalesce(func.sum(JournalLine.debit), 0).label("total_debit"),
            func.coalesce(func.sum(JournalLine.credit), 0).label("total_credit"),
        )
        .outerjoin(JournalLine, JournalLine.account_id == Account.id)
        .outerjoin(
            JournalEntry,
            and_(
                JournalEntry.id == JournalLine.entry_id,
                JournalEntry.status.in_([JournalStatus.posted, JournalStatus.locked]),
            ),
        )
        .where(Account.entity_id == eid)
        .group_by(Account.code, Account.name_nl, Account.name_en, Account.account_type)
        .order_by(Account.code)
    )
    if as_of:
        query = query.where(
            or_(JournalEntry.date <= as_of, JournalEntry.id.is_(None))
        )

    result = await session.execute(query)
    rows = result.all()
    accounts = []
    for row in rows:
        d = row.total_debit or Decimal("0")
        c = row.total_credit or Decimal("0")
        balance = d - c if row.account_type in (AccountType.asset, AccountType.expense) else c - d
        if d or c:
            accounts.append({
                "code": row.code,
                "name_nl": row.name_nl,
                "name_en": row.name_en,
                "type": row.account_type,
                "debit": d,
                "credit": c,
                "balance": balance,
            })
    return {"entity_id": entity_id, "as_of": as_of or "today", "accounts": accounts}


async def tool_get_transactions(
    session: AsyncSession,
    entity_id: str,
    date_from: str | None = None,
    date_to: str | None = None,
    account_code: str | None = None,
) -> dict:
    """Get transactions for an entity."""
    eid = uuid.UUID(entity_id)
    query = (
        select(
            JournalEntry.id,
            JournalEntry.date,
            JournalEntry.reference,
            JournalEntry.description,
            JournalEntry.status,
            JournalLine.debit,
            JournalLine.credit,
            JournalLine.description.label("line_desc"),
            Account.code,
            Account.name_nl,
        )
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalEntry.entity_id == eid)
        .order_by(JournalEntry.date.desc())
        .limit(100)
    )
    if date_from:
        query = query.where(JournalEntry.date >= date_from)
    if date_to:
        query = query.where(JournalEntry.date <= date_to)
    if account_code:
        query = query.where(Account.code == account_code)

    result = await session.execute(query)
    rows = result.all()
    transactions = []
    for row in rows:
        transactions.append({
            "date": row.date,
            "reference": row.reference,
            "description": row.description,
            "account_code": row.code,
            "account_name": row.name_nl,
            "debit": row.debit,
            "credit": row.credit,
            "line_description": row.line_desc,
            "status": row.status,
        })
    return {"entity_id": entity_id, "count": len(transactions), "transactions": transactions}


async def tool_get_btw_summary(
    session: AsyncSession, entity_id: str, quarter: int, year: int
) -> dict:
    """Get BTW summary by rubriek for a quarter."""
    eid = uuid.UUID(entity_id)
    # Quarter date ranges
    q_start = date(year, (quarter - 1) * 3 + 1, 1)
    if quarter == 4:
        q_end = date(year, 12, 31)
    else:
        q_end = date(year, quarter * 3 + 1, 1)

    query = (
        select(
            Account.btw_code,
            func.sum(JournalLine.debit).label("total_debit"),
            func.sum(JournalLine.credit).label("total_credit"),
        )
        .join(JournalLine, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            Account.entity_id == eid,
            Account.btw_code.isnot(None),
            JournalEntry.date >= q_start,
            JournalEntry.date < q_end,
            JournalEntry.status.in_([JournalStatus.posted, JournalStatus.locked]),
        )
        .group_by(Account.btw_code)
    )
    result = await session.execute(query)
    rows = result.all()
    rubrieken = {}
    for row in rows:
        rubrieken[row.btw_code] = {
            "debit": row.total_debit or Decimal("0"),
            "credit": row.total_credit or Decimal("0"),
        }
    return {
        "entity_id": entity_id,
        "quarter": quarter,
        "year": year,
        "rubrieken": rubrieken,
    }


async def tool_get_entity_info(session: AsyncSession, entity_id: str) -> dict:
    """Get entity details."""
    eid = uuid.UUID(entity_id)
    result = await session.execute(select(Entity).where(Entity.id == eid))
    entity = result.scalar_one_or_none()
    if not entity:
        return {"error": "Entity not found"}

    # Get relationships
    rels_result = await session.execute(
        select(EntityRelationship).where(
            or_(
                EntityRelationship.parent_entity_id == eid,
                EntityRelationship.child_entity_id == eid,
            )
        )
    )
    rels = rels_result.scalars().all()
    relationships = []
    for r in rels:
        relationships.append({
            "type": r.relationship_type.value,
            "parent_id": str(r.parent_entity_id),
            "child_id": str(r.child_entity_id),
            "share_percentage": r.share_percentage,
        })

    return {
        "id": str(entity.id),
        "name": entity.name,
        "entity_type": entity.entity_type.value,
        "fiscal_number": entity.fiscal_number,
        "btw_number": entity.btw_number,
        "kvk_number": entity.kvk_number,
        "currency": entity.currency,
        "country": entity.country,
        "relationships": relationships,
    }


async def tool_search_transactions(
    session: AsyncSession, entity_id: str, query: str
) -> dict:
    """Search transactions by description or amount."""
    eid = uuid.UUID(entity_id)
    search_pattern = f"%{query}%"

    # Try to parse as amount
    amount_filter = None
    try:
        amount = Decimal(query.replace(",", ".").replace("€", "").strip())
        amount_filter = or_(
            JournalLine.debit == amount,
            JournalLine.credit == amount,
        )
    except Exception:
        pass

    text_filter = or_(
        JournalEntry.description.ilike(search_pattern),
        JournalEntry.reference.ilike(search_pattern),
        JournalLine.description.ilike(search_pattern),
    )
    combined = text_filter if amount_filter is None else or_(text_filter, amount_filter)

    stmt = (
        select(
            JournalEntry.date,
            JournalEntry.reference,
            JournalEntry.description,
            JournalLine.debit,
            JournalLine.credit,
            Account.code,
            Account.name_nl,
        )
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalEntry.entity_id == eid, combined)
        .order_by(JournalEntry.date.desc())
        .limit(50)
    )
    result = await session.execute(stmt)
    rows = result.all()
    matches = []
    for row in rows:
        matches.append({
            "date": row.date,
            "reference": row.reference,
            "description": row.description,
            "account_code": row.code,
            "account_name": row.name_nl,
            "debit": row.debit,
            "credit": row.credit,
        })
    return {"entity_id": entity_id, "query": query, "count": len(matches), "results": matches}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "get_trial_balance": tool_get_trial_balance,
    "get_transactions": tool_get_transactions,
    "get_btw_summary": tool_get_btw_summary,
    "get_entity_info": tool_get_entity_info,
    "search_transactions": tool_search_transactions,
}


async def execute_tool(
    session: AsyncSession, tool_name: str, arguments: dict
) -> str:
    """Execute a tool and return JSON result."""
    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        return _json_dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        result = await handler(session, **arguments)
        return _json_dumps(result)
    except Exception as e:
        logger.exception("Tool execution error: %s", tool_name)
        return _json_dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Ollama chat with tool calling and streaming
# ---------------------------------------------------------------------------

async def check_ollama_available() -> bool:
    """Check if Ollama is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False


async def chat_stream(
    messages: list[dict],
    session: AsyncSession,
    entity_id: str | None = None,
    lang: str = "nl",
) -> AsyncGenerator[str, None]:
    """Stream a chat response from Ollama, handling tool calls.

    Yields chunks of the assistant's text response.
    """
    from openboek.ai.prompts import SYSTEM_PROMPT_NL, SYSTEM_PROMPT_EN, TOOL_DESCRIPTIONS

    # Build system prompt with knowledge
    system_template = SYSTEM_PROMPT_NL if lang == "nl" else SYSTEM_PROMPT_EN
    system_prompt = system_template.format(
        year=datetime.now().year,
        tax_knowledge=get_tax_knowledge(),
    )

    full_messages = [{"role": "system", "content": system_prompt}] + messages

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            # First call — may trigger tool use
            payload = {
                "model": settings.ollama_model,
                "messages": full_messages,
                "stream": True,
                "tools": TOOL_DESCRIPTIONS,
            }

            accumulated_content = ""
            tool_calls_pending = []

            async with client.stream(
                "POST",
                f"{settings.ollama_url}/api/chat",
                json=payload,
                timeout=180,
            ) as response:
                if response.status_code != 200:
                    yield "⚠️ AI service unavailable. Please try again later."
                    return

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg = chunk.get("message", {})

                    # Check for tool calls
                    if msg.get("tool_calls"):
                        tool_calls_pending.extend(msg["tool_calls"])

                    # Stream text content
                    content = msg.get("content", "")
                    if content:
                        accumulated_content += content
                        yield content

                    if chunk.get("done"):
                        break

            # Handle tool calls if any
            if tool_calls_pending:
                # Add assistant message with tool calls
                full_messages.append({
                    "role": "assistant",
                    "content": accumulated_content,
                    "tool_calls": tool_calls_pending,
                })

                # Execute each tool call
                for tc in tool_calls_pending:
                    fn = tc.get("function", {})
                    tool_name = fn.get("name", "")
                    arguments = fn.get("arguments", {})

                    # Inject entity_id if not provided
                    if entity_id and "entity_id" not in arguments:
                        arguments["entity_id"] = entity_id

                    yield f"\n\n🔍 *Querying: {tool_name}...*\n\n"
                    tool_result = await execute_tool(session, tool_name, arguments)

                    full_messages.append({
                        "role": "tool",
                        "content": tool_result,
                    })

                # Second call with tool results — stream the final answer
                payload2 = {
                    "model": settings.ollama_model,
                    "messages": full_messages,
                    "stream": True,
                }

                async with client.stream(
                    "POST",
                    f"{settings.ollama_url}/api/chat",
                    json=payload2,
                    timeout=180,
                ) as response2:
                    if response2.status_code != 200:
                        yield "\n\n⚠️ Error processing tool results."
                        return

                    async for line in response2.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            yield content
                        if chunk.get("done"):
                            break

    except httpx.ConnectError:
        yield "⚠️ Cannot connect to AI service (Ollama). The AI Tax Consultant is currently offline."
    except httpx.TimeoutException:
        yield "⚠️ AI service timed out. Please try a simpler question or try again later."
    except Exception as e:
        logger.exception("AI chat error")
        yield f"⚠️ An error occurred: {str(e)}"
