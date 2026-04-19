"""Task handler registry — maps task_type strings to async handler functions."""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Type alias for handler functions
HandlerFunc = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

# Registry: task_type -> handler function
_handlers: dict[str, HandlerFunc] = {}


def register(task_type: str):
    """Decorator to register a task handler."""
    def decorator(fn: HandlerFunc) -> HandlerFunc:
        _handlers[task_type] = fn
        logger.info("Registered task handler: %s", task_type)
        return fn
    return decorator


def get_handler(task_type: str) -> HandlerFunc | None:
    """Look up a handler by task type."""
    return _handlers.get(task_type)


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------

@register("ocr_receipt")
async def handle_ocr_receipt(payload: dict[str, Any]) -> None:
    """Process a receipt image with OCR.

    Expected payload:
        - file_path: str — path to the receipt image
        - file_id: str — UUID of the receipt_files record
        - entity_id: str — entity UUID
    """
    import json
    from pathlib import Path
    from sqlalchemy import text

    from openboek.db import async_session_factory
    from openboek.scanner.ocr import ocr_receipt

    file_path = payload.get("file_path")
    file_id = payload.get("file_id")
    if not file_path or not file_id:
        raise ValueError("ocr_receipt requires file_path and file_id in payload")

    logger.info("Running OCR on %s (file_id=%s)", file_path, file_id)
    ocr_result = await ocr_receipt(Path(file_path))
    ocr_error = ocr_result.get("error")
    status = "failed" if ocr_error else "done"

    async with async_session_factory() as session:
        await session.execute(
            text("UPDATE receipt_files SET ocr_status = :status, ocr_result = :result WHERE id = :id"),
            {"status": status, "result": json.dumps(ocr_result), "id": file_id},
        )
        await session.commit()

    if ocr_error:
        raise RuntimeError(f"OCR failed: {ocr_error}")

    logger.info("OCR complete for file_id=%s: vendor=%s", file_id, ocr_result.get("vendor"))


@register("bank_sync")
async def handle_bank_sync(payload: dict[str, Any]) -> None:
    """Sync bank transactions via GoCardless.

    Expected payload:
        - entity_id: str — entity UUID
        - connection_id: str — GoCardless connection UUID
    """
    import uuid

    from openboek.banking.sync import sync_gocardless_transactions
    from openboek.db import async_session_factory

    entity_id = payload.get("entity_id")
    connection_id = payload.get("connection_id")
    if not entity_id or not connection_id:
        raise ValueError("bank_sync requires entity_id and connection_id")

    logger.info("Syncing bank transactions for entity=%s connection=%s", entity_id, connection_id)

    async with async_session_factory() as session:
        result = await sync_gocardless_transactions(
            session, uuid.UUID(entity_id), uuid.UUID(connection_id)
        )
        await session.commit()

    logger.info(
        "Bank sync complete: imported=%d skipped=%d",
        result.get("imported", 0),
        result.get("skipped", 0),
    )


@register("ecb_rates")
async def handle_ecb_rates(payload: dict[str, Any]) -> None:
    """Fetch latest ECB exchange rates.

    Expected payload: (none required)
    """
    import httpx
    import xml.etree.ElementTree as ET
    from sqlalchemy import text

    from openboek.db import async_session_factory

    logger.info("Fetching ECB exchange rates")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
        )
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    ns = {"gesmes": "http://www.gesmes.org/xml/2002-08-01", "eurofxref": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    cube = root.find(".//eurofxref:Cube/eurofxref:Cube", ns)
    if cube is None:
        raise RuntimeError("Could not parse ECB rates XML")

    rate_date = cube.attrib.get("time")
    rates = {}
    for child in cube:
        currency = child.attrib.get("currency")
        rate = child.attrib.get("rate")
        if currency and rate:
            rates[currency] = float(rate)

    logger.info("ECB rates for %s: %d currencies", rate_date, len(rates))
    # Store rates — simple upsert into a rates table or log
    # For now just log; actual persistence depends on schema


@register("ai_insights")
async def handle_ai_insights(payload: dict[str, Any]) -> None:
    """Generate AI-powered financial insights for an entity.

    Expected payload:
        - entity_id: str — entity UUID
        - user_id: str — requesting user UUID (optional)
    """
    import uuid

    from openboek.ai.advisor import run_advisor
    from openboek.db import async_session_factory

    entity_id = payload.get("entity_id")
    if not entity_id:
        raise ValueError("ai_insights requires entity_id")

    user_id = payload.get("user_id")

    logger.info("Generating AI insights for entity=%s", entity_id)

    async with async_session_factory() as session:
        insights = await run_advisor(
            session,
            uuid.UUID(entity_id),
            user_id=uuid.UUID(user_id) if user_id else None,
        )
        await session.commit()

    logger.info("Generated %d insights for entity=%s", len(insights), entity_id)
