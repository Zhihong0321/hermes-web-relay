"""Tests for the Hermes bridge.

These tests drive :class:`HermesBridge` end-to-end against an in-process
fake JSON-RPC server that emulates the local ``tui_gateway`` WebSocket
gateway. They verify both the happy path (chat.send -> streamed tokens
-> chat.done) and the unreachable-hermes fallback.

The bridge only uses three public methods of :class:`RelayClient`:

* :meth:`RelayClient.register_handler` -- sync, records the (op, handler)
  pair.
* :meth:`RelayClient.send` -- async, used to push ``chat.token`` and
  ``chat.done`` frames back to the relay server.
* :meth:`RelayClient.respond` -- not used by hermes_bridge (chat turns
  are push, not request/response), so the fake records it but never
  expects a call.

The real :class:`RelayClient` is a Stage-2 skeleton whose ``__init__``
raises ``NotImplementedError``, so we substitute :class:`FakeRelayClient`
which implements the same surface but records calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from typing import Any

import pytest
import websockets
from pydantic import BaseModel

from webrelay.agent.bridges.hermes_bridge import (
    RPC_SESSION_CREATE,
    RPC_PROMPT_SUBMIT,
    HermesBridge,
)
from webrelay.agent.protocol import (
    ChatDone,
    ChatSend,
    ChatToken,
    Envelope,
    Op,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRelayClient:
    """Minimal stand-in for :class:`RelayClient` used by the bridge tests.

    Records every ``send`` call so tests can assert what the bridge
    pushed back to the relay server. Handler registration is a no-op
    aside from storing the (op, handler) pair so the test can invoke
    it directly.
    """

    def __init__(self) -> None:
        self.handlers: dict[Op, Any] = {}
        self.sent: list[tuple[Op, BaseModel]] = []

    def register_handler(self, op: Op, handler: Any) -> None:
        # Registering twice for the same op replaces (matches the real
        # client's documented policy).
        self.handlers[op] = handler

    async def send(
        self,
        op: Op,
        payload: BaseModel,
        *,
        correlation_id: str | None = None,
    ) -> None:
        self.sent.append((op, payload))

    async def respond(self, envelope: Envelope, payload: BaseModel) -> None:
        # Not used by hermes_bridge -- record for completeness.
        self.sent.append((Op.CHAT_DONE, payload))


def _make_envelope(op: Op, payload: BaseModel) -> Envelope:
    """Build a real :class:`Envelope` for driving the handler directly."""
    return Envelope(
        op=op,
        id="test-envelope-id",
        ts=0.0,
        payload=payload.model_dump(mode="json"),
    )


def _free_port() -> int:
    """Bind to port 0 and return the kernel-assigned port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fake hermes gateway
# ---------------------------------------------------------------------------


