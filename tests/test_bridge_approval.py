"""Tests for the approval bridge.

Three test groups:

1. :func:`test_request_approval_round_trip` -- the happy path. The
   bridge sends ``Op.APPROVAL_REQUESTED`` and the simulated server
   replies with ``Op.APPROVAL_RESPOND``; the future resolves with the
   right ``(decision, reason)`` pair.

2. :func:`test_request_approval_timeout_denies` -- if no response
   arrives within ``timeout_s``, the bridge returns
   ``("deny", "timeout — ...")`` (fail-closed).

3. :func:`test_pretooluse_cli_no_server_allows` -- when the bridge IPC
   server is not running, the synchronous ``preToolUse`` script dials
   127.0.0.1:15999, the connect fails, and the script prints
   ``{"continue": true}`` so Claude Code doesn't block on a missing
   agent.

The bridge only uses three public methods of :class:`RelayClient`:

* :meth:`RelayClient.register_handler` -- sync
* :meth:`RelayClient.send` -- async

so :class:`_FakeRelayClient` is the smallest stand-in that exercises
the contract.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from webrelay.agent.bridges.approval_bridge import (
    IPC_PORT,
    ApprovalBridge,
)
from webrelay.agent.protocol import (
    ApprovalRequested,
    ApprovalRespond,
    Envelope,
    Op,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRelayClient:
    """Minimal stand-in for :class:`RelayClient` used by the bridge tests.

    Records every ``send`` call and stores the registered handler so
    tests can drive it directly. Mirrors the documented contract
    (``register_handler`` is sync; ``send`` is async; the most
    recently registered handler wins).
    """

    def __init__(self) -> None:
        self.handlers: dict[Op, Any] = {}
        self.sent: list[tuple[Op, BaseModel]] = []
        # If non-None, every send() will block here. Used to simulate
        # a server that never replies (so the timeout test can race).
        self.send_gate: asyncio.Event | None = None

    def register_handler(self, op: Op, handler: Any) -> None:
        self.handlers[op] = handler

    async def send(
        self,
        op: Op,
        payload: BaseModel,
        *,
        correlation_id: str | None = None,
    ) -> None:
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.sent.append((op, payload))


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
# 1. Round-trip
# ---------------------------------------------------------------------------


async def test_request_approval_round_trip() -> None:
    """request_approval -> on_approval_respond returns the right decision."""
    client = _FakeRelayClient()
    bridge = ApprovalBridge(client)
    await bridge.start()

    try:
        # Drive request_approval as a background task so we can fire
        # the inbound respond handler while it's awaiting.
        task = asyncio.create_task(
            bridge.request_approval(
                tool_name="Bash",
                command="rm -rf /tmp/foo",
                context="delete stale cache",
            )
        )

        # Yield so request_approval has a chance to register the future
        # and call client.send.
        for _ in range(10):
            if client.sent:
                break
            await asyncio.sleep(0)

        assert client.sent, "request_approval did not push an APPROVAL_REQUESTED"
        op, payload = client.sent[-1]
        assert op == Op.APPROVAL_REQUESTED
        assert isinstance(payload, ApprovalRequested)
        assert payload.tool_name == "Bash"
        assert payload.command == "rm -rf /tmp/foo"
        assert payload.context == "delete stale cache"
        prompt_id = payload.prompt_id

        # Simulate the server's reply.
        handler = client.handlers[Op.APPROVAL_RESPOND]
        await handler(
            _make_envelope(
                Op.APPROVAL_RESPOND,
                ApprovalRespond(
                    prompt_id=prompt_id,
                    decision="allow",
                    reason="operator tapped allow",
                ),
            ),
            ApprovalRespond(
                prompt_id=prompt_id,
                decision="allow",
                reason="operator tapped allow",
            ),
        )

        decision, reason = await asyncio.wait_for(task, timeout=2.0)
        assert decision == "allow"
        assert reason == "operator tapped allow"
    finally:
        await bridge.stop()


# ---------------------------------------------------------------------------
# 2. Timeout -> deny
# ---------------------------------------------------------------------------


async def test_request_approval_timeout_denies() -> None:
    """If no respond arrives, the bridge returns (deny, timeout — ...).

    The bridge first awaits ``client.send``. We arrange the fake so
    that send is a no-op (returns immediately) but we never deliver
    an ``approval.respond`` frame. The bridge's own
    :func:`asyncio.wait_for` then trips its 50 ms timeout, which is
    the branch under test.
    """
    client = _FakeRelayClient()
    bridge = ApprovalBridge(client)
    await bridge.start()

    try:
        decision, reason = await bridge.request_approval(
            tool_name="Bash",
            command="do something scary",
            context="test",
            timeout_s=0.05,  # 50ms
        )
        assert decision == "deny"
        assert reason is not None
        assert reason.startswith("timeout")
    finally:
        await bridge.stop()


# ---------------------------------------------------------------------------
# 3. preToolUse CLI: connect fails -> allow
# ---------------------------------------------------------------------------


def _load_pretooluse_module() -> Any:
    """Load the standalone preToolUse.py script by file path.

    Imported this way (not via the package) so the test exercises the
    same import surface Claude Code uses.
    """
    path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "webrelay"
        / "agent"
        / "hooks"
        / "preToolUse.py"
    )
    spec = importlib.util.spec_from_file_location(
        "webrelay_agent_hooks_pretooluse", str(path)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_cli_with_stdin(stdin_payload: str) -> tuple[str, int]:
    """Run ``preToolUse.main()`` with the given stdin payload.

    Returns ``(stdout, returncode)``. Swaps ``sys.stdin`` /
    ``sys.stdout`` for the duration of the call and restores them.
    """
    module = _load_pretooluse_module()
    saved_stdin = sys.stdin
    saved_stdout = sys.stdout
    sys.stdin = io.StringIO(stdin_payload)
    sys.stdout = io.StringIO()
    try:
        rc = module.main()
        return sys.stdout.getvalue(), rc
    finally:
        sys.stdin = saved_stdin
        sys.stdout = saved_stdout


def test_pretooluse_cli_no_server_allows() -> None:
    """When the bridge IPC server isn't running, the hook defaults to allow.

    We pick an arbitrary free port and patch the module's IPC_PORT
    constant to it, so the test never collides with a real running
    agent on the canonical 15999.
    """
    module = _load_pretooluse_module()
    # Ensure no agent is on the port we ask the script to dial.
    free = _free_port()
    saved_port = module.IPC_PORT
    module.IPC_PORT = free
    try:
        stdout, rc = _run_cli_with_stdin(
            json.dumps(
                {
                    "tool_name": "Bash",
                    "command_string": "echo hi",
                    "context": "test",
                }
            )
        )
    finally:
        module.IPC_PORT = saved_port

    assert rc == 0
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"expected at least one JSON line, got {stdout!r}"
    payload = json.loads(lines[-1])
    assert payload == {"continue": True}, payload


async def test_pretooluse_cli_server_allows_and_denies() -> None:
    """End-to-end: hook -> bridge IPC -> allow or deny round-trip.

    Imports :mod:`webrelay.agent.hooks.preToolUse` as a real package
    module so we can patch its ``IPC_PORT`` in one place and have
    both the hook CLI and the bridge see the same value.
    """
    import webrelay.agent.bridges.approval_bridge as bridge_mod
    import webrelay.agent.hooks.preToolUse as hook_mod

    client = _FakeRelayClient()
    bridge = ApprovalBridge(client)

    free = _free_port()
    saved_bridge_port = bridge_mod.IPC_PORT
    saved_hook_port = hook_mod.IPC_PORT
    bridge_mod.IPC_PORT = free
    hook_mod.IPC_PORT = free
    try:
        await bridge.start()
        try:
            # Build the stdin payload the hook will read.
            hook_input = json.dumps(
                {
                    "tool_name": "Bash",
                    "command_string": "rm -rf /",
                    "context": "scary",
                }
            )

            # Run the CLI in a thread so its sync socket code can
            # talk to the asyncio IPC server on the main thread.
            def _run_cli() -> tuple[str, int]:
                saved_stdin = sys.stdin
                saved_stdout = sys.stdout
                sys.stdin = io.StringIO(hook_input)
                sys.stdout = io.StringIO()
                try:
                    rc = hook_mod.main()
                    return sys.stdout.getvalue(), rc
                finally:
                    sys.stdin = saved_stdin
                    sys.stdout = saved_stdout

            cli_task = asyncio.create_task(
                asyncio.to_thread(_run_cli)
            )

            # Wait for the request to land on the fake client.
            for _ in range(200):
                if client.sent:
                    break
                await asyncio.sleep(0.01)
            assert client.sent, "bridge did not push APPROVAL_REQUESTED"
            op, payload = client.sent[-1]
            assert op == Op.APPROVAL_REQUESTED
            prompt_id = payload.prompt_id  # type: ignore[attr-defined]

            # Simulate server reply: deny.
            handler = client.handlers[Op.APPROVAL_RESPOND]
            await handler(
                _make_envelope(
                    Op.APPROVAL_RESPOND,
                    ApprovalRespond(
                        prompt_id=prompt_id,
                        decision="deny",
                        reason="too scary",
                    ),
                ),
                ApprovalRespond(
                    prompt_id=prompt_id,
                    decision="deny",
                    reason="too scary",
                ),
            )

            stdout, rc = await asyncio.wait_for(cli_task, timeout=10.0)
            assert rc == 0
            lines = [line for line in stdout.splitlines() if line.strip()]
            assert lines, f"no JSON line in stdout: {stdout!r}"
            response = json.loads(lines[-1])
            assert response == {
                "continue": False,
                "stop_reason": "too scary",
            }, response
        finally:
            await bridge.stop()
    finally:
        bridge_mod.IPC_PORT = saved_bridge_port
        hook_mod.IPC_PORT = saved_hook_port


# ---------------------------------------------------------------------------
# Sanity: the bridge wires up on the canonical IPC port
# ---------------------------------------------------------------------------


async def test_bridge_canonical_ipc_port() -> None:
    """The bridge uses the canonical 127.0.0.1:15999 by default.

    The spec dictates the port and a regression that defaulted to a
    different port would break the hook script. We assert the
    constant (not a live bind), because a real agent may already be
    bound to 15999 in the developer's environment.
    """
    import webrelay.agent.bridges.approval_bridge as bridge_mod
    import webrelay.agent.hooks.preToolUse as hook_mod

    assert bridge_mod.IPC_HOST == "127.0.0.1"
    assert bridge_mod.IPC_PORT == 15999
    assert hook_mod.IPC_HOST == "127.0.0.1"
    assert hook_mod.IPC_PORT == 15999
