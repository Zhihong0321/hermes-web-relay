"""Tests for the /approvals route module.

Covers:
* Empty pending list renders the empty-state page.
* Pending list renders one card per row.
* POST /{prompt_id}/decision with "allow" updates the row and pushes
  Op.APPROVAL_RESPOND to the hub with the right payload.
* POST with "deny" does the same.
* GET /badge returns the pending count as a small text fragment.

The TestClient wires up a real FastAPI app with a real async sqlite
session factory (per-test tmp file) and a ``FakeHub`` that records every
``push`` call. We also register a minimal ``base.html`` with the Jinja
env so ``approvals.html`` can extend it — the file lives under the
templates directory in the real deployment (S3 owns it), but for these
isolated route tests we inject a stub.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.sessions import SessionMiddleware

from webrelay.server.models import ApprovalRequest, Base, init_db
from webrelay.server.protocol import ApprovalRespond, Op
from webrelay.server.routes.approvals import router as approvals_router


# ---------------------------------------------------------------------------
# Stub base template
# ---------------------------------------------------------------------------

STUB_BASE_HTML = """<!DOCTYPE html>
<html>
<head><title>{% block title %}{% endblock %}</title></head>
<body>
  <nav id="nav">
    <span id="approvals-badge"></span>
  </nav>
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Fake hub
# ---------------------------------------------------------------------------

class FakeHub:
    """Records every push() call so tests can assert on it."""

    def __init__(self) -> None:
        self.pushes: list[tuple[Op, Any]] = []
        self.connected = True

    async def push(self, op: Op, payload: Any) -> None:
        self.pushes.append((op, payload))

    def is_connected(self) -> bool:
        return self.connected


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def app_with_db(tmp_path: Path):
    """Build a FastAPI app wired with approvals router + sqlite + Jinja env.

    Yields ``(app, hub, engine)`` so tests can poke the engine directly
    to seed rows and inspect the hub for recorded pushes.
    """
    db_file = tmp_path / "approvals-test.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    engine = create_async_engine(db_url, future=True)
    await init_db(engine)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(approvals_router)
    # SessionMiddleware is wired by the auth layer in production (R5).
    # For these isolated route tests we install a no-secret variant so
    # ``request.session.get("sid")`` works in the route handler.
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    # Build a Jinja env that knows about both the real templates dir
    # (for approvals.html) and the stub base.html dict.
    templates_dir = Path(__file__).resolve().parent.parent / "src" / "webrelay" / "server" / "templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": STUB_BASE_HTML}),
                FileSystemLoader(str(templates_dir)),
            ]
        )
    )
    app.state.jinja_env = env
    app.state.db_session_factory = session_factory

    hub = FakeHub()
    app.state.hub = hub

    try:
        yield app, hub, engine
    finally:
        await engine.dispose()


@pytest.fixture
def seed_request_session(monkeypatch):
    """Patch Starlette's Session to inject a stable sid for any request.

    The real auth layer is wired in Stage 3; for these route tests we
    give every incoming request a deterministic session id so the
    responded_by_session column is non-NULL and assertable.
    """
    from starlette.requests import Request

    original_init = Request.__init__

    def patched_init(self, scope, receive=None, send=None):  # type: ignore[no-untyped-def]
        original_init(self, scope, receive, send)
        # Ensure the session dict has a sid (the auth middleware would do
        # this in production).
        if "sid" not in self.session:
            self.session["sid"] = "test-session-id"

    monkeypatch.setattr(Request, "__init__", patched_init)


