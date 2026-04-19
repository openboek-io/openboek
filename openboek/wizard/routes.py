"""Setup wizard routes — multi-step onboarding for new users."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.accounting.models import Account, AccountType
from openboek.auth.models import User
from openboek.auth.utils import (
    SESSION_COOKIE,
    create_session_token,
    hash_password,
    verify_password,
)
from openboek.audit.service import log_action
from openboek.db import get_session
from openboek.entities.models import (
    AccessRole,
    Entity,
    EntityAccess,
    EntityRelationship,
    EntityType,
    RelationshipType,
)

router = APIRouter(tags=["wizard"])

# Step definitions — order matters
STEPS = [
    "welcome",
    "account",
    "personal",
    "work",
    "business",
    "holding",
    "banking",
    "btw",
    "summary",
    "complete",
]


def _templates():
    from openboek.main import templates
    return templates


def _get_step_index(step: str) -> int:
    """Return the 0-based index of a step, or 0 if not found."""
    try:
        return STEPS.index(step)
    except ValueError:
        return 0


def _load_wizard_yaml() -> dict:
    """Load the NL wizard YAML for question content."""
    path = Path(__file__).resolve().parent.parent.parent / "tax_modules" / "nl" / "wizard.yaml"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _get_lang(request: Request) -> str:
    """Get language from wizard session or default."""
    wizard = request.session.get("wizard", {}) if hasattr(request, "session") else {}
    return wizard.get("language", "nl")


def _should_skip_step(step: str, wizard_data: dict) -> str | None:
    """Check if a step should be skipped based on wizard state. Returns next step or None."""
    work = wizard_data.get("work_situation", [])
    if isinstance(work, str):
        work = [work]

    if step == "business":
        # Skip if no ZZP or BV selected
        has_zzp = "zzp" in work
        has_bv = "bv" in work
        if not has_zzp and not has_bv:
            return "banking"

    if step == "holding":
        # Skip if no BV selected
        has_bv = "bv" in work
        if not has_bv:
            return "banking"

    if step == "btw":
        # Skip if only employed/no business
        has_zzp = "zzp" in work
        has_bv = "bv" in work
        if not has_zzp and not has_bv:
            return "summary"

    return None


def _build_context(request: Request, step: str, **extra: Any) -> dict:
    """Build template context for a wizard step."""
    wizard_data = request.session.get("wizard", {}) if hasattr(request, "session") else {}
    lang = wizard_data.get("language", "nl")
    step_idx = _get_step_index(step)

    # Calculate effective steps (skip hidden ones)
    visible_steps = _get_visible_steps(wizard_data)
    try:
        progress_idx = visible_steps.index(step)
    except ValueError:
        progress_idx = step_idx
    total_visible = len(visible_steps)

    return {
        "request": request,
        "step": step,
        "step_index": step_idx,
        "progress": progress_idx,
        "total_steps": total_visible,
        "progress_pct": int(((progress_idx) / max(total_visible - 1, 1)) * 100),
        "wizard": wizard_data,
        "lang": lang,
        "steps": STEPS,
        "visible_steps": visible_steps,
        **extra,
    }


def _get_visible_steps(wizard_data: dict) -> list[str]:
    """Return list of steps that are visible given current wizard state."""
    visible = ["welcome", "account", "personal", "work"]
    work = wizard_data.get("work_situation", [])
    if isinstance(work, str):
        work = [work]

    has_zzp = "zzp" in work
    has_bv = "bv" in work

    if has_zzp or has_bv:
        visible.append("business")
    if has_bv:
        visible.append("holding")
    visible.append("banking")
    if has_zzp or has_bv:
        visible.append("btw")
    visible.extend(["summary", "complete"])
    return visible


# ---------------------------------------------------------------------------
# Starlette session middleware integration
# We store wizard data in request.session["wizard"]
# The main app already has SessionMiddleware, but we need Starlette's session
# for dict storage. We use signed cookies via itsdangerous.
# Actually, we'll store wizard state in a simple dict-based approach using
# the existing cookie mechanism + server-side storage.
# For simplicity: store in a global dict keyed by a wizard token cookie.
# ---------------------------------------------------------------------------

import secrets
import time

# In-memory wizard session store (lost on restart — that's fine for wizard)
_wizard_sessions: dict[str, dict] = {}
WIZARD_COOKIE = "openboek_wizard"
WIZARD_MAX_AGE = 3600  # 1 hour


def _get_wizard_session(request: Request) -> dict:
    """Get or create wizard session data."""
    token = request.cookies.get(WIZARD_COOKIE)
    if token and token in _wizard_sessions:
        data = _wizard_sessions[token]
        if time.time() - data.get("_created", 0) < WIZARD_MAX_AGE:
            return data
    return {}


def _save_wizard_session(request: Request, data: dict, response=None) -> str:
    """Save wizard session data and return the token."""
    token = request.cookies.get(WIZARD_COOKIE)
    if not token or token not in _wizard_sessions:
        token = secrets.token_urlsafe(32)
        data["_created"] = time.time()
    _wizard_sessions[token] = data
    return token


def _set_wizard_cookie(response, token: str):
    """Set the wizard cookie on a response."""
    response.set_cookie(
        WIZARD_COOKIE, token, httponly=True, samesite="lax", max_age=WIZARD_MAX_AGE,
    )


def _clear_wizard_session(request: Request, response):
    """Remove wizard session."""
    token = request.cookies.get(WIZARD_COOKIE)
    if token and token in _wizard_sessions:
        del _wizard_sessions[token]
    response.delete_cookie(WIZARD_COOKIE)


# Monkey-patch request to have .session-like access for templates
class _WizardRequest:
    """Wrapper to inject wizard session as request.session for templates."""
    pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/wizard", response_class=HTMLResponse)
async def wizard_get(
    request: Request,
    step: str = "welcome",
    session: AsyncSession = Depends(get_session),
):
    """Show a wizard step."""
    # If user is already logged in, redirect to dashboard
    user_id = getattr(request.state, "user_id", None)
    if user_id and step != "complete":
        return RedirectResponse(url="/dashboard", status_code=303)

    wizard_data = _get_wizard_session(request)

    # Check if step should be skipped
    skip_to = _should_skip_step(step, wizard_data)
    if skip_to:
        return RedirectResponse(url=f"/wizard?step={skip_to}", status_code=303)

    ctx = _build_context(request, step, wizard=wizard_data)
    template_name = f"wizard/{step}.html"
    return _templates().TemplateResponse(ctx.get("request") or ctx["request"], template_name, {k:v for k,v in ctx.items() if k != "request"})


@router.post("/wizard", response_class=HTMLResponse)
async def wizard_post(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Process a wizard step submission."""
    form = await request.form()
    step = form.get("step", "welcome")
    wizard_data = _get_wizard_session(request)

    # Process each step's form data
    next_step = None

    if step == "welcome":
        wizard_data["language"] = form.get("language", "nl")
        next_step = "account"

    elif step == "account":
        # Validate account creation fields
        username = form.get("username", "").strip()
        email = form.get("email", "").strip()
        password = form.get("password", "")
        password_confirm = form.get("password_confirm", "")

        error = None
        if len(username) < 3:
            error = "Username must be at least 3 characters" if wizard_data.get("language") == "en" else "Gebruikersnaam moet minimaal 3 tekens zijn"
        elif len(password) < 8:
            error = "Password must be at least 8 characters" if wizard_data.get("language") == "en" else "Wachtwoord moet minimaal 8 tekens zijn"
        elif password != password_confirm:
            error = "Passwords do not match" if wizard_data.get("language") == "en" else "Wachtwoorden komen niet overeen"

        if not error:
            existing = await session.execute(
                select(User).where(User.username == username)
            )
            if existing.scalar_one_or_none():
                error = "Username already taken" if wizard_data.get("language") == "en" else "Gebruikersnaam al in gebruik"

        if error:
            ctx = _build_context(request, "account", wizard=wizard_data, error=error)
            response = _templates().TemplateResponse(ctx["request"], "wizard/account.html", {k:v for k,v in ctx.items() if k != "request"}); response.status_code = 400
            token = _save_wizard_session(request, wizard_data)
            _set_wizard_cookie(response, token)
            return response

        wizard_data["username"] = username
        wizard_data["email"] = email
        wizard_data["password"] = password  # Stored temporarily in memory only
        next_step = "personal"

    elif step == "personal":
        wizard_data["display_name"] = form.get("display_name", "").strip()
        wizard_data["has_partner"] = form.get("has_partner") == "yes"
        wizard_data["partner_name"] = form.get("partner_name", "").strip() if wizard_data["has_partner"] else ""
        next_step = "work"

    elif step == "work":
        work_situation = form.getlist("work_situation")
        wizard_data["work_situation"] = work_situation

        partner_work = form.getlist("partner_work_situation")
        wizard_data["partner_work_situation"] = partner_work

        # Determine next step based on work situation
        has_zzp = "zzp" in work_situation
        has_bv = "bv" in work_situation
        if has_zzp or has_bv:
            next_step = "business"
        else:
            next_step = "banking"

    elif step == "business":
        work = wizard_data.get("work_situation", [])
        has_zzp = "zzp" in work
        has_bv = "bv" in work

        if has_zzp:
            wizard_data["zzp_name"] = form.get("zzp_name", "").strip()
            wizard_data["zzp_kvk"] = form.get("zzp_kvk", "").strip()
            wizard_data["zzp_btw_number"] = form.get("zzp_btw_number", "").strip()

        if has_bv:
            wizard_data["bv_name"] = form.get("bv_name", "").strip()
            wizard_data["bv_kvk"] = form.get("bv_kvk", "").strip()
            wizard_data["bv_btw_number"] = form.get("bv_btw_number", "").strip()

        if has_bv:
            next_step = "holding"
        else:
            next_step = "banking"

    elif step == "holding":
        wizard_data["has_holding"] = form.get("has_holding") == "yes"
        wizard_data["holding_name"] = form.get("holding_name", "").strip() if wizard_data["has_holding"] else ""
        wizard_data["management_fee"] = form.get("management_fee") == "yes" if wizard_data["has_holding"] else False
        next_step = "banking"

    elif step == "banking":
        banks = form.getlist("banks")
        wizard_data["banks"] = banks
        work = wizard_data.get("work_situation", [])
        has_zzp = "zzp" in work
        has_bv = "bv" in work
        if has_zzp or has_bv:
            next_step = "btw"
        else:
            next_step = "summary"

    elif step == "btw":
        wizard_data["btw_status"] = form.get("btw_status", "standard")
        wizard_data["btw_frequency"] = form.get("btw_frequency", "quarterly")
        next_step = "summary"

    elif step == "summary":
        # User confirmed — create everything
        return await _finalize_wizard(request, wizard_data, session)

    elif step == "navigate":
        # Navigation — go to a specific step for editing
        target = form.get("target_step", "welcome")
        next_step = target

    # Save wizard state and redirect to next step
    token = _save_wizard_session(request, wizard_data)
    response = RedirectResponse(url=f"/wizard?step={next_step}", status_code=303)
    _set_wizard_cookie(response, token)
    return response