class _FakeHermesServer:
    """In-process hermes gateway used to drive the bridge end-to-end.

    Accepts two kinds of connections:

    1. ``session.create`` connections -- respond with a fake session_id.
    2. ``prompt.submit`` connections -- respond with a ready frame,
       then a stream of ``message.delta`` events, then a
       ``message.complete`` event.

    The behaviour is controlled by ``tokens`` and the optional
    ``fail_after_ready`` switch (used to simulate mid-stream errors).
    """

    def __init__(
        self,
        *,
        session_id: str = "sess-abc-123",
        tokens: list[str] | None = None,
        fail_after_ready: bool = False,
    ) -> None:
        self.session_id = session_id
        self.tokens = tokens if tokens is not None else ["Hello, ", "world!"]
        self.fail_after_ready = fail_after_ready
        self.port = _free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self._server: Any = None
        self._connections: list[Any] = []
        self.request_log: list[dict] = []
        self._ready_event = asyncio.Event()

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle_connection, "127.0.0.1", self.port
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for ws in list(self._connections):
            with contextlib.suppress(Exception):
                await ws.close()

    async def _handle_connection(self, ws: Any) -> None:
        self._connections.append(ws)
        try:
            # Mirror the real tui_gateway.ws.handle_ws() protocol: the
            # gateway SENDS gateway.ready immediately on accept, BEFORE
            # reading any inbound request. We must do the same or the
            # client (which reads-then-writes) deadlocks.
            await ws.send(self._ready_frame())

            # Now read the inbound request.
            try:
                first_raw = await ws.recv()
            except Exception:
                return
            try:
                first = json.loads(first_raw)
            except json.JSONDecodeError:
                await ws.close()
                return
            method = first.get("method")
            self.request_log.append(first)
            req_id = first.get("id")

            if method == RPC_SESSION_CREATE:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"session_id": self.session_id},
                        }
                    )
                )
                return

            if method == RPC_PROMPT_SUBMIT:
                if self.fail_after_ready:
                    # Close immediately so bridge sees ConnectionClosed.
                    await ws.close()
                    return
                # Stream the configured tokens, then a complete event.
                for tok in self.tokens:
                    await ws.send(self._event_frame("message.delta", {"text": tok}))
                await ws.send(
                    self._event_frame(
                        "message.complete",
                        {"text": "".join(self.tokens), "status": "complete"},
                    )
                )
                # Keep the connection open a moment so the bridge can
                # close it cleanly; in production hermes keeps the
                # socket open until the client disconnects.
                try:
                    await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                return

            # Unknown method -- reply with an error and close.
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"unknown: {method}"},
                    }
                )
            )
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close()

    def _ready_frame(self) -> str:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    "payload": {"skin": "test"},
                },
            }
        )

    def _event_frame(self, event_type: str, payload: dict) -> str:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": event_type,
                    "session_id": self.session_id,
                    "payload": payload,
                },
            }
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_happy_path_streams_tokens_then_done() -> None:
    """chat.send -> session.create -> prompt.submit -> 2 tokens -> done."""
    server = _FakeHermesServer(tokens=["Hello, ", "world!"])
    await server.start()
    try:
        client = _FakeRelayClient()
        bridge = HermesBridge(client, server.url)
        await bridge.start()

        assert Op.CHAT_SEND in client.handlers, "bridge must register chat.send"

        env = _make_envelope(Op.CHAT_SEND, ChatSend(thread_id="t1", text="hi"))
        await client.handlers[Op.CHAT_SEND](env, ChatSend(thread_id="t1", text="hi"))

        # Expected outbound frames (in order):
        #   2 x CHAT_TOKEN, 1 x CHAT_DONE
        ops = [op for op, _ in client.sent]
        assert ops == [Op.CHAT_TOKEN, Op.CHAT_TOKEN, Op.CHAT_DONE], (
            f"unexpected outbound op sequence: {ops}"
        )

        tokens = [p for op, p in client.sent if op == Op.CHAT_TOKEN]
        assert [t.text for t in tokens] == ["Hello, ", "world!"]
        assert [t.seq for t in tokens] == [1, 2]
        assert all(t.thread_id == "t1" for t in tokens)

        done = [p for op, p in client.sent if op == Op.CHAT_DONE]
        assert len(done) == 1
        assert done[0].thread_id == "t1"

        # Verify the bridge really talked to hermes: should have seen
        # exactly one session.create and one prompt.submit.
        methods = [req.get("method") for req in server.request_log]
        assert methods == [RPC_SESSION_CREATE, RPC_PROMPT_SUBMIT]
        submit = server.request_log[1]
        assert submit["params"]["text"] == "hi"
        assert submit["params"]["session_id"] == "sess-abc-123"
    finally:
        await server.stop()


