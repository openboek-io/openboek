"""AI Tax Consultant routes — chat interface and insights dashboard."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.auth.dependencies import get_current_user
from openboek.auth.models import User
from openboek.db import get_session
from openboek.entities.models import Entity, EntityAccess

router = APIRouter(prefix="/ai", tags=["ai"])


def _templates():
    from openboek.main import templates
    return templates


async def _get_user_entities(
    user: User, session: AsyncSession
) -> list[Entity]:
    """Get all entities the user has access to."""
    owned = await session.execute(
        select(Entity).where(Entity.owner_user_id == user.id)
    )
    entities = list(owned.scalars().all())
    shared = await session.execute(
        select(Entity)
        .join(EntityAccess, EntityAccess.entity_id == Entity.id)
        .where(EntityAccess.user_id == user.id)
    )
    seen = {e.id for e in entities}
    for e in shared.scalars().all():
        if e.id not in seen:
            entities.append(e)
    return entities


@router.get("", response_class=HTMLResponse)
async def ai_chat_page(
    request: Request,
    entity_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Render the AI Tax Consultant chat interface."""
    from openboek.ai.service import check_ollama_available

    entities = await _get_user_entities(user, session)
    ai_available = await check_ollama_available()

    # Default to first entity if none selected
    selected_entity_id = entity_id
    if not selected_entity_id and entities:
        selected_entity_id = str(entities[0].id)

    return _templates().TemplateResponse(request, "ai/chat.html", {
        "user": user,
        "entities": entities,
        "selected_entity_id": selected_entity_id,
        "ai_available": ai_available,
        "lang": user.preferred_lang,
    })


@router.post("/chat")
async def ai_chat(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Handle chat message — returns SSE stream."""
    from openboek.ai.service import chat_stream

    body = await request.json()
    message = body.get("message", "").strip()
    entity_id = body.get("entity_id")
    history = body.get("history", [])

    if not message:
        return StreamingResponse(
            iter(["⚠️ Please enter a message."]),
            media_type="text/plain",
        )

    # Build message list from history + new message
    messages = []
    for h in history[-20:]:  # Keep last 20 messages for context
        messages.append({
            "role": h.get("role", "user"),
            "content": h.get("content", ""),
        })
    messages.append({"role": "user", "content": message})

    async def generate():
        async for chunk in chat_stream(
            messages=messages,
            session=session,
            entity_id=entity_id,
            lang=user.preferred_lang,
        ):
            # SSE format
            yield f"data: {json.dumps({'content': chunk})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/insights", response_class=HTMLResponse)
async def ai_insights_page(
    request: Request,
    entity_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Render the proactive insights dashboard."""
    entities = await _get_user_entities(user, session)
    from openboek.ai.service import check_ollama_available

    ai_available = await check_ollama_available()

    selected_entity_id = entity_id
    if not selected_entity_id and entities:
        selected_entity_id = str(entities[0].id)

    # Load real insights from DB
    from sqlalchemy import text as sa_text
    insights_list = []
    if selected_entity_id:
        try:
            result = await session.execute(
                sa_text(
                    "SELECT * FROM insights WHERE entity_id = :eid AND status = 'active' "
                    "ORDER BY created_at DESC LIMIT 50"
                ),
                {"eid": selected_entity_id},
            )
            for row in result.mappings().all():
                insights_list.append({
                    "id": str(row["id"]),
                    "title_nl": row["title_nl"],
                    "title_en": row["title_en"],
                    "description_nl": row["description_nl"],
                    "description_en": row["description_en"],
                    "impact": f"€{row['impact_eur']:.2f}" if row.get("impact_eur") else None,
                    "risk_level": row.get("risk_level", "safe"),
                    "legal_basis": row.get("legal_basis"),
                    "category": row.get("category", "other"),
                })
        except Exception:
            pass  # Table may not exist yet

    return _templates().TemplateResponse(request, "ai/insights.html", {
        "user": user,
        "entities": entities,
        "selected_entity_id": selected_entity_id,
        "ai_available": ai_available,
        "insights": insights_list,
        "lang": user.preferred_lang,
    })


@router.post("/insights/generate")
async def ai_generate_insights(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Run the proactive advisor to generate fresh insights."""
    body = await request.json()
    entity_id = body.get("entity_id")
    if not entity_id:
        return {"error": "entity_id required"}

    from openboek.ai.advisor import run_advisor

    insights = await run_advisor(
        session, uuid.UUID(entity_id), user_id=user.id
    )
    return {"generated": len(insights)}


@router.post("/insights/{insight_id}/dismiss")
async def dismiss_insight(
    request: Request,
    insight_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Dismiss an insight."""
    from sqlalchemy import text as sa_text
    from datetime import datetime, timezone
    await session.execute(
        sa_text("UPDATE insights SET status = 'dismissed', dismissed_at = :ts WHERE id = :id"),
        {"ts": datetime.now(timezone.utc), "id": insight_id},
    )
    return {"ok": True}


@router.post("/insights/{insight_id}/snooze")
async def snooze_insight(
    request: Request,
    insight_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Snooze an insight for 7 days."""
    from sqlalchemy import text as sa_text
    from datetime import date, timedelta
    snooze_until = date.today() + timedelta(days=7)
    await session.execute(
        sa_text("UPDATE insights SET status = 'snoozed', snoozed_until = :until WHERE id = :id"),
        {"until": snooze_until, "id": insight_id},
    )
    return {"ok": True}
