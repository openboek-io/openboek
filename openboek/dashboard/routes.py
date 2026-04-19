"""Dashboard route — overview of all entities with quick stats."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.accounting.models import (
    Account,
    AccountType,
    JournalEntry,
    JournalLine,
    JournalStatus,
)
from openboek.auth.dependencies import get_current_user
from openboek.auth.models import User
from openboek.db import get_session
from openboek.entities.models import Entity, EntityAccess

router = APIRouter(tags=["dashboard"])


def _templates():
    from openboek.main import templates
    return templates


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Overview dashboard with entity stats."""
    # Get all user's entities
    owned_result = await session.execute(
        select(Entity).where(Entity.owner_user_id == user.id)
    )
    entities = list(owned_result.scalars().all())

    # Shared entities
    shared_result = await session.execute(
        select(Entity)
        .join(EntityAccess, EntityAccess.entity_id == Entity.id)
        .where(EntityAccess.user_id == user.id)
    )
    for e in shared_result.scalars().all():
        if e.id not in {x.id for x in entities}:
            entities.append(e)

    # Calculate quick stats per entity
    entity_stats = []
    for entity in entities:
        # Get account balances for assets and liabilities
        lines_result = await session.execute(
            select(
                Account.account_type,
                func.sum(JournalLine.debit).label("total_debit"),
                func.sum(JournalLine.credit).label("total_credit"),
            )
            .join(JournalLine, JournalLine.account_id == Account.id)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(
                Account.entity_id == entity.id,
                JournalEntry.status.in_([JournalStatus.posted, JournalStatus.locked]),
            )
            .group_by(Account.account_type)
        )
        rows = list(lines_result.all())

        assets = Decimal("0.00")
        liabilities = Decimal("0.00")
        revenue = Decimal("0.00")
        expenses = Decimal("0.00")

        for row in rows:
            d = row.total_debit or Decimal("0.00")
            c = row.total_credit or Decimal("0.00")
            if row.account_type == AccountType.asset:
                assets = d - c
            elif row.account_type == AccountType.liability:
                liabilities = c - d
            elif row.account_type == AccountType.revenue:
                revenue = c - d
            elif row.account_type == AccountType.expense:
                expenses = d - c

        # Count recent entries
        recent_result = await session.execute(
            select(func.count(JournalEntry.id))
            .where(JournalEntry.entity_id == entity.id)
        )
        entry_count = recent_result.scalar() or 0

        entity_stats.append({
            "entity": entity,
            "assets": assets,
            "liabilities": liabilities,
            "revenue": revenue,
            "expenses": expenses,
            "profit": revenue - expenses,
            "entry_count": entry_count,
        })

    return _templates().TemplateResponse(request, "dashboard.html", {"entity_stats": entity_stats,
        "user": user,
        "lang": user.preferred_lang,
    })
