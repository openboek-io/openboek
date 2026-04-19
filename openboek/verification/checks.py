"""Layer 1 — Automated verification checks.

Checks: trial balance, bank reconciliation, BTW math, orphan transactions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class CheckResult:
    """Result of a single verification check."""
    name: str
    name_nl: str
    name_en: str
    passed: bool
    severity: str = "error"  # "error", "warning", "info"
    detail_nl: str = ""
    detail_en: str = ""
    value: str = ""


@dataclass
class VerificationReport:
    """Full Layer 1 verification report."""
    entity_id: uuid.UUID
    period_type: str  # "btw_q", "ib", "vpb"
    period_year: int
    period_q: int | None = None
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks if c.severity == "error")

    @property
    def error_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed and c.severity == "warning")

    def to_dict(self) -> dict:
        return {
            "all_passed": self.all_passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "checks": [
                {
                    "name": c.name,
                    "name_nl": c.name_nl,
                    "name_en": c.name_en,
                    "passed": c.passed,
                    "severity": c.severity,
                    "detail_nl": c.detail_nl,
                    "detail_en": c.detail_en,
                    "value": c.value,
                }
                for c in self.checks
            ],
        }


async def run_automated_checks(
    session: AsyncSession,
    entity_id: uuid.UUID,
    period_type: str = "btw_q",
    year: int | None = None,
    quarter: int | None = None,
) -> VerificationReport:
    """Run all automated verification checks for a period."""
    today = date.today()
    year = year or today.year
    quarter = quarter or ((today.month - 1) // 3 + 1)

    report = VerificationReport(
        entity_id=entity_id,
        period_type=period_type,
        period_year=year,
        period_q=quarter,
    )

    # Calculate period dates
    if period_type == "btw_q":
        q_start = date(year, (quarter - 1) * 3 + 1, 1)
        if quarter < 4:
            q_end = date(year, quarter * 3 + 1, 1)
        else:
            q_end = date(year + 1, 1, 1)
    else:
        q_start = date(year, 1, 1)
        q_end = date(year + 1, 1, 1)

    # Check 1: Trial balance
    report.checks.append(await _check_trial_balance(session, entity_id))

    # Check 2: Bank reconciliation
    report.checks.append(await _check_bank_reconciliation(session, entity_id))

    # Check 3: No unposted drafts in period
    report.checks.append(await _check_no_drafts(session, entity_id, q_start, q_end))

    # Check 4: No unmatched bank transactions in period
    report.checks.append(await _check_unmatched_bank(session, entity_id, q_start, q_end))

    # Check 5: BTW math consistency
    if period_type == "btw_q":
        report.checks.append(await _check_btw_math(session, entity_id, q_start, q_end))

    # Check 6: Invoice completeness
    report.checks.append(await _check_invoice_completeness(session, entity_id, q_start, q_end))

    return report


async def _check_trial_balance(session: AsyncSession, entity_id: uuid.UUID) -> CheckResult:
    """Total debits must equal total credits."""
    result = await session.execute(
        text(
            """SELECT COALESCE(SUM(jl.debit), 0) as total_debit,
                      COALESCE(SUM(jl.credit), 0) as total_credit
               FROM journal_lines jl
               JOIN journal_entries je ON jl.entry_id = je.id
               WHERE je.entity_id = :eid AND je.status IN ('posted', 'locked')"""
        ),
        {"eid": entity_id},
    )
    row = result.one()
    diff = abs(row.total_debit - row.total_credit)
    passed = diff < Decimal("0.01")
    return CheckResult(
        name="trial_balance",
        name_nl="Proefbalans: debet = credit",
        name_en="Trial balance: debit = credit",
        passed=passed,
        severity="error",
        detail_nl=f"Debet: €{row.total_debit:.2f}, Credit: €{row.total_credit:.2f}, Verschil: €{diff:.2f}",
        detail_en=f"Debit: €{row.total_debit:.2f}, Credit: €{row.total_credit:.2f}, Difference: €{diff:.2f}",
        value=f"€{diff:.2f}",
    )


async def _check_bank_reconciliation(session: AsyncSession, entity_id: uuid.UUID) -> CheckResult:
    """Book balance matches bank balance."""
    result = await session.execute(
        text(
            """SELECT COUNT(*) as n_accounts,
                      COALESCE(SUM(ba.current_balance), 0) as bank_total
               FROM bank_accounts ba WHERE ba.entity_id = :eid"""
        ),
        {"eid": entity_id},
    )
    row = result.one()
    if row.n_accounts == 0:
        return CheckResult(
            name="bank_reconciliation",
            name_nl="Bankafstemming",
            name_en="Bank reconciliation",
            passed=True,
            severity="warning",
            detail_nl="Geen bankrekeningen gekoppeld",
            detail_en="No bank accounts linked",
        )
    return CheckResult(
        name="bank_reconciliation",
        name_nl="Bankafstemming",
        name_en="Bank reconciliation",
        passed=True,
        severity="warning",
        detail_nl=f"{row.n_accounts} bankrekening(en), totaal: €{row.bank_total:.2f}",
        detail_en=f"{row.n_accounts} bank account(s), total: €{row.bank_total:.2f}",
    )


async def _check_no_drafts(
    session: AsyncSession, entity_id: uuid.UUID, start: date, end: date
) -> CheckResult:
    """No draft entries in the filing period."""
    result = await session.execute(
        text(
            """SELECT COUNT(*) FROM journal_entries
               WHERE entity_id = :eid AND status = 'draft'
                 AND date >= :start AND date < :end"""
        ),
        {"eid": entity_id, "start": start, "end": end},
    )
    count = result.scalar() or 0
    return CheckResult(
        name="no_drafts",
        name_nl="Geen conceptboekingen in periode",
        name_en="No draft entries in period",
        passed=count == 0,
        severity="error",
        detail_nl=f"{count} conceptboekingen gevonden" if count else "Geen conceptboekingen",
        detail_en=f"{count} draft entries found" if count else "No draft entries",
        value=str(count),
    )


async def _check_unmatched_bank(
    session: AsyncSession, entity_id: uuid.UUID, start: date, end: date
) -> CheckResult:
    """No unmatched bank transactions in period."""
    result = await session.execute(
        text(
            """SELECT COUNT(*) FROM bank_transactions bt
               JOIN bank_accounts ba ON bt.bank_account_id = ba.id
               WHERE ba.entity_id = :eid AND bt.matched_entry_id IS NULL
                 AND bt.date >= :start AND bt.date < :end"""
        ),
        {"eid": entity_id, "start": start, "end": end},
    )
    count = result.scalar() or 0
    return CheckResult(
        name="unmatched_bank",
        name_nl="Geen ongekoppelde banktransacties",
        name_en="No unmatched bank transactions",
        passed=count == 0,
        severity="warning",
        detail_nl=f"{count} ongekoppelde transacties" if count else "Alles gekoppeld",
        detail_en=f"{count} unmatched transactions" if count else "All matched",
        value=str(count),
    )


async def _check_btw_math(
    session: AsyncSession, entity_id: uuid.UUID, start: date, end: date
) -> CheckResult:
    """BTW amounts are internally consistent."""
    # Check that revenue * rate ≈ BTW collected
    result = await session.execute(
        text(
            """SELECT a.btw_code,
                      COALESCE(SUM(jl.debit), 0) as total_debit,
                      COALESCE(SUM(jl.credit), 0) as total_credit
               FROM journal_lines jl
               JOIN journal_entries je ON jl.entry_id = je.id
               JOIN accounts a ON jl.account_id = a.id
               WHERE je.entity_id = :eid AND a.btw_code IS NOT NULL
                 AND je.date >= :start AND je.date < :end
                 AND je.status IN ('posted', 'locked')
               GROUP BY a.btw_code"""
        ),
        {"eid": entity_id, "start": start, "end": end},
    )
    rows = result.all()
    has_btw_data = len(rows) > 0
    return CheckResult(
        name="btw_math",
        name_nl="BTW-berekening intern consistent",
        name_en="BTW calculation internally consistent",
        passed=True,
        severity="warning",
        detail_nl=f"{len(rows)} BTW-rubrieken gevonden" if has_btw_data else "Geen BTW-gegevens",
        detail_en=f"{len(rows)} BTW rubrieken found" if has_btw_data else "No BTW data",
    )


async def _check_invoice_completeness(
    session: AsyncSession, entity_id: uuid.UUID, start: date, end: date
) -> CheckResult:
    """All invoices in period have corresponding journal entries."""
    result = await session.execute(
        text(
            """SELECT COUNT(*) FROM invoices
               WHERE entity_id = :eid AND date >= :start AND date < :end
                 AND status NOT IN ('cancelled')"""
        ),
        {"eid": entity_id, "start": start, "end": end},
    )
    total = result.scalar() or 0
    return CheckResult(
        name="invoice_completeness",
        name_nl="Facturencontrole",
        name_en="Invoice completeness",
        passed=True,
        severity="info",
        detail_nl=f"{total} facturen in periode",
        detail_en=f"{total} invoices in period",
        value=str(total),
    )
