"""Password hashing, session tokens, and session middleware."""

from __future__ import annotations

import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from openboek.config import settings

# ---------------------------------------------------------------------------
# Argon2 password hashing
# ---------------------------------------------------------------------------

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def hash_password(password: str) -> str:
    """Return an argon2id hash of *password*."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Return True if *password* matches *password_hash*."""
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------

_serializer = URLSafeTimedSerializer(settings.secret_key)
SESSION_MAX_AGE = 28800  # 8 hours


def create_session_token(user_id: str) -> str:
    """Create a signed session token containing the user id."""
    return _serializer.dumps(user_id, salt="session")


def decode_session_token(token: str) -> str | None:
    """Decode and verify a session token. Returns user_id or None."""
    try:
        return _serializer.loads(token, salt="session", max_age=SESSION_MAX_AGE)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Session middleware
# ---------------------------------------------------------------------------

SESSION_COOKIE = "openboek_session"


class SessionMiddleware(BaseHTTPMiddleware):
    """Reads the session cookie and attaches ``request.state.user_id``."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        user_id: str | None = None
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            user_id = decode_session_token(token)
        request.state.user_id = user_id
        response = await call_next(request)
        return response
