"""Server-side WebSocket relay hub.

This module defines the public API of the server-side relay hub. The Coolify
FastAPI server instantiates a single ``RelayHub`` and uses it to:

* Accept the inbound WebSocket connection from the local agent (the single
  client we expect to be attached at a time).
* Correlate request/response cycles over the single multiplexed WebSocket.
  The server sends an op with a correlation ``id`` and the local agent
  replies with a matching ``id``; ``RelayHub.request()`` is an awaitable
  that resolves when the correlated reply arrives (or times out).
* Fan-out unsolicited pushes (e.g. ``chat.token`` stream events) to
  in-process subscribers, including the Server-Sent-Event (SSE) handlers
  used by the HTMX front-end.
* Reject all activity unless exactly one client is attached.

The implementation (Stage 2) keeps the public surface declared in the
skeleton. It maintains:

* ``_websocket``         -- the single attached FastAPI/Starlette WS.
* ``_pending``           -- ``dict[correlation_id -> asyncio.Future[BaseModel]]``
                            for in-flight request/response cycles.
* ``_subscribers``       -- ``dict[Op -> list[asyncio.Queue[BaseModel]]]``
                            for fan-out of push events.
* ``_lock``              -- guards mutations of the above dicts from
                            concurrent coroutines (the FastAPI event loop
                            runs request / on_inbound / subscribe / detach
                            concurrently).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

# ``Op`` and ``Envelope`` are defined by C1 in ``webrelay.server.protocol``.
# We re-import them lazily inside the methods that need them so this skeleton
# module remains importable even if C1 has not yet committed the protocol
# module. The signatures themselves are part of the public contract.
try:  # pragma: no cover - exercised by S2 + later stages
    from webrelay.server.protocol import (  # type: ignore[attr-defined]
        Envelope,
        ErrorPayload,
        Hello,
        Op,
        PAYLOAD_MAP,
        Pong,
        ProtocolError,
        build_envelope,
        parse_envelope,
    )
except ImportError:  # pragma: no cover - skeleton boot
    Op = Any  # type: ignore[assignment,misc]
    Envelope = Any  # type: ignore[assignment,misc]
    ErrorPayload = Any  # type: ignore[assignment]
    Hello = Any  # type: ignore[assignment]
    Pong = Any  # type: ignore[assignment]
    ProtocolError = Any  # type: ignore[assignment]
    PAYLOAD_MAP = {}  # type: ignore[assignment]
    build_envelope = None  # type: ignore[assignment]
    parse_envelope = None  # type: ignore[assignment]


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal sentinel: signals a subscriber queue that the hub is shutting down.
# ---------------------------------------------------------------------------


_SHUTDOWN = object()


class _RelayHubError(RuntimeError):
    """Raised internally for protocol violations.

    Kept private for now; the public-facing errors callers actually see
    are :class:`asyncio.TimeoutError` (request timed out) and the
    :class:`ConnectionError` raised by :meth:`detach` / :meth:`request`
    when no socket is attached.
    """


class RelayHub:
    """Multiplexes a single inbound WS connection from the local agent.

    The hub is **single-client by design**: only one local agent may be
    attached at a time. A second ``attach()`` is rejected with
    :class:`ValueError` (after a structured log line) so a misbehaving
    second client cannot quietly steal the link.

    Public surface (this is the contract every Stage-2 feature agent codes
    against):

    * :meth:`attach` / :meth:`detach` — lifecycle hooks called by the
      ``/api/relay/ws`` endpoint after the bearer-token check passes.
    * :meth:`is_connected` — cheap boolean for the nav-bar "Connected"
      badge in ``base.html``.
    * :meth:`request` — server-initiated correlated RPC. Returns the
      correlated reply payload (Pydantic model) or raises on timeout.
    * :meth:`push` — fire-and-forget. Returns once the frame has been
      written to the socket; it does NOT wait for an ack.
    * :meth:`on_inbound` — every frame the WS endpoint reads is handed
      here for routing.
    * :meth:`subscribe` — async generator yielding inbound pushes of a
      given op. Used by SSE endpoints (chat stream, ledger change feed).
    """

    def __init__(self, *, request_timeout_s: float = 30.0) -> None:
        """Initialize the hub."""
        self.request_timeout_s: float = request_timeout_s
        # Active websocket (None when no agent is attached).
        self._websocket: Any = None
        # Hello frame sent by the agent at attach time; surfaced for ops.
        self._hello: BaseModel | None = None
        # Outstanding request/response futures, keyed by correlation id.
        self._pending: dict[str, asyncio.Future[BaseModel]] = {}
        # Per-op list of subscriber queues.
        self._subscribers: dict[Op, list[asyncio.Queue[BaseModel | object]]] = {}
        # Guards mutations of _pending and _subscribers.
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def attach(self, websocket: Any, hello_payload: BaseModel) -> None:
        """Accept a newly-authenticated WS connection from the local agent.

        Rejects (with :class:`ValueError`) if another agent is already
        attached. On success, stores the socket, records the agent's
        hello frame, sends the server-side hello ack, and returns.
        """
        async with self._lock:
            if self._websocket is not None:
                _log.warning(
                    "relay_hub.attach: refusing second client; one is already attached"
                )
                raise ValueError(
                    "an agent is already attached; detach the existing connection first"
                )
            self._websocket = websocket
            self._hello = hello_payload

        # Send the server-side hello ack outside the lock so a slow send
        # does not block other operations. (At this point we are the
        # single client; no one else is racing us.)
        try:
            server_hello = Hello(
                agent_version="0.0.0",  # server side; agent does not consume this
                hermes_endpoint=None,
                host="server",
                platform="coolify",
            )
        except Exception:  # pragma: no cover - defensive if Hello is mocked
            server_hello = None  # type: ignore[assignment]

        if server_hello is not None and build_envelope is not None:
            try:
                await websocket.send_text(build_envelope(Op.HELLO, server_hello))
            except Exception as exc:  # pragma: no cover - best-effort ack
                _log.warning("relay_hub.attach: hello ack send failed: %s", exc)

    async def detach(self, websocket: Any) -> None:
        """Tear down a WS connection and fail any in-flight requests.

        Cancels every pending :meth:`request` future with
        :class:`ConnectionError`, sends a shutdown sentinel to every
        subscriber queue so SSE consumers unblock cleanly, and clears
        all hub state.
        """
        async with self._lock:
            # Only clear state if the websocket we're being asked to
            # detach is the one we currently hold. A second client
            # hitting attach/detach races are still safe.
            if self._websocket is not None and (
                websocket is None or self._websocket is websocket
            ):
                # 1) Cancel pending requests.
                pending = list(self._pending.items())
                self._pending.clear()
                # 2) Snapshot subscribers so we can close them outside
                #    the lock.
                subs = [(op, list(qs)) for op, qs in self._subscribers.items()]
                self._subscribers.clear()
                # 3) Clear the socket / hello.
                self._websocket = None
                self._hello = None
            else:
                pending = []
                subs = []

        for cid, fut in pending:
            if not fut.done():
                fut.set_exception(
                    ConnectionError("relay hub detached before reply arrived")
                )
        for op, queues in subs:
            for q in queues:
                try:
                    q.put_nowait(_SHUTDOWN)
                except asyncio.QueueFull:  # pragma: no cover - unbounded
                    pass

    def is_connected(self) -> bool:
        """Return ``True`` iff exactly one agent is currently attached."""
        return self._websocket is not None

    # ------------------------------------------------------------------
    # Server-initiated traffic
    # ------------------------------------------------------------------

    async def request(self, op: Op, payload: BaseModel) -> BaseModel:
        """Send a request op and await the correlated reply.

        Generates a uuid4 hex correlation id, sends the envelope over
        the active websocket, registers a future, and resolves with the
        matching reply's payload Pydantic model. Raises
        :class:`asyncio.TimeoutError` after ``request_timeout_s`` if no
        reply arrives, and :class:`ConnectionError` if no client is
        attached.
        """
        if self._websocket is None:
            raise ConnectionError("relay hub has no attached agent")
        if build_envelope is None:  # pragma: no cover - protocol not loaded
            raise RuntimeError("wire protocol module is not importable")

        correlation_id = uuid.uuid4().hex
        frame = build_envelope(op, payload, id=correlation_id)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[BaseModel] = loop.create_future()

        async with self._lock:
            # Re-check under the lock in case detach raced us.
            if self._websocket is None:
                raise ConnectionError("relay hub detached before send")
            self._pending[correlation_id] = future

        try:
            await self._websocket.send_text(frame)
        except Exception:
            # Send failed: drop the future so we do not leak it.
            async with self._lock:
                self._pending.pop(correlation_id, None)
            if not future.done():
                future.cancel()
            raise

        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout_s)
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending.pop(correlation_id, None)
            _log.warning(
                "relay_hub.request: timed out waiting for %s id=%s",
                op,
                correlation_id,
            )
            raise

    async def push(self, op: Op, payload: BaseModel) -> None:
        """Fire-and-forget send. No correlation id; no reply awaited.

        Raises :class:`ConnectionError` if no client is attached -- push
        must never silently drop frames.
        """
        if self._websocket is None:
            raise ConnectionError("relay hub has no attached agent")
        if build_envelope is None:  # pragma: no cover - protocol not loaded
            raise RuntimeError("wire protocol module is not importable")

        frame = build_envelope(op, payload)
        await self._websocket.send_text(frame)

    # ------------------------------------------------------------------
    # Inbound dispatch
    # ------------------------------------------------------------------

    async def on_inbound(self, raw: str) -> None:
        """Route a single inbound WS frame.

        1. Parse + validate the envelope + payload.
        2. If a matching ``request()`` future exists, resolve it.
        3. Otherwise, fan out the payload to every subscriber queue
           registered for the envelope's op.
        4. Drop malformed frames with a structured log line; never
           crash the WS read loop on a single bad frame.
        """
        if parse_envelope is None or PAYLOAD_MAP is None:  # pragma: no cover
            return

        try:
            envelope, payload_obj = parse_envelope(raw)
        except ProtocolError as exc:
            _log.warning(
                "relay_hub.on_inbound: protocol error code=%s msg=%s",
                exc.code,
                exc.message,
            )
            return
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError etc.
            _log.warning("relay_hub.on_inbound: failed to parse frame: %s", exc)
            return

        # 1) Correlated reply?
        async with self._lock:
            future = self._pending.pop(envelope.id, None)

        if future is not None:
            if envelope.op == Op.ERROR:
                # Agent reported a protocol/handler error for a prior
                # request -- resolve the future with an exception so
                # the caller surfaces a 502-ish failure.
                if not future.done():
                    err_payload = payload_obj  # ErrorPayload
                    msg = getattr(err_payload, "message", "agent reported error")
                    future.set_exception(_RelayHubError(str(msg)))
                return
            if not future.done():
                future.set_result(payload_obj)
            return

        # 2) Fan-out to subscribers.
        async with self._lock:
            queues = list(self._subscribers.get(envelope.op, ()))
        if not queues:
            _log.debug(
                "relay_hub.on_inbound: no subscribers for op=%s id=%s",
                envelope.op,
                envelope.id,
            )
            return
        for q in queues:
            try:
                q.put_nowait(payload_obj)
            except asyncio.QueueFull:  # pragma: no cover - unbounded
                _log.warning(
                    "relay_hub.on_inbound: subscriber queue full; dropping op=%s",
                    envelope.op,
                )

    async def subscribe(self, op: Op) -> AsyncIterator[BaseModel]:  # type: ignore[override]
        """Async-iterator of inbound pushes for ``op``.

        Each :meth:`on_inbound` frame whose envelope op matches delivers
        the parsed payload to every queue registered for that op. The
        generator removes its queue on close (try/finally) so a browser
        closing the EventSource cleans up after itself.
        """
        queue: asyncio.Queue[BaseModel | object] = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(op, []).append(queue)
        try:
            while True:
                item = await queue.get()
                if item is _SHUTDOWN:
                    return
                assert isinstance(item, BaseModel)  # noqa: S101 - sanity
                yield item
        finally:
            async with self._lock:
                lst = self._subscribers.get(op)
                if lst is not None:
                    try:
                        lst.remove(queue)
                    except ValueError:
                        pass
                    if not lst:
                        del self._subscribers[op]


__all__ = ["RelayHub"]
