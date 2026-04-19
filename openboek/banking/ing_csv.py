"""ING CSV bank statement parser.

ING (Netherlands) CSV format:
- Semicolon-separated
- Columns: Datum, Naam/Omschrijving, Rekening, Tegenrekening, Code, Af Bij, Bedrag (EUR), Mutatiesoort, Mededelingen
- "Af" = money out (negative), "Bij" = money in (positive)
- Date format: YYYYMMDD
- Amounts use comma as decimal separator
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from openboek.banking.mt940 import ParsedTransaction


def detect_ing_csv(content: str) -> bool:
    """Check if content looks like an ING CSV export."""
    first_line = content.split("\n", 1)[0].strip().strip('"')
    if "Naam / Omschrijving" in first_line and "Af Bij" in first_line:
        return True
    if "Datum" in first_line and "Bedrag" in first_line and ";" in first_line:
        return True
    return False


def parse_ing_csv(content: str) -> list[ParsedTransaction]:
    """Parse ING CSV content into ParsedTransaction list."""
    transactions: list[ParsedTransaction] = []
    reader = csv.DictReader(io.StringIO(content), delimiter=";")

    for row in reader:
        try:
            date_str = row.get("Datum", "").strip().strip('"')
            if len(date_str) == 8 and date_str.isdigit():
                tx_date = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
            else:
                continue

            amount_str = row.get("Bedrag (EUR)", "0").strip().strip('"').replace(",", ".")
            try:
                amount = Decimal(amount_str)
            except (InvalidOperation, ValueError):
                amount = Decimal("0")

            direction = row.get("Af Bij", "").strip().strip('"').lower()
            if direction == "af":
                amount = -abs(amount)
            elif direction == "bij":
                amount = abs(amount)

            counterparty_name = row.get("Naam / Omschrijving", "").strip().strip('"')
            counterparty_iban = row.get("Tegenrekening", "").strip().strip('"')
            description = row.get("Mededelingen", "").strip().strip('"')
            mutation_type = row.get("Mutatiesoort", "").strip().strip('"')
            code = row.get("Code", "").strip().strip('"')

            full_desc = description
            if mutation_type and mutation_type not in description:
                full_desc = f"[{mutation_type}] {description}" if description else mutation_type

            hash_input = f"{tx_date}|{amount}|{counterparty_name}|{counterparty_iban}|{description[:50]}"
            import_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

            transactions.append(ParsedTransaction(
                date=tx_date,
                amount=amount,
                counterparty_name=counterparty_name,
                counterparty_iban=counterparty_iban,
                description=full_desc,
                reference=code,
                import_hash=import_hash,
            ))
        except Exception:
            continue

    return transactions
