"""Admin routes for task queue monitoring."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.auth.dependencies import get_current_user
from openboek.auth.models import User
from openboek.db import get_session

router = APIRouter(prefix="/admin/tasks", tags=["tasks"])


def _templates():
    from openboek.main import templates
    return templates


@router.get("", response_class=HTMLResponse)
async def task_list(
    request: Request,
    status: str | None = Query(None),
    task_type: str | None = Query(None),
    limit: int = Query(50, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show task queue dashboard with filtering."""
    # Build query
    where_clauses = []
    params: dict = {}
    if status:
        where_clauses.append("status = :status")
        params["status"] = status
    if task_type:
        where_clauses.append("task_type = :task_type")
        params["task_type"] = task_type

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    params["limit"] = limit

    # Get tasks
    result = await session.execute(
        text(f"SELECT * FROM tasks WHERE {where_sql} ORDER BY created_at DESC LIMIT :limit"),
        params,
    )
    tasks = result.mappings().all()

    # Get status counts
    counts_result = await session.execute(
        text("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status ORDER BY status")
    )
    status_counts = {row.status: row.cnt for row in counts_result}

    # Get task type counts
    types_result = await session.execute(
        text("SELECT task_type, COUNT(*) as cnt FROM tasks GROUP BY task_type ORDER BY task_type")
    )
    type_counts = {row.task_type: row.cnt for row in types_result}

    return _templates().TemplateResponse(request, "tasks/list.html", {
        "user": user,
        "tasks": tasks,
        "status_counts": status_counts,
        "type_counts": type_counts,
        "current_status": status,
        "current_type": task_type,
        "lang": getattr(user, "preferred_lang", "nl"),
    })


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Cancel a pending task."""
    result = await session.execute(
        text("UPDATE tasks SET status = 'cancelled', completed_at = :now WHERE id = :id AND status = 'pending'"),
        {"now": datetime.now(timezone.utc), "id": task_id},
    )
    return {"ok": True, "cancelled": result.rowcount > 0}


@router.post("/{task_id}/retry")
async def retry_task(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Retry a failed task."""
    result = await session.execute(
        text(
            "UPDATE tasks SET status = 'pending', error = NULL, "
            "started_at = NULL, completed_at = NULL, scheduled_for = now() "
            "WHERE id = :id AND status = 'failed'"
        ),
        {"id": task_id},
    )
    return {"ok": True, "retried": result.rowcount > 0}


@router.get("/api/stats")
async def task_stats(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """JSON endpoint for task queue statistics (for HTMX polling)."""
    counts_result = await session.execute(
        text("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status")
    )
    status_counts = {row.status: row.cnt for row in counts_result}

    running_result = await session.execute(
        text("SELECT id, task_type, started_at FROM tasks WHERE status = 'running' ORDER BY started_at")
    )
    running = [
        {"id": str(r.id), "task_type": r.task_type, "started_at": r.started_at.isoformat() if r.started_at else None}
        for r in running_result
    ]

    return {
        "counts": status_counts,
        "running": running,
    }
