"""End-to-end tests for the implemented ``RelayHub``.

Each test wires a tiny fake ``WebSocket`` (just an async ``send_text``
that records the frame) onto a real :class:`RelayHub`, then drives
``on_inbound`` directly to simulate agent responses.

The fake websocket intentionally is **not** an asyncio mock -- we use
a real list to collect sent frames so we can assert on the exact wire
format. The hub's contract is "I send this string, you reply with that
string," and a list-collecting fake makes that contract directly
testable.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from webrelay.server.protocol import (
    ChatToken,
    FileRead,
    FileResult,
    build_envelope,
)
from webrelay.server.relay_hub import RelayHub


# ---------------------------------------------------------------------------
# Fake websocket
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Minimal async-send stub that records every frame.

    The hub's only outbound call is ``await websocket.send_text(frame)``
    (see ``FastAPI.WebSocket``'s API). It does not need to model
    ``receive`` -- inbound traffic is driven by the test via
    ``hub.on_inbound``.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        # If set, send_text raises this on the next call.
        self.send_exc: BaseException | None = None

    async def send_text(self, frame: str) -> None:
        if self.send_exc is not None:
            exc, self.send_exc = self.send_exc, None
            raise exc
        self.sent.append(frame)

    def last_sent(self) -> dict[str, Any]:
        """Return the last sent frame parsed as JSON (for assertions)."""
        return json.loads(self.sent[-1])

    def frames_of(self, op: str) -> list[dict[str, Any]]:
        """Return all sent frames whose ``op`` matches."""
        return [json.loads(f) for f in self.sent if json.loads(f)["op"] == op]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _attach(hub: RelayHub, ws: FakeWebSocket) -> None:
    """Attach a fake socket to a fresh hub using a real Hello payload."""
    from webrelay.server.protocol import Hello

    await hub.attach(ws, Hello(agent_version="test", host="t", platform="t"))


def _reply_for(request_id: str, op: str, payload: BaseModel) -> str:
    """Build a wire reply that matches the request's correlation id."""
    return build_envelope(op, payload, id=request_id)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_is_connected_reflects_attach_and_detach() -> None:
    hub: RelayHub = RelayHub()
    ws = FakeWebSocket()
    assert hub.is_connected() is False
    await _attach(hub, ws)
    assert hub.is_connected() is True
    await hub.detach(ws)
    assert hub.is_connected() is False


async def test_attach_rejects_second_client() -> None:
    hub = RelayHub()
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()
    await _attach(hub, ws1)
    with pytest.raises(ValueError):
        await _attach(hub, ws2)
    # First client is still attached.
    assert hub.is_connected() is True


async def test_attach_sends_server_hello_ack() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)
    hello_frames = ws.frames_of("hello")
    assert len(hello_frames) == 1
    assert hello_frames[0]["op"] == "hello"
    assert isinstance(hello_frames[0]["id"], str) and hello_frames[0]["id"]


# ---------------------------------------------------------------------------
# request / response
# ---------------------------------------------------------------------------


async def test_request_resolves_on_matching_reply() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)
    # Clear the hello frame so "sent" is just the request frame.
    ws.sent.clear()

    task = asyncio.create_task(
        hub.request(Op_FILE_READ := "file.read", FileRead(path="x"))
    )
    # Yield to let request() register its future and send the frame.
    await asyncio.sleep(0)
    assert len(ws.sent) == 1
    sent = ws.last_sent()
    assert sent["op"] == "file.read"
    cid = sent["id"]
    assert isinstance(cid, str) and len(cid) >= 16  # uuid4 hex is 32 chars
    assert sent["payload"] == {"path": "x"}

    # Simulate the agent's reply.
    await hub.on_inbound(
        _reply_for(cid, "file.result", FileResult(path="x", kind="file", content="hi"))
    )

    reply = await asyncio.wait_for(task, timeout=1.0)
    assert isinstance(reply, FileResult)
    assert reply.content == "hi"
    assert reply.path == "x"


async def test_request_correlation_id_is_unique_per_call() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)
    ws.sent.clear()

    t1 = asyncio.create_task(hub.request("file.read", FileRead(path="a")))
    t2 = asyncio.create_task(hub.request("file.read", FileRead(path="b")))
    await asyncio.sleep(0)
    ids = [json.loads(f)["id"] for f in ws.sent]
    assert len(ids) == 2
    assert ids[0] != ids[1]


async def test_request_raises_timeout_when_no_reply() -> None:
    hub = RelayHub(request_timeout_s=0.05)
    ws = FakeWebSocket()
    await _attach(hub, ws)
    ws.sent.clear()

    with pytest.raises(asyncio.TimeoutError):
        await hub.request("file.read", FileRead(path="x"))

    # After timeout, the future is no longer pending (no leak).
    assert not hub._pending  # type: ignore[attr-defined]


