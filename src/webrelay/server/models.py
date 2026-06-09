"""SQLAlchemy 2.0 declarative ORM models for the web-relay server.

These tables are the server-canonical state for the mobile UI:
- RelayClient     — registered local agents (1:1 with bearer token hash)
- ChatThread      — top-level chat conversation, may spawn 0..N task-ledgers
- ChatMessage     — individual turn in a thread
- LedgerSnapshot  — most recent content of a `task_ledger_*.md` file
- ApprovalRequest — pending/resolved sensitive tool prompts sent to the phone

The schema is intentionally append-mostly: only the `LedgerSnapshot.content` and
`updated_at` columns are ever overwritten (latest-snapshot-wins); every other
table grows monotonically. This makes the server a clean event log you can
replay when the local agent reconnects.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncEngine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession  # noqa: F401


def _utcnow() -> dt.datetime:
    """Return a naive UTC datetime (matches SQLite's default storage of ISO strings)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(AsyncAttrs, DeclarativeBase):
    """Shared declarative base. AsyncAttrs lets us `await row.attr` for lazy loads."""


# ---------------------------------------------------------------------------
# RelayClient
# ---------------------------------------------------------------------------
class RelayClient(Base):
    """A registered local hermes-agent installation.

    There is at most one *connected* client at a time (the hub rejects
    additional dials while one is authenticated), but we keep history of every
    client that has registered so we can show device management in the UI and
    rotate tokens.
    """

    __tablename__ = "relay_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    host: Mapped[str] = mapped_column(String(255))
    platform: Mapped[str] = mapped_column(String(64))
    agent_version: Mapped[str] = mapped_column(String(64))
    first_seen: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, server_default=func.current_timestamp()
    )
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# ChatThread / ChatMessage
# ---------------------------------------------------------------------------
class ChatThread(Base):
    """A single chat conversation between the user and hermes."""

    __tablename__ = "chat_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, server_default=func.current_timestamp()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, server_default=func.current_timestamp()
    )
    is_archived: Mapped[bool] = mapped_column(
        default=False, server_default=text("0")
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
    )
    ledgers: Mapped[list["LedgerSnapshot"]] = relationship(
        back_populates="chat_thread",
    )


class ChatMessage(Base):
    """A single turn inside a ChatThread.

    `task_ledger_id` is set when an assistant reply caused hermes to spawn a
    task-ledger; it's a logical foreign key to ``LedgerSnapshot.ledger_id`` but
    we don't enforce it at the DB layer because the ledger snapshot is
    pushed asynchronously by the local agent and may arrive after the chat
    message is stored.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_thread_id", "thread_id"),
        Index("ix_chat_messages_task_ledger_id", "task_ledger_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_threads.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant" | "system"
    content: Mapped[str] = mapped_column(Text)
    task_ledger_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, server_default=func.current_timestamp()
    )
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    thread: Mapped["ChatThread"] = relationship(back_populates="messages")


# ---------------------------------------------------------------------------
# LedgerSnapshot
# ---------------------------------------------------------------------------
class LedgerSnapshot(Base):
    """Latest mirror of a ``task_ledger_*.md`` file.

    ``ledger_id`` is the file basename without the ``task_ledger_`` prefix and
    without the ``.md`` suffix (e.g. ``task_ledger_webrelay.md`` -> ``webrelay``).
    The ledger_bridge overwrites the same row whenever the file changes; we
    keep no history because the source of truth is the local file.
    """

    __tablename__ = "ledger_snapshots"
    __table_args__ = (
        Index("ix_ledger_snapshots_chat_thread_id", "chat_thread_id"),
    )

    ledger_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))  # parsed from ledger frontmatter
    chat_thread_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("chat_threads.id", ondelete="SET NULL"), nullable=True
    )
    mtime: Mapped[float] = mapped_column(Float)  # file mtime as a unix timestamp
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, server_default=func.current_timestamp()
    )

    chat_thread: Mapped["ChatThread | None"] = relationship(back_populates="ledgers")


# ---------------------------------------------------------------------------
# ApprovalRequest
# ---------------------------------------------------------------------------
class ApprovalRequest(Base):
    """A pending or resolved sensitive-tool approval prompt.

    The local Claude Code PreToolUse hook posts a row whenever it needs the
    user to tap allow/deny. The phone UI shows pending rows ordered by
    ``requested_at`` desc; once the user responds, ``decision`` and
    ``responded_at`` are filled in and the row is archived from the active
    view (but kept in the table for history).
    """

    __tablename__ = "approval_requests"
    __table_args__ = (
        # Partial index for fast "what's still pending" lookups.
        Index(
            "ix_approval_requests_pending",
            "requested_at",
            postgresql_where=text("decision IS NULL"),
            sqlite_where=text("decision IS NULL"),
        ),
    )

    prompt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(128))
    command: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )  # "allow" | "deny" | NULL=pending
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, server_default=func.current_timestamp()
    )
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    responded_by_session: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # session id of the browser that responded


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def init_db(engine: AsyncEngine) -> None:
    """Create every table declared on ``Base.metadata`` if it doesn't exist.

    Safe to call on every startup — SQLAlchemy's ``create_all`` is a no-op for
    tables that already exist. For schema migrations use Alembic in a later
    stage.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_pending_approvals(session: AsyncSession) -> list[ApprovalRequest]:
    """Return all approval requests whose decision is still NULL, newest first."""
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.decision.is_(None))
        .order_by(ApprovalRequest.requested_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_latest_ledgers(
    session: AsyncSession, limit: int = 50
) -> list[LedgerSnapshot]:
    """Return the ``limit`` most-recently-updated ledger snapshots, newest first."""
    stmt = select(LedgerSnapshot).order_by(LedgerSnapshot.updated_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())
