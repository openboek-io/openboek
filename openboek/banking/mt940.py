"""MT940 bank statement parser using the mt-940 library."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import mt940


@dataclass
class ParsedTransaction:
    """A single parsed bank transaction."""

    date: date
    amount: Decimal
    counterparty_name: str
    counterparty_iban: str
    description: str
    reference: str
    import_hash: str


def parse_mt940(content: str | bytes) -> list[ParsedTransaction]:
    """Parse MT940 content and return a list of transactions.

    Args:
        content: Raw MT940 file content (string or bytes).

    Returns:
        List of ParsedTransaction objects ready for import.
    """
    if isinstance(content, str):
        content = content.encode("utf-8")

    transactions: list[ParsedTransaction] = []

    try:
        statements = mt940.parse(content)
    except Exception:
        return []

    for statement in statements:
        for tx in statement.transactions:
            data = tx.data
            amount = Decimal(str(data.get("amount", {}).get("amount", "0")))
            tx_date = data.get("date", date.today())
            if hasattr(tx_date, "date"):
                tx_date = tx_date.date() if callable(tx_date.date) else tx_date

            # Extract details from transaction
            detail = tx.data.get("transaction_details", "") or ""
            # Try to parse counterparty info from details
            counterparty_name = ""
            counterparty_iban = ""
            reference = data.get("customer_reference", "") or data.get("bank_reference", "") or ""

            # Common MT940 detail parsing: lines starting with /NAME/ or /IBAN/
            for line in detail.split("\n"):
                line = line.strip()
                if line.startswith("/NAME/"):
                    counterparty_name = line[6:]
                elif line.startswith("/IBAN/"):
                    counterparty_iban = line[6:]
                elif line.startswith("/REMI/"):
                    if not reference:
                        reference = line[6:]
            if not counterparty_name:
                counterparty_name = detail[:100] if detail else ""

            description = detail[:500] if detail else ""

            # Generate unique hash for dedup
            hash_input = f"{tx_date}|{amount}|{counterparty_name}|{reference}|{description[:50]}"
            import_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

            transactions.append(ParsedTransaction(
                date=tx_date if isinstance(tx_date, date) else date.today(),
                amount=amount,
                counterparty_name=counterparty_name,
                counterparty_iban=counterparty_iban,
                description=description,
                reference=reference,
                import_hash=import_hash,
            ))

    return transactions
