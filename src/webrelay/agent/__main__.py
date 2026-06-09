"""Local agent process entry point.

This is the program the user runs to keep their machine's relay socket
alive. Three sub-commands are supported:

* ``python -m webrelay.agent setup``     -- (optional) one-time
  credential setup. If a ``setup`` module is available we delegate to
  it; otherwise we emit a friendly message telling the user to edit
  ``~/.hermes/vault.json`` by hand. The agent itself only needs
  ``webrelay.server_url`` and ``webrelay.bearer_token`` in the vault.

* ``python -m webrelay.agent run``       -- (default) connect to the
  relay server and run forever, processing inbound envelopes via the
  four bridges (Hermes, Ledger, File, Approval).

* ``python -m webrelay.agent uninstall`` -- (optional) tear-down hook.
  Mirrors ``setup`` so the install/uninstall pair is symmetric.

``run`` blocks until the relay socket is closed or the process is
signalled. SIGINT / SIGTERM cleanly close the WS via the cancellation
path in :meth:`RelayClient.run`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import sys
from typing import Sequence

from pydantic import BaseModel

from webrelay.agent.bridges.approval_bridge import ApprovalBridge
from webrelay.agent.bridges.file_bridge import FileBridge
from webrelay.agent.bridges.hermes_bridge import HermesBridge
from webrelay.agent.bridges.ledger_bridge import LedgerBridge
from webrelay.agent.client import RelayClient
from webrelay.agent.config import load_config
from webrelay.agent.protocol import Hello


_log = logging.getLogger(__name__)


# Default agent version. Bumped by release scripts; not load-bearing.
AGENT_VERSION = "0.1.0"


def _build_hello() -> Hello:
    """Build the initial ``Hello`` frame the agent sends on connect.

    The server uses ``host`` and ``platform`` for the nav-badge tooltip
    and the connection status page; ``agent_version`` lets the server
    refuse an incompatible client.
    """
    return Hello(
        agent_version=AGENT_VERSION,
        hermes_endpoint=None,
        host=socket.gethostname(),
        platform=platform.platform(),
    )


def _load_setup_module():
    """Best-effort: import the optional ``webrelay.agent.setup`` module.

    Returns the module or ``None`` if it is not present. A missing setup
    module is not an error for ``run``; it only matters when the user
    explicitly invokes the ``setup`` subcommand.
    """
    import importlib

    try:
        return importlib.import_module("webrelay.agent.setup")
    except Exception:  # noqa: BLE001 - any failure = not installed
        return None


def _cmd_setup(argv: Sequence[str]) -> int:
    """Handle the ``setup`` subcommand.

    Delegates to ``webrelay.agent.setup`` if it exists; otherwise prints
    a short message and exits 0. The agent itself never calls this — it
    is a user-facing one-time command.
    """
    setup = _load_setup_module()
    if setup is None:
        print(
            "No setup module available. Edit ~/.hermes/vault.json by hand and\n"
            "add:\n"
            '  "webrelay": {\n'
            '    "server_url": "wss://your-relay.example.com/api/relay/ws",\n'
            '    "bearer_token": "<long-random-string>"\n'
            "  }",
            file=sys.stderr,
        )
        return 0
    # Pass the rest of argv through so the setup module can parse its
    # own flags (e.g. ``--write-token``).
    sys.argv = ["webrelay.agent.setup", *argv]
    run = getattr(setup, "main", None) or getattr(setup, "run", None)
    if run is None:
        print("setup module has no main() or run() entry point", file=sys.stderr)
        return 1
    result = run()
    return int(result) if isinstance(result, int) else 0


def _cmd_uninstall(argv: Sequence[str]) -> int:
    """Handle the ``uninstall`` subcommand.

    Symmetric to ``setup`` — delegates to the setup module's
    ``uninstall`` entry point if available.
    """
    setup = _load_setup_module()
    if setup is None:
        print("No setup module available; nothing to uninstall.", file=sys.stderr)
        return 0
    sys.argv = ["webrelay.agent.setup", "uninstall", *argv]
    run = getattr(setup, "uninstall", None) or getattr(setup, "cmd_uninstall", None)
    if run is None:
        print("setup module has no uninstall() entry point", file=sys.stderr)
        return 1
    result = run()
    return int(result) if isinstance(result, int) else 0


def _cmd_run(argv: Sequence[str]) -> int:
    """Handle the ``run`` subcommand (the default).

    Loads the agent config from the vault, builds the Hello frame,
    constructs a :class:`RelayClient`, instantiates all four bridges,
    calls :meth:`Bridge.start` on each, and then enters the
    :meth:`RelayClient.run` loop. ``client.run`` is a coroutine that
    only returns on cancellation / fatal error, so this call blocks
    forever under normal operation.
    """
    config = load_config()
    hello = _build_hello()
    client = RelayClient(
        server_url=config.server_url,
        bearer_token=config.bearer_token,
        hello=hello,
    )

    # Bridges. Each one registers its inbound handlers on the client
    # during :meth:`start`. We construct them all up front so a failure
    # in one (e.g. ledger watcher can't find its dir) surfaces a clean
    # traceback instead of a half-started agent.
    bridges = [
        HermesBridge(client, hermes_ws_url=config.hermes_ws_url),
        LedgerBridge(client, watched_dir=config.watched_ledger_dir),
        FileBridge(client, sandbox_root=config.file_sandbox_root),
        ApprovalBridge(client),
    ]

    async def _bootstrap() -> None:
        for bridge in bridges:
            await bridge.start()
        _log.info(
            "agent started: url=%s host=%s bridges=%d",
            config.server_url,
            hello.host,
            len(bridges),
        )
        # Hand off to the connect-loop. This coroutine only returns on
        # cancellation or fatal WS error.
        await client.run()

    try:
        asyncio.run(_bootstrap())
    except KeyboardInterrupt:  # pragma: no cover - ctrl-C path
        _log.info("agent interrupted; exiting")
    finally:
        # Best-effort teardown of background tasks (the ledger watcher,
        # the approval IPC server). The client itself has already been
        # cancelled by asyncio.run's loop teardown.
        async def _stop() -> None:
            for bridge in bridges:
                stop = getattr(bridge, "stop", None)
                if stop is not None:
                    try:
                        result = stop()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:  # noqa: BLE001 - best effort
                        _log.exception("bridge.stop failed: %r", bridge)

        try:
            asyncio.run(_stop())
        except Exception:  # noqa: BLE001
            pass

    return 0


# ---------------------------------------------------------------------------
# Argv parser
# ---------------------------------------------------------------------------


def _parse_argv(argv: Sequence[str]) -> tuple[str, list[str]]:
    """Pick the sub-command out of ``argv``.

    Returns ``(command, rest)`` where ``command`` is one of
    ``{"setup", "run", "uninstall"}`` and ``rest`` is whatever follows
    for the sub-command to interpret. ``run`` is the default when no
    sub-command is present.
    """
    if not argv:
        return "run", []
    head = argv[0].lstrip("-").lower()
    if head in ("setup", "run", "uninstall"):
        return head, list(argv[1:])
    # Unrecognised first token: treat the whole argv as arguments to
    # the default ``run`` sub-command so ``python -m webrelay.agent
    # --debug`` still boots the agent (the flag is just ignored here).
    return "run", list(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Module entry point.

    Dispatches to ``_cmd_setup`` / ``_cmd_uninstall`` / ``_cmd_run``
    based on the first non-flag argv token. ``run`` is the default.

    Returns the integer exit code the sub-command produced.
    """
    logging.basicConfig(
        level=os.environ.get("WEBRELAY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if argv is None:
        argv = sys.argv[1:]
    command, rest = _parse_argv(argv)
    if command == "setup":
        return _cmd_setup(rest)
    if command == "uninstall":
        return _cmd_uninstall(rest)
    return _cmd_run(rest)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())