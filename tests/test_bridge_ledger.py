"""Tests for the ledger bridge.

Three behaviors are verified:

1. ``on_ledger_list`` returns the expected ids (in the sentinel
   ``LedgerResult`` envelope).
2. ``on_ledger_read`` returns the file's content + mtime.
3. ``_watch_loop`` debounces a write and emits a ``LedgerChanged``
   frame via ``client.send`` after the quiet period elapses.

A small ``RecordingClient`` stands in for :class:`RelayClient`. It
records every ``send`` and ``respond`` call so the tests can assert on
what the bridge wrote.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from webrelay.agent.bridges.ledger_bridge import (
    DEBOUNCE_MS,
    LEDGER_PREFIX,
    LEDGER_SUFFIX,
    LIST_SENTINEL_ID,
    LedgerBridge,
    _ledger_id_from_path,
)
from webrelay.agent.protocol import Envelope, LedgerChanged, LedgerResult, Op


# ---------------------------------------------------------------------------
# Recording fake for RelayClient
# ---------------------------------------------------------------------------


class RecordingClient:
    """Drop-in fake for :class:`webrelay.agent.client.RelayClient`.

    Captures every call to ``send`` and ``respond`` on two lists so the
    tests can assert on the frames the bridge produced.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[Op, Any]] = []
        self.responded: list[tuple[Envelope, Any]] = []
        # Handlers registered by the bridge, keyed by op, so tests can
        # also dispatch inbound frames manually if they want to.
        self.handlers: dict[Op, Any] = {}

    def register_handler(self, op: Op, handler: Any) -> None:
        self.handlers[op] = handler

    async def send(
        self, op: Op, payload: Any, *, correlation_id: str | None = None
    ) -> None:
        self.sent.append((op, payload))

    async def respond(self, envelope: Envelope, payload: Any) -> None:
        self.responded.append((envelope, payload))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger_dir(tmp_path: Path) -> Path:
    """An empty directory for ledger files."""
    d = tmp_path / "ledgers"
    d.mkdir()
    return d


@pytest.fixture
def client() -> RecordingClient:
    return RecordingClient()


@pytest.fixture
def bridge(client: RecordingClient, ledger_dir: Path) -> LedgerBridge:
    return LedgerBridge(client, str(ledger_dir))  # type: ignore[arg-type]


@pytest.fixture
def make_envelope() -> Any:
    """Factory for an Envelope with a known id."""

    def _make(envelope_id: str = "env-1") -> Envelope:
        return Envelope(op=Op.LEDGER_LIST, id=envelope_id, ts=0.0, payload={})

    return _make


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Unit tests for the id/path helpers
# ---------------------------------------------------------------------------


def test_ledger_id_from_path_strips_prefix_and_suffix(tmp_path: Path) -> None:
    p = tmp_path / f"{LEDGER_PREFIX}2026-06-08-001{LEDGER_SUFFIX}"
    assert _ledger_id_from_path(p) == "2026-06-08-001"


def test_ledger_id_from_path_rejects_non_ledger(tmp_path: Path) -> None:
    p = tmp_path / "not_a_ledger.txt"
    with pytest.raises(ValueError):
        _ledger_id_from_path(p)


# ---------------------------------------------------------------------------
# on_ledger_list
# ---------------------------------------------------------------------------


async def test_on_ledger_list_returns_expected_ids(
    bridge: LedgerBridge,
    client: RecordingClient,
    ledger_dir: Path,
    make_envelope: Any,
) -> None:
    # Seed two ledger files.
    (ledger_dir / f"{LEDGER_PREFIX}alpha{LEDGER_SUFFIX}").write_text("a")
    (ledger_dir / f"{LEDGER_PREFIX}beta{LEDGER_SUFFIX}").write_text("b")
    (ledger_dir / "not_a_ledger.txt").write_text("ignored")

    envelope = make_envelope("env-list")
    # Build a LedgerList-shaped payload (the handler ignores it).
    payload = MagicMock(name="LedgerList")

    await bridge.on_ledger_list(envelope, payload)

    assert len(client.responded) == 1
    resp_env, resp_payload = client.responded[0]
    assert resp_env is envelope
    assert isinstance(resp_payload, LedgerResult)
    assert resp_payload.ledger_id == LIST_SENTINEL_ID
    # The content is a JSON-encoded list of ids (sorted).
    decoded = json.loads(resp_payload.content)
    assert decoded == ["alpha", "beta"]


