"""Invoice routes — CRUD, send, paid, PDF generation."""

from __future__ import annotations

import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openboek.accounting.models import (
    Account,
    JournalEntry,
    JournalLine,
    JournalStatus,
)
from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.audit.service import log_action
from openboek.db import get_session
from openboek.entities.models import Entity
from openboek.invoices.models import Invoice, InvoiceLine, InvoiceStatus, InvoiceType

router = APIRouter(tags=["invoices"])


def _templates():
    from openboek.main import templates
    return templates


@router.get("/entities/{entity_id}/invoices", response_class=HTMLResponse)
async def invoice_list(
    request: Request,
    entity_id: uuid.UUID,
    invoice_type: str | None = Query(None),
    status: str | None = Query(None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List invoices with optional filters."""
    entity = await get_entity_for_user(entity_id, user, session)
    query = (
        select(Invoice)
        .where(Invoice.entity_id == entity_id)
        .order_by(Invoice.date.desc())
    )
    if invoice_type:
        try:
            query = query.where(Invoice.invoice_type == InvoiceType(invoice_type))
        except ValueError:
            pass
    if status:
        try:
            query = query.where(Invoice.status == InvoiceStatus(status))
        except ValueError:
            pass

    result = await session.execute(query)
    invoices = list(result.scalars().all())

    return _templates().TemplateResponse(request, "invoices/list.html", {"entity": entity,
        "invoices": invoices,
        "type_filter": invoice_type or "",
        "status_filter": status or "",
        "user": user,
        "lang": user.preferred_lang,
    })


@router.get("/entities/{entity_id}/invoices/new", response_class=HTMLResponse)
async def invoice_new(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """New invoice form."""
    entity = await get_entity_for_user(entity_id, user, session)
    accounts_result = await session.execute(
        select(Account).where(Account.entity_id == entity_id).order_by(Account.code)
    )
    accounts = list(accounts_result.scalars().all())

    return _templates().TemplateResponse(request, "invoices/form.html", {"entity": entity,
        "invoice": None,
        "accounts": accounts,
        "error": None,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.post("/entities/{entity_id}/invoices", response_class=HTMLResponse)
async def invoice_create(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Create a new invoice from form data."""
    entity = await get_entity_for_user(entity_id, user, session)
    form = await request.form()

    inv_type = form.get("invoice_type", "sales")
    inv_number = form.get("invoice_number", "")
    inv_date_str = form.get("date", "")
    due_date_str = form.get("due_date", "")
    counterparty = form.get("counterparty_name", "")
    counterparty_vat = form.get("counterparty_vat", "")

    try:
        inv_date = date.fromisoformat(inv_date_str)
    except (ValueError, TypeError):
        inv_date = date.today()
    try:
        due_date = date.fromisoformat(due_date_str) if due_date_str else None
    except (ValueError, TypeError):
        due_date = None

    invoice = Invoice(
        entity_id=entity_id,
        invoice_type=InvoiceType(inv_type),
        invoice_number=inv_number,
        date=inv_date,
        due_date=due_date,
        counterparty_name=counterparty,
        counterparty_vat=counterparty_vat or None,
        status=InvoiceStatus.draft,
    )
    session.add(invoice)
    await session.flush()

    # Parse invoice lines
    total_excl = Decimal("0.00")
    total_btw = Decimal("0.00")
    line_idx = 0
    while True:
        desc = form.get(f"line_desc_{line_idx}")
        if desc is None:
            break
        try:
            qty = Decimal(form.get(f"line_qty_{line_idx}", "1") or "1")
            price = Decimal(form.get(f"line_price_{line_idx}", "0") or "0")
            btw_rate = Decimal(form.get(f"line_btw_{line_idx}", "21") or "21")
        except InvalidOperation:
            line_idx += 1
            continue

        line_total = qty * price
        btw_amount = (line_total * btw_rate / Decimal("100")).quantize(Decimal("0.01"))
        account_id = form.get(f"line_account_{line_idx}") or None

        inv_line = InvoiceLine(
            invoice_id=invoice.id,
            description=desc,
            quantity=qty,
            unit_price=price,
            btw_rate=btw_rate,
            btw_amount=btw_amount,
            total=line_total + btw_amount,
            account_id=uuid.UUID(account_id) if account_id else None,
        )
        session.add(inv_line)
        total_excl += line_total
        total_btw += btw_amount
        line_idx += 1

    invoice.total_excl = total_excl
    invoice.total_btw = total_btw
    invoice.total_incl = total_excl + total_btw

    await log_action(
        session, action="invoice.create", user_id=user.id, entity_id=entity_id,
        table_name="invoices", record_id=str(invoice.id),
        after_data={"number": inv_number, "type": inv_type, "total": str(invoice.total_incl)},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(url=f"/entities/{entity_id}/invoices/{invoice.id}", status_code=303)


@router.get("/entities/{entity_id}/invoices/{inv_id}", response_class=HTMLResponse)
async def invoice_view(
    request: Request,
    entity_id: uuid.UUID,
    inv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """View invoice detail."""
    entity = await get_entity_for_user(entity_id, user, session)
    result = await session.execute(
        select(Invoice)
        .where(Invoice.id == inv_id, Invoice.entity_id == entity_id)
        .options(selectinload(Invoice.lines))
    )
    invoice = result.scalar_one_or_none()
    if invoice is None:
        return RedirectResponse(url=f"/entities/{entity_id}/invoices", status_code=303)

    return _templates().TemplateResponse(request, "invoices/detail.html", {"entity": entity,
        "invoice": invoice,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.post("/entities/{entity_id}/invoices/{inv_id}/send")
async def invoice_send(
    request: Request,
    entity_id: uuid.UUID,
    inv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Mark invoice as sent."""
    entity = await get_entity_for_user(entity_id, user, session)
    result = await session.execute(
        select(Invoice).where(Invoice.id == inv_id, Invoice.entity_id == entity_id)
    )
    invoice = result.scalar_one_or_none()
    if invoice and invoice.status == InvoiceStatus.draft:
        invoice.status = InvoiceStatus.sent
        await log_action(
            session, action="invoice.send", user_id=user.id, entity_id=entity_id,
            table_name="invoices", record_id=str(inv_id),
            ip_address=request.client.host if request.client else None,
        )
    return RedirectResponse(url=f"/entities/{entity_id}/invoices/{inv_id}", status_code=303)


@router.post("/entities/{entity_id}/invoices/{inv_id}/paid")
async def invoice_paid(
    request: Request,
    entity_id: uuid.UUID,
    inv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Mark invoice as paid and auto-create journal entry."""
    entity = await get_entity_for_user(entity_id, user, session)
    result = await session.execute(
        select(Invoice)
        .where(Invoice.id == inv_id, Invoice.entity_id == entity_id)
        .options(selectinload(Invoice.lines))
    )
    invoice = result.scalar_one_or_none()
    if invoice is None or invoice.status == InvoiceStatus.paid:
        return RedirectResponse(url=f"/entities/{entity_id}/invoices/{inv_id}", status_code=303)

    invoice.status = InvoiceStatus.paid

    # Auto-create journal entry for the payment
    # Find debtors/creditors and bank accounts
    accounts_result = await session.execute(
        select(Account).where(Account.entity_id == entity_id)
    )
    accounts = {a.code: a for a in accounts_result.scalars().all()}

    # Use standard accounts: 1200 = Debtors, 1300 = Creditors, 1000 = Bank
    bank_acc = accounts.get("1000")
    if invoice.invoice_type == InvoiceType.sales:
        counter_acc = accounts.get("1200")  # Debtors
    else:
        counter_acc = accounts.get("1300")  # Creditors

    if bank_acc and counter_acc:
        je = JournalEntry(
            entity_id=entity_id,
            date=date.today(),
            description=f"Payment: {invoice.invoice_number}",
            reference=invoice.invoice_number,
            status=JournalStatus.posted,
            created_by=user.id,
            posted_at=datetime.now(timezone.utc),
            posted_by=user.id,
        )
        session.add(je)
        await session.flush()

        if invoice.invoice_type == InvoiceType.sales:
            # Debit bank, credit debtors
            session.add(JournalLine(
                entry_id=je.id, account_id=bank_acc.id,
                debit=invoice.total_incl, credit=Decimal("0.00"),
            ))
            session.add(JournalLine(
                entry_id=je.id, account_id=counter_acc.id,
                debit=Decimal("0.00"), credit=invoice.total_incl,
            ))
        else:
            # Debit creditors, credit bank
            session.add(JournalLine(
                entry_id=je.id, account_id=counter_acc.id,
                debit=invoice.total_incl, credit=Decimal("0.00"),
            ))
            session.add(JournalLine(
                entry_id=je.id, account_id=bank_acc.id,
                debit=Decimal("0.00"), credit=invoice.total_incl,
            ))

    await log_action(
        session, action="invoice.paid", user_id=user.id, entity_id=entity_id,
        table_name="invoices", record_id=str(inv_id),
        after_data={"total": str(invoice.total_incl)},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(url=f"/entities/{entity_id}/invoices/{inv_id}", status_code=303)


@router.get("/entities/{entity_id}/invoices/{inv_id}/pdf")
async def invoice_pdf(
    request: Request,
    entity_id: uuid.UUID,
    inv_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Generate a simple invoice PDF (text-based)."""
    entity = await get_entity_for_user(entity_id, user, session)
    result = await session.execute(
        select(Invoice)
        .where(Invoice.id == inv_id, Invoice.entity_id == entity_id)
        .options(selectinload(Invoice.lines))
    )
    invoice = result.scalar_one_or_none()
    if invoice is None:
        return RedirectResponse(url=f"/entities/{entity_id}/invoices", status_code=303)

    # Try WeasyPrint PDF first, fall back to text
    from openboek.invoices.pdf import generate_invoice_pdf

    lang = user.preferred_lang
    pdf_bytes = generate_invoice_pdf(invoice, entity, lang)

    if pdf_bytes:
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="invoice_{invoice.invoice_number}.pdf"'},
        )

    # Fallback: plain text invoice
    lines_text = []
    for line in invoice.lines:
        lines_text.append(
            f"  {line.description or '-':<40} "
            f"{line.quantity:>8.2f} x \u20ac{line.unit_price:>10.2f} "
            f"BTW {line.btw_rate}% = \u20ac{line.total:>10.2f}"
        )

    content = f"""
{'=' * 72}
{entity.name}
{entity.address or ''}
{entity.city or ''}
KVK: {entity.kvk_number or '-'}  BTW: {entity.btw_number or '-'}
{'=' * 72}

FACTUUR / INVOICE
Nummer: {invoice.invoice_number}
Datum:  {invoice.date}
Vervaldatum: {invoice.due_date or '-'}

Aan / To: {invoice.counterparty_name}
BTW nr:   {invoice.counterparty_vat or '-'}

{'-' * 72}
{'Omschrijving':<40} {'Aantal':>8}   {'Prijs':>10}   {'Totaal':>10}
{'-' * 72}
{chr(10).join(lines_text)}
{'-' * 72}
{'Subtotaal / Subtotal:':<50} \u20ac{invoice.total_excl:>10.2f}
{'BTW / VAT:':<50} \u20ac{invoice.total_btw:>10.2f}
{'Totaal / Total:':<50} \u20ac{invoice.total_incl:>10.2f}
{'=' * 72}
"""

    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="invoice_{invoice.invoice_number}.txt"'},
    )
