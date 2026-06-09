"""Shared pytest fixtures for hermes-web-relay.

Currently provides:
- tmp_sqlite_path: a per-test sqlite URL on the OS tempdir, auto-cleaned.
- mocked_httpx: a respx.MockRouter pre-installed on an httpx.AsyncClient
  so tests that hit the Coolify API never touch the network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx


@pytest.fixture
def tmp_sqlite_path(tmp_path: Path) -> Iterator[str]:
    """Yield a unique sqlite file path inside tmp_path and unlink it after.

    The path is suitable for use as `WEBRELAY_DB_PATH`. Tests can pass
    `sqlite+aiosqlite:///<yielded>` to SQLAlchemy directly.
    """
    db_file = tmp_path / "webrelay-test.db"
    yield str(db_file)
    # Best-effort cleanup; the file may not exist if the test never opened it.
    if db_file.exists():
        try:
            db_file.unlink()
        except OSError:
            pass


@pytest.fixture
def tmp_sqlite_url(tmp_sqlite_path: str) -> str:
    """Yield a SQLAlchemy async URL pointing at tmp_sqlite_path."""
    # On Windows, sqlite needs three slashes for an absolute path.
    normalized = tmp_sqlite_path.replace("\\", "/")
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return f"sqlite+aiosqlite:///{normalized}"


@pytest.fixture
def mocked_httpx() -> Iterator[respx.MockRouter]:
    """Activate a respx mock router; all httpx calls are intercepted."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def http_client(mocked_httpx: respx.MockRouter) -> Iterator[httpx.AsyncClient]:
    """An httpx.AsyncClient bound to the respx mock transport."""
    async with httpx.AsyncClient(base_url="https://coolify.test") as client:
        yield client


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe WEBRELAY_* env vars so tests don't inherit the developer's .env."""
    for key in list(os.environ):
        if key.startswith("WEBRELAY_"):
            monkeypatch.delenv(key, raising=False)
