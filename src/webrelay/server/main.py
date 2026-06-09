"""FastAPI app entry point + ASGI runner.

This module is the only file in the server package allowed to import
*every* router and wire them into a single FastAPI application. The
wiring contract:

* A single ``RelayHub`` instance is created at module import time and
  attached to ``app.state.hub`` so the routers can find it via
  ``request.app.state.hub``.
* A Jinja2 :class:`Environment` is created (with a FileSystemLoader
  pointed at ``server/templates/``) and stashed on ``app.state.jinja_env``
  AND ``app.state.templates`` (the file-browser route uses the latter;
  the approvals route uses the former).
* ``SessionMiddleware`` is mounted with a secret pulled from
  ``WEBRELAY_SESSION_SECRET`` so the auth layer can stash
  ``request.session["sid"]``.
* ``StaticFiles`` is mounted at ``/static`` so the base template can
  pull ``/static/app.css`` and ``/static/manifest.json``.
* A root route redirects ``GET /`` to ``/chat`` so the user's first
  tap lands on the chat tab.
* An app-level middleware short-circuits unauthenticated requests for
  non-auth routes to ``/auth/login``.

Missing routers (chat / ledgers / relay / auth) are imported lazily and
silently skipped if the corresponding module is absent. This lets the
``main`` module import cleanly while upstream agents are still building
those route modules in parallel.

Run with::

    python -m webrelay.server.main

or via the ``webrelay-server`` console script declared in ``pyproject.toml``.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from jinja2 import ChoiceLoader, Environment, FileSystemLoader
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from starlette.responses import RedirectResponse as _RedirectResponse  # noqa: F401

from webrelay.server import db
from webrelay.server.relay_hub import RelayHub

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_STATIC_DIR = _HERE / "static"
_TEMPLATES_DIR = _HERE / "templates"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

# One hub per process. Route handlers reach for it via
# ``request.app.state.hub``; the lifespan below stashes it on ``app.state``
# but we also keep a module-level handle so tests can swap it out.
hub: RelayHub = RelayHub()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: bring up the DB and stash the hub on app state.

    The order is intentional — we want the DB ready before the first
    request hits a route that depends on it (approvals, ledgers, chat
    history). The hub is created at import time so it is reachable even
    before this hook runs (handy for tests).
    """
    await db.on_startup()
    app.state.hub = hub
    app.state.db_session_factory = db.get_session_maker()
    _log.info(
        "webrelay server starting: hub=ready db=%s templates=%s static=%s",
        app.state.db_session_factory,
        _TEMPLATES_DIR,
        _STATIC_DIR,
    )
    # Try spawning background ledger watcher
    import asyncio
    watcher_task = None
    try:
        from webrelay.server.routes.ledgers import register_ledger_watcher
        watcher_task = asyncio.create_task(register_ledger_watcher(app))
    except Exception as exc:
        _log.info("Skipping ledger watcher registration: %s", exc)

    try:
        yield
    finally:
        if watcher_task:
            watcher_task.cancel()
            try:
                await watcher_task
            except (asyncio.CancelledError, Exception):
                pass
        # Best-effort: fail any in-flight requests so connected SSE
        # consumers unblock cleanly during shutdown.
        try:
            await hub.detach(hub._websocket)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - shutdown is best-effort
            pass


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Kept as a factory (rather than constructed at import time) so tests
    can call it with a different DB path or a stub hub without
    monkey-patching module globals.
    """
    app = FastAPI(
        title="Hermes Web Relay",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    # --- Session middleware ---------------------------------------------
    # Pull the secret from env, falling back to a development default.
    # In production this MUST be set explicitly (the auth layer checks
    # for it and refuses to start otherwise).
    #
    # Order matters in Starlette: ``add_middleware`` stacks in LIFO
    # order (last added runs OUTERMOST). We want the auth gate to run
    # INSIDE the session middleware so ``request.session`` is populated
    # when the gate checks ``session.get("sid")``. We therefore add
    # the auth gate FIRST and the session middleware SECOND.
    _install_auth_gate(app)

    session_secret = os.environ.get("WEBRELAY_SESSION_SECRET") or "dev-secret-change-me"
    app.add_middleware(SessionMiddleware, secret_key=session_secret, max_age=60 * 60 * 24 * 14)

    # --- Static files ---------------------------------------------------
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # --- Templates (Jinja2) --------------------------------------------
    # The approvals route uses ``request.app.state.jinja_env`` (an
    # :class:`Environment`); the file-browser route uses
    # ``request.app.state.templates`` (a :class:`Jinja2Templates`).
    # We expose both so neither route has to be rewritten. They share
    # the same loader chain (filesystem + the dict loader for tests).
    templates_env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
    )
    app.state.jinja_env = templates_env
    # Late import to avoid pulling starlette.templating at module load
    # if it's not installed in the test environment.
    from starlette.templating import Jinja2Templates

    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # --- Routers --------------------------------------------------------
    # All routers live in ``webrelay.server.routes``. We import each one
    # independently so a missing module (still being implemented by
    # another agent) doesn't block the whole app from booting.
    _include_optional(app, "webrelay.server.routes.status", "router")
    _include_optional(app, "webrelay.server.routes.auth", "router")
    _include_optional(app, "webrelay.server.routes.chat", "router")
    _include_optional(app, "webrelay.server.routes.ledgers", "router")
    _include_optional(app, "webrelay.server.routes.files", "router")
    _include_optional(app, "webrelay.server.routes.approvals", "router")
    _include_optional(app, "webrelay.server.routes.relay", "router")

    # --- Auth gate middleware -------------------------------------------
    # Installed by ``_install_auth_gate`` below BEFORE SessionMiddleware
    # so the session is populated when the gate runs.
    # --- Root redirect --------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Redirect the bare URL to the chat tab."""
        return RedirectResponse(url="/chat", status_code=302)

    return app


