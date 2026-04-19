"""Invoice and InvoiceLine models."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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

class InvoiceType(str, enum.Enum):
    sales = "sales"
    purchase = "purchase"


class InvoiceStatus(str, enum.Enum):
    draft = "draft"
    sent = "sent"
    paid = "paid"
    cancelled = "cancelled"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Invoice(Base):
    """A sales or purchase invoice."""

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False
    )
    invoice_type: Mapped[InvoiceType] = mapped_column(
        Enum(InvoiceType, name="invoice_type_enum"), nullable=False
    )
    invoice_number: Mapped[str] = mapped_column(String(50), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    counterparty_name: Mapped[str] = mapped_column(String(255), nullable=False)
    counterparty_vat: Mapped[str | None] = mapped_column(String(20), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status_enum"), default=InvoiceStatus.draft
    )
    total_excl: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    total_btw: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    total_incl: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    lines: Mapped[list[InvoiceLine]] = relationship(
        "InvoiceLine", back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceLine(Base):
    """A single line item on an invoice."""

    __tablename__ = "invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 3), default=Decimal("1.000"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    btw_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("21.00"))
    btw_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True
    )

    invoice: Mapped[Invoice] = relationship("Invoice", back_populates="lines")
