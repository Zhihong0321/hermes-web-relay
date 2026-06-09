"""Standalone Claude Code PreToolUse hook for hermes-web-relay.

This script is registered as the ``PreToolUse`` hook in the operator's
``~/.claude/settings.json`` (or per-project settings). When Claude Code
is about to run a tool that needs human approval, it spawns this script
with a JSON description of the proposed call on stdin, and consumes a
JSON decision on stdout.

Wire format (Claude Code -> this script)
----------------------------------------
Stdin (single line, JSON)::

    {
      "tool_name": "Bash",
      "command_string": "rm -rf /tmp/foo",
      "context": "deleting stale build cache"
    }

Wire format (this script -> Claude Code)
----------------------------------------
Stdout (single line, JSON)::

    {"continue": true}                                  # allow
    {"continue": false, "stop_reason": "denied by ..."}  # deny

Implementation
--------------
The script dials the in-process :class:`ApprovalBridge` over loopback
TCP (``127.0.0.1:15999``). If the relay agent isn't running the
connect call fails, we print ``{"continue": true}`` and exit 0 — the
operator's work is never blocked just because the relay is offline.

This is intentionally a *synchronous* script (no asyncio import) so
it can run inside the minimal Python environment that Claude Code
exposes to hooks.
"""

from __future__ import annotations

import json
import socket
import sys

# Keep these constants in sync with ApprovalBridge. We hard-code them
# here (rather than importing from the bridge module) so the hook can
# run even if the bridge module fails to import (e.g. dependencies
# missing on a fresh install).
IPC_HOST = "127.0.0.1"
IPC_PORT = 15999
IPC_TIMEOUT_S = 10.0


def _read_hook_input() -> dict[str, str]:
    """Read the Claude Code hook payload from stdin.

    Returns an empty dict on empty / malformed input. Never raises.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "tool_name": str(parsed.get("tool_name") or ""),
        "command_string": str(parsed.get("command_string") or ""),
        "context": str(parsed.get("context") or ""),
    }


def _ipc_request(payload: dict[str, str]) -> dict[str, object]:
    """Synchronous loopback IPC to the running ApprovalBridge.

    On any failure returns ``{"continue": True}`` so the hook fails
    open when the agent is offline. The agent will deny its own
    timeouts, so the safe default is still safe -- this just means
    "we couldn't ask, so we don't block the operator".
    """
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((IPC_HOST, IPC_PORT), timeout=IPC_TIMEOUT_S)
        sock.settimeout(IPC_TIMEOUT_S)
        body = (json.dumps(payload) + "\n").encode("utf-8")
        sock.sendall(body)

        buf = bytearray()
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                return {"continue": True}
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in buf:
                break

        if not buf:
            return {"continue": True}
        line = bytes(buf).split(b"\n", 1)[0]
        try:
            response = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            return {"continue": True}
        if not isinstance(response, dict):
            return {"continue": True}
        return response
    except (OSError, socket.timeout):
        return {"continue": True}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def main() -> int:
    """Read hook input, ask the bridge for a decision, print the result."""
    hook_input = _read_hook_input()
    tool_name = hook_input.get("tool_name", "")
    if not tool_name:
        # Nothing useful to forward; default to allow so we never
        # silently break a tool call.
        sys.stdout.write(json.dumps({"continue": True}))
        sys.stdout.write("\n")
        return 0

    response = _ipc_request(hook_input)
    sys.stdout.write(json.dumps(response))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
