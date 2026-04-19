"""Bank account and bank transaction models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openboek.db import Base


class BankAccount(Base):
    """A bank account linked to an entity."""

    __tablename__ = "bank_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    iban: Mapped[str] = mapped_column(String(34), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), default=Decimal("0.00")
    )
    current_balance: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), default=Decimal("0.00")
    )

    transactions: Mapped[list[BankTransaction]] = relationship(
        "BankTransaction", back_populates="bank_account"
    )


class BankTransaction(Base):
    """An imported bank transaction."""

    __tablename__ = "bank_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bank_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_accounts.id"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    counterparty_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    counterparty_iban: Mapped[str | None] = mapped_column(String(34), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    matched_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("journal_entries.id"), nullable=True
    )
    import_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    bank_account: Mapped[BankAccount] = relationship(
        "BankAccount", back_populates="transactions"
    )