@pytest.fixture
def client(app_with_db, seed_request_session):
    """A TestClient bound to the wired app."""
    app, hub, engine = app_with_db
    with TestClient(app) as c:
        # Stash the hub and engine on the client for easy access in tests.
        c.app.hub = hub  # type: ignore[attr-defined]
        c.app.engine = engine  # type: ignore[attr-defined]
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _insert_pending(engine, prompt_id: str, tool: str = "Bash",
                          command: str = "rm -rf /tmp/x",
                          context: str = "test context",
                          requested_at: dt.datetime | None = None) -> None:
    """Insert a pending ApprovalRequest row directly via the engine."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        row = ApprovalRequest(
            prompt_id=prompt_id,
            tool_name=tool,
            command=command,
            context=context,
            requested_at=requested_at or dt.datetime.utcnow(),
        )
        session.add(row)
        await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_list_empty(client: TestClient) -> None:
    """An empty pending list renders the empty-state page."""
    resp = client.get("/approvals/")
    assert resp.status_code == 200
    body = resp.text
    assert "No pending approvals" in body
    assert "approvals__empty" in body
    # No card list rendered when empty.
    assert 'id="approvals-list"' not in body


def test_list_with_pending(client: TestClient) -> None:
    """One pending row produces one card with tool, command, and buttons."""
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        _insert_pending(client.app.engine, "p-1", tool="Bash", command="ls -la")
    )
    resp = client.get("/approvals/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="approval-p-1"' in body
    assert "Bash" in body
    assert "ls -la" in body
    # Both buttons present with the right decision values.
    assert 'value="allow"' in body
    assert 'value="deny"' in body
    # hx-post points at the right decision URL.
    assert 'hx-post="/approvals/p-1/decision"' in body


def test_allow_a_request(client: TestClient) -> None:
    """POSTing allow updates the row, pushes to hub, returns a remove partial."""
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        _insert_pending(client.app.engine, "p-allow", tool="Edit",
                        command="vim /etc/hosts")
    )
    resp = client.post(
        "/approvals/p-allow/decision",
        data={"decision": "allow"},
    )
    assert resp.status_code == 200

    # Hub saw a single push with the expected payload.
    hub: FakeHub = client.app.hub
    assert len(hub.pushes) == 1
    op, payload = hub.pushes[0]
    assert op == Op.APPROVAL_RESPOND
    assert isinstance(payload, ApprovalRespond)
    assert payload.prompt_id == "p-allow"
    assert payload.decision == "allow"

    # Row state: decision filled in, responded_at non-null.
    async def _row() -> ApprovalRequest:
        from sqlalchemy import select
        async with async_sessionmaker(client.app.engine, expire_on_commit=False)() as s:
            return (await s.execute(
                select(ApprovalRequest).where(ApprovalRequest.prompt_id == "p-allow")
            )).scalar_one()

    row = asyncio.get_event_loop().run_until_complete(_row())
    assert row.decision == "allow"
    assert row.responded_at is not None
    assert row.responded_by_session == "test-session-id"

    # Response body is the (empty) outerHTML-swap partial.
    assert "resolved:p-allow" in resp.text


def test_deny_a_request(client: TestClient) -> None:
    """POSTing deny records 'deny' and pushes the same decision."""
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        _insert_pending(client.app.engine, "p-deny", tool="Bash",
                        command="rm -rf /")
    )
    resp = client.post(
        "/approvals/p-deny/decision",
        data={"decision": "deny", "reason": "too destructive"},
    )
    assert resp.status_code == 200

    hub: FakeHub = client.app.hub
    assert len(hub.pushes) == 1
    op, payload = hub.pushes[0]
    assert op == Op.APPROVAL_RESPOND
    assert payload.decision == "deny"
    assert payload.reason == "too destructive"

    # The list page no longer shows the denied card.
    follow = client.get("/approvals/")
    assert 'id="approval-p-deny"' not in follow.text
    assert "No pending approvals" in follow.text


def test_badge_count(client: TestClient) -> None:
    """GET /badge returns the pending count as a small text fragment."""
    import asyncio

    # Three pending, one already-resolved -> badge = 3.
    asyncio.get_event_loop().run_until_complete(_insert_pending(client.app.engine, "a"))
    asyncio.get_event_loop().run_until_complete(_insert_pending(client.app.engine, "b"))
    asyncio.get_event_loop().run_until_complete(_insert_pending(client.app.engine, "c"))
    # Resolved row (decision set) should NOT count.
    async def _seed_resolved() -> None:
        async with async_sessionmaker(client.app.engine, expire_on_commit=False)() as s:
            s.add(ApprovalRequest(
                prompt_id="z",
                tool_name="Bash",
                command="echo done",
                context="ctx",
                decision="allow",
                responded_at=dt.datetime.utcnow(),
                responded_by_session="x",
            ))
            await s.commit()
    asyncio.get_event_loop().run_until_complete(_seed_resolved())

    resp = client.get("/approvals/badge")
    assert resp.status_code == 200
    assert resp.text.strip() == "3"
