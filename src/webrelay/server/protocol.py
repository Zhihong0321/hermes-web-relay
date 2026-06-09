"""Wire protocol for the hermes-web-relay.

Defines the JSON message format exchanged over the single relay WebSocket
between the Coolify-hosted FastAPI server and the local PC agent.

Every message is an :class:`Envelope` (op, id, ts, payload) whose ``op``
determines the shape of ``payload``. The :data:`PAYLOAD_MAP` table is the
single source of truth: every :class:`Op` member maps to exactly one
pydantic v2 model class. :func:`parse_envelope` and :func:`build_envelope`
use it to validate and serialize in both directions.

This module is intentionally self-contained: it imports only ``pydantic``
and the standard library so it can be reused by both the server and the
agent (the two copies of this file under ``server/`` and ``agent/`` are
byte-identical).
"""

from __future__ import annotations

import json
import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


# ---------------------------------------------------------------------------
# Op enum
# ---------------------------------------------------------------------------


class Op(str, Enum):
    """The set of valid wire-protocol operations.

    Values are the dotted strings used on the wire; they form the
    contract between server and agent. Direction is documented in
    ``planning-mode-agile-sky.md`` (section "Wire protocol (JSON over
    the single relay WebSocket)").
    """

    CHAT_SEND = "chat.send"
    CHAT_TOKEN = "chat.token"
    CHAT_DONE = "chat.done"
    FILE_READ = "file.read"
    FILE_LIST = "file.list"
    FILE_RESULT = "file.result"
    LEDGER_LIST = "ledger.list"
    LEDGER_READ = "ledger.read"
    LEDGER_RESULT = "ledger.result"
    LEDGER_CHANGED = "ledger.changed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESPOND = "approval.respond"
    PING = "ping"
    PONG = "pong"
    HELLO = "hello"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class Envelope(BaseModel):
    """The outer wrapper of every message on the wire.

    Attributes:
        op: The operation. Determines the expected payload schema.
        id: A correlation id. Server-originated requests and the
            matching agent replies share the same id; one-shot pushes
            (``chat.token``, ``ledger.changed``, ...) also use it for
            de-duplication.
        ts: Unix seconds (float) when the envelope was created.
        payload: The raw payload dict; validated separately against
            the model registered in :data:`PAYLOAD_MAP`.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    op: Op
    id: str
    ts: float
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------


class ChatSend(BaseModel):
    """Server -> local: forward a user chat message to hermes."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    text: str


class ChatToken(BaseModel):
    """Local -> server: one streaming token from the hermes reply."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    text: str
    seq: int


class ChatDone(BaseModel):
    """Local -> server: end of a hermes reply.

    ``task_ledger_id`` is set when the chat spawned a task ledger.
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    task_ledger_id: str | None = None


class FileRead(BaseModel):
    """Server -> local: read a file (sandboxed to E:/hermes-agent)."""

    model_config = ConfigDict(extra="forbid")

    path: str


class FileList(BaseModel):
    """Server -> local: list a directory (sandboxed to E:/hermes-agent)."""

    model_config = ConfigDict(extra="forbid")

    path: str


class FileResult(BaseModel):
    """Local -> server: response to a ``file.read`` or ``file.list``.

    Exactly one of ``content`` / ``entries`` / ``error`` is populated,
    indicated by ``kind``:
        * ``"file"``  -> ``content`` is the file body
        * ``"dir"``   -> ``entries`` is a list of ``{name, kind, size?}``
        * ``"error"`` -> ``error`` is the human-readable message
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: str
    content: str | None = None
    entries: list[dict[str, Any]] | None = None
    error: str | None = None


class LedgerList(BaseModel):
    """Server -> local: request the list of known ledgers.

    Empty payload; local pushes ``ledger.result`` rows back.
    """

    model_config = ConfigDict(extra="forbid")


class LedgerRead(BaseModel):
    """Server -> local: fetch a single ledger's current content."""

    model_config = ConfigDict(extra="forbid")

    ledger_id: str


class LedgerResult(BaseModel):
    """Local -> server: ledger content + its mtime."""

    model_config = ConfigDict(extra="forbid")

    ledger_id: str
    content: str
    mtime: float


class LedgerChanged(BaseModel):
    """Local -> server: push when the local file watcher fires."""

    model_config = ConfigDict(extra="forbid")

    ledger_id: str
    content: str
    mtime: float


class ApprovalRequested(BaseModel):
    """Local -> server: a sensitive op is pending user approval."""

    model_config = ConfigDict(extra="forbid")

    prompt_id: str
    tool_name: str
    command: str
    context: str


class ApprovalRespond(BaseModel):
    """Server -> local: user tapped allow/deny on their phone."""

    model_config = ConfigDict(extra="forbid")

    prompt_id: str
    decision: str  # "allow" | "deny"
    reason: str | None = None


class Ping(BaseModel):
    """Heartbeat (both directions). Empty payload."""

    model_config = ConfigDict(extra="forbid")


class Pong(BaseModel):
    """Heartbeat reply (both directions). Empty payload."""

    model_config = ConfigDict(extra="forbid")


class Hello(BaseModel):
    """Both directions, sent on connect.

    Server includes ``hermes_endpoint`` when configured (so the agent
    can verify it reached the right instance); agent populates ``host``
    and ``platform`` for the operator UI.
    """

    model_config = ConfigDict(extra="forbid")

    agent_version: str
    hermes_endpoint: str | None = None
    host: str
    platform: str


