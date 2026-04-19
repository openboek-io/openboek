"""Audit log routes — global and per-entity audit trails."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.audit.models import AuditLog
from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.db import get_session
from openboek.entities.models import Entity

router = APIRouter(tags=["audit"])


def _templates():
    from openboek.main import templates
    return templates


@router.get("/audit", response_class=HTMLResponse)
async def global_audit_log(
    request: Request,
    page: int = Query(1, ge=1),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Global audit log (all entities the user has access to)."""
    per_page = 50
    offset = (page - 1) * per_page

    result = await session.execute(
        select(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    entries = list(result.scalars().all())

    return _templates().TemplateResponse(request, "audit/log.html", {"entries": entries,
        "entity": None,
        "page": page,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.get("/entities/{entity_id}/audit", response_class=HTMLResponse)
async def entity_audit_log(
    request: Request,
    entity_id: uuid.UUID,
    page: int = Query(1, ge=1),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Audit log for a specific entity."""
    entity = await get_entity_for_user(entity_id, user, session)
    per_page = 50
    offset = (page - 1) * per_page

    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    entries = list(result.scalars().all())

    return _templates().TemplateResponse(request, "audit/log.html", {"entries": entries,
        "entity": entity,
        "page": page,
        "user": user,
        "lang": user.preferred_lang,
    })
