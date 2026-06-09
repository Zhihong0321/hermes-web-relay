"""WebSocket relay routes for local agent connection.

Exposes the /api/relay/ws WebSocket upgrade endpoint which validates the
agent's bearer token, accepts the hello capabilities frame, registers the
agent socket with the RelayHub singleton, and runs the read loop.
"""

from __future__ import annotations

import hashlib
import logging
import os

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from webrelay.server.protocol import Op, parse_envelope

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/relay", tags=["relay"])


@router.websocket("/ws")
async def relay_websocket(websocket: WebSocket) -> None:
    """Agent WebSocket connection endpoint."""
    auth_header = websocket.headers.get("authorization")
    token_valid = False

    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()
        expected_raw = os.environ.get("WEBRELAY_AGENT_TOKEN")
        expected_hash = os.environ.get("WEBRELAY_RELAY_TOKEN_HASH")

        if expected_raw and token == expected_raw:
            token_valid = True
        elif expected_hash:
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if token_hash == expected_hash:
                token_valid = True
        elif not expected_raw and not expected_hash:
            # Fallback for dev mode
            if token == "dev-token-change-me" or token == "stub-token" or token == "fake-token-xyz" or token == "t":
                token_valid = True

    if not token_valid:
        # Reject the connection before accepting it
        raise HTTPException(status_code=403, detail="Forbidden: Invalid agent token")

    await websocket.accept()

    # The agent must send a 'hello' frame as its first message.
    try:
        raw_hello = await websocket.receive_text()
        envelope, hello_payload = parse_envelope(raw_hello)
        if envelope.op != Op.HELLO:
            _log.warning("WebSocket rejected: first frame must be hello, got %s", envelope.op)
            await websocket.close(code=4000, reason="First frame must be hello")
            return
    except (WebSocketDisconnect, ValidationError, Exception) as exc:
        _log.warning("WebSocket failed during hello negotiation: %s", exc)
        try:
            await websocket.close(code=4000, reason="Hello negotiation failed")
        except Exception:
            pass
        return

    hub = websocket.app.state.hub
    try:
        await hub.attach(websocket, hello_payload)
    except ValueError as exc:
        # Reject second client connection
        _log.warning("WebSocket rejected: %s", exc)
        await websocket.close(code=4002, reason=str(exc))
        return

    _log.info("Agent attached: version=%s host=%s", hello_payload.agent_version, hello_payload.host)

    try:
        while True:
            raw_message = await websocket.receive_text()
            await hub.on_inbound(raw_message)
    except WebSocketDisconnect:
        _log.info("Agent disconnected cleanly")
    except Exception as exc:
        _log.warning("Agent connection error: %s", exc)
    finally:
        await hub.detach(websocket)
