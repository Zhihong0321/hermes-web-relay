"""Tests for the wire-protocol schema (Op enum, payload models, helpers).

Three required suites:
1. ``test_payload_map_covers_every_op`` -- PAYLOAD_MAP has an entry per Op.
2. ``test_build_parse_round_trip`` -- build_envelope -> parse_envelope is lossless.
3. ``test_parse_envelope_rejects_unknown_op`` -- unknown ops raise ProtocolError.

The payload round-trip table is data-driven: every Op -> payload class
in PAYLOAD_MAP gets exercised with a representative valid instance.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from webrelay.agent.protocol import (  # also valid for server; the two copies
    ApprovalRequested,
    ApprovalRespond,
    ChatDone,
    ChatSend,
    ChatToken,
    ErrorPayload,
    FileList,
    FileRead,
    FileResult,
    Hello,
    LedgerChanged,
    LedgerList,
    LedgerRead,
    LedgerResult,
    Op,
    PAYLOAD_MAP,
    Ping,
    Pong,
    ProtocolError,
    build_envelope,
    parse_envelope,
)


# ---------------------------------------------------------------------------
# Fixtures / example payloads
# ---------------------------------------------------------------------------


def _examples() -> list[tuple[Op, object]]:
    """Return a representative valid payload for every Op in PAYLOAD_MAP.

    Each example exercises every field of its payload model (including
    Optional / default-valued fields) so the round-trip test verifies
    that nothing is silently dropped.
    """
    return [
        (
            Op.CHAT_SEND,
            ChatSend(thread_id="t-1", text="hello hermes"),
        ),
        (
            Op.CHAT_TOKEN,
            ChatToken(thread_id="t-1", text="Hel", seq=3),
        ),
        (
            Op.CHAT_DONE,
            ChatDone(thread_id="t-1", task_ledger_id="task_ledger_42.md"),
        ),
        (
            Op.FILE_READ,
            FileRead(path="task_ledger_copilot_m3.md"),
        ),
        (
            Op.FILE_LIST,
            FileList(path="."),
        ),
        (
            Op.FILE_RESULT,
            FileResult(
                path=".",
                kind="dir",
                content=None,
                entries=[{"name": "a.md", "kind": "file", "size": 42}],
                error=None,
            ),
        ),
        (
            Op.LEDGER_LIST,
            LedgerList(),
        ),
        (
            Op.LEDGER_READ,
            LedgerRead(ledger_id="task_ledger_42.md"),
        ),
        (
            Op.LEDGER_RESULT,
            LedgerResult(ledger_id="task_ledger_42.md", content="# hi", mtime=1717_171_717.0),
        ),
        (
            Op.LEDGER_CHANGED,
            LedgerChanged(ledger_id="task_ledger_42.md", content="# hi v2", mtime=1717_171_800.5),
        ),
        (
            Op.APPROVAL_REQUESTED,
            ApprovalRequested(
                prompt_id="p-9",
                tool_name="Bash",
                command="rm -rf /tmp/test",
                context="delete scratch",
            ),
        ),
        (
            Op.APPROVAL_RESPOND,
            ApprovalRespond(prompt_id="p-9", decision="deny", reason="too scary"),
        ),
        (
            Op.PING,
            Ping(),
        ),
        (
            Op.PONG,
            Pong(),
        ),
        (
            Op.HELLO,
            Hello(
                agent_version="0.1.0",
                hermes_endpoint="https://hermes.example.com",
                host="windows-laptop",
                platform="win32",
            ),
        ),
        (
            Op.ERROR,
            ErrorPayload(
                code="internal",
                message="boom",
                original_op="chat.send",
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# 1. Every Op has an entry in PAYLOAD_MAP
# ---------------------------------------------------------------------------


def test_payload_map_covers_every_op() -> None:
    """Every member of the Op enum MUST appear in PAYLOAD_MAP exactly once."""
    assert set(PAYLOAD_MAP.keys()) == set(Op)
    for op, cls in PAYLOAD_MAP.items():
        assert isinstance(op, Op), f"key {op!r} is not an Op"
        assert isinstance(cls, type) and issubclass(cls, object)
        # Sanity: the payload class name should be the PascalCase form
        # of the op value (``chat.send`` -> ``ChatSend``), with the one
        # documented exception (``error`` -> ``ErrorPayload``).
        expected = _payload_class_name(op)
        if op is Op.ERROR:
            assert cls.__name__ == "ErrorPayload", (
                f"error op must map to ErrorPayload, got {cls.__name__}"
            )
        else:
            assert cls.__name__ == expected, (
                f"payload class {cls.__name__} does not match op {op.value!r}"
            )


def test_payload_map_keys_match_examples() -> None:
    """The example table is exhaustive: every Op is exercised."""
    covered = {op for op, _ in _examples()}
    assert covered == set(Op)


def test_payload_map_values_are_pydantic_models() -> None:
    """All values in PAYLOAD_MAP must be pydantic v2 BaseModel subclasses."""
    from pydantic import BaseModel

    for op, cls in PAYLOAD_MAP.items():
        assert issubclass(cls, BaseModel), f"{op.value} maps to non-BaseModel {cls!r}"


def _payload_class_name(op: Op) -> str:
    """Map an Op (e.g. ``chat.send``) to the documented payload class name."""
    return "".join(part.capitalize() for part in op.value.split("."))


# ---------------------------------------------------------------------------
# 2. build_envelope -> parse_envelope round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op", "payload"), _examples(), ids=lambda v: v.value if isinstance(v, Op) else "")
def test_build_parse_round_trip(op: Op, payload: object) -> None:
    """build_envelope -> parse_envelope is lossless for every op."""
    # `payload` is a pydantic model instance from _examples().
    assert hasattr(payload, "model_dump")

    raw = build_envelope(op, payload)  # type: ignore[arg-type]
    assert isinstance(raw, str)

    # The wire form must be valid JSON.
    decoded = json.loads(raw)
    assert decoded["op"] == op.value
    assert isinstance(decoded["id"], str) and decoded["id"]
    assert isinstance(decoded["ts"], (int, float))
    assert isinstance(decoded["payload"], dict)

    envelope, parsed = parse_envelope(raw)

    assert envelope.op is op
    assert envelope.id == decoded["id"]
    assert envelope.ts == decoded["ts"]
    assert parsed == payload


def test_build_envelope_uses_uuid4_hex_when_id_omitted() -> None:
    """When id is None, build_envelope must generate a 32-char hex uuid4."""
    raw = build_envelope(Op.PING, Ping())
    decoded = json.loads(raw)
    assert len(decoded["id"]) == 32
    int(decoded["id"], 16)  # raises if not valid hex


def test_build_envelope_honors_explicit_id() -> None:
    """When id is provided, build_envelope must preserve it verbatim."""
    raw = build_envelope(Op.PING, Ping(), id="corr-123")
    decoded = json.loads(raw)
    assert decoded["id"] == "corr-123"


def test_build_envelope_accepts_bytes_input_via_parse() -> None:
    """parse_envelope must accept the bytes form too (it UTF-8 decodes)."""
    raw = build_envelope(Op.PING, Ping(), id="b-1")
    envelope, payload = parse_envelope(raw.encode("utf-8"))
    assert envelope.id == "b-1"
    assert isinstance(payload, Ping)


# ---------------------------------------------------------------------------
# 3. parse_envelope rejects unknown ops
# ---------------------------------------------------------------------------


def test_parse_envelope_rejects_unknown_op_string() -> None:
    """An op string that is not a registered Op value must raise ProtocolError."""
    bad = json.dumps({"op": "totally.bogus", "id": "x", "ts": 0.0, "payload": {}})
    with pytest.raises(ProtocolError) as excinfo:
        parse_envelope(bad)
    assert excinfo.value.code == "unknown_op"
    assert "totally.bogus" in excinfo.value.message
    assert excinfo.value.original_op is None


def test_parse_envelope_rejects_op_in_wrong_type() -> None:
    """A non-string op must raise ProtocolError, not crash."""
    bad = json.dumps({"op": 42, "id": "x", "ts": 0.0, "payload": {}})
    with pytest.raises(ProtocolError) as excinfo:
        parse_envelope(bad)
    assert excinfo.value.code == "unknown_op"


def test_parse_envelope_rejects_malformed_json() -> None:
    """Non-JSON input raises ProtocolError with code='bad_json'."""
    with pytest.raises(ProtocolError) as excinfo:
        parse_envelope("not json {")
    assert excinfo.value.code == "bad_json"


def test_parse_envelope_rejects_non_object_json() -> None:
    """A JSON array (not an object) raises ProtocolError, code='bad_envelope'."""
    with pytest.raises(ProtocolError) as excinfo:
        parse_envelope("[1, 2, 3]")
    assert excinfo.value.code == "bad_envelope"


def test_parse_envelope_rejects_missing_required_envelope_fields() -> None:
    """Missing 'op' in the envelope raises ValidationError, not ProtocolError."""
    bad = json.dumps({"id": "x", "ts": 0.0, "payload": {}})
    with pytest.raises(ValidationError):
        parse_envelope(bad)


def test_protocol_error_to_payload_round_trip() -> None:
    """ProtocolError -> ErrorPayload round-trips through the wire format."""
    err = ProtocolError(code="boom", message="kaboom", original_op="chat.send")
    payload = err.to_payload()
    raw = build_envelope(Op.ERROR, payload, id="e-1")
    envelope, parsed = parse_envelope(raw)
    assert envelope.op is Op.ERROR
    assert parsed == payload
