"""Banking routes — accounts, MT940 import, reconciliation."""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.audit.service import log_action
from openboek.banking.models import BankAccount, BankTransaction
from openboek.banking.mt940 import parse_mt940
from openboek.db import get_session
from openboek.entities.models import Entity
from openboek.accounting.models import JournalEntry

router = APIRouter(tags=["banking"])


def _templates():
    from openboek.main import templates
    return templates


@router.get("/entities/{entity_id}/banking", response_class=HTMLResponse)
async def banking_overview(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show bank accounts and recent transactions."""
    entity = await get_entity_for_user(entity_id, user, session)

    accounts_result = await session.execute(
        select(BankAccount).where(BankAccount.entity_id == entity_id)
    )
    bank_accounts = list(accounts_result.scalars().all())

    # Get recent transactions across all accounts
    account_ids = [a.id for a in bank_accounts]
    transactions = []
    if account_ids:
        tx_result = await session.execute(
            select(BankTransaction)
            .where(BankTransaction.bank_account_id.in_(account_ids))
            .order_by(BankTransaction.date.desc())
            .limit(50)
        )
        transactions = list(tx_result.scalars().all())

    return _templates().TemplateResponse(request, "banking/overview.html", {"entity": entity,
        "bank_accounts": bank_accounts,
        "transactions": transactions,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.post("/entities/{entity_id}/banking/accounts")
async def add_bank_account(
    request: Request,
    entity_id: uuid.UUID,
    name: str = Form(...),
    iban: str = Form(...),
    opening_balance: str = Form("0.00"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Add a bank account to an entity."""
    entity = await get_entity_for_user(entity_id, user, session)

    try:
        bal = Decimal(opening_balance)
    except Exception:
        bal = Decimal("0.00")

    account = BankAccount(
        entity_id=entity_id,
        name=name,
        iban=iban.replace(" ", "").upper(),
        opening_balance=bal,
        current_balance=bal,
    )
    session.add(account)
    await session.flush()

    await log_action(
        session, action="bank_account.create", user_id=user.id, entity_id=entity_id,
        table_name="bank_accounts", record_id=str(account.id),
        after_data={"name": name, "iban": iban},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(url=f"/entities/{entity_id}/banking", status_code=303)


@router.get("/entities/{entity_id}/banking/import", response_class=HTMLResponse)
async def import_form(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show MT940 import form."""
    entity = await get_entity_for_user(entity_id, user, session)
    accounts_result = await session.execute(
        select(BankAccount).where(BankAccount.entity_id == entity_id)
    )
    bank_accounts = list(accounts_result.scalars().all())

    return _templates().TemplateResponse(request, "banking/import.html", {"entity": entity,
        "bank_accounts": bank_accounts,
        "preview": None,
        "error": None,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.post("/entities/{entity_id}/banking/import", response_class=HTMLResponse)
async def import_mt940(
    request: Request,
    entity_id: uuid.UUID,
    bank_account_id: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Parse MT940 file, show preview, and import transactions."""
    entity = await get_entity_for_user(entity_id, user, session)
    content = await file.read()

    parsed = parse_mt940(content)
    if not parsed:
        accounts_result = await session.execute(
            select(BankAccount).where(BankAccount.entity_id == entity_id)
        )
        bank_accounts = list(accounts_result.scalars().all())
        return _templates().TemplateResponse(request, "banking/import.html", {"entity": entity,
            "bank_accounts": bank_accounts,
            "preview": None,
            "error": "Could not parse the MT940 file. Please check the format.",
            "user": user,
            "lang": user.preferred_lang,
        })

    # Import transactions (skip duplicates via import_hash)
    imported_count = 0
    skipped_count = 0
    ba_id = uuid.UUID(bank_account_id)

    for tx in parsed:
        existing = await session.execute(
            select(BankTransaction).where(BankTransaction.import_hash == tx.import_hash)
        )
        if existing.scalar_one_or_none():
            skipped_count += 1
            continue

        bank_tx = BankTransaction(
            bank_account_id=ba_id,
            date=tx.date,
            amount=tx.amount,
            counterparty_name=tx.counterparty_name,
            counterparty_iban=tx.counterparty_iban,
            description=tx.description,
            reference=tx.reference,
            import_hash=tx.import_hash,
        )
        session.add(bank_tx)
        imported_count += 1

    await log_action(
        session, action="banking.import", user_id=user.id, entity_id=entity_id,
        after_data={"imported": imported_count, "skipped": skipped_count, "filename": file.filename},
        ip_address=request.client.host if request.client else None,
    )

    accounts_result = await session.execute(
        select(BankAccount).where(BankAccount.entity_id == entity_id)
    )
    bank_accounts = list(accounts_result.scalars().all())

    return _templates().TemplateResponse(request, "banking/import.html", {"entity": entity,
        "bank_accounts": bank_accounts,
        "preview": parsed,
        "imported_count": imported_count,
        "skipped_count": skipped_count,
        "error": None,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.get("/entities/{entity_id}/banking/reconcile", response_class=HTMLResponse)
async def reconcile_list(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show unmatched bank transactions for reconciliation."""
    entity = await get_entity_for_user(entity_id, user, session)

    accounts_result = await session.execute(
        select(BankAccount).where(BankAccount.entity_id == entity_id)
    )
    bank_accounts = list(accounts_result.scalars().all())
    account_ids = [a.id for a in bank_accounts]

    unmatched = []
    if account_ids:
        tx_result = await session.execute(
            select(BankTransaction)
            .where(
                BankTransaction.bank_account_id.in_(account_ids),
                BankTransaction.matched_entry_id.is_(None),
            )
            .order_by(BankTransaction.date.desc())
        )
        unmatched = list(tx_result.scalars().all())

    # Get journal entries for matching
    entries_result = await session.execute(
        select(JournalEntry)
        .where(JournalEntry.entity_id == entity_id)
        .order_by(JournalEntry.date.desc())
        .limit(100)
    )
    entries = list(entries_result.scalars().all())

    return _templates().TemplateResponse(request, "banking/reconcile.html", {"entity": entity,
        "unmatched": unmatched,
        "entries": entries,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.post("/entities/{entity_id}/banking/reconcile/{tx_id}/match")
async def reconcile_match(
    request: Request,
    entity_id: uuid.UUID,
    tx_id: uuid.UUID,
    journal_entry_id: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Match a bank transaction to a journal entry."""
    entity = await get_entity_for_user(entity_id, user, session)

    result = await session.execute(
        select(BankTransaction).where(BankTransaction.id == tx_id)
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        return RedirectResponse(url=f"/entities/{entity_id}/banking/reconcile", status_code=303)

    tx.matched_entry_id = uuid.UUID(journal_entry_id)

    await log_action(
        session, action="banking.match", user_id=user.id, entity_id=entity_id,
        table_name="bank_transactions", record_id=str(tx_id),
        after_data={"journal_entry_id": journal_entry_id},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(url=f"/entities/{entity_id}/banking/reconcile", status_code=303)


# ---------------------------------------------------------------------------
# GoCardless bank connection routes
# ---------------------------------------------------------------------------

@router.get("/entities/{entity_id}/banking/connect", response_class=HTMLResponse)
async def banking_connect_page(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show GoCardless bank connection page."""
    entity = await get_entity_for_user(entity_id, user, session)

    from openboek.banking.gocardless import is_configured
    gc_available = is_configured()

    # Get existing connections
    from sqlalchemy import text as sa_text
    connections = []
    if gc_available:
        try:
            result = await session.execute(
                sa_text("SELECT * FROM gocardless_connections WHERE entity_id = :eid ORDER BY created_at DESC"),
                {"eid": entity_id},
            )
            connections = result.mappings().all()
        except Exception:
            pass

    return _templates().TemplateResponse(request, "banking/connect.html", {"entity": entity,
        "gc_available": gc_available,
        "connections": connections,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.post("/entities/{entity_id}/banking/connect")
async def banking_connect_start(
    request: Request,
    entity_id: uuid.UUID,
    institution_id: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Start GoCardless bank connection flow."""
    entity = await get_entity_for_user(entity_id, user, session)

    from openboek.banking.gocardless import GoCardlessClient

    client = GoCardlessClient()
    redirect_url = str(request.url_for("banking_connect_callback", entity_id=entity_id))

    try:
        requisition = await client.create_requisition(
            institution_id=institution_id,
            redirect_url=redirect_url,
            reference=str(entity_id),
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/entities/{entity_id}/banking/connect?error=Failed+to+connect:+{str(e)[:100]}",
            status_code=303,
        )

    # Save connection record
    from sqlalchemy import text as sa_text
    conn_id = uuid.uuid4()
    await session.execute(
        sa_text(
            """INSERT INTO gocardless_connections (id, entity_id, requisition_id, institution_id, status)
               VALUES (:id, :eid, :req_id, :inst_id, 'pending')"""
        ),
        {
            "id": conn_id,
            "eid": entity_id,
            "req_id": requisition["id"],
            "inst_id": institution_id,
        },
    )

    await log_action(
        session, action="banking.gocardless_connect", user_id=user.id, entity_id=entity_id,
        after_data={"institution_id": institution_id, "requisition_id": requisition["id"]},
        ip_address=request.client.host if request.client else None,
    )

    # Redirect to bank authorization
    return RedirectResponse(url=requisition["link"], status_code=303)


@router.get("/entities/{entity_id}/banking/callback", name="banking_connect_callback")
async def banking_connect_callback(
    request: Request,
    entity_id: uuid.UUID,
    ref: str = "",
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """GoCardless OAuth callback after bank authorization."""
    entity = await get_entity_for_user(entity_id, user, session)

    from openboek.banking.gocardless import GoCardlessClient
    from sqlalchemy import text as sa_text

    # Find the pending connection
    result = await session.execute(
        sa_text(
            "SELECT id, requisition_id FROM gocardless_connections "
            "WHERE entity_id = :eid AND status = 'pending' ORDER BY created_at DESC LIMIT 1"
        ),
        {"eid": entity_id},
    )
    conn = result.one_or_none()
    if not conn:
        return RedirectResponse(
            url=f"/entities/{entity_id}/banking/connect?error=No+pending+connection",
            status_code=303,
        )

    # Check requisition status
    client = GoCardlessClient()
    try:
        req_data = await client.get_requisition(conn.requisition_id)
        accounts = req_data.get("accounts", [])
        status = req_data.get("status", "unknown")

        import json
        await session.execute(
            sa_text(
                "UPDATE gocardless_connections SET status = :status, account_ids = :accounts WHERE id = :id"
            ),
            {
                "status": "linked" if accounts else status,
                "accounts": json.dumps(accounts),
                "id": conn.id,
            },
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/entities/{entity_id}/banking/connect?error=Callback+failed:+{str(e)[:100]}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/entities/{entity_id}/banking/connect?success=Bank+connected+successfully",
        status_code=303,
    )


@router.post("/entities/{entity_id}/banking/sync/{connection_id}")
async def banking_sync(
    request: Request,
    entity_id: uuid.UUID,
    connection_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Trigger manual sync for a GoCardless connection."""
    entity = await get_entity_for_user(entity_id, user, session)

    from openboek.banking.sync import sync_gocardless_transactions

    result = await sync_gocardless_transactions(session, entity_id, connection_id)
    imported = result.get("imported", 0)
    skipped = result.get("skipped", 0)

    await log_action(
        session, action="banking.sync", user_id=user.id, entity_id=entity_id,
        after_data={"imported": imported, "skipped": skipped},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(
        url=f"/entities/{entity_id}/banking/connect?success=Synced:+{imported}+imported,+{skipped}+skipped",
        status_code=303,
    )
