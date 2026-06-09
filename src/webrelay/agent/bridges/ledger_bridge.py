"""Ledger bridge for the hermes-web-relay agent.

The local agent process watches a directory (default: ``E:/hermes-agent``)
for ``task_ledger_*.md`` files. This bridge:

* Registers inbound handlers for ``Op.LEDGER_LIST`` and
  ``Op.LEDGER_READ`` so the operator UI on the phone can browse and
  fetch ledger content.
* Spawns a background task that uses :func:`watchfiles.awatch` to watch
  the same directory and pushes ``Op.LEDGER_CHANGED`` events to the
  server when a ledger file is created / modified / deleted.

The ``ledger.list`` response shape is not in :mod:`webrelay.server.protocol`
(no payload field carries a list of ids), so we smuggle the listing into
``LedgerResult.content`` as a JSON-encoded string with
``ledger_id="__list__"``. This keeps the contract immutable while still
delivering the full inventory to the server.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from watchfiles import Change, awatch

from webrelay.agent.client import RelayClient
from webrelay.agent.protocol import Envelope, LedgerChanged, LedgerRead, LedgerResult, Op

# Prefix and suffix used to derive the ledger_id from a filename like
# ``task_ledger_2026-06-08-001.md`` -> ``2026-06-08-001``.
LEDGER_PREFIX = "task_ledger_"
LEDGER_SUFFIX = ".md"

# Sentinel ledger_id used in the LEDGER_LIST response to carry the JSON-
# encoded list of ids inside ``LedgerResult.content``.
LIST_SENTINEL_ID = "__list__"

# Debounce window (ms) for repeated change events on the same file.
DEBOUNCE_MS = 300


def _ledger_id_from_path(path: Path) -> str:
    """Derive the ledger_id from a ledger file's path.

    Strips the ``task_ledger_`` prefix and the ``.md`` suffix. Raises
    :class:`ValueError` if the path does not have the expected shape.
    """
    name = path.name
    if not name.startswith(LEDGER_PREFIX) or not name.endswith(LEDGER_SUFFIX):
        raise ValueError(f"not a ledger file: {name!r}")
    return name[len(LEDGER_PREFIX) : -len(LEDGER_SUFFIX)]


def _ledger_path_from_id(watched_dir: Path, ledger_id: str) -> Path:
    """Resolve a ledger_id to an absolute path under ``watched_dir``."""
    return watched_dir / f"{LEDGER_PREFIX}{ledger_id}{LEDGER_SUFFIX}"


class LedgerBridge:
    """Bridge that exposes ``task_ledger_*.md`` files to the relay server.

    The bridge is bound to a single :class:`RelayClient` and watches a
    single directory. It is safe to instantiate from the agent's main
    coroutine; :meth:`start` must be awaited to actually begin serving.
    """

    def __init__(self, client: RelayClient, watched_dir: str) -> None:
        """Configure the bridge.

        Args:
            client: The relay client used both to register inbound
                handlers and to push ``ledger.changed`` events.
            watched_dir: Absolute path to the directory to watch. Must
                already exist; missing directories raise on :meth:`start`.
        """
        self._client = client
        self._watched_dir = Path(watched_dir)
        self._watch_task: asyncio.Task[None] | None = None
        # Per-file debounce timers (filename -> asyncio.Task). When a
        # change event fires we cancel any pending task and reschedule a
        # new emit after DEBOUNCE_MS milliseconds of quiet.
        self._debounce_tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Register handlers and spawn the watcher background task."""
        if not self._watched_dir.is_dir():
            raise FileNotFoundError(
                f"watched directory does not exist: {self._watched_dir}"
            )

        # Register inbound request handlers. The server replies with
        # ``ledger.result`` (we use ``client.respond`` so the correlation
        # id matches the original request).
        self._client.register_handler(Op.LEDGER_LIST, self.on_ledger_list)
        self._client.register_handler(Op.LEDGER_READ, self.on_ledger_read)

        # Spawn the watch loop. We do not await it: it runs for the
        # lifetime of the agent process and is cancelled on shutdown.
        self._watch_task = asyncio.create_task(
            self._watch_loop(), name="ledger-bridge-watch"
        )

    async def stop(self) -> None:
        """Cancel the watcher and clear any pending debounce timers."""
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watch_task = None

        for task in list(self._debounce_tasks.values()):
            task.cancel()
        self._debounce_tasks.clear()

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    async def on_ledger_list(
        self, envelope: Envelope, payload: Any
    ) -> None:
        """Reply to ``ledger.list`` with the list of known ledger ids.

        Because :class:`LedgerResult` has no field for a list, the
        inventory is JSON-encoded and placed in ``content`` with
        ``ledger_id="__list__"``. The server side knows to decode the
        content for this sentinel id.
        """
        ids = self._list_ledger_ids()
        listing = LedgerResult(
            ledger_id=LIST_SENTINEL_ID,
            content=json.dumps(ids),
            mtime=0.0,
        )
        await self._client.respond(envelope, listing)

    async def on_ledger_read(
        self, envelope: Envelope, payload: LedgerRead
    ) -> None:
        """Reply to ``ledger.read`` with the file's content + mtime.

        If the file is missing the reply's ``content`` is the empty
        string and ``mtime`` is 0.0 — this lets the server distinguish
        "not yet created" from "exists and is empty".
        """
        path = _ledger_path_from_id(self._watched_dir, payload.ledger_id)
        if not path.is_file():
            result = LedgerResult(
                ledger_id=payload.ledger_id,
                content="",
                mtime=0.0,
            )
        else:
            stat = path.stat()
            # Read text with explicit encoding; fall back gracefully.
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="utf-8", errors="replace")
            result = LedgerResult(
                ledger_id=payload.ledger_id,
                content=content,
                mtime=stat.st_mtime,
            )
        await self._client.respond(envelope, result)

    # ------------------------------------------------------------------
    # File watcher
    # ------------------------------------------------------------------

    async def _watch_loop(self) -> None:
        """Watch ``watched_dir`` (non-recursively) and push changes.

        Uses :func:`watchfiles.awatch` with ``step=200`` (poll/wait
        step in ms) and debounces per-file with our own
        :func:`_schedule_emit` so a burst of editor saves collapses
        into a single ``ledger.changed`` event.
        """
        async for changes in awatch(
            str(self._watched_dir),
            recursive=False,
            step=200,
        ):
            for change_type, raw_path in changes:
                path = Path(raw_path)
                # Only react to files matching the ledger naming scheme.
                if not (path.name.startswith(LEDGER_PREFIX) and path.name.endswith(LEDGER_SUFFIX)):
                    continue
                if change_type == Change.deleted:
                    # For deletes we still emit so the server can mark
                    # the ledger as gone; content is empty.
                    await self._emit(path, deleted=True)
                else:
                    self._schedule_emit(path)

    def _schedule_emit(self, path: Path) -> None:
        """Debounce-per-file: cancel any pending emit and schedule a new one.

        If the user (or a tool) writes the same file many times in quick
        succession, only the last write — once it has been quiet for
        ``DEBOUNCE_MS`` ms — produces a ``ledger.changed`` event.
        """
        key = path.name
        existing = self._debounce_tasks.get(key)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _delayed_emit() -> None:
            try:
                await asyncio.sleep(DEBOUNCE_MS / 1000.0)
            except asyncio.CancelledError:
                return
            await self._emit(path, deleted=False)
            # Clean up our own entry from the map.
            self._debounce_tasks.pop(key, None)

        self._debounce_tasks[key] = asyncio.create_task(
            _delayed_emit(), name=f"ledger-bridge-debounce-{key}"
        )

    async def _emit(self, path: Path, *, deleted: bool) -> None:
        """Send a single ``ledger.changed`` event for ``path``."""
        try:
            ledger_id = _ledger_id_from_path(path)
        except ValueError:
            return

        if deleted:
            content = ""
            mtime = 0.0
        else:
            try:
                content = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Raced with a delete; treat as delete.
                content = ""
                mtime = 0.0
            except UnicodeDecodeError:
                content = path.read_text(encoding="utf-8", errors="replace")
                mtime = path.stat().st_mtime
            else:
                mtime = path.stat().st_mtime

        frame = LedgerChanged(
            ledger_id=ledger_id,
            content=content,
            mtime=mtime,
        )
        await self._client.send(Op.LEDGER_CHANGED, frame)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _list_ledger_ids(self) -> list[str]:
        """Return the sorted list of ledger ids currently on disk."""
        ids: list[str] = []
        if not self._watched_dir.is_dir():
            return ids
        for entry in os.listdir(self._watched_dir):
            if not (entry.startswith(LEDGER_PREFIX) and entry.endswith(LEDGER_SUFFIX)):
                continue
            path = self._watched_dir / entry
            if not path.is_file():
                continue
            try:
                ids.append(_ledger_id_from_path(path))
            except ValueError:
                continue
        ids.sort()
        return ids


__all__ = ["LedgerBridge"]
