"""Task queue operations — enqueue, dequeue (SELECT FOR UPDATE SKIP LOCKED), complete, fail."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.tasks.models import Task


async def enqueue(
    session: AsyncSession,
    task_type: str,
    payload: dict[str, Any] | None = None,
    delay: timedelta | None = None,
    priority: int = 0,
    max_retries: int = 3,
) -> Task:
    """Add a task to the queue.

    Args:
        session: Database session (caller manages commit).
        task_type: Handler name (e.g. 'ocr_receipt', 'bank_sync').
        payload: JSON-serializable dict passed to the handler.
        delay: Optional delay before task becomes eligible.
        priority: Higher = picked first (default 0).
        max_retries: Max retry attempts on failure.

    Returns:
        The created Task instance.
    """
    now = datetime.now(timezone.utc)
    scheduled = now + delay if delay else now

    task = Task(
        task_type=task_type,
        payload=payload or {},
        status="pending",
        priority=priority,
        scheduled_for=scheduled,
        max_retries=max_retries,
    )
    session.add(task)
    await session.flush()
    return task


async def dequeue(session: AsyncSession) -> Task | None:
    """Atomically claim the next eligible task using SELECT FOR UPDATE SKIP LOCKED.

    Returns the claimed Task (status='running') or None if queue is empty.
    """
    now = datetime.now(timezone.utc)

    # Raw SQL for the atomic claim — SQLAlchemy Core doesn't natively support SKIP LOCKED
    result = await session.execute(
        text("""
            UPDATE tasks
            SET status = 'running', started_at = :now
            WHERE id = (
                SELECT id FROM tasks
                WHERE status = 'pending' AND scheduled_for <= :now
                ORDER BY priority DESC, scheduled_for ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
        """),
        {"now": now},
    )
    row = result.one_or_none()
    if row is None:
        return None

    # Map the row back to a Task ORM instance
    task = Task(
        id=row.id,
        task_type=row.task_type,
        payload=row.payload,
        status=row.status,
        priority=row.priority,
        created_at=row.created_at,
        scheduled_for=row.scheduled_for,
        started_at=row.started_at,
        completed_at=row.completed_at,
        error=row.error,
        retry_count=row.retry_count,
        max_retries=row.max_retries,
    )
    return task


async def complete(session: AsyncSession, task_id: uuid.UUID) -> None:
    """Mark a task as completed."""
    await session.execute(
        text(
            "UPDATE tasks SET status = 'completed', completed_at = :now WHERE id = :id"
        ),
        {"now": datetime.now(timezone.utc), "id": task_id},
    )


async def fail(
    session: AsyncSession,
    task_id: uuid.UUID,
    error: str,
    retry_count: int = 0,
    max_retries: int = 3,
) -> None:
    """Mark a task as failed. If retries remain, re-queue with exponential backoff."""
    now = datetime.now(timezone.utc)
    new_retry = retry_count + 1

    if new_retry < max_retries:
        # Exponential backoff: 30s, 120s, 480s, ...
        delay_seconds = 30 * (2 ** retry_count)
        scheduled = now + timedelta(seconds=delay_seconds)
        await session.execute(
            text(
                "UPDATE tasks SET status = 'pending', error = :error, "
                "retry_count = :retry, scheduled_for = :scheduled, "
                "started_at = NULL WHERE id = :id"
            ),
            {
                "error": error,
                "retry": new_retry,
                "scheduled": scheduled,
                "id": task_id,
            },
        )
    else:
        # Max retries exhausted — dead letter
        await session.execute(
            text(
                "UPDATE tasks SET status = 'failed', error = :error, "
                "retry_count = :retry, completed_at = :now WHERE id = :id"
            ),
            {"error": error, "retry": new_retry, "now": now, "id": task_id},
        )
