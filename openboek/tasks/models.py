"""SQLAlchemy model for the tasks table."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from openboek.db import Base


class Task(Base):
    """Background task queue entry."""

    __tablename__ = "tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))
    task_type = Column(String, nullable=False, index=True)
    payload = Column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    status = Column(String, default="pending", server_default=text("'pending'"), index=True)
    priority = Column(Integer, default=0, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=text("now()"))
    scheduled_for = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=text("now()"))
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, server_default=text("0"))
    max_retries = Column(Integer, default=3, server_default=text("3"))

    def __repr__(self) -> str:
        return f"<Task {self.id} type={self.task_type} status={self.status}>"
