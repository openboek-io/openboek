"""Entity, EntityRelationship, and EntityAccess models."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openboek.db import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EntityType(str, enum.Enum):
    zzp = "zzp"
    bv = "bv"
    holding = "holding"
    personal = "personal"


class RelationshipType(str, enum.Enum):
    holding_opco = "holding_opco"
    fiscal_partner = "fiscal_partner"
    shareholder = "shareholder"


class AccessRole(str, enum.Enum):
    owner = "owner"
    editor = "editor"
    viewer = "viewer"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Entity(Base):
    """A legal or personal entity (ZZP, BV, Holding, Personal)."""

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, name="entity_type_enum"), nullable=False
    )
    fiscal_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    btw_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kvk_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str] = mapped_column(String(2), default="NL")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    relationships_as_parent: Mapped[list[EntityRelationship]] = relationship(
        "EntityRelationship",
        foreign_keys="EntityRelationship.parent_entity_id",
        back_populates="parent_entity",
    )
    relationships_as_child: Mapped[list[EntityRelationship]] = relationship(
        "EntityRelationship",
        foreign_keys="EntityRelationship.child_entity_id",
        back_populates="child_entity",
    )
    access_entries: Mapped[list[EntityAccess]] = relationship(
        "EntityAccess", back_populates="entity"
    )


class EntityRelationship(Base):
    """Typed relationship between two entities."""

    __tablename__ = "entity_relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parent_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False
    )
    child_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id"), nullable=False
    )
    relationship_type: Mapped[RelationshipType] = mapped_column(
        Enum(RelationshipType, name="relationship_type_enum"), nullable=False
    )
    share_percentage: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    parent_entity: Mapped[Entity] = relationship(
        "Entity", foreign_keys=[parent_entity_id], back_populates="relationships_as_parent"
    )
    child_entity: Mapped[Entity] = relationship(
        "Entity", foreign_keys=[child_entity_id], back_populates="relationships_as_child"
    )


class EntityAccess(Base):
    """User access level for an entity."""

    __tablename__ = "entity_access"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id"), primary_key=True
    )
    role: Mapped[AccessRole] = mapped_column(
        Enum(AccessRole, name="access_role_enum"), nullable=False
    )

    entity: Mapped[Entity] = relationship("Entity", back_populates="access_entries")
