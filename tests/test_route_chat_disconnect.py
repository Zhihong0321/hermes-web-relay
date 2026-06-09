"""Tests for the chat disconnect error handling.

Verifies that:
* When the agent is disconnected (RelayHub.push raises ConnectionError),
  POST /chat returns the user bubble and error text, along with a custom
  header 'X-Agent-Disconnected: true' and status 200.
* When the agent is connected, the custom header is NOT present.
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

from webrelay.server.models import ChatThread, ChatMessage, init_db
from webrelay.server.protocol import Op
from webrelay.server.routes.chat import router as chat_router


class FakeHub:
    def __init__(self, raise_conn_error: bool = False) -> None:
        self.raise_conn_error = raise_conn_error
        self.pushes: list[tuple[Op, Any]] = []

    async def push(self, op: Op, payload: Any) -> None:
        if self.raise_conn_error:
            raise ConnectionError("no agent connected")
        self.pushes.append((op, payload))


@pytest.fixture
async def app_with_db(tmp_path: Path):
    db_file = tmp_path / "chat-test.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    engine = create_async_engine(db_url, future=True)
    await init_db(engine)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(chat_router)
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    # Stub base.html and chat/index.html since we only need the endpoint logic
    templates_dir = Path(__file__).resolve().parent.parent / "src" / "webrelay" / "server" / "templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({
                    "base.html": "<html>{% block content %}{% endblock %}</html>",
                    "chat/index.html": "<html>Chat thread: {{ active_thread_id }}</html>",
                }),
                FileSystemLoader(str(templates_dir)),
            ]
        )
    )
    app.state.jinja_env = env
    app.state.templates = env
    app.state.db_session_factory = session_factory

    hub = FakeHub()
    app.state.hub = hub

    try:
        yield app, hub, engine
    finally:
        await engine.dispose()


@pytest.fixture
def seed_request_session(monkeypatch):
    from starlette.requests import Request
    original_init = Request.__init__

    def patched_init(self, scope, receive=None, send=None):
        original_init(self, scope, receive, send)
        if "sid" not in self.session:
            self.session["sid"] = "test-session-id"

    monkeypatch.setattr(Request, "__init__", patched_init)


@pytest.fixture
def client(app_with_db, seed_request_session):
    app, hub, engine = app_with_db
    with TestClient(app) as c:
        c.app.hub = hub
        c.app.engine = engine
        yield c


async def _insert_thread(engine, thread_id: str, title: str = "New Chat") -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        row = ChatThread(
            id=thread_id,
            title=title,
            updated_at=dt.datetime.utcnow(),
        )
        session.add(row)
        await session.commit()


@pytest.mark.asyncio
async def test_chat_post_agent_disconnected(client: TestClient) -> None:
    # Set the hub to raise ConnectionError (agent disconnected)
    client.app.hub.raise_conn_error = True

    thread_id = "test-thread-disconnected"
    await _insert_thread(client.app.engine, thread_id)

    # Post a message
    resp = client.post(
        f"/chat/?thread_id={thread_id}",
        data={"text": "Hello, agent!"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("X-Agent-Disconnected") == "true"
    assert "Error: Local agent is disconnected." in resp.text
    assert "Hello, agent!" in resp.text


@pytest.mark.asyncio
async def test_chat_post_agent_connected(client: TestClient) -> None:
    # Agent is connected (raise_conn_error = False by default)
    client.app.hub.raise_conn_error = False

    thread_id = "test-thread-connected"
    await _insert_thread(client.app.engine, thread_id)

    # Post a message
    resp = client.post(
        f"/chat/?thread_id={thread_id}",
        data={"text": "Hello, agent!"},
    )

    assert resp.status_code == 200
    assert "X-Agent-Disconnected" not in resp.headers
    assert "Hello, agent!" in resp.text
    assert "Error: Local agent is disconnected." not in resp.text
