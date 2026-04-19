"""Reports routes — trial balance, P&L, balance sheet."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openboek.accounting.models import Account, AccountType, JournalEntry, JournalLine, JournalStatus
from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.db import get_session
from openboek.entities.models import Entity

router = APIRouter(tags=["reports"])


def _templates():
    from openboek.main import templates
    return templates


async def _get_account_balances(
    session: AsyncSession,
    entity_id: uuid.UUID,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[uuid.UUID, dict]:
    """Calculate debit/credit totals per account for posted entries in date range."""
    accounts_result = await session.execute(
        select(Account).where(Account.entity_id == entity_id).order_by(Account.code)
    )
    accounts = list(accounts_result.scalars().all())

    # Build query for journal lines from posted entries
    query = (
        select(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            JournalEntry.entity_id == entity_id,
            JournalEntry.status.in_([JournalStatus.posted, JournalStatus.locked]),
        )
    )
    if date_from:
        query = query.where(JournalEntry.date >= date_from)
    if date_to:
        query = query.where(JournalEntry.date <= date_to)

    lines_result = await session.execute(query)
    lines = list(lines_result.scalars().all())

    # Aggregate per account
    balances: dict[uuid.UUID, dict] = {}
    for acc in accounts:
        balances[acc.id] = {
            "account": acc,
            "debit": Decimal("0.00"),
            "credit": Decimal("0.00"),
            "balance": Decimal("0.00"),
        }

    for line in lines:
        if line.account_id in balances:
            balances[line.account_id]["debit"] += line.debit or Decimal("0.00")
            balances[line.account_id]["credit"] += line.credit or Decimal("0.00")

    # Calculate balance (debit - credit for assets/expenses, credit - debit for liabilities/equity/revenue)
    for acc_id, data in balances.items():
        acc = data["account"]
        if acc.account_type in (AccountType.asset, AccountType.expense):
            data["balance"] = data["debit"] - data["credit"]
        else:
            data["balance"] = data["credit"] - data["debit"]

    return balances


@router.get("/entities/{entity_id}/reports/trial-balance", response_class=HTMLResponse)
async def trial_balance(
    request: Request,
    entity_id: uuid.UUID,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Trial balance report."""
    entity = await get_entity_for_user(entity_id, user, session)

    d_from = None
    d_to = None
    try:
        if date_from:
            d_from = date.fromisoformat(date_from)
        if date_to:
            d_to = date.fromisoformat(date_to)
    except ValueError:
        pass

    balances = await _get_account_balances(session, entity_id, d_from, d_to)

    # Filter out zero-balance accounts
    active_balances = {
        k: v for k, v in balances.items()
        if v["debit"] != 0 or v["credit"] != 0
    }

    total_debit = sum(v["debit"] for v in active_balances.values())
    total_credit = sum(v["credit"] for v in active_balances.values())

    return _templates().TemplateResponse(request, "reports/trial_balance.html", {"entity": entity,
        "balances": active_balances,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "user": user,
        "lang": user.preferred_lang,
    })


@router.get("/entities/{entity_id}/reports/profit-loss", response_class=HTMLResponse)
async def profit_loss(
    request: Request,
    entity_id: uuid.UUID,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Profit & Loss report."""
    entity = await get_entity_for_user(entity_id, user, session)

    d_from = None
    d_to = None
    try:
        if date_from:
            d_from = date.fromisoformat(date_from)
        if date_to:
            d_to = date.fromisoformat(date_to)
    except ValueError:
        pass

    balances = await _get_account_balances(session, entity_id, d_from, d_to)

    revenue_items = {k: v for k, v in balances.items() if v["account"].account_type == AccountType.revenue and v["balance"] != 0}
    expense_items = {k: v for k, v in balances.items() if v["account"].account_type == AccountType.expense and v["balance"] != 0}

    total_revenue = sum(v["balance"] for v in revenue_items.values())
    total_expenses = sum(v["balance"] for v in expense_items.values())
    net_result = total_revenue - total_expenses

    return _templates().TemplateResponse(request, "reports/profit_loss.html", {"entity": entity,
        "revenue_items": revenue_items,
        "expense_items": expense_items,
        "total_revenue": total_revenue,
        "total_expenses": total_expenses,
        "net_result": net_result,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "user": user,
        "lang": user.preferred_lang,
    })


@router.get("/entities/{entity_id}/reports/balance-sheet", response_class=HTMLResponse)
async def balance_sheet(
    request: Request,
    entity_id: uuid.UUID,
    as_of: str | None = Query(None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Balance sheet report."""
    entity = await get_entity_for_user(entity_id, user, session)

    d_to = None
    try:
        if as_of:
            d_to = date.fromisoformat(as_of)
    except ValueError:
        pass

    balances = await _get_account_balances(session, entity_id, date_to=d_to)

    asset_items = {k: v for k, v in balances.items() if v["account"].account_type == AccountType.asset and v["balance"] != 0}
    liability_items = {k: v for k, v in balances.items() if v["account"].account_type == AccountType.liability and v["balance"] != 0}
    equity_items = {k: v for k, v in balances.items() if v["account"].account_type == AccountType.equity and v["balance"] != 0}

    total_assets = sum(v["balance"] for v in asset_items.values())
    total_liabilities = sum(v["balance"] for v in liability_items.values())
    total_equity = sum(v["balance"] for v in equity_items.values())

    return _templates().TemplateResponse(request, "reports/balance_sheet.html", {"entity": entity,
        "asset_items": asset_items,
        "liability_items": liability_items,
        "equity_items": equity_items,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
        "as_of": as_of or "",
        "user": user,
        "lang": user.preferred_lang,
    })