class ErrorPayload(BaseModel):
    """Local -> server: unrecoverable error for a prior request.

    ``original_op`` is the dotted op name of the request that failed,
    or None for protocol-level errors (e.g. unknown op).
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    original_op: str | None = None


# ---------------------------------------------------------------------------
# Op -> payload class table
# ---------------------------------------------------------------------------


PAYLOAD_MAP: dict[Op, type[BaseModel]] = {
    Op.CHAT_SEND: ChatSend,
    Op.CHAT_TOKEN: ChatToken,
    Op.CHAT_DONE: ChatDone,
    Op.FILE_READ: FileRead,
    Op.FILE_LIST: FileList,
    Op.FILE_RESULT: FileResult,
    Op.LEDGER_LIST: LedgerList,
    Op.LEDGER_READ: LedgerRead,
    Op.LEDGER_RESULT: LedgerResult,
    Op.LEDGER_CHANGED: LedgerChanged,
    Op.APPROVAL_REQUESTED: ApprovalRequested,
    Op.APPROVAL_RESPOND: ApprovalRespond,
    Op.PING: Ping,
    Op.PONG: Pong,
    Op.HELLO: Hello,
    Op.ERROR: ErrorPayload,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_op(value: Any) -> Op:
    """Best-effort coercion of a wire-supplied op into an :class:`Op`.

    Accepts ``Op`` instances, exact dotted strings, and enum lookups by
    name. Anything else raises :class:`ProtocolError` with a message
    that names the unknown op value -- this is the "clear error" path
    exercised by ``test_protocol.py``.
    """
    if isinstance(value, Op):
        return value
    if isinstance(value, str):
        if value in Op._value2member_map_:
            return Op(value)
        # Allow lookups by member name (CHAT_SEND -> "chat.send").
        try:
            return Op[value]
        except KeyError:
            pass
    raise ProtocolError(
        code="unknown_op",
        message=f"unknown op: {value!r}",
        original_op=None,
    )


class ProtocolError(ValueError):
    """Raised by :func:`parse_envelope` for malformed/illegal messages.

    Carries the same fields as :class:`ErrorPayload` so callers can turn
    it directly into an outbound ``error`` envelope.
    """

    def __init__(self, code: str, message: str, original_op: str | None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.original_op = original_op

    def to_payload(self) -> ErrorPayload:
        return ErrorPayload(
            code=self.code,
            message=self.message,
            original_op=self.original_op,
        )


def parse_envelope(raw: str | bytes) -> tuple[Envelope, BaseModel]:
    """Parse a wire message and return ``(envelope, validated_payload)``.

    ``raw`` may be either a JSON string or UTF-8 bytes. The envelope is
    always validated against :class:`Envelope`; the payload is
    validated against the model registered in :data:`PAYLOAD_MAP` for
    the envelope's op.

    Raises :class:`ProtocolError` with ``code="unknown_op"`` for any op
    that is not in :data:`PAYLOAD_MAP` (i.e. not a valid :class:`Op`),
    and re-raises pydantic :class:`ValidationError` for malformed
    envelopes or payloads.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(
            code="bad_json",
            message=f"invalid JSON: {exc.msg}",
            original_op=None,
        ) from exc
    if not isinstance(data, dict):
        raise ProtocolError(
            code="bad_envelope",
            message=f"envelope must be a JSON object, got {type(data).__name__}",
            original_op=None,
        )

    # If "op" is missing, let pydantic's Envelope validator emit the
    # canonical "missing field" ValidationError -- that's what callers
    # expect for a malformed envelope. We only treat a *present-but-
    # unknown* op as ProtocolError("unknown_op").
    if "op" not in data:
        envelope = Envelope.model_validate(data)
        # Unreachable: the line above raises first.
        raise ProtocolError(  # pragma: no cover
            code="bad_envelope",
            message="envelope is missing required field 'op'",
            original_op=None,
        )

    op = _coerce_op(data["op"])

    try:
        envelope = Envelope.model_validate(data)
    except ValidationError:
        raise

    payload_cls = PAYLOAD_MAP.get(op)
    if payload_cls is None:  # pragma: no cover - defensive
        raise ProtocolError(
            code="unknown_op",
            message=f"no payload model registered for op {op.value!r}",
            original_op=op.value,
        )

    payload_obj = payload_cls.model_validate(envelope.payload)
    return envelope, payload_obj


def build_envelope(
    op: Op,
    payload: BaseModel,
    id: str | None = None,
) -> str:
    """Serialize a payload into a wire-ready JSON envelope string.

    ``id`` defaults to a random uuid4 hex. ``ts`` defaults to the
    current unix time. The returned string is the on-the-wire form.
    """
    envelope = Envelope(
        op=op,
        id=uuid.uuid4().hex if id is None else id,
        ts=time.time(),
        payload=payload.model_dump(mode="json"),
    )
    return envelope.model_dump_json()


__all__ = [
    "ApprovalRequested",
    "ApprovalRespond",
    "ChatDone",
    "ChatSend",
    "ChatToken",
    "Envelope",
    "ErrorPayload",
    "FileList",
    "FileRead",
    "FileResult",
    "Hello",
    "LedgerChanged",
    "LedgerList",
    "LedgerRead",
    "LedgerResult",
    "Op",
    "PAYLOAD_MAP",
    "Ping",
    "Pong",
    "ProtocolError",
    "build_envelope",
    "parse_envelope",
]
