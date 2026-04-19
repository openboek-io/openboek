"""Tax optimization routes — fiscal partnership optimizer."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.auth.dependencies import get_current_user, get_entity_for_user
from openboek.auth.models import User
from openboek.db import get_session

router = APIRouter(tags=["tax"])


def _templates():
    from openboek.main import templates
    return templates


@router.get("/entities/{entity_id}/tax/optimizer", response_class=HTMLResponse)
async def tax_optimizer_page(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Show the fiscal partnership optimizer form."""
    entity = await get_entity_for_user(entity_id, user, session)

    return _templates().TemplateResponse(request, "tax/optimizer.html", {
        "entity": entity,
        "user": user,
        "lang": user.preferred_lang,
        "result": None,
    })


@router.post("/entities/{entity_id}/tax/optimizer", response_class=HTMLResponse)
async def tax_optimizer_calculate(
    request: Request,
    entity_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Run the fiscal partnership optimization."""
    entity = await get_entity_for_user(entity_id, user, session)
    form = await request.form()

    def _dec(key: str, default: str = "0") -> Decimal:
        try:
            return Decimal(form.get(key, default) or default)
        except InvalidOperation:
            return Decimal(default)

    from openboek.tax.fiscal_partner import (
        PartnerInput,
        SharedDeductions,
        optimize,
    )

    partner_a = PartnerInput(
        name=form.get("partner_a_name", "Partner A"),
        box1_income=_dec("partner_a_income"),
        box2_income=_dec("partner_a_box2"),
        box3_vermogen=_dec("partner_a_box3"),
    )

    partner_b = PartnerInput(
        name=form.get("partner_b_name", "Partner B"),
        box1_income=_dec("partner_b_income"),
        box2_income=_dec("partner_b_box2"),
        box3_vermogen=_dec("partner_b_box3"),
    )

    shared = SharedDeductions(
        hypotheekrenteaftrek=_dec("hypotheekrenteaftrek"),
        eigenwoningforfait=_dec("eigenwoningforfait"),
        woz_waarde=_dec("woz_waarde"),
        giften=_dec("giften"),
        zorgkosten=_dec("zorgkosten"),
        studiekosten=_dec("studiekosten"),
    )

    # Auto-calculate eigenwoningforfait from WOZ if not provided
    if shared.woz_waarde > 0 and shared.eigenwoningforfait == 0:
        from openboek.tax.fiscal_partner import EIGENWONINGFORFAIT_RATE
        shared.eigenwoningforfait = (shared.woz_waarde * EIGENWONINGFORFAIT_RATE).quantize(Decimal("0.01"))

    result = optimize(partner_a, partner_b, shared)

    return _templates().TemplateResponse(request, "tax/optimizer.html", {
        "entity": entity,
        "user": user,
        "lang": user.preferred_lang,
        "result": result,
        "partner_a": partner_a,
        "partner_b": partner_b,
        "shared": shared,
    })
