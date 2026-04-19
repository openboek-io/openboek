"""Accounting routes — chart of accounts, journal entries."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openboek.accounting.models import Account, JournalEntry, JournalLine, JournalStatus
from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.audit.service import log_action
from openboek.db import get_session
from openboek.entities.models import Entity

router = APIRouter(tags=["accounting"])


def _templates():
    from openboek.main import templates
    return templates


# ---------------------------------------------------------------------------
# Chart of Accounts
# ---------------------------------------------------------------------------

@router.get("/entities/{entity_id}/accounts", response_class=HTMLResponse)
async def chart_of_accounts(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Display chart of accounts as a tree."""
    entity = await get_entity_for_user(entity_id, user, session)
    result = await session.execute(
        select(Account)
        .where(Account.entity_id == entity_id)
        .order_by(Account.code)
    )
    accounts = list(result.scalars().all())

    # Build tree structure
    account_map = {a.id: a for a in accounts}
    roots = [a for a in accounts if a.parent_id is None]

    return _templates().TemplateResponse(request, "accounting/accounts.html", {"entity": entity,
        "accounts": accounts,
        "roots": roots,
        "account_map": account_map,
        "user": user,
        "lang": user.preferred_lang,
    })


# ---------------------------------------------------------------------------
# Journal Entries
# ---------------------------------------------------------------------------

