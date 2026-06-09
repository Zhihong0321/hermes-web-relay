"""Tests for the read-only file-browser route (R3 slice).

Strategy
--------
We mount only the files router on a tiny FastAPI app per-test, attach
a fake ``hub`` that records every ``request()`` call and returns a
canned ``FileResult``, and then drive the endpoints with
``fastapi.testclient.TestClient``.

Coverage:
  * ``GET /`` renders the file-browser shell with the sandbox-root
    breadcrumb and does NOT touch the hub.
  * ``GET /list?path=docs`` calls ``hub.request(Op.FILE_LIST, ...)``,
    returns an HTMX partial with folders-first ordering, and renders
    the full shell when called outside an HX-Request.
  * ``GET /read?path=docs/x.md`` returns a monospace <pre> with the
    file content escaped; binary payloads render as the
    "<binary file, N bytes>" notice.
  * Path-traversal patterns (``../etc/passwd``, ``/etc/passwd``,
    ``..\\windows``, ``/abs``, ``\\back``) all return 400 and NEVER
    touch the hub.
  * A bridge-rejected symlink target is surfaced as a 502 with the
    bridge's error message — the route does not silently swallow it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.templating import Jinja2Templates

from webrelay.server.protocol import (
    FileList,
    FileRead,
    FileResult,
    Op,
)
from webrelay.server.routes.files import router


# ---------------------------------------------------------------------------
# Fake hub
# ---------------------------------------------------------------------------


class FakeHub:
    """In-memory RelayHub stub that records every request.

    Tests pre-load ``self.responses`` with a list of ``FileResult`` (or
    ``Exception``) entries; each call to ``await hub.request(op, payload)``
    pops the next entry. ``self.calls`` accumulates the (op, payload)
    tuples in order so tests can assert on them.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Op, Any]] = []
        self.responses: list[FileResult | Exception] = []

    async def request(self, op: Op, payload: Any) -> Any:
        self.calls.append((op, payload))
        if not self.responses:
            raise AssertionError(
                f"FakeHub.request called with no queued response (op={op!r})"
            )
        next_response = self.responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response

    async def push(self, op: Op, payload: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def is_connected(self) -> bool:  # pragma: no cover
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_hub() -> FakeHub:
    return FakeHub()


@pytest.fixture
def client(fake_hub: FakeHub) -> TestClient:
    app = FastAPI()
    app.state.hub = fake_hub
    # The real app's ``server/main.py`` will build the Jinja2Templates
    # instance with the templates package; for tests we point directly
    # at the on-disk template directory. file_browser.html {% extends
    # "base.html" %}, so base.html must be resolvable too.
    here = Path(__file__).resolve()
    templates_dir = here.parent.parent / "src" / "webrelay" / "server" / "templates"
    assert templates_dir.is_dir(), f"templates dir missing: {templates_dir}"
    # Change into the templates dir so the {% extends "base.html" %}
    # relative-style lookup works the same way as the real app.
    cwd_before = Path(os.getcwd())
    os.chdir(templates_dir)
    try:
        app.state.templates = Jinja2Templates(directory=str(templates_dir))
        app.include_router(router)
        test_client = TestClient(app)
        yield test_client
    finally:
        os.chdir(cwd_before)


# ---------------------------------------------------------------------------
# /  -- the file-browser shell
# ---------------------------------------------------------------------------


def test_root_renders_browser_shell_without_touching_hub(client: TestClient, fake_hub: FakeHub) -> None:
    """``GET /files/`` renders the file-browser shell and does not call the hub.

    The actual listing is fetched lazily by HTMX on page load — the
    initial page render must succeed even when the local agent is
    offline, so the user at least sees the breadcrumb and nav.
    """
    response = client.get("/files/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    # Sandbox root is always shown in the breadcrumb.
    assert "E:/hermes-agent" in body
    # The lazy-load target is present — the actual fetch happens via HTMX.
    assert 'id="file-table"' in body
    # No hub calls.
    assert fake_hub.calls == []


# ---------------------------------------------------------------------------
# /list -- directory listing
# ---------------------------------------------------------------------------


def test_list_partial_returns_folders_first_htmx(client: TestClient, fake_hub: FakeHub) -> None:
    """``GET /files/list?path=docs`` returns an HTMX partial.

    The fake hub returns a mixed list of folders + files; the partial
    must show folders first (alphabetical) and files after (alphabetical).
    The hub call uses the exact ``FileList`` payload we sent.
    """
    fake_hub.responses.append(
        FileResult(
            path="docs",
            kind="dir",
            entries=[
                {"name": "zeta.md", "kind": "file", "size": 1024},
                {"name": "alpha", "kind": "dir"},
                {"name": "beta.md", "kind": "file", "size": 256},
                {"name": "alpha.md", "kind": "file", "size": 128},
            ],
        )
    )

    response = client.get(
        "/files/list?path=docs",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    body = response.text

    # Hub saw exactly one call with the right op + payload.
    assert len(fake_hub.calls) == 1
    op, payload = fake_hub.calls[0]
    assert op == Op.FILE_LIST
    assert isinstance(payload, FileList)
    assert payload.path == "docs"

    # Order in the rendered HTML: folders first, then files, both
    # alphabetical (case-insensitive). The only folder is "alpha"; the
    # files are alpha.md, beta.md, zeta.md — so the position order is
    # alpha (folder) -> alpha.md -> beta.md -> zeta.md.
    pos_alpha = body.index(">alpha<")  # folder, no extension
    pos_alpha_md = body.index(">alpha.md<")
    pos_beta = body.index(">beta.md<")
    pos_zeta = body.index(">zeta.md<")
    assert pos_alpha < pos_alpha_md < pos_beta < pos_zeta, (
        f"Expected alpha(dir) < alpha.md < beta.md < zeta.md, got positions "
        f"{pos_alpha=}, {pos_alpha_md=}, {pos_beta=}, {pos_zeta=}"
    )


def test_list_returns_full_shell_when_not_htmx(client: TestClient, fake_hub: FakeHub) -> None:
    """A non-HTMX request gets the full page (so opening in a new tab works)."""
    fake_hub.responses.append(
        FileResult(
            path="",
            kind="dir",
            entries=[{"name": "web-relay", "kind": "dir"}],
        )
    )
    response = client.get("/files/list?path=")
    assert response.status_code == 200
    # Full page shell markers:
    assert "<!DOCTYPE html>" in response.text or "<html" in response.text
    assert "file-table" in response.text
    # The file/folder name shows up somewhere.
    assert "web-relay" in response.text


def test_list_bridge_error_is_502(client: TestClient, fake_hub: FakeHub) -> None:
    """A kind="error" from the bridge surfaces as 502 with the bridge message."""
    fake_hub.responses.append(
        FileResult(
            path="missing",
            kind="error",
            error="no such directory: missing",
        )
    )
    response = client.get("/files/list?path=missing")
    assert response.status_code == 502
    assert "no such directory" in response.text


# ---------------------------------------------------------------------------
# /read -- file content
# ---------------------------------------------------------------------------


def test_read_text_file_renders_escaped_pre(client: TestClient, fake_hub: FakeHub) -> None:
    """A text file's content is HTML-escaped and wrapped in <pre>."""
    fake_hub.responses.append(
        FileResult(
            path="docs/intro.md",
            kind="file",
            content="<script>alert('x')</script>\nHello, world.\n",
        )
    )
    response = client.get("/files/read?path=docs/intro.md")
    assert response.status_code == 200
    body = response.text
    assert "<pre" in body
    # The <script> tag is escaped, not interpreted.
    assert "&lt;script&gt;" in body
    assert "<script>alert" not in body
    # Hub call shape:
    assert len(fake_hub.calls) == 1
    op, payload = fake_hub.calls[0]
    assert op == Op.FILE_READ
    assert isinstance(payload, FileRead)
    assert payload.path == "docs/intro.md"


def test_read_binary_file_shows_size_notice(client: TestClient, fake_hub: FakeHub) -> None:
    """A payload that looks binary (control bytes) becomes a size notice, not mojibake."""
    # Build a "binary" string by embedding control characters that the
    # detector checks for (ord(c) < 0x09 and c not in '\\n\\t').
    binary_content = "PNG\x01\x02\x03\x04binary blob"
    fake_hub.responses.append(
        FileResult(
            path="images/hero.png",
            kind="file",
            content=binary_content,
        )
    )
    response = client.get("/files/read?path=images/hero.png")
    assert response.status_code == 200
    body = response.text
    # The body shows "<binary file, N bytes>" but the < is HTML-escaped
    # in the rendered output (&lt;binary file, …&gt;).
    assert "&lt;binary file," in body
    assert "bytes" in body
    # We never emit the raw control bytes.
    assert "\x01\x02" not in body


def test_read_bridge_error_is_502(client: TestClient, fake_hub: FakeHub) -> None:
    fake_hub.responses.append(
        FileResult(
            path="secret.txt",
            kind="error",
            error="access denied: path is outside sandbox",
        )
    )
    response = client.get("/files/read?path=secret.txt")
    assert response.status_code == 502
    assert "access denied" in response.text


# ---------------------------------------------------------------------------
# Path-traversal defense (defense-in-depth at the route)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../etc/passwd",
        "../../etc/passwd",
        "docs/../../../etc/passwd",
        "/etc/passwd",
        "C:/Windows/System32",
        "\\windows\\system32",
        "docs\\..\\..\\etc",
        "..",
        "..\\sneaky",
    ],
)
def test_traversal_paths_are_400_and_skip_hub(
    client: TestClient, fake_hub: FakeHub, path: str
) -> None:
    """Every traversal-shaped path returns 400 and never reaches the hub."""
    response = client.get(f"/files/list?path={path}")
    assert response.status_code == 400, (
        f"expected 400 for path={path!r}, got {response.status_code}: {response.text}"
    )
    # The defense-in-depth check must short-circuit BEFORE hub.request.
    assert fake_hub.calls == [], (
        f"hub was called for bad path={path!r}: {fake_hub.calls!r}"
    )

    # And the same path on /read is also rejected.
    response_read = client.get(f"/files/read?path={path}")
    assert response_read.status_code == 400
    assert fake_hub.calls == []


def test_path_with_null_byte_is_400(client: TestClient, fake_hub: FakeHub) -> None:
    response = client.get("/files/list?path=docs%00.txt")
    # FastAPI's query parser may reject the null byte before our code
    # sees it (status 400 either way); the important thing is that the
    # hub is never called.
    assert response.status_code in (400, 422)
    assert fake_hub.calls == []


# ---------------------------------------------------------------------------
# Symlink / sandbox escape (simulated bridge rejection)
# ---------------------------------------------------------------------------


def test_symlink_escape_is_rejected_by_bridge_mock(
    client: TestClient, fake_hub: FakeHub
) -> None:
    """The route forwards ``docs/link -> /etc/passwd`` to the bridge.

    The bridge (mocked here) refuses to follow symlinks that escape the
    sandbox and returns ``kind="error"``; the route surfaces that as a
    502 with the bridge's message. This proves the route does NOT
    silently swallow bridge errors and does NOT do its own symlink
    resolution that would mask the violation.
    """
    fake_hub.responses.append(
        FileResult(
            path="docs/link",
            kind="error",
            error="symlink target /etc/passwd is outside the sandbox",
        )
    )
    response = client.get("/files/read?path=docs/link")
    assert response.status_code == 502
    assert "outside the sandbox" in response.text
    # The hub was called exactly once with the bridge's view of the path
    # (not a resolved/normalized one) — defense-in-depth happens
    # entirely in the bridge.
    assert len(fake_hub.calls) == 1
    op, payload = fake_hub.calls[0]
    assert op == Op.FILE_READ
    assert payload.path == "docs/link"


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def test_module_imports_cleanly() -> None:
    """The route module exposes a single APIRouter named ``router``."""
    from webrelay.server.routes import files

    assert hasattr(files, "router")
    # The three documented routes are registered.
    paths = {r.path for r in files.router.routes}
    assert "/files/" in paths
    assert "/files/list" in paths
    assert "/files/read" in paths
