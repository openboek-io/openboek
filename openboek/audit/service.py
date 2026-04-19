"""Audit logging service — records all state changes."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from openboek.audit.models import AuditLog


async def log_action(
    session: AsyncSession,
    *,
    action: str,
    user_id: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
    table_name: str | None = None,
    record_id: str | None = None,
    before_data: dict[str, Any] | None = None,
    after_data: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Insert an audit log entry."""
    entry = AuditLog(
        user_id=user_id,
        entity_id=entity_id,
        action=action,
        table_name=table_name,
        record_id=str(record_id) if record_id else None,
        before_data=before_data,
        after_data=after_data,
        ip_address=ip_address,
    )
    session.add(entry)
    await session.flush()
    return entry