@router.get("/entities/{entity_id}/journal", response_class=HTMLResponse)
async def journal_list(
    request: Request,
    entity_id: uuid.UUID,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    status: str | None = Query(None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List journal entries with optional date/status filters."""
    entity = await get_entity_for_user(entity_id, user, session)

    query = (
        select(JournalEntry)
        .where(JournalEntry.entity_id == entity_id)
        .order_by(JournalEntry.date.desc())
    )
    if date_from:
        try:
            query = query.where(JournalEntry.date >= date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.where(JournalEntry.date <= date.fromisoformat(date_to))
        except ValueError:
            pass
    if status:
        try:
            query = query.where(JournalEntry.status == JournalStatus(status))
        except ValueError:
            pass

    result = await session.execute(query.options(selectinload(JournalEntry.lines)))
    entries = list(result.scalars().unique().all())

    return _templates().TemplateResponse(request, "accounting/journal_list.html", {"entity": entity,
        "entries": entries,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "status_filter": status or "",
        "user": user,
        "lang": user.preferred_lang,
    })


@router.get("/entities/{entity_id}/journal/new", response_class=HTMLResponse)
async def journal_new(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """New journal entry form."""
    entity = await get_entity_for_user(entity_id, user, session)
    accounts_result = await session.execute(
        select(Account).where(Account.entity_id == entity_id).order_by(Account.code)
    )
    accounts = list(accounts_result.scalars().all())

    return _templates().TemplateResponse(request, "accounting/journal_form.html", {"entity": entity,
        "entry": None,
        "accounts": accounts,
        "error": None,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.post("/entities/{entity_id}/journal", response_class=HTMLResponse)
async def journal_create(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Create a new journal entry with lines from form data."""
    entity = await get_entity_for_user(entity_id, user, session)
    form = await request.form()

    entry_date_str = form.get("date", "")
    description = form.get("description", "")
    reference = form.get("reference", "")

    try:
        entry_date = date.fromisoformat(entry_date_str)
    except (ValueError, TypeError):
        entry_date = date.today()

    entry = JournalEntry(
        entity_id=entity_id,
        date=entry_date,
        description=description or None,
        reference=reference or None,
        status=JournalStatus.draft,
        created_by=user.id,
    )
    session.add(entry)
    await session.flush()

    # Parse lines from form: line_account_0, line_debit_0, line_credit_0, etc.
    total_debit = Decimal("0.00")
    total_credit = Decimal("0.00")
    line_idx = 0
    while True:
        acc_id = form.get(f"line_account_{line_idx}")
        if acc_id is None:
            break
        try:
            debit = Decimal(form.get(f"line_debit_{line_idx}", "0") or "0")
            credit = Decimal(form.get(f"line_credit_{line_idx}", "0") or "0")
        except InvalidOperation:
            debit = Decimal("0.00")
            credit = Decimal("0.00")

        if debit != 0 or credit != 0:
            line = JournalLine(
                entry_id=entry.id,
                account_id=uuid.UUID(acc_id),
                debit=debit,
                credit=credit,
                description=form.get(f"line_desc_{line_idx}") or None,
            )
            session.add(line)
            total_debit += debit
            total_credit += credit
        line_idx += 1

    # Validate double-entry
    if total_debit != total_credit:
        await session.rollback()
        accounts_result = await session.execute(
            select(Account).where(Account.entity_id == entity_id).order_by(Account.code)
        )
        accounts = list(accounts_result.scalars().all())
        return _templates().TemplateResponse(request, "accounting/journal_form.html", {"entity": entity,
            "entry": None,
            "accounts": accounts,
            "error": f"Debit ({total_debit}) ≠ Credit ({total_credit}). Entry must balance.",
            "user": user,
            "lang": user.preferred_lang,
        }, status_code=400)

    await log_action(
        session, action="journal.create", user_id=user.id, entity_id=entity_id,
        table_name="journal_entries", record_id=str(entry.id),
        after_data={"date": str(entry_date), "description": description, "total": str(total_debit)},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(url=f"/entities/{entity_id}/journal/{entry.id}", status_code=303)


@router.get("/entities/{entity_id}/journal/{entry_id}", response_class=HTMLResponse)
async def journal_view(
    request: Request,
    entity_id: uuid.UUID,
    entry_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """View a journal entry with its lines."""
    entity = await get_entity_for_user(entity_id, user, session)
    result = await session.execute(
        select(JournalEntry)
        .where(JournalEntry.id == entry_id, JournalEntry.entity_id == entity_id)
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        return RedirectResponse(url=f"/entities/{entity_id}/journal", status_code=303)

    accounts_result = await session.execute(
        select(Account).where(Account.entity_id == entity_id).order_by(Account.code)
    )
    accounts = list(accounts_result.scalars().all())

    return _templates().TemplateResponse(request, "accounting/journal_detail.html", {"entity": entity,
        "entry": entry,
        "accounts": accounts,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.post("/entities/{entity_id}/journal/{entry_id}/post")
async def journal_post(
    request: Request,
    entity_id: uuid.UUID,
    entry_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Post a draft journal entry — makes it affect reports."""
    entity = await get_entity_for_user(entity_id, user, session)
    result = await session.execute(
        select(JournalEntry)
        .where(JournalEntry.id == entry_id, JournalEntry.entity_id == entity_id)
        .options(selectinload(JournalEntry.lines))
    )
    entry = result.scalar_one_or_none()
    if entry is None or entry.status != JournalStatus.draft:
        return RedirectResponse(url=f"/entities/{entity_id}/journal", status_code=303)

    # Verify balance before posting
    total_debit = sum(l.debit for l in entry.lines)
    total_credit = sum(l.credit for l in entry.lines)
    if total_debit != total_credit:
        return RedirectResponse(
            url=f"/entities/{entity_id}/journal/{entry_id}?error=unbalanced",
            status_code=303,
        )

    entry.status = JournalStatus.posted
    entry.posted_at = datetime.now(timezone.utc)
    entry.posted_by = user.id

    await log_action(
        session, action="journal.post", user_id=user.id, entity_id=entity_id,
        table_name="journal_entries", record_id=str(entry.id),
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(url=f"/entities/{entity_id}/journal/{entry_id}", status_code=303)


@router.post("/entities/{entity_id}/journal/{entry_id}/lock")
async def journal_lock(
    request: Request,
    entity_id: uuid.UUID,
    entry_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Lock a posted journal entry — irreversible."""
    entity = await get_entity_for_user(entity_id, user, session)
    result = await session.execute(
        select(JournalEntry)
        .where(JournalEntry.id == entry_id, JournalEntry.entity_id == entity_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None or entry.status != JournalStatus.posted:
        return RedirectResponse(url=f"/entities/{entity_id}/journal", status_code=303)

    entry.status = JournalStatus.locked

    await log_action(
        session, action="journal.lock", user_id=user.id, entity_id=entity_id,
        table_name="journal_entries", record_id=str(entry.id),
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(url=f"/entities/{entity_id}/journal/{entry_id}", status_code=303)
