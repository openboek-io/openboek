"""Verification routes — three-layer verification UI."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.audit.service import log_action
from openboek.db import get_session

router = APIRouter(tags=["verification"])


def _templates():
    from openboek.main import templates
    return templates


@router.get("/entities/{entity_id}/verification", response_class=HTMLResponse)
async def verification_page(
    request: Request,
    entity_id: uuid.UUID,
    period_type: str = Query("btw_q"),
    year: int | None = Query(None),
    quarter: int | None = Query(None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show the triple verification review page."""
    entity = await get_entity_for_user(entity_id, user, session)

    today = date.today()
    year = year or today.year
    quarter = quarter or ((today.month - 1) // 3 + 1)

    # Layer 1: Automated checks
    from openboek.verification.checks import run_automated_checks
    automated = await run_automated_checks(session, entity_id, period_type, year, quarter)

    # Layer 2: AI review
    from openboek.verification.ai_review import run_ai_review
    ai_review = await run_ai_review(session, entity_id, period_type, year, quarter, user.preferred_lang)

    # Check for existing sign-off
    signoff = None
    try:
        result = await session.execute(
            sa_text(
                """SELECT * FROM verification_signoffs
                   WHERE entity_id = :eid AND period_year = :year AND period_q = :q AND period_type = :pt
                   ORDER BY created_at DESC LIMIT 1"""
            ),
            {"eid": entity_id, "year": year, "q": quarter, "pt": period_type},
        )
        signoff = result.mappings().first()
    except Exception:
        pass

    return _templates().TemplateResponse(request, "verification/review.html", {
        "entity": entity,
        "user": user,
        "lang": user.preferred_lang,
        "period_type": period_type,
        "year": year,
        "quarter": quarter,
        "automated": automated,
        "ai_review": ai_review,
        "signoff": signoff,
        "can_signoff": automated.all_passed,
    })


@router.post("/entities/{entity_id}/verification/signoff")
async def verification_signoff(
    request: Request,
    entity_id: uuid.UUID,
    period_type: str = Form("btw_q"),
    year: int = Form(...),
    quarter: int = Form(1),
    notes: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Sign off on a verified return."""
    entity = await get_entity_for_user(entity_id, user, session)

    # Re-run checks to ensure they still pass
    from openboek.verification.checks import run_automated_checks
    automated = await run_automated_checks(session, entity_id, period_type, year, quarter)

    if not automated.all_passed:
        return RedirectResponse(
            url=f"/entities/{entity_id}/verification?year={year}&quarter={quarter}&error=Checks+failed",
            status_code=303,
        )

    # Create sign-off record
    signoff_id = uuid.uuid4()
    try:
        await session.execute(
            sa_text(
                """INSERT INTO verification_signoffs
                   (id, entity_id, period_type, period_year, period_q,
                    automated_checks, signoff_user_id, signoff_at, status, notes)
                   VALUES (:id, :eid, :pt, :year, :q, :checks, :uid, :ts, 'signed_off', :notes)"""
            ),
            {
                "id": signoff_id,
                "eid": entity_id,
                "pt": period_type,
                "year": year,
                "q": quarter,
                "checks": json.dumps(automated.to_dict()),
                "uid": user.id,
                "ts": datetime.now(timezone.utc),
                "notes": notes,
            },
        )
    except Exception:
        pass

    await log_action(
        session, action="verification.signoff", user_id=user.id, entity_id=entity_id,
        after_data={"period_type": period_type, "year": year, "quarter": quarter},
        ip_address=request.client.host if request.client else None,
    )

    return RedirectResponse(
        url=f"/entities/{entity_id}/verification?year={year}&quarter={quarter}&period_type={period_type}&success=Signed+off",
        status_code=303,
    )