async def test_request_raises_connection_error_when_not_attached() -> None:
    hub = RelayHub()
    with pytest.raises(ConnectionError):
        await hub.request("file.read", FileRead(path="x"))


async def test_on_inbound_drops_unknown_id_without_subscribers() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)
    # Just should not raise; the frame is silently dropped.
    await hub.on_inbound(_reply_for("nope", "file.result", FileResult(path=".", kind="error", error="x")))


async def test_on_inbound_malformed_frame_is_dropped() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)
    # Bad JSON -- should not raise.
    await hub.on_inbound("not json at all")
    # Unknown op -- should not raise.
    await hub.on_inbound('{"op": "nope.unknown", "id": "x", "ts": 0, "payload": {}}')
    # Missing required field -- should not raise.
    await hub.on_inbound('{"op": "file.read", "id": "x", "ts": 0, "payload": {}}')


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


async def test_push_writes_frame_without_future() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)
    ws.sent.clear()

    await hub.push("chat.token", ChatToken(thread_id="t", text="hi", seq=1))
    assert len(ws.sent) == 1
    sent = ws.last_sent()
    assert sent["op"] == "chat.token"
    assert sent["payload"] == {"thread_id": "t", "text": "hi", "seq": 1}
    # No future is created for a push.
    assert not hub._pending  # type: ignore[attr-defined]


async def test_push_raises_when_not_attached() -> None:
    hub = RelayHub()
    with pytest.raises(ConnectionError):
        await hub.push("chat.token", ChatToken(thread_id="t", text="x", seq=0))


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


async def test_subscribe_yields_pushed_events() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)

    gen = hub.subscribe("chat.token")
    assert hasattr(gen, "__anext__")

    # Concurrently push an event while the generator is awaiting.
    async def _push_after() -> None:
        await asyncio.sleep(0)
        await hub.on_inbound(
            build_envelope("chat.token", ChatToken(thread_id="t", text="Hel", seq=1))
        )

    pusher = asyncio.create_task(_push_after())
    token = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    await pusher
    assert isinstance(token, ChatToken)
    assert token.text == "Hel"
    assert token.seq == 1

    # Closing the generator should remove its queue.
    await gen.aclose()
    assert "chat.token" not in hub._subscribers  # type: ignore[attr-defined]


async def test_subscribe_ignores_other_ops() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)

    gen = hub.subscribe("chat.token")
    # Drive the generator once so it actually registers its queue.
    # It will block on queue.get(); schedule a non-matching push +
    # a cancel via detach so it returns.
    waiter = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)  # let __anext__ park
    assert "chat.token" in hub._subscribers  # type: ignore[attr-defined]

    # A non-matching op must not be delivered to this subscriber.
    await hub.on_inbound(
        build_envelope("file.result", FileResult(path=".", kind="file", content="x"))
    )
    q = hub._subscribers["chat.token"][0]  # type: ignore[attr-defined]
    assert q.empty()

    # Unblock the waiter by detaching (sentinel).
    await hub.detach(ws)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiter, timeout=1.0)


async def test_detach_unblocks_subscribers() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)
    gen = hub.subscribe("chat.token")
    # The generator is parked on queue.get() in another task. Detach
    # should push the shutdown sentinel and let it return.
    waiter = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)  # let __anext__ start waiting
    await hub.detach(ws)
    # The generator returns (StopAsyncIteration) instead of yielding a payload.
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiter, timeout=1.0)


# ---------------------------------------------------------------------------
# detach
# ---------------------------------------------------------------------------


async def test_detach_cancels_pending_request_with_connection_error() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)

    task = asyncio.create_task(hub.request("file.read", FileRead(path="x")))
    await asyncio.sleep(0)
    assert len(hub._pending) == 1  # type: ignore[attr-defined]

    await hub.detach(ws)
    with pytest.raises(ConnectionError):
        await task

    # State cleared.
    assert not hub._pending  # type: ignore[attr-defined]
    assert not hub._subscribers  # type: ignore[attr-defined]
    assert not hub.is_connected()


async def test_detach_when_not_attached_is_a_noop() -> None:
    hub = RelayHub()
    ws = FakeWebSocket()
    # Should not raise even though no one is attached.
    await hub.detach(ws)
    assert not hub.is_connected()


async def test_pending_futures_cleared_after_successful_request() -> None:
    """After a request resolves, its slot in _pending is freed."""
    hub = RelayHub()
    ws = FakeWebSocket()
    await _attach(hub, ws)
    ws.sent.clear()

    task = asyncio.create_task(hub.request("file.read", FileRead(path="x")))
    await asyncio.sleep(0)
    cid = ws.last_sent()["id"]
    await hub.on_inbound(
        _reply_for(cid, "file.result", FileResult(path="x", kind="file", content="y"))
    )
    await task
    assert not hub._pending  # type: ignore[attr-defined]
