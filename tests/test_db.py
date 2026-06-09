"""Tests for the async SQLAlchemy engine + session factory in
``webrelay.server.db``.

These tests cover the public contract the S1 agent committed to:

- :func:`get_engine` is a lazy singleton: it returns the *same*
  :class:`AsyncEngine` instance on every call inside a process.
- :func:`on_startup` is a FastAPI lifespan hook that creates every
  table declared on ``Base.metadata``. We verify the side effect by
  inserting a row through the session maker and reading it back.
- :func:`async_session` is an async-generator dependency that yields a
  usable :class:`AsyncSession` (we run a real INSERT/SELECT through it).

Each test points the module at a per-test temp file by setting
``WEBRELAY_DB_PATH`` (the conftest wipes any inherited ``WEBRELAY_*``
env vars automatically) and resets the module-level singleton between
tests so the new env var takes effect.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select

from webrelay.server import db
from webrelay.server.models import Base, ChatMessage, ChatThread


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _windows_safe_path_to_url(path: str) -> str:
    """Build a sqlite+aiosqlite URL that aiosqlite can open on Windows.

    Mirrors :func:`webrelay.server.db._path_to_url`: forward slashes and
    three slashes (the relative form the db module uses for non-absolute
    paths). Windows drive-letter paths (e.g. ``C:/...``) need three
    slashes, not four, so we don't prepend a leading slash.
    """
    normalized = path.replace("\\", "/")
    return f"sqlite+aiosqlite:///{normalized}"


def _tables_in(engine) -> set[str]:
    """Return the set of table names present in the sqlite file."""
    import asyncio

    async def _fetch() -> set[str]:
        async with engine.connect() as conn:
            rows = await conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            return {r[0] for r in rows}

    return asyncio.get_event_loop().run_until_complete(_fetch())


# ---------------------------------------------------------------------------
# get_engine singleton
# ---------------------------------------------------------------------------
def test_get_engine_returns_same_instance(tmp_sqlite_path: str, monkeypatch):
    """Calling get_engine twice must return the *same* AsyncEngine.

    This is the core lazy-singleton contract: FastAPI request handlers
    should never accidentally spin up a second connection pool.
    """
    monkeypatch.setenv("WEBRELAY_DB_PATH", tmp_sqlite_path)
    # Drop any cached engine from a previous test (or from a prior import).
    db.reset_engine()

    first = db.get_engine()
    second = db.get_engine()
    assert first is second, "get_engine must return the same instance on repeat calls"


def test_get_engine_respects_env_var(tmp_sqlite_path: str, monkeypatch):
    """The engine URL must reflect the WEBRELAY_DB_PATH env var."""
    monkeypatch.setenv("WEBRELAY_DB_PATH", tmp_sqlite_path)
    db.reset_engine()

    engine = db.get_engine()
    # Render the URL to a string for comparison. SQLAlchemy's URL.render
    # strips the driver bits the way aiosqlite expects.
    rendered = str(engine.url)
    assert rendered.startswith("sqlite+aiosqlite")
    # The DB filename should appear somewhere in the URL.
    assert tmp_sqlite_path.replace("\\", "/") in rendered


# ---------------------------------------------------------------------------
# on_startup
# ---------------------------------------------------------------------------
async def test_on_startup_creates_tables(tmp_sqlite_path: str, monkeypatch):
    """on_startup must run create_all so the ORM tables are queryable."""
    monkeypatch.setenv("WEBRELAY_DB_PATH", tmp_sqlite_path)
    db.reset_engine()

    await db.on_startup()

    engine = db.get_engine()
    # The ORM tables we expect on a fresh install.
    expected = {
        "relay_clients",
        "chat_threads",
        "chat_messages",
        "ledger_snapshots",
        "approval_requests",
    }
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {r[0] for r in rows}
    assert expected.issubset(names), f"missing tables; got {sorted(names)}"


async def test_on_startup_round_trip_insert_and_select(tmp_sqlite_path: str, monkeypatch):
    """End-to-end: startup -> insert via session maker -> select the row back.

    This is the "it actually works" smoke test: the engine, the session
    maker, and the schema all line up. We use ``ChatThread`` because it
    has a uuid-string primary key and a ``created_at`` default -- it
    exercises the default-value path as well.
    """
    monkeypatch.setenv("WEBRELAY_DB_PATH", tmp_sqlite_path)
    db.reset_engine()

    await db.on_startup()

    maker = db.get_session_maker()
    thread_id = str(uuid.uuid4())

    # Insert
    async with maker() as session:
        thread = ChatThread(id=thread_id, title="hello db")
        session.add(thread)
        await session.commit()

    # Select back via a *fresh* session (proves the row hit the disk).
    async with maker() as session:
        stmt = select(ChatThread).where(ChatThread.id == thread_id)
        fetched = (await session.execute(stmt)).scalar_one()
        assert fetched.id == thread_id
        assert fetched.title == "hello db"
        assert fetched.created_at is not None


async def test_on_startup_is_idempotent(tmp_sqlite_path: str, monkeypatch):
    """Calling on_startup twice must not raise (create_all is a no-op for
    existing tables)."""
    monkeypatch.setenv("WEBRELAY_DB_PATH", tmp_sqlite_path)
    db.reset_engine()

    await db.on_startup()
    # Second call should be a no-op.
    await db.on_startup()

    engine = db.get_engine()
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = [r[0] for r in rows]
    # No duplicate table entries.
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# async_session dependency
# ---------------------------------------------------------------------------
async def test_async_session_dependency_yields_usable_session(tmp_sqlite_path: str, monkeypatch):
    """``async_session`` is the FastAPI dependency; it must yield a session
    that can insert and read a row, and it must clean up the session on
    exit. We drive it manually (no FastAPI app) by calling the
    underlying async generator."""
    monkeypatch.setenv("WEBRELAY_DB_PATH", tmp_sqlite_path)
    db.reset_engine()

    await db.on_startup()

    # Run an insert + a read through the dependency directly.
    thread_id = str(uuid.uuid4())

    gen = db.async_session()
    session = await gen.__anext__()
    try:
        session.add(ChatMessage(thread_id=thread_id, role="user", content="hi"))
        await session.commit()
    finally:
        await gen.aclose()

    # Open a fresh session through the maker and confirm the row is there.
    maker = db.get_session_maker()
    async with maker() as verify:
        stmt = select(ChatMessage).where(ChatMessage.thread_id == thread_id)
        msg = (await verify.execute(stmt)).scalar_one()
        assert msg.role == "user"
        assert msg.content == "hi"


async def test_async_session_dependency_is_async_iterator(tmp_sqlite_path: str, monkeypatch):
    """``async_session`` must be an async generator, which is the shape
    FastAPI's dependency-injection system understands (it can be used
    with ``Depends(db.async_session)``)."""
    monkeypatch.setenv("WEBRELAY_DB_PATH", tmp_sqlite_path)
    db.reset_engine()
    await db.on_startup()

    gen = db.async_session()
    try:
        assert hasattr(gen, "__anext__"), "async_session must be an async iterator"
        session = await gen.__anext__()
        # It's a real AsyncSession, not None or a placeholder.
        assert hasattr(session, "execute")
        assert hasattr(session, "commit")
        assert hasattr(session, "rollback")
    finally:
        await gen.aclose()


# ---------------------------------------------------------------------------
# get_session_maker
# ---------------------------------------------------------------------------
async def test_get_session_maker_singleton(tmp_sqlite_path: str, monkeypatch):
    """``get_session_maker`` must also be a lazy singleton, paired with
    the engine: same engine -> same sessionmaker."""
    monkeypatch.setenv("WEBRELAY_DB_PATH", tmp_sqlite_path)
    db.reset_engine()

    first = db.get_session_maker()
    second = db.get_session_maker()
    assert first is second

    # And it should be wired to the same engine.
    engine = db.get_engine()
    # The sessionmaker stores its bound engine on `.kw["bind"]` (SA 2.0).
    bound = getattr(first, "kw", {}).get("bind", None)
    if bound is not None:
        assert bound is engine
