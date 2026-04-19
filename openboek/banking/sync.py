"""Background bank sync — poll GoCardless for new transactions, deduplicate, import."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.banking.models import BankAccount, BankTransaction

logger = logging.getLogger(__name__)


async def sync_gocardless_transactions(
    session: AsyncSession,
    entity_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> dict[str, int]:
    """Sync transactions from GoCardless for a specific connection.

    Returns dict with 'imported' and 'skipped' counts.
    """
    from openboek.banking.gocardless import GoCardlessClient, is_configured

    if not is_configured():
        return {"imported": 0, "skipped": 0, "error": "GoCardless not configured"}

    # Get the connection details
    result = await session.execute(
        text("SELECT requisition_id, account_ids FROM gocardless_connections WHERE id = :id"),
        {"id": connection_id},
    )
    row = result.one_or_none()
    if not row:
        return {"imported": 0, "skipped": 0, "error": "Connection not found"}

    requisition_id = row.requisition_id
    account_ids = row.account_ids or []

    client = GoCardlessClient()
    imported_total = 0
    skipped_total = 0

    for gc_account_id in account_ids:
        try:
            # Get account details to find matching bank account
            details = await client.get_account_details(gc_account_id)
            account_data = details.get("account", {})
            iban = account_data.get("iban", "")

            # Find or create bank account
            ba_result = await session.execute(
                select(BankAccount).where(
                    BankAccount.entity_id == entity_id,
                    BankAccount.iban == iban,
                )
            )
            bank_account = ba_result.scalar_one_or_none()
            if not bank_account:
                bank_account = BankAccount(
                    entity_id=entity_id,
                    name=account_data.get("ownerName", iban),
                    iban=iban,
                    currency=account_data.get("currency", "EUR"),
                )
                session.add(bank_account)
                await session.flush()

            # Fetch transactions
            tx_data = await client.get_transactions(gc_account_id)
            booked = tx_data.get("transactions", {}).get("booked", [])

            for tx in booked:
                # Create import hash for deduplication
                tx_hash = _make_hash(
                    iban=iban,
                    date=tx.get("bookingDate", ""),
                    amount=tx.get("transactionAmount", {}).get("amount", "0"),
                    ref=tx.get("internalTransactionId", tx.get("transactionId", "")),
                )

                # Check for duplicate
                existing = await session.execute(
                    select(BankTransaction).where(BankTransaction.import_hash == tx_hash)
                )
                if existing.scalar_one_or_none():
                    skipped_total += 1
                    continue

                # Parse amount
                amount_str = tx.get("transactionAmount", {}).get("amount", "0")
                try:
                    amount = Decimal(amount_str)
                except Exception:
                    amount = Decimal("0.00")

                # Extract counterparty info
                counterparty_name = (
                    tx.get("creditorName")
                    or tx.get("debtorName")
                    or tx.get("remittanceInformationUnstructured", "")[:100]
                )
                counterparty_iban = (
                    tx.get("creditorAccount", {}).get("iban")
                    or tx.get("debtorAccount", {}).get("iban")
                )

                description = tx.get("remittanceInformationUnstructured", "")
                reference = tx.get("endToEndId") or tx.get("internalTransactionId", "")

                bank_tx = BankTransaction(
                    bank_account_id=bank_account.id,
                    date=date.fromisoformat(tx.get("bookingDate", str(date.today()))),
                    amount=amount,
                    counterparty_name=counterparty_name,
                    counterparty_iban=counterparty_iban,
                    description=description[:500] if description else None,
                    reference=reference[:255] if reference else None,
                    import_hash=tx_hash,
                )
                session.add(bank_tx)
                imported_total += 1

        except Exception as e:
            logger.exception("Error syncing GoCardless account %s: %s", gc_account_id, e)
            continue

    # Update last synced timestamp
    await session.execute(
        text("UPDATE gocardless_connections SET last_synced_at = :ts WHERE id = :id"),
        {"ts": datetime.now(timezone.utc), "id": connection_id},
    )

    return {"imported": imported_total, "skipped": skipped_total}


def _make_hash(iban: str, date: str, amount: str, ref: str) -> str:
    """Create a deterministic hash for deduplication."""
    raw = f"{iban}|{date}|{amount}|{ref}"
    return hashlib.sha256(raw.encode()).hexdigest()
