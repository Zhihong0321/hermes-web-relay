"""Hermes bridge for the hermes-web-relay agent.

Forwards ``chat.send`` envelopes received from the relay server into a real
local Hermes agent running on the same machine. The local Hermes is
exposed as a WebSocket gateway by ``tui_gateway.ws.handle_ws``; the
WebSocket protocol is line-delimited JSON-RPC 2.0 (see
``hermes-install/tui_gateway/server.py`` for the canonical implementation).

Wire-protocol decisions (verified against ``tui_gateway/server.py``)
-------------------------------------------------------------------
* **RPC method to send a user prompt:** ``prompt.submit`` (NOT
  ``chat.send`` -- that name is reserved for the web-relay side of the
  wire). The server-side handler is registered at
  ``@method("prompt.submit")`` (line 4342 of ``server.py``) and expects
  ``params.session_id`` and ``params.text``.

* **Session bootstrap:** ``prompt.submit`` requires an existing
  ``session_id``. The bridge lazily creates one via
  ``@method("session.create")`` (line 3088) the first time it sees a
  new ``thread_id`` from the relay server, and caches the mapping for
  subsequent turns.

* **Ready event on connect:** the gateway emits a single
  ``{"jsonrpc": "2.0", "method": "event", "params": {"type":
  "gateway.ready", "payload": {...}}}`` frame immediately after the
  upgrade. We read + discard that frame before sending anything.

* **Streaming token events:** the per-token frames are emitted as JSON-RPC
  notifications with ``method == "event"`` and
  ``params.type == "message.delta"`` (line 4729). The token text lives
  in ``params.payload.text``. We map every such frame to one
  ``Op.CHAT_TOKEN`` push to the relay server.

* **End-of-turn event:** ``params.type == "message.complete"`` (line
  4816). We send one final ``Op.CHAT_DONE`` and close the Hermes WS.

* **Error event:** ``params.type == "error"`` with
  ``params.payload.message``. We surface that as a ``CHAT_TOKEN`` and
  then ``CHAT_DONE`` so the user sees the failure text on their phone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, WebSocketException

from webrelay.agent.client import RelayClient
from webrelay.agent.protocol import (
    ChatDone,
    ChatSend,
    ChatToken,
    Envelope,
    Op,
)

_log = logging.getLogger(__name__)


# JSON-RPC method names verified against hermes-install/tui_gateway/server.py.
RPC_PROMPT_SUBMIT = "prompt.submit"
RPC_SESSION_CREATE = "session.create"

# Event "type" values emitted by the gateway (tui_gateway.server._emit()).
EVENT_GATEWAY_READY = "gateway.ready"
EVENT_MESSAGE_START = "message.start"
EVENT_MESSAGE_DELTA = "message.delta"
EVENT_MESSAGE_COMPLETE = "message.complete"
EVENT_ERROR = "error"

# How long to wait for the gateway.ready frame before giving up. Keeps
# a wedged / wrong-protocol server from hanging a chat turn forever.
_READY_TIMEOUT_S = 5.0

# How long to allow the initial TCP/TLS upgrade. Hermes usually lives on
# localhost; a long timeout mostly defends against the user's machine
# being saturated.
_OPEN_TIMEOUT_S = 5.0

# Sentinel session_id used by the relay server that we map to a brand
# new hermes session. The relay protocol always carries a ``thread_id``
# (the chat thread on the phone); we use it as the cache key.
_NEW_THREAD_SENTINEL = ""

# Canned user-visible message used when the local hermes is unreachable.
_HERMES_UNREACHABLE_TEXT = (
    "[Local hermes is not running. Please start hermes-agent on this machine.]"
)


class HermesBridge:
    """Bridge chat envelopes from the relay server to the local Hermes gateway.

    One instance per agent process. The bridge is bound to a single
    :class:`RelayClient`; it registers an inbound handler for
    :attr:`Op.CHAT_SEND` and streams the agent's reply back as
    :attr:`Op.CHAT_TOKEN` and :attr:`Op.CHAT_DONE` frames.

    The local Hermes is assumed to be reachable over a WebSocket at
    ``hermes_ws_url`` (default in production: ``ws://127.0.0.1:8765/ws``;
    configurable from the agent's environment). Each inbound
    ``chat.send`` opens its own short-lived WS connection, drains the
    stream, and closes. A persistent connection is unnecessary because
    the gateway re-uses the underlying session via ``session_id`` once
    ``session.create`` has been called.
    """

    def __init__(self, client: RelayClient, hermes_ws_url: str) -> None:
        """Configure the bridge.

        Args:
            client: The relay client used to register the inbound
                ``chat.send`` handler and to push ``chat.token`` /
                ``chat.done`` frames.
            hermes_ws_url: Full WebSocket URL of the local hermes
                gateway, e.g. ``ws://127.0.0.1:8765/ws``.
        """
        self._client = client
        self._hermes_ws_url = hermes_ws_url
        # thread_id (relay-side) -> hermes session_id. Persists for the
        # lifetime of the agent so subsequent turns in the same chat
        # re-use the same hermes session (preserves history).
        self._session_map: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Register the ``Op.CHAT_SEND`` handler on the relay client."""
        self._client.register_handler(Op.CHAT_SEND, self.on_chat_send)
        _log.info("hermes bridge started url=%s", self._hermes_ws_url)

    # ------------------------------------------------------------------
    # Inbound request handler
    # ------------------------------------------------------------------

    async def on_chat_send(self, envelope: Envelope, payload: ChatSend) -> None:
        """Forward a user chat message to the local Hermes agent.

        Sequence:
        1. Lazily create / look up a hermes ``session_id`` for
           ``payload.thread_id``.
        2. Open a fresh WS to the hermes gateway, drain
           ``gateway.ready``, send ``prompt.submit``.
        3. For each ``message.delta`` event from hermes, push a
           ``CHAT_TOKEN`` frame back to the relay server.
        4. On ``message.complete`` (or terminal ``error``), push
           ``CHAT_DONE`` and close.
        5. If hermes is unreachable, push a single error
           ``CHAT_TOKEN`` + ``CHAT_DONE`` so the user's phone shows a
           useful message instead of hanging.
        """
        thread_id = payload.thread_id
        try:
            await self._run_chat_turn(envelope, payload)
        except Exception as exc:  # pragma: no cover - defensive net
            _log.exception(
                "hermes bridge: chat turn crashed thread_id=%s", thread_id
            )
            await self._send_error_and_done(
                thread_id,
                f"[Bridge error: {type(exc).__name__}: {exc}]",
            )

    # ------------------------------------------------------------------
    # Core: open WS, drive one turn
    # ------------------------------------------------------------------

    async def _run_chat_turn(
        self, envelope: Envelope, payload: ChatSend
    ) -> None:
        """Drive one user turn end-to-end against the local Hermes gateway."""
        thread_id = payload.thread_id

        # 1. Resolve session_id (creating one on first sight).
        try:
            session_id = await self._ensure_session(thread_id)
        except HermesUnreachable as exc:
            _log.warning("hermes bridge: hermes unreachable: %s", exc)
            await self._send_error_and_done(thread_id, _HERMES_UNREACHABLE_TEXT)
            return
        except Exception as exc:  # pragma: no cover - defensive
            _log.exception("hermes bridge: session create failed")
            await self._send_error_and_done(
                thread_id, f"[Hermes session create failed: {exc}]"
            )
            return

        # 2. Open the streaming WS for the turn.
        ws: ClientConnection | None = None
        try:
            try:
                ws = await websockets.connect(
                    self._hermes_ws_url,
                    open_timeout=_OPEN_TIMEOUT_S,
                )
            except (
                OSError,
                ConnectionClosed,
                WebSocketException,
                asyncio.TimeoutError,
            ) as exc:
                # Connection refused, timeout, DNS failure, etc. The
                # server may not be running at all.
                raise HermesUnreachable(str(exc)) from exc

            # 3. Drain gateway.ready (ignore its payload).
            try:
                ready_raw = await asyncio.wait_for(
                    ws.recv(), timeout=_READY_TIMEOUT_S
                )
            except asyncio.TimeoutError as exc:
                raise HermesUnreachable(
                    "timed out waiting for gateway.ready"
                ) from exc
            except ConnectionClosed as exc:
                raise HermesUnreachable(
                    f"connection closed before gateway.ready: {exc}"
                ) from exc

            try:
                ready_msg = json.loads(ready_raw)
            except json.JSONDecodeError:
                ready_msg = {}
            if not _is_gateway_ready(ready_msg):
                _log.warning(
                    "hermes bridge: expected gateway.ready, got %r",
                    ready_msg,
                )
                # Best effort: still proceed; hermes may just not send
                # the ready frame if it's been customized.

            # 4. Send prompt.submit.
            submit_id = uuid.uuid4().hex
            submit_frame = {
                "jsonrpc": "2.0",
                "id": submit_id,
                "method": RPC_PROMPT_SUBMIT,
                "params": {
                    "session_id": session_id,
                    "text": payload.text,
                },
            }
            await ws.send(json.dumps(submit_frame, ensure_ascii=False))

            # 5. Stream message.delta -> CHAT_TOKEN until message.complete.
            seq = 0
            done = False
            while not done:
                try:
                    raw = await ws.recv()
                except ConnectionClosed:
                    _log.warning(
                        "hermes bridge: ws closed mid-stream thread_id=%s",
                        thread_id,
                    )
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    _log.warning("hermes bridge: non-JSON frame: %r", raw)
                    continue

                event_type = _event_type(msg)
                if event_type == EVENT_MESSAGE_DELTA:
                    text = _event_payload_text(msg)
                    if text is None:
                        continue
                    seq += 1
                    await self._client.send(
                        Op.CHAT_TOKEN,
                        ChatToken(thread_id=thread_id, text=text, seq=seq),
                    )
                elif event_type == EVENT_MESSAGE_COMPLETE:
                    done = True
                elif event_type == EVENT_ERROR:
                    err_text = _event_payload_message(msg) or "hermes error"
                    seq += 1
                    await self._client.send(
                        Op.CHAT_TOKEN,
                        ChatToken(
                            thread_id=thread_id,
                            text=f"[Hermes error: {err_text}]",
                            seq=seq,
                        ),
                    )
                    done = True
                elif event_type == EVENT_GATEWAY_READY:
                    # A spurious extra ready frame (shouldn't happen
                    # but be tolerant). Ignore.
                    continue
                else:
                    # Unknown / informational event. Log and continue.
                    _log.debug(
                        "hermes bridge: unhandled event thread_id=%s type=%r",
                        thread_id,
                        event_type,
                    )

        finally:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:  # pragma: no cover - best effort
                    pass

        # 6. Always end with CHAT_DONE.
        await self._client.send(
            Op.CHAT_DONE, ChatDone(thread_id=thread_id, task_ledger_id=None)
        )

    # ------------------------------------------------------------------
    # Session bootstrap
    # ------------------------------------------------------------------

    async def _ensure_session(self, thread_id: str) -> str:
        """Return a hermes session_id for ``thread_id``, creating one if needed."""
        cached = self._session_map.get(thread_id)
        if cached:
            return cached

        session_id = await self._create_session()
        self._session_map[thread_id] = session_id
        _log.info(
            "hermes bridge: created session thread_id=%s session_id=%s",
            thread_id,
            session_id,
        )
        return session_id

    async def _create_session(self) -> str:
        """Open a one-shot WS, call ``session.create``, return its id.

        Wire sequence (mirrors the real TUI client pattern -- the
        gateway sends ``gateway.ready`` immediately on accept, BEFORE
        the client sends anything, so we must read first then write):

        1. connect
        2. read ``gateway.ready`` (discard)
        3. write ``session.create`` request
        4. read frames until we see the response with our id

        Raises :class:`HermesUnreachable` on connection failure.
        """
        try:
            ws = await websockets.connect(
                self._hermes_ws_url,
                open_timeout=_OPEN_TIMEOUT_S,
            )
        except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
            raise HermesUnreachable(str(exc)) from exc

        try:
            # 2. Drain gateway.ready FIRST. The gateway sends this
            #    immediately on accept; if we wrote before reading, the
            #    send would block until the server also read, but the
            #    server is busy sending us the ready frame -> deadlock.
            try:
                ready_raw = await asyncio.wait_for(
                    ws.recv(), timeout=_READY_TIMEOUT_S
                )
            except asyncio.TimeoutError as exc:
                raise HermesUnreachable(
                    "timed out waiting for gateway.ready"
                ) from exc
            # Even if ready wasn't a real ready frame, proceed best-effort.

            # 3. Send session.create.
            req_id = uuid.uuid4().hex
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": RPC_SESSION_CREATE,
                        "params": {},
                    }
                )
            )

            # Read frames until we see the response matching our id.
            while True:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=_READY_TIMEOUT_S
                    )
                except asyncio.TimeoutError as exc:
                    raise HermesUnreachable(
                        "timed out waiting for session.create response"
                    ) from exc
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != req_id:
                    # Probably a stray event -- ignore and keep reading.
                    continue
                # Pull the session_id out of either result.session_id
                # (normal) or a fallback path.
                result = msg.get("result") or {}
                sid = result.get("session_id") if isinstance(result, dict) else None
                if not sid and isinstance(result, dict):
                    # Some implementations nest deeper; look for any
                    # string field that looks like a session id.
                    for key in ("id", "session_key", "sid"):
                        candidate = result.get(key)
                        if isinstance(candidate, str) and candidate:
                            sid = candidate
                            break
                if not sid:
                    raise HermesUnreachable(
                        f"session.create returned no session_id: {msg!r}"
                    )
                return sid
        finally:
            try:
                await ws.close()
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_error_and_done(self, thread_id: str, text: str) -> None:
        """Push one error ``CHAT_TOKEN`` followed by ``CHAT_DONE``.

        Used when the local hermes is unreachable so the user's phone
        shows a useful message instead of hanging.
        """
        await self._client.send(
            Op.CHAT_TOKEN,
            ChatToken(thread_id=thread_id, text=text, seq=1),
        )
        await self._client.send(
            Op.CHAT_DONE,
            ChatDone(thread_id=thread_id, task_ledger_id=None),
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HermesUnreachable(Exception):
    """Raised when the local hermes WebSocket cannot be reached."""


# ---------------------------------------------------------------------------
# JSON-RPC frame helpers
# ---------------------------------------------------------------------------


def _is_gateway_ready(msg: Any) -> bool:
    """True if ``msg`` is the gateway.ready event notification."""
    if not isinstance(msg, dict):
        return False
    if msg.get("method") != "event":
        return False
    params = msg.get("params")
    return isinstance(params, dict) and params.get("type") == EVENT_GATEWAY_READY


def _event_type(msg: Any) -> str | None:
    """Return the ``params.type`` of a JSON-RPC event notification, or None."""
    if not isinstance(msg, dict):
        return None
    if msg.get("method") != "event":
        return None
    params = msg.get("params")
    if isinstance(params, dict):
        t = params.get("type")
        return t if isinstance(t, str) else None
    return None


def _event_payload(msg: Any) -> dict[str, Any] | None:
    """Return the ``params.payload`` dict of an event notification, or None."""
    if not isinstance(msg, dict):
        return None
    params = msg.get("params")
    if not isinstance(params, dict):
        return None
    payload = params.get("payload")
    return payload if isinstance(payload, dict) else None


def _event_payload_text(msg: Any) -> str | None:
    """Return the streaming token text from a message.delta event, or None."""
    payload = _event_payload(msg)
    if payload is None:
        return None
    text = payload.get("text")
    return text if isinstance(text, str) else None


def _event_payload_message(msg: Any) -> str | None:
    """Return the error message from an error event, or None."""
    payload = _event_payload(msg)
    if payload is None:
        return None
    msg_text = payload.get("message")
    return msg_text if isinstance(msg_text, str) else None


__all__ = [
    "HermesBridge",
    "HermesUnreachable",
    "RPC_PROMPT_SUBMIT",
    "RPC_SESSION_CREATE",
]
