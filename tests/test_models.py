"""Tests for the SQLAlchemy 2.0 ORM models.

Covers:
- create_all builds the expected tables on a fresh sqlite file
- round-trip insert + query for every table
- get_pending_approvals returns only decision IS NULL rows in correct order
- get_latest_ledgers returns rows ordered by updated_at desc
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webrelay.server.models import (
    ApprovalRequest,
    Base,
    ChatMessage,
    ChatThread,
    LedgerSnapshot,
    RelayClient,
    get_latest_ledgers,
    get_pending_approvals,
    init_db,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _windows_safe_sqlite_url(path: str) -> str:
    """Build a sqlite+aiosqlite URL that aiosqlite can actually open on Windows.

    The shared conftest fixture blindly prepends a leading "/" to the path,
    which on Windows produces ``sqlite+aiosqlite:////C:/...`` (four slashes).
    aiosqlite on Windows expects three slashes followed by a drive letter, so
    we detect a drive-letter path and drop the spurious leading slash.
    """
    normalized = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", normalized):
        return f"sqlite+aiosqlite:///{normalized}"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return f"sqlite+aiosqlite:///{normalized}"


@pytest.fixture
async def engine(tmp_sqlite_path: str):
    """Build a fresh async engine on the per-test sqlite file, run create_all."""
    eng = create_async_engine(_windows_safe_sqlite_url(tmp_sqlite_path), future=True)
    await init_db(eng)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    """An async_sessionmaker bound to the per-test engine."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
async def session(session_factory) -> AsyncSession:
    """Yield an AsyncSession. Tests that just insert should commit explicitly."""
    async with session_factory() as s:
        yield s


# ---------------------------------------------------------------------------
# create_all
# ---------------------------------------------------------------------------
async def test_create_all_creates_expected_tables(engine):
    async with engine.connect() as conn:
        # Pull the table list straight from sqlite_master
        rows = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {r[0] for r in rows}

    # The ORM tables...
    expected = {
        "relay_clients",
        "chat_threads",
        "chat_messages",
        "ledger_snapshots",
        "approval_requests",
    }
    assert expected.issubset(names), f"missing tables; got {sorted(names)}"


async def test_init_db_is_idempotent(engine):
    # Calling create_all twice should not raise or duplicate tables.
    await init_db(engine)
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = [r[0] for r in rows]
    # No duplicate 'relay_clients' entries, etc.
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# RelayClient
# ---------------------------------------------------------------------------
async def test_relay_client_insert_and_query(session):
    client = RelayClient(
        token_hash="abc123" * 10 + "abcd",  # 64-char sha256 hex
        host="laptop.local",
        platform="windows",
        agent_version="0.1.0",
    )
    session.add(client)
    await session.commit()

    from sqlalchemy import select

    result = await session.execute(select(RelayClient).where(RelayClient.host == "laptop.local"))
    fetched = result.scalar_one()
    assert fetched.token_hash.startswith("abc123")
    assert fetched.platform == "windows"
    assert isinstance(fetched.first_seen, dt.datetime)
    assert fetched.last_seen is None


# ---------------------------------------------------------------------------
# ChatThread + ChatMessage
# ---------------------------------------------------------------------------
async def test_chat_thread_and_messages_round_trip(session):
    thread = ChatThread(id=str(uuid.uuid4()), title="hello world")
    session.add(thread)
    await session.flush()

    m1 = ChatMessage(thread_id=thread.id, role="user", content="hi")
    m2 = ChatMessage(thread_id=thread.id, role="assistant", content="hello back", token_count=2)
    session.add_all([m1, m2])
    await session.commit()

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    stmt = (
        select(ChatThread)
        .options(selectinload(ChatThread.messages))
        .where(ChatThread.id == thread.id)
    )
    fetched = (await session.execute(stmt)).scalar_one()
    assert fetched.title == "hello world"
    assert [m.role for m in fetched.messages] == ["user", "assistant"]
    assert fetched.messages[1].token_count == 2


# ---------------------------------------------------------------------------
# LedgerSnapshot
# ---------------------------------------------------------------------------
async def test_ledger_snapshot_overwrite_same_id(session):
    """Latest-snapshot-wins: re-inserting the same ledger_id should overwrite,
    not duplicate. The canonical pattern is to fetch the existing row and
    mutate it in-place (or use a server-side UPSERT)."""
    from sqlalchemy import select

    snap1 = LedgerSnapshot(
        ledger_id="webrelay",
        filename="task_ledger_webrelay.md",
        content="# first",
        status="PLANNING",
        mtime=100.0,
    )
    session.add(snap1)
    await session.commit()

    # Fetch the existing row and mutate it — this is the "latest snapshot
    # wins" pattern: we never create a second row with the same primary key.
    existing = (
        await session.execute(
            select(LedgerSnapshot).where(LedgerSnapshot.ledger_id == "webrelay")
        )
    ).scalar_one()
    existing.content = "# second"
    existing.status = "IN_PROGRESS"
    existing.mtime = 200.0
    await session.commit()

    from sqlalchemy import func

    # Should still be exactly one row for that ledger_id
    count = (
        await session.execute(
            select(func.count()).select_from(LedgerSnapshot).where(LedgerSnapshot.ledger_id == "webrelay")
        )
    ).scalar_one()
    assert count == 1

    latest = (
        await session.execute(
            select(LedgerSnapshot).where(LedgerSnapshot.ledger_id == "webrelay")
        )
    ).scalar_one()
    assert latest.content == "# second"
    assert latest.status == "IN_PROGRESS"