def _install_auth_gate(app: FastAPI) -> None:
    """Add the auth-gate middleware to ``app``.

    Registered as a :class:`BaseHTTPMiddleware` subclass so the
    middleware ends up in ``app.user_middleware`` BEFORE the
    SessionMiddleware (which is added right after by ``create_app``).
    In Starlette's LIFO stack, that means SessionMiddleware wraps the
    auth gate and ``request.session`` is populated by the time the
    gate looks at it.

    The allow-list matches the spec: static assets, the ops liveness
    probe, the relay WS endpoint (which does its own bearer-token
    auth), and the relay status partial polled by the base template.
    """

    class _AuthGate(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any):  # type: ignore[override]
            path = request.url.path
            if (
                path.startswith("/auth/")
                or path.startswith("/static/")
                or path == "/healthz"
                or path == "/api/relay/status"
                or path == "/api/relay/ws"
            ):
                return await call_next(request)
            session = request.scope.get("session") or {}
            if session.get("sid"):
                return await call_next(request)
            target = "/auth/login"
            if path and path != "/":
                target = f"/auth/login?next={path}"
            return _RedirectResponse(url=target, status_code=302)

    app.add_middleware(_AuthGate)


def _include_optional(app: FastAPI, module_name: str, attr: str) -> None:
    """Best-effort ``app.include_router(import_module(module_name).attr)``.

    Silently skips when the module or attribute is missing — this is how
    the main app boots before every router has been written. We log at
    INFO so it's visible in startup logs but doesn't pollute WARNING.
    """
    import importlib

    try:
        mod = importlib.import_module(module_name)
        router = getattr(mod, attr, None)
    except Exception as exc:  # noqa: BLE001 - any import failure is a skip
        _log.info("skipping optional router %s: %s", module_name, exc)
        return
    if router is None:
        _log.info("skipping optional router %s (no %r attribute)", module_name, attr)
        return
    try:
        app.include_router(router)
        _log.info("mounted router: %s", module_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("failed to include router %s: %s", module_name, exc)


# ---------------------------------------------------------------------------
# Module-level app instance for uvicorn
# ---------------------------------------------------------------------------

# ``uvicorn webrelay.server.main:app`` looks up ``app`` at module scope.
app: FastAPI = create_app()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run() -> None:
    """Run the server under uvicorn on 0.0.0.0:8000.

    ``proxy_headers=True`` makes uvicorn honour ``X-Forwarded-Proto``
    / ``X-Forwarded-For`` from the Coolify reverse proxy so
    ``request.url.scheme`` is ``"https"`` when it should be.
    """
    import uvicorn

    uvicorn.run(
        "webrelay.server.main:app",
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


__all__ = ["app", "create_app", "hub", "lifespan", "run"]


# Touch ``ChoiceLoader`` so an import path stays available to downstream
# tests that build their own Jinja env with a dict loader fallback.
_ = ChoiceLoader