async def _finalize_wizard(
    request: Request, wizard_data: dict, session: AsyncSession
) -> RedirectResponse:
    """Create user, entities, chart of accounts, and relationships."""
    lang = wizard_data.get("language", "nl")

    # 1. Create user
    user = User(
        username=wizard_data.get("username", ""),
        email=wizard_data.get("email") or None,
        password_hash=hash_password(wizard_data.get("password", "")),
        preferred_lang=lang,
    )
    session.add(user)
    await session.flush()

    await log_action(
        session, action="user.register", user_id=user.id,
        ip_address=request.client.host if request.client else None,
        after_data={"username": user.username, "source": "wizard"},
    )

    # 2. Create entities based on wizard choices
    work = wizard_data.get("work_situation", [])
    if isinstance(work, str):
        work = [work]

    created_entities: dict[str, Entity] = {}

    # Always create personal entity if has_partner or for personal finance
    if wizard_data.get("has_partner") or "employed" in work or not work:
        personal = Entity(
            name=f"{wizard_data.get('display_name', 'Persoonlijk')} — {'Privé' if lang == 'nl' else 'Personal'}",
            entity_type=EntityType.personal,
            owner_user_id=user.id,
        )
        session.add(personal)
        await session.flush()
        created_entities["personal"] = personal
        await _provision_chart(session, personal, "personal")

    # ZZP entity
    if "zzp" in work:
        zzp_name = wizard_data.get("zzp_name") or (
            f"{wizard_data.get('display_name', 'Mijn onderneming')} ZZP"
        )
        zzp = Entity(
            name=zzp_name,
            entity_type=EntityType.zzp,
            kvk_number=wizard_data.get("zzp_kvk") or None,
            btw_number=wizard_data.get("zzp_btw_number") or None,
            owner_user_id=user.id,
        )
        session.add(zzp)
        await session.flush()
        created_entities["zzp"] = zzp
        await _provision_chart(session, zzp, "zzp")

    # BV entity
    if "bv" in work:
        bv_name = wizard_data.get("bv_name") or "Mijn BV"
        bv = Entity(
            name=bv_name,
            entity_type=EntityType.bv,
            kvk_number=wizard_data.get("bv_kvk") or None,
            btw_number=wizard_data.get("bv_btw_number") or None,
            owner_user_id=user.id,
        )
        session.add(bv)
        await session.flush()
        created_entities["bv"] = bv
        await _provision_chart(session, bv, "bv")

    # Holding entity
    if wizard_data.get("has_holding") and "bv" in work:
        holding_name = wizard_data.get("holding_name") or "Holding BV"
        holding = Entity(
            name=holding_name,
            entity_type=EntityType.holding,
            owner_user_id=user.id,
        )
        session.add(holding)
        await session.flush()
        created_entities["holding"] = holding
        await _provision_chart(session, holding, "bv")  # Holdings use BV chart

        # Create relationship: holding → operating BV
        if "bv" in created_entities:
            rel = EntityRelationship(
                parent_entity_id=holding.id,
                child_entity_id=created_entities["bv"].id,
                relationship_type=RelationshipType.holding_opco,
            )
            session.add(rel)

    # 3. Create EntityAccess entries (owner for all)
    for key, entity in created_entities.items():
        access = EntityAccess(
            user_id=user.id,
            entity_id=entity.id,
            role=AccessRole.owner,
        )
        session.add(access)

    # 4. Create fiscal partner relationship if applicable
    if wizard_data.get("has_partner") and "personal" in created_entities:
        # Note: partner entity/user would be created when they register
        # For now, we store the intent in the wizard data
        pass

    await session.flush()

    await log_action(
        session, action="wizard.complete", user_id=user.id,
        ip_address=request.client.host if request.client else None,
        after_data={
            "entities_created": list(created_entities.keys()),
            "work_situation": work,
            "has_holding": wizard_data.get("has_holding", False),
        },
    )

    # 5. Log in the user
    token = create_session_token(str(user.id))
    response = RedirectResponse(url="/wizard?step=complete", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=28800,
    )

    # Clean up wizard session
    wiz_token = request.cookies.get(WIZARD_COOKIE)
    if wiz_token and wiz_token in _wizard_sessions:
        del _wizard_sessions[wiz_token]
    response.delete_cookie(WIZARD_COOKIE)

    return response


