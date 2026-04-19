"""Chart of accounts, journal entries, and journal lines."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openboek.db import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AccountType(str, enum.Enum):
    asset = "asset"
    liability = "liability"
    equity = "equity"
    revenue = "revenue"
    expense = "expense"


class JournalStatus(str, enum.Enum):
    draft = "draft"
    posted = "posted"
    locked = "locked"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Account(Base):
    """A ledger account (RGS-compatible chart of accounts)."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name_nl: Mapped[str] = mapped_column(String(255), nullable=False)
    name_en: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[AccountType] = mapped_column(
        Enum(AccountType, name="account_type_enum"), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True
    )
    btw_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    parent: Mapped[Account | None] = relationship(
        "Account", remote_side="Account.id", back_populates="children"
    )
    children: Mapped[list[Account]] = relationship("Account", back_populates="parent")
    journal_lines: Mapped[list[JournalLine]] = relationship(
        "JournalLine", back_populates="account"
    )


class JournalEntry(Base):
    """A double-entry journal entry (header)."""

    __tablename__ = "journal_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[JournalStatus] = mapped_column(
        Enum(JournalStatus, name="journal_status_enum"), default=JournalStatus.draft
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    posted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    lines: Mapped[list[JournalLine]] = relationship(
        "JournalLine", back_populates="entry", cascade="all, delete-orphan"
    )


class JournalLine(Base):
    """A single debit or credit line within a journal entry."""

    __tablename__ = "journal_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    debit: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    credit: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), default=Decimal("1.000000")
    )
    amount_original: Mapped[Decimal | None] = mapped_column(
        Numeric(15, 2), nullable=True
    )

    entry: Mapped[JournalEntry] = relationship("JournalEntry", back_populates="lines")
    account: Mapped[Account] = relationship("Account", back_populates="journal_lines")
