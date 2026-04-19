"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text

from openboek import __version__
from openboek.auth.utils import SessionMiddleware
from starlette.middleware.sessions import SessionMiddleware as StarletteSessionMiddleware
from openboek.config import settings
from openboek.db import engine
from openboek.i18n.utils import t

BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Lifespan — create engine on startup, dispose on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))

    # Start background task worker
    from openboek.tasks.worker import run_worker
    worker_task = asyncio.create_task(run_worker(), name="task-worker")
    logging.getLogger("openboek.tasks").info("Background task worker started")

    yield

    # Stop worker
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    logging.getLogger("openboek.tasks").info("Background task worker stopped")
    await engine.dispose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OpenBoek",
    version=__version__,
    description="Self-hosted bookkeeping for Dutch small businesses",
    lifespan=lifespan,
)

# Session middleware
app.add_middleware(SessionMiddleware)
app.add_middleware(StarletteSessionMiddleware, secret_key=settings.secret_key)

# Static files
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Jinja2 templates
templates_dir = BASE_DIR / "templates"
templates_dir.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.globals["t"] = t
templates.env.globals["_"] = t


# ---------------------------------------------------------------------------
# Import and register routers
# ---------------------------------------------------------------------------

from openboek.auth.routes import router as auth_router
from openboek.dashboard.routes import router as dashboard_router
from openboek.entities.routes import router as entities_router
from openboek.accounting.routes import router as accounting_router
from openboek.invoices.routes import router as invoices_router
from openboek.banking.routes import router as banking_router
from openboek.reports.routes import router as reports_router
from openboek.audit.routes import router as audit_router
from openboek.ai.routes import router as ai_router
from openboek.tax.routes import router as tax_router
from openboek.verification.routes import router as verification_router
from openboek.scanner.routes import router as scanner_router
from openboek.wizard.routes import router as wizard_router
from openboek.tasks.routes import router as tasks_router

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(entities_router)
app.include_router(accounting_router)
app.include_router(invoices_router)
app.include_router(banking_router)
app.include_router(reports_router)
app.include_router(audit_router)
app.include_router(ai_router)
app.include_router(tax_router)
app.include_router(verification_router)
app.include_router(scanner_router)
app.include_router(wizard_router)
app.include_router(tasks_router)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------

@app.get("/")
async def root(request: Request):
    """Redirect to dashboard if logged in, or to login/register."""
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return RedirectResponse(url="/dashboard", status_code=303)

    # Check if any users exist — if not, go to register (first run)
    from openboek.auth.models import User
    from openboek.db import async_session_factory

    async with async_session_factory() as session:
        count = await session.execute(select(func.count(User.id)))
        user_count = count.scalar() or 0

    if user_count == 0:
        return RedirectResponse(url="/wizard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)
