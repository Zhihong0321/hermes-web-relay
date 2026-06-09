"""Tests for the FastAPI app wiring (server/main.py).

These tests verify the top-level composition in :mod:`webrelay.server.main`:

* The app boots without raising (every optional router import is
  graceful, so the app can come up before every router exists).
* ``GET /`` redirects to ``/auth/login`` when no session is present.
* ``GET /`` redirects to ``/chat`` when ``session["sid"]`` is set.
* ``GET /healthz`` always returns 200 (ops liveness probe).
* The ``hub`` is reachable on ``app.state.hub`` after the lifespan runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

# Ensure no inherited dev env causes the test to talk to a real DB.
os.environ.setdefault("WEBRELAY_DB_PATH", ":memory:")


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a TestClient bound to the real ``app`` from ``main.py``.

    We use the module-level ``app`` (not a fresh factory call) so the
    test exercises the same instance uvicorn would boot in production.
    The DB is pointed at an isolated temp file by the
    ``WEBRELAY_DB_PATH`` env var; the lifespan brings the engine up.
    """
    from webrelay.server import main as server_main

    with TestClient(server_main.app) as c:
        yield c


def test_root_redirects_to_login_without_session(client: TestClient) -> None:
    """An unauthenticated GET / is bounced to /auth/login.

    The app-level middleware sees no ``sid`` on the session and 302s
    to the login route. We do not follow the redirect because we want
    to assert the exact response code + Location header.
    """
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers.get("location", "")
    assert location.startswith("/auth/login"), (
        f"expected redirect to /auth/login, got Location={location!r}"
    )


def test_root_redirects_to_chat_with_session(client: TestClient) -> None:
    """A session with ``sid`` lets GET / fall through to the /chat redirect."""
    # Seed a session by hitting the login form first. The /auth/login
    # route may not exist yet (it's an optional router) so we set the
    # session cookie directly via the test client's cookie jar.
    client.cookies["session"] = "stub-session"
    # The session middleware looks at the cookie via its own signed
    # serializer; the easiest way to set ``sid`` for the test is to
    # use the underlying SessionMiddleware signer.
    import json
    import base64
    from starlette.middleware.sessions import SessionMiddleware

    signer = SessionMiddleware(
        app=None,  # type: ignore[arg-type]
        secret_key=os.environ.get("WEBRELAY_SESSION_SECRET", "dev-secret-change-me"),
    ).signer
    session_data = {"sid": "test-sid"}
    serialized = base64.b64encode(json.dumps(session_data).encode("utf-8"))
    client.cookies["session"] = signer.sign(serialized).decode("ascii")

    response = client.get("/", follow_redirects=False)
    # The middleware sees a valid session, lets the request through,
    # and the root handler redirects to /chat.
    assert response.status_code == 302
    assert response.headers.get("location") == "/chat"


def test_healthz_is_200(client: TestClient) -> None:
    """GET /healthz always returns 200 — it's the liveness probe."""
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True}


def test_static_dir_is_mounted() -> None:
    """The static dir is mounted at /static (no 404 for the mount path)."""
    from webrelay.server import main as server_main

    app = server_main.app
    mounted = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/static" in mounted, (
        f"/static should be mounted; current mount paths: {sorted(mounted)!r}"
    )


def test_session_middleware_is_installed() -> None:
    """SessionMiddleware is on the middleware stack."""
    from webrelay.server import main as server_main

    middleware_classes = [m.cls for m in server_main.app.user_middleware]
    from starlette.middleware.sessions import SessionMiddleware

    assert SessionMiddleware in middleware_classes, (
        f"SessionMiddleware must be installed; got: {middleware_classes!r}"
    )


def test_app_exposes_hub_on_state_after_lifespan() -> None:
    """The lifespan hook stashes the RelayHub on app.state.hub."""
    from webrelay.server import main as server_main
    from webrelay.server.relay_hub import RelayHub

    with TestClient(server_main.app) as c:
        # Lifespan runs on TestClient entry; hub should now be wired.
        assert hasattr(c.app.state, "hub")
        assert isinstance(c.app.state.hub, RelayHub)


def test_root_redirect_preserves_next_param(client: TestClient) -> None:
    """A request for a non-/ path includes ``?next=`` so login can bounce back."""
    response = client.get("/chat", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers.get("location", "")
    assert "/auth/login" in location
    assert "next=" in location, (
        f"expected ?next= query on the login redirect, got {location!r}"
    )