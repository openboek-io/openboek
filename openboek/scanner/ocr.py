"""OCR service — extract receipt data using Ollama vision model (minicpm-v:8b)."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from openboek.config import settings

logger = logging.getLogger(__name__)

OCR_PROMPT = """Analyze this receipt/invoice image. Extract and return as JSON only (no markdown, no explanation):
{
  "vendor": "name",
  "date": "YYYY-MM-DD",
  "total_incl": 0.00,
  "total_excl": 0.00,
  "btw_amount": 0.00,
  "btw_rate": 21,
  "currency": "EUR",
  "line_items": [{"description": "", "amount": 0.00}],
  "category_hint": "office|travel|food|telecom|insurance|other"
}
If you cannot determine a field, use null. Always return valid JSON."""


async def ocr_receipt(image_path: str | Path) -> dict[str, Any]:
    """Send an image to Ollama minicpm-v:8b for OCR extraction.

    Returns parsed JSON dict with receipt data, or error dict.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        return {"error": "Image file not found"}

    # Read and base64-encode the image
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            payload = {
                "model": "minicpm-v:8b",
                "prompt": OCR_PROMPT,
                "images": [image_b64],
                "stream": False,
            }
            resp = await client.post(
                f"{settings.ollama_url}/api/generate",
                json=payload,
                timeout=120,
            )
            if resp.status_code != 200:
                logger.error("Ollama OCR returned %d: %s", resp.status_code, resp.text[:200])
                return {"error": f"OCR service returned status {resp.status_code}"}

            data = resp.json()
            raw_text = data.get("response", "")

            # Try to parse JSON from the response
            return _parse_ocr_response(raw_text)

    except httpx.ConnectError:
        logger.error("Cannot connect to Ollama for OCR")
        return {"error": "OCR service unavailable (Ollama offline)"}
    except httpx.TimeoutException:
        logger.error("Ollama OCR timed out")
        return {"error": "OCR timed out — image may be too complex"}
    except Exception as e:
        logger.exception("OCR error")
        return {"error": str(e)}


def _parse_ocr_response(raw_text: str) -> dict[str, Any]:
    """Extract JSON from OCR response text, handling markdown fences etc."""
    text = raw_text.strip()

    # Remove markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try direct JSON parse
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return _normalize_ocr_result(result)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            if isinstance(result, dict):
                return _normalize_ocr_result(result)
        except json.JSONDecodeError:
            pass

    # Return raw text as fallback
    return {"error": "Could not parse OCR result", "raw_text": raw_text}


def _normalize_ocr_result(data: dict) -> dict:
    """Normalize and validate OCR result fields."""
    result = {
        "vendor": data.get("vendor"),
        "date": data.get("date"),
        "total_incl": _to_float(data.get("total_incl")),
        "total_excl": _to_float(data.get("total_excl")),
        "btw_amount": _to_float(data.get("btw_amount")),
        "btw_rate": _to_float(data.get("btw_rate", 21)),
        "currency": data.get("currency", "EUR"),
        "line_items": data.get("line_items", []),
        "category_hint": data.get("category_hint", "other"),
    }

    # Auto-calculate missing fields
    if result["total_incl"] and result["total_excl"] and not result["btw_amount"]:
        result["btw_amount"] = round(result["total_incl"] - result["total_excl"], 2)
    elif result["total_incl"] and result["btw_amount"] and not result["total_excl"]:
        result["total_excl"] = round(result["total_incl"] - result["btw_amount"], 2)
    elif result["total_excl"] and result["btw_rate"] and not result["total_incl"]:
        btw = round(result["total_excl"] * result["btw_rate"] / 100, 2)
        result["btw_amount"] = btw
        result["total_incl"] = round(result["total_excl"] + btw, 2)

    return result


def _to_float(val: Any) -> float | None:
    """Safely convert to float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
