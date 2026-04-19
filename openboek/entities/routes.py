"""Entity management routes — CRUD + relationships + detail overview."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openboek.accounting.models import Account, AccountType, JournalEntry, JournalLine, JournalStatus
from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.audit.service import log_action
from openboek.db import get_session
from openboek.entities.models import (
    AccessRole,
    Entity,
    EntityAccess,
    EntityRelationship,
    EntityType,
    RelationshipType,
)

router = APIRouter(prefix="/entities", tags=["entities"])

YAML_DIR = Path(__file__).resolve().parent.parent.parent / "tax_modules" / "nl" / "chart_of_accounts"


def _templates():
    from openboek.main import templates
    return templates


async def _get_all_user_entities(user: User, session: AsyncSession) -> list[Entity]:
    """Get all entities a user has access to."""
    result = await session.execute(
        select(Entity).where(Entity.owner_user_id == user.id)
    )
    owned = list(result.scalars().all())
    access_result = await session.execute(
        select(Entity)
        .join(EntityAccess, EntityAccess.entity_id == Entity.id)
        .where(EntityAccess.user_id == user.id)
    )
    shared = [e for e in access_result.scalars().all() if e.id not in {o.id for o in owned}]
    return owned + shared


def _provision_accounts_from_yaml(
    entity_id: uuid.UUID, entity_type: str,
) -> list[Account]:
    """Load RGS chart of accounts from YAML template and return Account objects."""
    type_map = {
        "zzp": "zzp.yaml",
        "bv": "bv.yaml",
        "holding": "bv.yaml",
        "personal": "personal.yaml",
    }
    filename = type_map.get(entity_type, "zzp.yaml")
    yaml_path = YAML_DIR / filename
    if not yaml_path.exists():
        return []

    with open(yaml_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    accounts: list[Account] = []

    def _walk(items: list[dict], parent_id: uuid.UUID | None = None):
        for item in items:
            acc_type = AccountType(item.get("type", "asset"))
            acc = Account(
                entity_id=entity_id,
                code=item["code"],
                name_nl=item["name_nl"],
                name_en=item["name_en"],
                account_type=acc_type,
                parent_id=parent_id,
                btw_code=item.get("btw_code"),
                is_system=item.get("system", False),
            )
            accounts.append(acc)
            children = item.get("children", [])
            if children:
                _walk(children, acc.id)

    _walk(data.get("accounts", []))
    return accounts


@router.get("", response_class=HTMLResponse)
async def list_entities(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List all entities the user has access to."""
    entities = await _get_all_user_entities(user, session)

    return _templates().TemplateResponse(request, "entities/list.html", {
        "entities": entities,
        "all_entities": entities,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.get("/new", response_class=HTMLResponse)
async def create_entity_form(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show entity creation form."""
    all_entities = await _get_all_user_entities(user, session)
    return _templates().TemplateResponse(request, "entities/form.html", {
        "entity": None,
        "entity_types": list(EntityType),
        "all_entities": all_entities,
        "user": user,
        "error": None,
        "lang": user.preferred_lang,
    })


@router.post("", response_class=HTMLResponse)
async def create_entity(
    request: Request,
    name: str = Form(...),
    entity_type: str = Form(...),
    kvk_number: str = Form(""),
    btw_number: str = Form(""),
    address: str = Form(""),
    city: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Create entity and provision chart of accounts."""
    entity = Entity(
        name=name,
        entity_type=EntityType(entity_type),
        kvk_number=kvk_number or None,
        btw_number=btw_number or None,
        address=address or None,
        city=city or None,
        owner_user_id=user.id,
    )
    session.add(entity)
    await session.flush()

    # Provision chart of accounts from YAML
    accounts = _provision_accounts_from_yaml(entity.id, entity_type)
    for acc in accounts:
        session.add(acc)

    # Grant owner access
    access = EntityAccess(user_id=user.id, entity_id=entity.id, role=AccessRole.owner)
    session.add(access)

    await log_action(
        session, action="entity.create", user_id=user.id, entity_id=entity.id,
        table_name="entities", record_id=str(entity.id),
        after_data={"name": name, "type": entity_type},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(url=f"/entities/{entity.id}", status_code=303)


@router.get("/{entity_id}", response_class=HTMLResponse)
async def view_entity(
    request: Request,
    entity: Entity = Depends(get_entity_for_user),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Entity detail/overview page with stats and recent activity."""
    all_entities = await _get_all_user_entities(user, session)

    # Calculate financial stats
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

    stats = {
        "assets": assets,
        "liabilities": liabilities,
        "revenue": revenue,
        "expenses": expenses,
        "profit": revenue - expenses,
    }

    # Recent journal entries
    recent_result = await session.execute(
        select(JournalEntry)
        .where(JournalEntry.entity_id == entity.id)
        .order_by(JournalEntry.date.desc(), JournalEntry.created_at.desc())
        .limit(8)
    )
    recent_entries = list(recent_result.scalars().all())

    return _templates().TemplateResponse(request, "entities/detail.html", {
        "entity": entity,
        "stats": stats,
        "recent_entries": recent_entries,
        "all_entities": all_entities,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.get("/{entity_id}/edit", response_class=HTMLResponse)
async def edit_entity_form(
    request: Request,
    entity: Entity = Depends(get_entity_for_user),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show entity edit form."""
    all_entities = await _get_all_user_entities(user, session)
    return _templates().TemplateResponse(request, "entities/form.html", {
        "entity": entity,
        "entity_types": list(EntityType),
        "all_entities": all_entities,
        "user": user,
        "error": None,
        "lang": user.preferred_lang,
    })


@router.post("/{entity_id}", response_class=HTMLResponse)
async def update_entity(
    request: Request,
    entity_id: uuid.UUID,
    name: str = Form(...),
    kvk_number: str = Form(""),
    btw_number: str = Form(""),
    address: str = Form(""),
    city: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Update entity details."""
    entity = await get_entity_for_user(entity_id, user, session)
    before = {"name": entity.name, "kvk": entity.kvk_number, "btw": entity.btw_number}

    entity.name = name
    entity.kvk_number = kvk_number or None
    entity.btw_number = btw_number or None
    entity.address = address or None
    entity.city = city or None

    await log_action(
        session, action="entity.update", user_id=user.id, entity_id=entity.id,
        table_name="entities", record_id=str(entity.id),
        before_data=before,
        after_data={"name": name, "kvk": kvk_number, "btw": btw_number},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(url=f"/entities/{entity.id}", status_code=303)


@router.get("/{entity_id}/relationships", response_class=HTMLResponse)
async def entity_relationships(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Manage entity relationships."""
    entity = await get_entity_for_user(entity_id, user, session)
    all_entities = await _get_all_user_entities(user, session)

    result = await session.execute(
        select(EntityRelationship).where(
            (EntityRelationship.parent_entity_id == entity_id)
            | (EntityRelationship.child_entity_id == entity_id)
        )
    )
    relationships = list(result.scalars().all())

    return _templates().TemplateResponse(request, "entities/relationships.html", {
        "entity": entity,
        "relationships": relationships,
        "all_entities": all_entities,
        "relationship_types": list(RelationshipType),
        "user": user,
        "lang": user.preferred_lang,
    })
