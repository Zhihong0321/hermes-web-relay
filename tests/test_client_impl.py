"""Stage-2 implementation tests for the agent client modules.

Covers the L1 (RELAY CLIENT IMPL + CONFIG) deliverable:

* ``reconnect_backoff`` yields the expected sequence with a
  deterministic jitter function.
* ``load_config`` reads from a temp vault file (all three accepted
  layouts), and env-var overrides win over vault values.
* ``RelayClient.register_handler`` accepts multiple handlers per op
  and ``send`` writes the right envelope to a fake websocket.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from webrelay.agent.client import RelayClient
from webrelay.agent.config import (
    DEFAULT_FILE_SANDBOX_ROOT,
    DEFAULT_HERMES_WS_URL,
    DEFAULT_WATCHED_LEDGER_DIR,
    ENV_BEARER_TOKEN,
    ENV_HERMES_WS_URL,
    ENV_SANDBOX_ROOT,
    ENV_SERVER_URL,
    ENV_WATCHED_DIR,
    AgentConfig,
    load_config,
)
from webrelay.agent.protocol import (
    Envelope,
    Op,
    Pong,
    build_envelope,
    parse_envelope,
)
from webrelay.agent.reconnect import reconnect_backoff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Hello(BaseModel):
    """A minimal stand-in for the C1 ``Hello`` schema."""

    agent_version: str = "0.0.0"
    host: str = "test-host"
    platform: str = "test-platform"


def _drain(gen: Any, n: int) -> list[float]:
    """Advance a sync generator ``n`` steps and return the values."""
    out: list[float] = []
    for _ in range(n):
        out.append(next(gen))
    return out


class _FakeWebSocket:
    """Minimal stand-in for the websockets connection used by ``send``.

    Records every ``send`` call so the test can assert on the wire
    format. The ``__aiter__`` / ``__anext__`` pair is unused in these
    tests (we never start the read loop) but must exist so the type
    accepts the object where a real WebSocket is expected.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, frame: str) -> None:
        self.sent.append(frame)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:  # pragma: no cover - never reached
        raise StopAsyncIteration


# ---------------------------------------------------------------------------
# reconnect_backoff
# ---------------------------------------------------------------------------


def test_reconnect_backoff_yields_expected_sequence() -> None:
    """The base schedule is 1, 2, 4, 8, 16, 32, 60, 60, 60, ... seconds.

    With ``jitter_fn`` returning the midpoint, every yielded value
    equals the expected base exactly.
    """

    def _midpoint(low: float, high: float) -> float:
        return (low + high) / 2.0

    gen = reconnect_backoff(initial=1.0, max=60.0, jitter=0.3, jitter_fn=_midpoint)
    delays = _drain(gen, 10)
    # 1, 2, 4, 8, 16, 32, then capped at 60 for the rest.
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0]


def test_reconnect_backoff_jitter_band() -> None:
    """With ``jitter_fn`` returning ``low`` every call, every value is
    the bottom of its band; with ``high`` every value is the top.
    """

    gen_low = reconnect_backoff(initial=1.0, max=60.0, jitter=0.3, jitter_fn=lambda lo, hi: lo)
    low_values = _drain(gen_low, 4)
    assert low_values == pytest.approx([0.7, 1.4, 2.8, 5.6])

    gen_high = reconnect_backoff(initial=1.0, max=60.0, jitter=0.3, jitter_fn=lambda lo, hi: hi)
    high_values = _drain(gen_high, 4)
    assert high_values == pytest.approx([1.3, 2.6, 5.2, 10.4])


def test_reconnect_backoff_rejects_invalid_args() -> None:
    """Argument validation mirrors the docstring's preconditions.

    Note: the validation happens when the generator is first advanced
    (the body runs only when ``next()`` is called) — we therefore
    advance once to trigger the check.
    """
    for bad_kwargs in (
        {"initial": 0},
        {"initial": 10, "max": 1},
        {"jitter": 1.0},
        {"jitter": -0.1},
    ):
        gen = reconnect_backoff(**bad_kwargs)
        with pytest.raises(ValueError):
            next(gen)


