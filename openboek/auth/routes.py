"""Authentication routes — login, register, logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openboek.auth.models import User
from openboek.auth.utils import (
    SESSION_COOKIE,
    create_session_token,
    hash_password,
    verify_password,
)
from openboek.db import get_session
from openboek.audit.service import log_action

router = APIRouter(tags=["auth"])


def _templates():
    from openboek.main import templates
    return templates


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Show login form."""
    return _templates().TemplateResponse(request, "auth/login.html", {"error": None,
        "lang": getattr(request.state, "lang", "nl"),
    })


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Authenticate user and set session cookie."""
    result = await session.execute(
        select(User).where(User.username == username)
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, password):
        return _templates().TemplateResponse(request, "auth/login.html", {"error": "Invalid username or password",
            "lang": "nl",
        }, status_code=401)
    if not user.is_active:
        return _templates().TemplateResponse(request, "auth/login.html", {"error": "Account is disabled",
            "lang": "nl",
        }, status_code=403)

    await log_action(
        session, action="user.login", user_id=user.id,
        ip_address=request.client.host if request.client else None,
    )

    token = create_session_token(str(user.id))
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=28800,
    )
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Redirect to setup wizard (replaces old registration form)."""
    return RedirectResponse(url="/wizard", status_code=303)


@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Create a new user account."""
    # Only allow registration if no users exist (first run)
    count = await session.execute(select(func.count(User.id)))
    user_count = count.scalar() or 0
    if user_count > 0 and not getattr(request.state, "user_id", None):
        return RedirectResponse(url="/login", status_code=303)

    error = None
    if password != password_confirm:
        error = "Passwords do not match"
    elif len(password) < 8:
        error = "Password must be at least 8 characters"
    elif len(username) < 3:
        error = "Username must be at least 3 characters"

    if not error:
        existing = await session.execute(
            select(User).where(User.username == username)
        )
        if existing.scalar_one_or_none():
            error = "Username already taken"

    if error:
        return _templates().TemplateResponse(request, "auth/register.html", {"error": error,
            "first_run": user_count == 0,
            "lang": "nl",
        }, status_code=400)

    user = User(
        username=username,
        email=email or None,
        password_hash=hash_password(password),
    )
    session.add(user)
    await session.flush()

    await log_action(
        session, action="user.register", user_id=user.id,
        ip_address=request.client.host if request.client else None,
        after_data={"username": username, "email": email},
    )

    token = create_session_token(str(user.id))
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=28800,
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