# ---------------------------------------------------------------------------
# ApprovalRequest
# ---------------------------------------------------------------------------
async def test_approval_request_decision_default_is_null(session):
    req = ApprovalRequest(
        prompt_id="prompt-1",
        tool_name="Bash",
        command="rm -rf /",
        context="destructive",
    )
    session.add(req)
    await session.commit()

    from sqlalchemy import select

    fetched = (
        await session.execute(
            select(ApprovalRequest).where(ApprovalRequest.prompt_id == "prompt-1")
        )
    ).scalar_one()
    assert fetched.decision is None
    assert fetched.responded_at is None
    assert fetched.responded_by_session is None


# ---------------------------------------------------------------------------
# get_pending_approvals
# ---------------------------------------------------------------------------
async def test_get_pending_approvals_filters_and_orders(session):
    # Three pending, in deliberately NON-chronological insert order, plus
    # one already-decided row that must NOT appear in the result.
    older = ApprovalRequest(
        prompt_id="p-old",
        tool_name="Bash",
        command="ls",
        context="",
        requested_at=dt.datetime(2026, 6, 1, 10, 0, 0),
    )
    newer = ApprovalRequest(
        prompt_id="p-new",
        tool_name="Write",
        command="echo x",
        context="",
        requested_at=dt.datetime(2026, 6, 8, 10, 0, 0),
    )
    middle = ApprovalRequest(
        prompt_id="p-mid",
        tool_name="Edit",
        command="vim",
        context="",
        requested_at=dt.datetime(2026, 6, 5, 10, 0, 0),
    )
    decided = ApprovalRequest(
        prompt_id="p-done",
        tool_name="Bash",
        command="ls",
        context="",
        decision="allow",
        requested_at=dt.datetime(2026, 6, 7, 10, 0, 0),
        responded_at=dt.datetime(2026, 6, 7, 11, 0, 0),
        responded_by_session="sess-abc",
    )
    session.add_all([older, newer, middle, decided])
    await session.commit()

    pending = await get_pending_approvals(session)
    ids = [p.prompt_id for p in pending]
    # Resolved row excluded.
    assert "p-done" not in ids
    # Only the three NULL-decision rows present.
    assert set(ids) == {"p-old", "p-new", "p-mid"}
    # Newest first.
    assert ids == ["p-new", "p-mid", "p-old"]


async def test_get_pending_approvals_empty(session):
    # No rows at all.
    assert await get_pending_approvals(session) == []


# ---------------------------------------------------------------------------
# get_latest_ledgers
# ---------------------------------------------------------------------------
async def test_get_latest_ledgers_orders_by_updated_at_desc(session):
    now = dt.datetime(2026, 6, 8, 12, 0, 0)
    # The columns default to "now" so we must set them explicitly to make
    # ordering deterministic.
    a = LedgerSnapshot(
        ledger_id="a",
        filename="task_ledger_a.md",
        content="",
        status="PLANNING",
        mtime=1.0,
        updated_at=now - dt.timedelta(days=3),
    )
    b = LedgerSnapshot(
        ledger_id="b",
        filename="task_ledger_b.md",
        content="",
        status="IN_PROGRESS",
        mtime=2.0,
        updated_at=now - dt.timedelta(days=1),
    )
    c = LedgerSnapshot(
        ledger_id="c",
        filename="task_ledger_c.md",
        content="",
        status="COMPLETED",
        mtime=3.0,
        updated_at=now,
    )
    session.add_all([a, b, c])
    await session.commit()

    ledgers = await get_latest_ledgers(session)
    assert [l.ledger_id for l in ledgers] == ["c", "b", "a"]


async def test_get_latest_ledgers_respects_limit(session):
    base = dt.datetime(2026, 6, 1, 0, 0, 0)
    for i in range(5):
        session.add(
            LedgerSnapshot(
                ledger_id=f"l{i}",
                filename=f"task_ledger_l{i}.md",
                content="",
                status="PLANNING",
                mtime=float(i),
                updated_at=base + dt.timedelta(hours=i),
            )
        )
    await session.commit()

    top2 = await get_latest_ledgers(session, limit=2)
    assert [l.ledger_id for l in top2] == ["l4", "l3"]


# ---------------------------------------------------------------------------
# Cross-table FK: ChatThread <-> LedgerSnapshot.chat_thread_id
# ---------------------------------------------------------------------------
async def test_ledger_snapshot_can_link_to_chat_thread(session):
    thread = ChatThread(id=str(uuid.uuid4()), title="origin")
    session.add(thread)
    await session.flush()

    snap = LedgerSnapshot(
        ledger_id="from-chat",
        filename="task_ledger_from-chat.md",
        content="",
        status="IN_PROGRESS",
        chat_thread_id=thread.id,
        mtime=1.0,
    )
    session.add(snap)
    await session.commit()

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    fetched = (
        await session.execute(
            select(LedgerSnapshot).options(selectinload(LedgerSnapshot.chat_thread))
        )
    ).scalars().first()
    assert fetched is not None
    assert fetched.chat_thread is not None
    assert fetched.chat_thread.title == "origin"