async def _provision_chart(
    session: AsyncSession, entity: Entity, chart_type: str
) -> None:
    """Load a chart of accounts from YAML and create Account records."""
    chart_path = (
        Path(__file__).resolve().parent.parent.parent
        / "tax_modules" / "nl" / "chart_of_accounts" / f"{chart_type}.yaml"
    )
    if not chart_path.exists():
        return

    with open(chart_path, encoding="utf-8") as f:
        chart = yaml.safe_load(f) or {}

    accounts = chart.get("accounts", [])
    await _create_accounts_recursive(session, entity.id, accounts, parent_id=None)


async def _create_accounts_recursive(
    session: AsyncSession,
    entity_id: uuid.UUID,
    accounts: list[dict],
    parent_id: uuid.UUID | None,
) -> None:
    """Recursively create accounts from YAML structure."""
    type_map = {
        "asset": AccountType.asset,
        "liability": AccountType.liability,
        "equity": AccountType.equity,
        "revenue": AccountType.revenue,
        "expense": AccountType.expense,
    }

    for acct_data in accounts:
        acct_type = type_map.get(acct_data.get("type", ""), AccountType.asset)
        account = Account(
            entity_id=entity_id,
            code=acct_data.get("code", ""),
            name_nl=acct_data.get("name_nl", ""),
            name_en=acct_data.get("name_en", ""),
            account_type=acct_type,
            parent_id=parent_id,
            btw_code=acct_data.get("btw_code"),
            is_system=acct_data.get("system", False),
        )
        session.add(account)
        await session.flush()

        children = acct_data.get("children", [])
        if children:
            await _create_accounts_recursive(session, entity_id, children, account.id)
