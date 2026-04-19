"""Async task worker — polls PostgreSQL for pending tasks and dispatches to handlers."""

from __future__ import annotations

import asyncio
import logging
import traceback

from openboek.db import async_session_factory
from openboek.tasks import handlers  # noqa: F401 — triggers @register decorators
from openboek.tasks.handlers import get_handler
from openboek.tasks.queue import complete, dequeue, fail

logger = logging.getLogger(__name__)

# Poll interval when idle (seconds)
POLL_INTERVAL = 2.0
# Poll interval after processing a task (faster to drain bursts)
BURST_INTERVAL = 0.1
# Max consecutive errors before backing off
MAX_CONSECUTIVE_ERRORS = 5
ERROR_BACKOFF = 30.0


async def run_worker(*, stop_event: asyncio.Event | None = None) -> None:
    """Main worker loop. Runs until stop_event is set or cancelled.

    Polls the task queue, dispatches to registered handlers, handles
    retries with exponential backoff on failure.
    """
    logger.info("Task worker started")
    consecutive_errors = 0

    while True:
        # Check for shutdown
        if stop_event and stop_event.is_set():
            logger.info("Task worker shutting down (stop event)")
            break

        try:
            async with async_session_factory() as session:
                task = await dequeue(session)
                await session.commit()

            if task is None:
                # Nothing to do — idle poll
                await asyncio.sleep(POLL_INTERVAL)
                continue

            consecutive_errors = 0  # Reset on successful dequeue
            logger.info(
                "Processing task %s (type=%s, retry=%d/%d)",
                task.id, task.task_type, task.retry_count, task.max_retries,
            )

            handler = get_handler(task.task_type)
            if handler is None:
                error_msg = f"No handler registered for task type: {task.task_type}"
                logger.error(error_msg)
                async with async_session_factory() as session:
                    await fail(session, task.id, error_msg, task.retry_count, task.max_retries)
                    await session.commit()
                await asyncio.sleep(BURST_INTERVAL)
                continue

            # Execute the handler
            try:
                await handler(task.payload)

                # Mark complete
                async with async_session_factory() as session:
                    await complete(session, task.id)
                    await session.commit()

                logger.info("Task %s completed successfully", task.id)

            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                logger.error("Task %s failed: %s", task.id, exc)

                async with async_session_factory() as session:
                    await fail(session, task.id, error_msg, task.retry_count, task.max_retries)
                    await session.commit()

            await asyncio.sleep(BURST_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Task worker cancelled")
            break
        except Exception as exc:
            consecutive_errors += 1
            logger.error(
                "Worker loop error (%d/%d): %s",
                consecutive_errors, MAX_CONSECUTIVE_ERRORS, exc,
            )
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.error("Too many consecutive errors, backing off %.0fs", ERROR_BACKOFF)
                await asyncio.sleep(ERROR_BACKOFF)
                consecutive_errors = 0
            else:
                await asyncio.sleep(POLL_INTERVAL)

    logger.info("Task worker stopped")
