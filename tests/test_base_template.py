"""Stage-1 tests for the S3 shared mobile shell.

Covers:

* ``base.html`` is a valid Jinja template that renders end-to-end with a
  stub context, contains the "Hermes" brand string, references the
  static CSS, and loads HTMX from a CDN.
* ``GET /healthz`` returns ``200`` and ``{"ok": true}``.
* ``GET /api/relay/status`` returns the connected/disconnected badge
  fragment that ``base.html`` polls.
* The PWA ``manifest.json`` is well-formed JSON with the right name,
  theme color, and an ``icon-192.png`` reference.
* The static ``app.css`` contains the dark-theme palette, safe-area
  insets, and chat-bubble classes the templates rely on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape


# --- Paths -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "src" / "webrelay" / "server" / "templates"
STATIC_DIR = REPO_ROOT / "src" / "webrelay" / "server" / "static"


# --- Helpers ---------------------------------------------------------------


@pytest.fixture
def jinja_env() -> Environment:
    """Jinja env pointed at the templates dir, undefined = Chainable (silent)."""
    from jinja2 import ChainableUndefined

    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=ChainableUndefined,
    )


# --- base.html -------------------------------------------------------------


class TestBaseTemplate:
    """Render base.html and assert the shared shell is wired up."""

    def test_renders(self, jinja_env: Environment) -> None:
        template = jinja_env.get_template("base.html")
        html = template.render()
        # If Jinja blew up, the test would have failed above.
        assert "<html" in html.lower()
        assert "</html>" in html.lower()

    def test_contains_brand(self, jinja_env: Environment) -> None:
        html = jinja_env.get_template("base.html").render()
        assert "Hermes" in html, "base.html should brand itself 'Hermes'"

    def test_references_app_css(self, jinja_env: Environment) -> None:
        html = jinja_env.get_template("base.html").render()
        assert "/static/app.css" in html, "base.html must link the app stylesheet"

    def test_loads_htmx_from_cdn(self, jinja_env: Environment) -> None:
        html = jinja_env.get_template("base.html").render()
        assert "htmx" in html.lower()
        # Must be a CDN reference (http/https), not a relative /static path.
        assert "https://" in html or "http://" in html
        # And the SSE extension is needed for the chat stream + ledger feed.
        assert "sse" in html.lower()

    def test_has_four_bottom_tabs(self, jinja_env: Environment) -> None:
        html = jinja_env.get_template("base.html").render()
        for tab in ("Chat", "Ledgers", "Files", "Approvals"):
            assert tab in html, f"bottom nav should have a '{tab}' tab"

    def test_polls_relay_status(self, jinja_env: Environment) -> None:
        html = jinja_env.get_template("base.html").render()
        assert "/api/relay/status" in html
        assert 'every 5s' in html, "the connected badge must poll every 5 seconds"

    def test_pwa_manifest_and_theme_color(self, jinja_env: Environment) -> None:
        html = jinja_env.get_template("base.html").render()
        assert 'rel="manifest"' in html
        assert "/static/manifest.json" in html
        assert 'name="theme-color"' in html
        assert "#0a0a0a" in html

    def test_logout_link_present(self, jinja_env: Environment) -> None:
        html = jinja_env.get_template("base.html").render()
        assert "/auth/logout" in html

    def test_content_block(self, jinja_env: Environment) -> None:
        # Smoke-render the {% block content %} body so a child template can
        # actually inherit cleanly.
        html = jinja_env.get_template("base.html").render()
        assert "{% block content %}" not in html, "block tags must be expanded"
        assert '<main' in html


# --- manifest.json ---------------------------------------------------------


class TestManifest:
    """PWA manifest sanity checks."""

    @pytest.fixture
    def manifest(self) -> dict:
        path = STATIC_DIR / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_is_valid_json(self, manifest: dict) -> None:
        assert isinstance(manifest, dict)

    def test_app_name(self, manifest: dict) -> None:
        assert manifest["name"] == "Hermes Relay"
        assert manifest["short_name"] == "Hermes"

    def test_standalone_display(self, manifest: dict) -> None:
        assert manifest["display"] == "standalone"

    def test_dark_theme(self, manifest: dict) -> None:
        assert manifest["theme_color"] == "#0a0a0a"
        assert manifest["background_color"] == "#0a0a0a"

    def test_icon_path(self, manifest: dict) -> None:
        icons = manifest.get("icons", [])
        assert icons, "manifest should declare at least one icon"
        assert any(icon["src"].endswith("icon-192.png") for icon in icons)


# --- app.css ---------------------------------------------------------------


class TestAppCss:
    """The hand-rolled CSS file ships the few rules not covered by Tailwind."""

    @pytest.fixture
    def css(self) -> str:
        return (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    def test_dark_palette(self, css: str) -> None:
        assert "#0a0a0a" in css, "must declare the zinc-950 background"
        assert "safe-area-inset" in css, "must respect iOS safe-area insets"

    def test_bottom_nav_height(self, css: str) -> None:
        assert "56" in css, "bottom nav should pin to >=56px per the spec"

    def test_chat_bubble_classes(self, css: str) -> None:
        assert ".chat-bubble" in css
        # The two color variants the templates will set.
        assert ".chat-bubble.user" in css
        assert ".chat-bubble.assistant" in css


# --- routes/status.py ------------------------------------------------------


class _StubHub:
    """Minimal stand-in for :class:`RelayHub` that returns a fixed connection state."""

    def __init__(self, connected: bool = False) -> None:
        self._connected = connected

    def is_connected(self) -> bool:  # noqa: D401 — matches hub signature
        return self._connected


def _make_app(connected: bool = False) -> FastAPI:
    """Build a FastAPI app with just the status router + a fake hub on state."""
    from webrelay.server.routes.status import router as status_router

    app = FastAPI()
    app.include_router(status_router)
    app.state.hub = _StubHub(connected=connected)
    return app


class TestHealthz:
    def test_returns_200_and_ok(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


class TestRelayStatus:
    def test_disconnected_badge_when_no_client(self) -> None:
        client = TestClient(_make_app(connected=False))
        resp = client.get("/api/relay/status")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        body = resp.text
        assert "Disconnected" in body
        assert "is-disconnected" in body

    def test_connected_badge_when_client_attached(self) -> None:
        client = TestClient(_make_app(connected=True))
        resp = client.get("/api/relay/status")
        assert resp.status_code == 200
        body = resp.text
        assert "Connected" in body
        assert "is-connected" in body

    def test_status_survives_missing_hub(self) -> None:
        # If app.state.hub isn't set (early boot / mis-config), we should
        # still return *something* — never 500 the polling badge.
        app = FastAPI()
        from webrelay.server.routes.status import router as status_router

        app.include_router(status_router)
        # Deliberately do NOT set app.state.hub.
        client = TestClient(app)
        resp = client.get("/api/relay/status")
        assert resp.status_code == 200
        assert "Disconnected" in resp.text