def test_reconnect_backoff_is_regular_generator_function() -> None:
    """The function is a sync generator (per the L1 spec)."""
    assert inspect.isgeneratorfunction(reconnect_backoff)
    assert not inspect.iscoroutinefunction(reconnect_backoff)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def _write_vault(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_config_nested_layout(tmp_path: Path) -> None:
    """The most common layout: nested ``{"webrelay": {"...": ...}}``."""
    vault = tmp_path / "vault.json"
    _write_vault(
        vault,
        {
            "webrelay": {
                "server_url": "wss://example.test/api/relay/ws",
                "bearer_token": "token-abc-123",
            }
        },
    )
    cfg = load_config(vault_path=str(vault))
    assert isinstance(cfg, AgentConfig)
    assert cfg.server_url == "wss://example.test/api/relay/ws"
    assert cfg.bearer_token == "token-abc-123"
    # Defaults kick in for optional fields.
    assert cfg.hermes_ws_url == DEFAULT_HERMES_WS_URL
    assert cfg.watched_ledger_dir == DEFAULT_WATCHED_LEDGER_DIR
    assert cfg.file_sandbox_root == DEFAULT_FILE_SANDBOX_ROOT


def test_load_config_credentials_list_layout(tmp_path: Path) -> None:
    """The credential-record layout: list of ``{id, value}`` records."""
    vault = tmp_path / "vault.json"
    _write_vault(
        vault,
        {
            "credentials": [
                {"id": "webrelay.server_url", "value": "wss://list.test/ws"},
                {"id": "webrelay.bearer_token", "value": "list-token"},
            ]
        },
    )
    cfg = load_config(vault_path=str(vault))
    assert cfg.server_url == "wss://list.test/ws"
    assert cfg.bearer_token == "list-token"


def test_load_config_flat_layout(tmp_path: Path) -> None:
    """The flat layout: top-level keys named with the dotted id."""
    vault = tmp_path / "vault.json"
    _write_vault(
        vault,
        {
            "webrelay.server_url": "wss://flat.test/ws",
            "webrelay.bearer_token": "flat-token",
        },
    )
    cfg = load_config(vault_path=str(vault))
    assert cfg.server_url == "wss://flat.test/ws"
    assert cfg.bearer_token == "flat-token"


def test_load_config_optional_vault_overrides(tmp_path: Path) -> None:
    """Optional vault keys override the dataclass defaults."""
    vault = tmp_path / "vault.json"
    _write_vault(
        vault,
        {
            "webrelay": {
                "server_url": "wss://example.test/ws",
                "bearer_token": "tok",
                "hermes_ws_url": "ws://localhost:9999/api/ws",
                "watched_ledger_dir": "E:/somewhere/else",
                "file_sandbox_root": "E:/sandbox",
            }
        },
    )
    cfg = load_config(vault_path=str(vault))
    assert cfg.hermes_ws_url == "ws://localhost:9999/api/ws"
    assert cfg.watched_ledger_dir == "E:/somewhere/else"
    assert cfg.file_sandbox_root == "E:/sandbox"


def test_load_config_env_overrides_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every WEBRELAY_* env var wins over the vault value."""
    vault = tmp_path / "vault.json"
    _write_vault(
        vault,
        {
            "webrelay": {
                "server_url": "wss://vault.test/ws",
                "bearer_token": "vault-token",
                "hermes_ws_url": "ws://vault.test/hermes",
                "watched_ledger_dir": "E:/vault/ledgers",
                "file_sandbox_root": "E:/vault/sandbox",
            }
        },
    )
    monkeypatch.setenv(ENV_SERVER_URL, "wss://env.test/ws")
    monkeypatch.setenv(ENV_BEARER_TOKEN, "env-token")
    monkeypatch.setenv(ENV_HERMES_WS_URL, "ws://env.test/hermes")
    monkeypatch.setenv(ENV_WATCHED_DIR, "E:/env/ledgers")
    monkeypatch.setenv(ENV_SANDBOX_ROOT, "E:/env/sandbox")

    cfg = load_config(vault_path=str(vault))
    assert cfg.server_url == "wss://env.test/ws"
    assert cfg.bearer_token == "env-token"
    assert cfg.hermes_ws_url == "ws://env.test/hermes"
    assert cfg.watched_ledger_dir == "E:/env/ledgers"
    assert cfg.file_sandbox_root == "E:/env/sandbox"


def test_load_config_env_overrides_only_one_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting a single env var should not blank the others — vault still
    supplies the unspecified fields."""
    vault = tmp_path / "vault.json"
    _write_vault(
        vault,
        {
            "webrelay": {
                "server_url": "wss://vault.test/ws",
                "bearer_token": "vault-token",
            }
        },
    )
    monkeypatch.setenv(ENV_BEARER_TOKEN, "env-token")
    cfg = load_config(vault_path=str(vault))
    assert cfg.server_url == "wss://vault.test/ws"
    assert cfg.bearer_token == "env-token"


def test_load_config_missing_vault(tmp_path: Path) -> None:
    """A missing vault file raises FileNotFoundError pointing to setup."""
    with pytest.raises(FileNotFoundError) as excinfo:
        load_config(vault_path=str(tmp_path / "no-such.json"))
    assert "setup" in str(excinfo.value).lower()


def test_load_config_missing_credentials(tmp_path: Path) -> None:
    """A vault that exists but lacks the required keys raises KeyError."""
    vault = tmp_path / "vault.json"
    _write_vault(vault, {"webrelay": {"bearer_token": "tok"}})  # no server_url
    with pytest.raises(KeyError):
        load_config(vault_path=str(vault))


# ---------------------------------------------------------------------------
# RelayClient.register_handler + send
# ---------------------------------------------------------------------------


def _run_sync(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_register_handler_accepts_multiple_per_op() -> None:
    """Multiple handlers per op are allowed; both are stored."""
    client = RelayClient("wss://x", "tok", _Hello())

    async def _h1(env: Envelope, payload: BaseModel) -> None:
        return None

    async def _h2(env: Envelope, payload: BaseModel) -> None:
        return None

    client.register_handler(Op.PONG, _h1)
    client.register_handler(Op.PONG, _h2)
    # Internal list should have both handlers in registration order.
    assert client._handlers[Op.PONG] == [_h1, _h2]  # noqa: SLF001


def test_send_writes_envelope_to_websocket() -> None:
    """``send`` builds a wire-format envelope and writes it to the socket."""
    client = RelayClient("wss://x", "tok", _Hello())
    fake = _FakeWebSocket()
    client._ws = fake  # noqa: SLF001 - inject the fake for the test

    # Use the ``pong`` op with the matching ``Pong`` payload — the
    # wire format is round-tripped through ``parse_envelope`` below,
    # which validates payloads against the op's model.
    _run_sync(client.send(Op.PONG, Pong()))

    assert len(fake.sent) == 1
    envelope, decoded_payload = parse_envelope(fake.sent[0])
    assert envelope.op == Op.PONG
    # ``Pong`` has no required fields, so the parsed payload is a
    # default-constructed ``Pong``.
    assert isinstance(decoded_payload, Pong)
    # A fresh correlation id was generated (not None, hex length 32).
    assert envelope.id is not None
    assert len(envelope.id) == 32
    int(envelope.id, 16)  # must be valid hex


def test_send_uses_provided_correlation_id() -> None:
    """When ``correlation_id`` is given, it is preserved on the wire."""
    client = RelayClient("wss://x", "tok", _Hello())
    fake = _FakeWebSocket()
    client._ws = fake  # noqa: SLF001

    _run_sync(client.send(Op.PONG, Pong(), correlation_id="caller-id-001"))
    envelope, _ = parse_envelope(fake.sent[0])
    assert envelope.id == "caller-id-001"


def test_send_queues_and_warns_when_socket_down() -> None:
    """``send`` with no live socket queues the frame and emits a warning."""
    client = RelayClient("wss://x", "tok", _Hello())
    assert client._ws is None  # noqa: SLF001

    with pytest.warns(RuntimeWarning, match="relay socket down"):
        _run_sync(client.send(Op.PONG, Pong()))

    # The frame is in the internal queue, not lost.
    assert len(client._send_queue) == 1  # noqa: SLF001
    # And it is a parseable envelope so the next ``_flush_queue`` call
    # can send it.
    envelope, _ = parse_envelope(client._send_queue[0])  # noqa: SLF001
    assert envelope.op == Op.PONG


def test_respond_reuses_envelope_id() -> None:
    """``respond`` writes a frame whose id matches the inbound envelope."""
    client = RelayClient("wss://x", "tok", _Hello())
    fake = _FakeWebSocket()
    client._ws = fake  # noqa: SLF001

    # Use a ping envelope as the inbound so the responder sends a
    # pong back; both op and payload round-trip through parse_envelope.
    inbound = Envelope(
        op=Op.PING,
        id="request-7",
        ts=0.0,
        payload={},
    )
    _run_sync(client.respond(inbound, Pong()))
    envelope, _ = parse_envelope(fake.sent[0])
    assert envelope.id == "request-7"
    # Op is taken from the inbound envelope.
    assert envelope.op == Op.PING


def test_send_build_envelope_helper_used() -> None:
    """Sanity check: the wire format is what ``build_envelope`` produces.

    This guards against accidental drift between the L1 implementation
    and the protocol's reference builder. We compare the parsed
    envelopes rather than the raw JSON strings, because ``ts`` is the
    current wall-clock and differs between two ``build_envelope`` calls.
    """
    client = RelayClient("wss://x", "tok", _Hello())
    fake = _FakeWebSocket()
    client._ws = fake  # noqa: SLF001

    payload = Pong()
    _run_sync(client.send(Op.PONG, payload, correlation_id="abc"))
    expected = build_envelope(Op.PONG, payload, id="abc")
    actual_env, actual_payload = parse_envelope(fake.sent[0])
    expected_env, expected_payload = parse_envelope(expected)
    assert actual_env.id == expected_env.id == "abc"
    assert actual_env.op == expected_env.op == Op.PONG
    assert type(actual_payload) is type(expected_payload) is Pong