async def test_on_ledger_list_empty_dir(
    bridge: LedgerBridge,
    client: RecordingClient,
    make_envelope: Any,
) -> None:
    envelope = make_envelope()
    await bridge.on_ledger_list(envelope, MagicMock())

    assert len(client.responded) == 1
    _, payload = client.responded[0]
    assert isinstance(payload, LedgerResult)
    assert json.loads(payload.content) == []


# ---------------------------------------------------------------------------
# on_ledger_read
# ---------------------------------------------------------------------------


async def test_on_ledger_read_returns_content_and_mtime(
    bridge: LedgerBridge,
    client: RecordingClient,
    ledger_dir: Path,
    make_envelope: Any,
) -> None:
    target = ledger_dir / f"{LEDGER_PREFIX}001{LEDGER_SUFFIX}"
    target.write_text("# Hello\nWorld\n", encoding="utf-8")
    # Force a deterministic mtime.
    import os
    os.utime(target, (1_700_000_000, 1_700_000_500))

    envelope = make_envelope("env-read")
    from webrelay.agent.protocol import LedgerRead
    payload = LedgerRead(ledger_id="001")

    await bridge.on_ledger_read(envelope, payload)

    assert len(client.responded) == 1
    _, resp = client.responded[0]
    assert isinstance(resp, LedgerResult)
    assert resp.ledger_id == "001"
    assert resp.content == "# Hello\nWorld\n"
    assert resp.mtime == pytest.approx(1_700_000_500.0, abs=1e-3)


async def test_on_ledger_read_missing_file(
    bridge: LedgerBridge,
    client: RecordingClient,
    make_envelope: Any,
) -> None:
    from webrelay.agent.protocol import LedgerRead

    envelope = make_envelope()
    await bridge.on_ledger_read(envelope, LedgerRead(ledger_id="does-not-exist"))

    assert len(client.responded) == 1
    _, resp = client.responded[0]
    assert isinstance(resp, LedgerResult)
    assert resp.ledger_id == "does-not-exist"
    assert resp.content == ""
    assert resp.mtime == 0.0


# ---------------------------------------------------------------------------
# _watch_loop
# ---------------------------------------------------------------------------


async def test_watch_loop_emits_ledger_changed_on_write(
    client: RecordingClient,
    ledger_dir: Path,
) -> None:
    bridge = LedgerBridge(client, str(ledger_dir))  # type: ignore[arg-type]
    await bridge.start()

    try:
        # Give the watcher a moment to install its handle on the dir.
        await asyncio.sleep(0.2)

        # Create a ledger file. watchfiles should pick this up; our
        # internal debounce holds the event for DEBOUNCE_MS before
        # sending. We pad the wait to allow for filesystem latency.
        target = ledger_dir / f"{LEDGER_PREFIX}watched{LEDGER_SUFFIX}"
        target.write_text("hello world", encoding="utf-8")

        # Wait long enough for debounce + filesystem event delivery.
        # On slow Windows VMs the kernel notification can take a beat.
        timeout_s = (DEBOUNCE_MS / 1000.0) + 3.0
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if client.sent:
                break
            await asyncio.sleep(0.1)
    finally:
        await bridge.stop()

    # Verify the bridge sent exactly one LedgerChanged for our file.
    assert client.sent, (
        "watcher did not emit a ledger.changed frame within timeout"
    )
    op, payload = client.sent[0]
    assert op == Op.LEDGER_CHANGED
    assert isinstance(payload, LedgerChanged)
    assert payload.ledger_id == "watched"
    assert payload.content == "hello world"
    assert payload.mtime > 0.0


async def test_start_registers_handlers(
    client: RecordingClient,
    ledger_dir: Path,
) -> None:
    bridge = LedgerBridge(client, str(ledger_dir))  # type: ignore[arg-type]
    await bridge.start()
    try:
        assert Op.LEDGER_LIST in client.handlers
        assert Op.LEDGER_READ in client.handlers
    finally:
        await bridge.stop()
