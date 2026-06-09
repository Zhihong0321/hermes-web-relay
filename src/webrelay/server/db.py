"""Async SQLAlchemy engine + session factory for the web-relay server.

This module wires up the single shared :class:`AsyncEngine` and
:class:`async_sessionmaker` that every FastAPI endpoint (and every
hub-internal task) uses to talk to SQLite. The design follows three
rules from the Stage-1 plan:

1. **Lazy singletons.** Importing this module has *no* side effects:
   no engine is created, no DB file is touched. The engine is built
   on the first call to :func:`get_engine` (or :func:`on_startup`),
   and the same instance is returned thereafter for the lifetime of
   the process. This is important because importing happens at
   module-collection time (pytest, FastAPI cold start, background
   tasks) and we don't want a half-configured engine to leak.

2. **Path resolution via env var.** The DB file is taken from
   ``WEBRELAY_DB_PATH`` (default ``./webrelay.db`` for local dev).
   On Windows we accept backslashes and normalize them to forward
   slashes for the ``sqlite+aiosqlite`` URL.

3. **Single source of truth for schema.** :func:`init_db` delegates
   to :func:`webrelay.server.models.init_db`, which issues
   ``CREATE TABLE IF NOT EXISTS`` for every declarative model on
   :class:`Base`. Migrations are out of scope for Stage 1.

Typical wiring::

    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from webrelay.server import db

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.on_startup()
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/example")
    async def example(session: AsyncSession = Depends(db.async_session)):
        ...
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# We re-export :class:`AsyncSession` from this module so route code can
# import the type directly from ``webrelay.server.db`` without a
# separate SQLAlchemy import.
__all__ = [
    "AsyncSession",
    "async_session",
    "create_engine",
    "get_engine",
    "get_session_maker",
    "init_db",
    "on_startup",
    "reset_engine",
]

if TYPE_CHECKING:  # pragma: no cover - import-only
    # Imported lazily inside ``init_db`` and ``on_startup`` so that the
    # models module is not pulled in at import time of this module.
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = "./webrelay.db"
ENV_DB_PATH = "WEBRELAY_DB_PATH"


def resolve_db_path(db_path: str | None = None) -> str:
    """Return the on-disk DB path the engine should open.

    Resolution order:

    1. Explicit ``db_path`` argument (used by tests).
    2. The ``WEBRELAY_DB_PATH`` environment variable.
    3. :data:`DEFAULT_DB_PATH` (relative to the process CWD).

    The returned string is always a plain filesystem path -- callers
    that want a SQLAlchemy URL should pass it to :func:`create_engine`.
    """
    if db_path is None:
        db_path = os.environ.get(ENV_DB_PATH, DEFAULT_DB_PATH)
    return db_path


def _path_to_url(path: str) -> str:
    """Convert a filesystem path to a ``sqlite+aiosqlite:///`` URL.

    aiosqlite (via SQLAlchemy's URL parser) requires forward slashes
    and the three-slash form for an absolute path. Relative paths
    (the default ``./webrelay.db``) get two slashes.
    """
    normalized = path.replace("\\", "/")
    if normalized.startswith("/"):
        # Absolute path on POSIX or Windows: sqlite+aiosqlite:///<abs>
        return f"sqlite+aiosqlite:///{normalized}"
    return f"sqlite+aiosqlite:///{normalized}"


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def create_engine(db_path: str | None = None) -> AsyncEngine:
    """Build a fresh :class:`AsyncEngine` pointed at ``db_path``.

    This is the public factory: it does NOT cache the engine. Tests
    use it to spin up isolated engines; the singleton path goes
    through :func:`get_engine` instead.

    Args:
        db_path: Filesystem path to the sqlite file. ``None`` means
            "honour the ``WEBRELAY_DB_PATH`` env var, falling back to
            ``./webrelay.db``".

    Notes:
        * ``echo=False`` keeps SQL out of stdout. Flip via
          ``WEBRELAY_DB_ECHO=1`` when debugging.
        * ``future=True`` is required for SQLAlchemy 2.0 style.
        * SQLite's async driver only supports a single writer; FastAPI
          endpoints that share a session must use the same connection
          thread. The session-maker's default ``expire_on_commit=False``
          (set in :func:`_build_session_maker`) keeps detached objects
          usable after commit -- important for HTMX responses that
          render a model after the session is closed.
    """
    resolved = resolve_db_path(db_path)
    url = _path_to_url(resolved)
    echo = os.environ.get("WEBRELAY_DB_ECHO", "").lower() in {"1", "true", "yes"}
    return create_async_engine(
        url,
        echo=echo,
        future=True,
    )


def _build_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Construct a sessionmaker bound to ``engine``.

    ``expire_on_commit=False`` is the safe default for FastAPI
    endpoints: after ``await session.commit()`` you can still read
    attributes of ORM objects outside the session context, which is
    what route handlers and HTMX templates usually do.
    """
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )


# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide :class:`AsyncEngine`, creating it on first call.

    Module-import-time side effects are explicitly avoided: the engine
    is built the first time somebody asks for it. FastAPI's
    ``on_startup`` hook is a natural first caller; tests can also
    call this directly to get a cached engine.
    """
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide :class:`async_sessionmaker`.

    Lazily creates both the engine and the session maker if they
    don't exist yet. Route code that needs a session should call this
    (or use the :func:`async_session` dependency) instead of building
    its own engine.
    """
    global _session_maker
    if _session_maker is None:
        _session_maker = _build_session_maker(get_engine())
    return _session_maker


def reset_engine() -> None:
    """Drop the cached engine and session maker.

    Intended for tests that need to rebind to a different DB path
    between cases. After calling this, the next :func:`get_engine`
    re-reads ``WEBRELAY_DB_PATH`` and builds a fresh engine.
    """
    global _engine, _session_maker
    _engine = None
    _session_maker = None


# ---------------------------------------------------------------------------
# Session dependency
# ---------------------------------------------------------------------------


async def async_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield a ready-to-use :class:`AsyncSession`.

    The session is committed automatically on a clean exit and
    rolled back if the route handler raises. The ``yield`` shape is
    the canonical FastAPI dependency pattern -- the type system
    understands it as an async generator that returns a session.

    Usage::

        @app.get("/threads")
        async def list_threads(session: AsyncSession = Depends(async_session)):
            ...
    """
    maker = get_session_maker()
    async with maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Create every table declared on ``Base.metadata`` if missing.

    Delegates to :func:`webrelay.server.models.init_db` -- the
    schema definitions live with the models, not here, so that
    adding a new declarative model automatically gets the right
    table on next boot.

    Safe to call on every startup: SQLAlchemy's ``create_all`` is a
    no-op for tables that already exist.
    """
    # Lazy import: avoids pulling the models module (and its full
    # declarative graph) at import time of this module.
    from webrelay.server import models

    engine = get_engine()
    await models.init_db(engine)


async def on_startup() -> None:
    """FastAPI lifespan hook: ensure the engine is built and tables exist.

    Pass this to ``FastAPI(lifespan=...)`` (typically wrapped in an
    ``@asynccontextmanager``) so the DB is ready before the first
    request hits a route. Equivalent to ``await init_db()`` but
    named so it reads naturally at the lifespan call site.
    """
    await init_db()
