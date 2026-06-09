"""Local-agent WebSocket client.

This module defines the public API of the local agent's outbound WS
client. The agent process (started by ``python -m webrelay_agent``) owns
one ``RelayClient`` instance, configured from :mod:`webrelay.agent.config`,
which:

* Dials the Coolify server's ``/api/relay/ws`` endpoint with an
  ``Authorization: Bearer <token>`` header.
* Persists across network blips using the backoff schedule in
  :mod:`webrelay.agent.reconnect`.
* Maintains a 30 s heartbeat (``ping``/``pong``) so dead sockets are
  detected promptly.
* Dispatches inbound frames to per-op handlers registered by the bridge
  modules (``hermes_bridge``, ``ledger_bridge``, ``file_bridge``,
  ``approval_bridge``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from pydantic import BaseModel
from websockets.exceptions import ConnectionClosed

# ``Op`` and ``Envelope`` are defined in ``webrelay.agent.protocol`` (a
# byte-identical copy of ``webrelay.server.protocol``). We import them
# eagerly — every deployment of this module has C1 already in place.
from webrelay.agent.protocol import (
    PAYLOAD_MAP,
    Envelope,
    Op,
    Pong,
    ProtocolError,
    build_envelope,
    parse_envelope,
)
from webrelay.agent.reconnect import reconnect_backoff


log = logging.getLogger(__name__)


# A handler receives the parsed Envelope (so it can inspect
# ``envelope.id`` to respond to a correlated request) and the parsed
# payload Pydantic model for the op.
Handler = Callable[[Envelope, BaseModel], Awaitable[None]]


# Heartbeat cadence. 30 s is a balance between the AWS-recommended
# ``< 60 s`` and our server's ``request_timeout_s=30`` default.
PING_INTERVAL_S: float = 30.0


class RelayClient:
    """Outbound WebSocket client to the Coolify server.

    One process owns one instance. The instance is the only object that
    touches the socket; bridges register handlers on it and call
    :meth:`send` to talk to the server.

    Public surface (this is the contract every Stage-2 bridge agent
    codes against):

    * :meth:`run` — connect-loop with exponential backoff. Never returns
      under normal operation; cancellation triggers a graceful close.
    * :meth:`register_handler` — bind an async handler to an op. Multiple
      handlers per op are supported; all registered handlers are awaited
      for every inbound frame of the given op.
    * :meth:`send` — write a frame. ``correlation_id`` is auto-generated
      for request ops that expect a reply.
    * :meth:`respond` — write a reply frame that reuses the correlation
      id of an inbound request. Used by ``file_bridge`` to reply to
      ``file.read`` / ``file.list`` and by ``hermes_bridge`` to stream
      ``chat.token`` events tied to a specific ``chat.send`` request.
    """

    def __init__(self, server_url: str, bearer_token: str, hello: BaseModel) -> None:
        """Configure the client (does NOT connect).

        Args:
            server_url: Full WebSocket URL of the relay endpoint, e.g.
                ``wss://hermes.example.com/api/relay/ws``.
            bearer_token: Pre-shared token read from
                ``~/.hermes/vault.json`` under ``webrelay.bearer_token``.
                Sent as ``Authorization: Bearer <token>`` on the upgrade
                request.
            hello: The initial ``hello`` frame payload (capabilities,
                version, platform). Will be sent as the first frame
                after the upgrade succeeds.
        """
        self._server_url = server_url
        self._bearer_token = bearer_token
        self._hello = hello
        # op -> list[Handler]. Multiple handlers per op are allowed; all
        # of them are awaited for every inbound frame.
        self._handlers: dict[Op, list[Handler]] = {}
        # The live websocket, set while a connection is up, cleared on
        # any drop. ``send`` checks this and queues if it is None.
        self._ws: Any = None
        # Frames queued by ``send`` while the socket is down. Flushed
        # on the next successful connect.
        self._send_queue: list[str] = []
        # Set to True by ``run`` on cancellation so a connected socket
        # is closed cleanly before the coroutine returns.
        self._closing = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect-loop. Returns only on cancellation or fatal error.

        Responsibilities:

        1. Open the WebSocket with the bearer token.
        2. Send the hello frame as the first message.
        3. Start a background task that sends a ping every 30 s.
        4. Enter the read loop, dispatching frames to registered
           handlers and processing pong replies to our pings.
        5. On any disconnect (ConnectionClosed, OSError, TimeoutError),
           log it, sleep for ``next(reconnect_backoff())`` seconds, and
           try again.
        6. On ``asyncio.CancelledError`` (SIGINT/SIGTERM from
           ``__main__.py``), close the socket cleanly and re-raise.
        """
        backoff = reconnect_backoff()
        try:
            while True:
                try:
                    await self._connect_and_serve()
                    # _connect_and_serve only returns cleanly if the
                    # socket dropped and we should reconnect. Reset the
                    # backoff sequence on a successful disconnect
                    # (rare in practice) by creating a fresh iterator.
                    backoff = reconnect_backoff()
                except asyncio.CancelledError:
                    raise
                except (ConnectionClosed, OSError, TimeoutError) as exc:
                    if self._closing:
                        # Cancellation raced the close; exit quietly.
                        return
                    log.warning(
                        "relay socket dropped: %s; reconnecting", exc
                    )
                    delay = next(backoff)
                    log.info("sleeping %.2fs before reconnect", delay)
                    await asyncio.sleep(delay)
        finally:
            self._closing = True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _connect_and_serve(self) -> None:
        """Open the socket, send hello, run the read loop until it drops.

        Returns when the socket disconnects. The outer ``run`` loop
        catches the resulting exception and starts the backoff timer.
        """
        headers = {"Authorization": "Bearer " + self._bearer_token}
        log.info("connecting to %s", self._server_url)
        async with websockets.connect(
            self._server_url, additional_headers=headers
        ) as ws:
            self._ws = ws
            try:
                # First frame after upgrade: the hello envelope.
                await self._send_hello(ws)
                # Flush any frames queued while the socket was down.
                await self._flush_queue(ws)
                # Heartbeat + read loop run concurrently. We only need
                # to await one of them — when either raises, ``async
                # with`` tears down the connection and the other exits
                # via the cancellation it receives.
                ping_task = asyncio.create_task(self._ping_loop(ws))
                try:
                    await self._read_loop(ws)
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            finally:
                self._ws = None

    async def _send_hello(self, ws: Any) -> None:
        """Send the initial hello envelope (stage-2 spec: first frame)."""
        # The hello payload is a BaseModel (per the constructor's
        # contract). We use ``build_envelope`` so the wire format is
        # identical to every other outbound frame.
        # ``Op.HELLO`` may not exist on the enum depending on the
        # protocol version, so we look it up defensively.
        hello_op = getattr(Op, "HELLO", None)
        if hello_op is None:
            log.warning("Op.HELLO not present in protocol; skipping hello")
            return
        await ws.send(build_envelope(hello_op, self._hello))

    async def _flush_queue(self, ws: Any) -> None:
        """Drain the send queue accumulated while disconnected."""
        if not self._send_queue:
            return
        log.info("flushing %d queued frame(s)", len(self._send_queue))
        for frame in self._send_queue:
            await ws.send(frame)
        self._send_queue.clear()

    async def _ping_loop(self, ws: Any) -> None:
        """Send a ping every ``PING_INTERVAL_S`` seconds.

        Uses the protocol ``ping`` op rather than the WebSocket-level
        control frame, so the server can count them in its metrics and
        reply with the protocol ``pong`` op (which our read loop drops
        silently — it's a heartbeat, not user data).
        """
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_S)
                ping_op = getattr(Op, "PING", None)
                if ping_op is None:
                    continue
                try:
                    await ws.send(build_envelope(ping_op, self._empty_payload(ping_op)))
                except (ConnectionClosed, OSError):
                    return
        except asyncio.CancelledError:
            return

    async def _read_loop(self, ws: Any) -> None:
        """Read frames, dispatch to registered handlers.

        Pong replies are silently consumed (they are heartbeats, not
        user data). Unknown ops are logged and skipped — they may be
        future protocol additions the agent does not yet understand.
        """
        async for raw in ws:
            try:
                envelope, payload = parse_envelope(raw)
            except ProtocolError as exc:
                log.warning(
                    "received malformed envelope: code=%s message=%s",
                    exc.code,
                    exc.message,
                )
                continue
            except json.JSONDecodeError as exc:
                # ``parse_envelope`` already wraps JSON errors as
                # ProtocolError; this branch catches any other decoder
                # path (e.g. a text frame that is not even a string).
                log.warning("received non-text frame: %s", exc)
                continue

            # Heartbeats: silently drop ``pong``; we don't need to
            # forward them anywhere.
            if envelope.op == getattr(Op, "PONG", None):
                continue

            handlers = self._handlers.get(envelope.op)
            if not handlers:
                log.debug(
                    "no handler registered for op %s; ignoring frame id=%s",
                    envelope.op,
                    envelope.id,
                )
                continue

            # Dispatch to all registered handlers. We use
            # ``return_exceptions=True`` so a single bad handler does
            # not break the read loop or starve sibling handlers.
            results = await asyncio.gather(
                *(handler(envelope, payload) for handler in handlers),
                return_exceptions=True,
            )
            for handler, result in zip(handlers, results):
                if isinstance(result, Exception):
                    log.exception(
                        "handler %r for op %s raised",
                        handler,
                        envelope.op,
                        exc_info=result,
                    )

    @staticmethod
    def _empty_payload(op: Op) -> BaseModel:
        """Return an empty payload instance for the given op.

        The protocol declares every op's payload model; for
        heartbeat / empty-payload ops (``Ping``, ``Pong``) the
        default-constructed model is ``{}`` on the wire.
        """
        cls = PAYLOAD_MAP.get(op, Pong)
        return cls()

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def register_handler(self, op: Op, handler: Handler) -> None:
        """Bind an async handler to an inbound op.

        ``handler(envelope, payload)`` is awaited for every inbound
        frame of the given op. Multiple handlers per op are allowed;
        they are all awaited in registration order. Must be called
        BEFORE :meth:`run` (registration is not thread-safe; the
        caller is expected to do it at startup, before the connect
        loop starts).

        Example::

            client.register_handler("chat.token", hermes_bridge.on_token)
            client.register_handler("file.read",  file_bridge.on_read)
        """
        self._handlers.setdefault(op, []).append(handler)

    # ------------------------------------------------------------------
    # Outbound traffic
    # ------------------------------------------------------------------

    async def send(
        self,
        op: Op,
        payload: BaseModel,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Write a single frame to the server.

        Args:
            op: The wire-protocol op name.
            payload: Pydantic model to serialize into the frame body.
            correlation_id: If ``None`` (the default) a fresh UUID4 is
                generated. Pass an explicit id when the frame is a
                reply to a server-originated request, or when you need
                to correlate streamed pushes with the originating
                request.

        If the socket is currently down, the frame is queued and a
        warning is emitted. The queue is drained on the next successful
        connect. We deliberately do NOT raise here — a transient
        disconnect should not crash a bridge that is just pushing
        heartbeats.
        """
        frame = build_envelope(
            op, payload, id=correlation_id or uuid.uuid4().hex
        )
        ws = self._ws
        if ws is None:
            warnings.warn(
                f"relay socket down; queueing {op.value!r} frame "
                f"(correlation_id={correlation_id or '<auto>'})",
                RuntimeWarning,
                stacklevel=2,
            )
            self._send_queue.append(frame)
            return
        try:
            await ws.send(frame)
        except (ConnectionClosed, OSError) as exc:
            # Socket dropped between the ``is None`` check and the send.
            # Re-queue and warn so the caller can decide what to do.
            warnings.warn(
                f"relay socket dropped during send of {op.value!r}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            self._send_queue.append(frame)

    async def respond(self, envelope: Envelope, payload: BaseModel) -> None:
        """Reply to a server-originated request, reusing its correlation id.

        Convenience wrapper: equivalent to ``send(op, payload,
        correlation_id=envelope.id)`` but reads more clearly at the
        call site. Used by bridges to answer ``file.read``,
        ``file.list``, and (transitively) ``chat.send`` token-stream
        pushes.
        """
        reply_op = next(
            (op for op, cls in PAYLOAD_MAP.items() if isinstance(payload, cls)),
            envelope.op,
        )
        await self.send(reply_op, payload, correlation_id=envelope.id)


__all__ = ["Handler", "RelayClient"]
