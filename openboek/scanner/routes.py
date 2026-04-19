"""Scanner routes — receipt upload, OCR, review, confirm."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.accounting.models import Account, JournalEntry, JournalLine, JournalStatus
from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.audit.service import log_action
from openboek.db import get_session
from openboek.entities.models import Entity

router = APIRouter(tags=["scanner"])

# Base directory for receipt file storage
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "receipts"


def _templates():
    from openboek.main import templates
    return templates


@router.get("/entities/{entity_id}/scanner", response_class=HTMLResponse)
async def scanner_upload_page(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show the receipt scanner / upload page."""
    entity = await get_entity_for_user(entity_id, user, session)

    return _templates().TemplateResponse("scanner/upload.html", {
        "request": request,
        "entity": entity,
        "user": user,
        "lang": user.preferred_lang,
    })


@router.post("/entities/{entity_id}/scanner/upload", response_class=HTMLResponse)
async def scanner_upload(
    request: Request,
    entity_id: uuid.UUID,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload a receipt image, run OCR, show review page."""
    entity = await get_entity_for_user(entity_id, user, session)

    # Save the uploaded file
    now = datetime.now(timezone.utc)
    year = str(now.year)
    month = f"{now.month:02d}"
    file_id = str(uuid.uuid4())
    ext = Path(file.filename or "receipt.jpg").suffix or ".jpg"
    filename = f"{file_id}{ext}"

    storage_dir = DATA_DIR / str(entity_id) / year / month
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / filename

    content = await file.read()
    with open(storage_path, "wb") as f:
        f.write(content)

    # Store receipt file record in DB
    from openboek.db import Base  # Import here to avoid circular
    # Use raw SQL insert for the receipt_files table (no ORM model needed for simplicity)
    await session.execute(
        __import__("sqlalchemy").text(
            """INSERT INTO receipt_files (id, entity_id, uploaded_by, original_filename,
               storage_path, mime_type, file_size, ocr_status)
               VALUES (:id, :entity_id, :user_id, :filename, :path, :mime, :size, 'processing')"""
        ),
        {
            "id": uuid.UUID(file_id),
            "entity_id": entity_id,
            "user_id": user.id,
            "filename": file.filename,
            "path": str(storage_path),
            "mime": file.content_type or "image/jpeg",
            "size": len(content),
        },
    )

    # Run OCR
    from openboek.scanner.ocr import ocr_receipt

    ocr_result = await ocr_receipt(storage_path)
    ocr_error = ocr_result.get("error")

    # Update OCR status
    status = "failed" if ocr_error else "done"
    await session.execute(
        __import__("sqlalchemy").text(
            "UPDATE receipt_files SET ocr_status = :status, ocr_result = :result WHERE id = :id"
        ),
        {"status": status, "result": json.dumps(ocr_result), "id": uuid.UUID(file_id)},
    )

    await log_action(
        session, action="scanner.upload", user_id=user.id, entity_id=entity_id,
        after_data={"filename": file.filename, "ocr_status": status},
        ip_address=request.client.host if request.client else None,
    )

    # Get accounts for category assignment
    accounts_result = await session.execute(
        select(Account).where(Account.entity_id == entity_id).order_by(Account.code)
    )
    accounts = list(accounts_result.scalars().all())

    return _templates().TemplateResponse("scanner/review.html", {
        "request": request,
        "entity": entity,
        "user": user,
        "lang": user.preferred_lang,
        "ocr_result": ocr_result,
        "ocr_error": ocr_error,
        "file_id": file_id,
        "accounts": accounts,
    })


@router.post("/entities/{entity_id}/scanner/confirm", response_class=HTMLResponse)
async def scanner_confirm(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Confirm OCR results and create a draft journal entry."""
    entity = await get_entity_for_user(entity_id, user, session)
    form = await request.form()

    vendor = form.get("vendor", "")
    receipt_date_str = form.get("date", "")
    total_incl = form.get("total_incl", "0")
    total_excl = form.get("total_excl", "0")
    btw_amount = form.get("btw_amount", "0")
    btw_rate = form.get("btw_rate", "21")
    expense_account_id = form.get("expense_account_id")
    file_id = form.get("file_id")

    try:
        receipt_date = date.fromisoformat(receipt_date_str)
    except (ValueError, TypeError):
        receipt_date = date.today()

    try:
        amt_incl = Decimal(total_incl)
        amt_excl = Decimal(total_excl)
        amt_btw = Decimal(btw_amount)
    except Exception:
        amt_incl = Decimal("0.00")
        amt_excl = Decimal("0.00")
        amt_btw = Decimal("0.00")

    # Create draft journal entry
    je = JournalEntry(
        entity_id=entity_id,
        date=receipt_date,
        description=f"Receipt: {vendor}",
        reference=vendor,
        status=JournalStatus.draft,
        created_by=user.id,
    )
    session.add(je)
    await session.flush()

    # Find expense and BTW accounts
    accounts_result = await session.execute(
        select(Account).where(Account.entity_id == entity_id)
    )
    accounts = {str(a.id): a for a in accounts_result.scalars().all()}
    accounts_by_code = {a.code: a for a in accounts.values()}

    expense_acc = accounts.get(expense_account_id) if expense_account_id else None
    creditors_acc = accounts_by_code.get("1300")  # Creditors
    btw_input_acc = accounts_by_code.get("1510")  # BTW Voorbelasting

    if expense_acc and creditors_acc:
        # Debit expense account (excl BTW)
        session.add(JournalLine(
            entry_id=je.id,
            account_id=expense_acc.id,
            debit=amt_excl,
            credit=Decimal("0.00"),
            description=f"{vendor}",
        ))

        # Debit BTW input (if BTW applicable)
        if amt_btw > 0 and btw_input_acc:
            session.add(JournalLine(
                entry_id=je.id,
                account_id=btw_input_acc.id,
                debit=amt_btw,
                credit=Decimal("0.00"),
                description=f"BTW {btw_rate}% - {vendor}",
            ))

        # Credit creditors (total incl BTW)
        session.add(JournalLine(
            entry_id=je.id,
            account_id=creditors_acc.id,
            debit=Decimal("0.00"),
            credit=amt_incl,
            description=f"{vendor}",
        ))

    # Link receipt file to journal entry
    if file_id:
        import sqlalchemy
        await session.execute(
            sqlalchemy.text(
                "UPDATE receipt_files SET journal_entry_id = :je_id WHERE id = :file_id"
            ),
            {"je_id": je.id, "file_id": uuid.UUID(file_id)},
        )

    await log_action(
        session, action="scanner.confirm", user_id=user.id, entity_id=entity_id,
        table_name="journal_entries", record_id=str(je.id),
        after_data={"vendor": vendor, "total": str(amt_incl)},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(
        url=f"/entities/{entity_id}/journal/{je.id}?success=Receipt+processed",
        status_code=303,
    )