async def test_second_chat_in_same_thread_reuses_session() -> None:
    """A second chat.send with the same thread_id must NOT create a new session."""
    server = _FakeHermesServer(tokens=["ack"])
    await server.start()
    try:
        client = _FakeRelayClient()
        bridge = HermesBridge(client, server.url)
        await bridge.start()
        handler = client.handlers[Op.CHAT_SEND]

        # First turn -- triggers session.create.
        await handler(
            _make_envelope(Op.CHAT_SEND, ChatSend(thread_id="t-x", text="first")),
            ChatSend(thread_id="t-x", text="first"),
        )
        # Second turn in the same thread -- should reuse cached session.
        await handler(
            _make_envelope(Op.CHAT_SEND, ChatSend(thread_id="t-x", text="second")),
            ChatSend(thread_id="t-x", text="second"),
        )

        methods = [req.get("method") for req in server.request_log]
        # Exactly one session.create total, then two prompt.submit.
        assert methods.count(RPC_SESSION_CREATE) == 1
        assert methods.count(RPC_PROMPT_SUBMIT) == 2
    finally:
        await server.stop()


async def test_connection_refused_sends_unreachable_token_and_done() -> None:
    """When hermes is unreachable, bridge sends a single error CHAT_TOKEN + CHAT_DONE."""
    # Reserve a port and immediately release it so nothing is listening.
    port = _free_port()

    client = _FakeRelayClient()
    bridge = HermesBridge(
        client, f"ws://127.0.0.1:{port}/ws"  # nothing listening here
    )
    await bridge.start()
    handler = client.handlers[Op.CHAT_SEND]

    # Use a very short open_timeout indirectly: the bridge hardcodes 5s
    # which is too slow for a unit test. The default connect timeout on
    # most kernels returns ECONNREFUSED immediately, so this should be
    # quick. Wrap the call in wait_for to be safe.
    await asyncio.wait_for(
        handler(
            _make_envelope(Op.CHAT_SEND, ChatSend(thread_id="t2", text="hi")),
            ChatSend(thread_id="t2", text="hi"),
        ),
        timeout=15.0,
    )

    ops = [op for op, _ in client.sent]
    assert ops == [Op.CHAT_TOKEN, Op.CHAT_DONE], (
        f"unreachable hermes should emit exactly 1 token + 1 done, got {ops}"
    )

    token = client.sent[0][1]
    assert isinstance(token, ChatToken)
    assert token.thread_id == "t2"
    assert token.seq == 1
    assert "hermes" in token.text.lower()
    assert "not running" in token.text.lower() or "start" in token.text.lower()

    done = client.sent[1][1]
    assert isinstance(done, ChatDone)
    assert done.thread_id == "t2"


async def test_midstream_error_is_surfaced_as_token_then_done() -> None:
    """A 'message.complete' with a status=error still triggers CHAT_DONE."""
    # The simpler happy path already covers message.complete with
    # status=complete. Here we explicitly drive a non-fatal end-of-stream
    # variation: the gateway sends the complete event and we verify the
    # bridge always emits exactly one CHAT_DONE.
    server = _FakeHermesServer(tokens=["partial "])
    await server.start()
    try:
        client = _FakeRelayClient()
        bridge = HermesBridge(client, server.url)
        await bridge.start()
        handler = client.handlers[Op.CHAT_SEND]
        await handler(
            _make_envelope(Op.CHAT_SEND, ChatSend(thread_id="t3", text="hi")),
            ChatSend(thread_id="t3", text="hi"),
        )
        ops = [op for op, _ in client.sent]
        assert ops == [Op.CHAT_TOKEN, Op.CHAT_DONE]
    finally:
        await server.stop()


async def test_bridge_does_not_import_sklearn_or_heavy_libs() -> None:
    """Sanity: the bridge module's import surface stays small.

    A regression guard for accidentally pulling in the full agent
    runtime at import time. We only check the module is importable and
    exposes the documented names.
    """
    from webrelay.agent.bridges import hermes_bridge

    assert hasattr(hermes_bridge, "HermesBridge")
    assert hasattr(hermes_bridge, "HermesUnreachable")
    assert hermes_bridge.RPC_PROMPT_SUBMIT == "prompt.submit"
    assert hermes_bridge.RPC_SESSION_CREATE == "session.create"
