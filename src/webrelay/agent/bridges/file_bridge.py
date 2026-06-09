"""File bridge for the hermes-web-relay agent.

Handles ``Op.FILE_READ`` and ``Op.FILE_LIST`` envelopes from the relay
server. The bridge is bound to a single :class:`RelayClient` and a
``sandbox_root`` (the only directory the agent is allowed to read from
on behalf of the phone UI).

Security model
--------------
The bridge is deliberately paranoid: every inbound path is validated
against the sandbox BEFORE any I/O is attempted. A path is rejected if
any of the following is true:

* It is an absolute path (we accept only relative paths so the
  client cannot probe the local filesystem layout).
* It contains a ``..`` segment anywhere (no traversal escapes).
* It resolves to a location outside ``sandbox_root`` (defence in depth
  on top of the two checks above).
* Any component in the path is a symlink (a symlink may point
  outside the sandbox even if its textual form lives inside it).

Wire-protocol decisions (verified against ``server/protocol.py``)
-----------------------------------------------------------------
* **Inbound (server -> local):** :attr:`Op.FILE_READ` and
  :attr:`Op.FILE_LIST` with ``FileRead`` / ``FileList`` payloads
  (single ``path: str`` field).
* **Outbound (local -> server):** :attr:`Op.FILE_RESULT` with
  :class:`FileResult` payload. ``kind`` is one of:

  * ``"file"``  -- the requested file's content (utf-8 text only;
    binary files are rejected with kind="error" + msg="<binary>").
  * ``"dir"``   -- ``entries`` is a list of ``{name, kind, size, mtime}``.
  * ``"error"`` -- ``error`` is the human-readable reason.

We always reply via :meth:`RelayClient.respond` so the correlation id
matches the original request.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any

from webrelay.agent.client import RelayClient
from webrelay.agent.protocol import (
    Envelope,
    FileList,
    FileRead,
    FileResult,
    Op,
)

_log = logging.getLogger(__name__)


#: Maximum size in bytes we will read from a single file. Mirrors the
#: spec; chosen to be well below the relay WebSocket frame budget so a
#: 5 MB file is safe to inline in a single ``file.result`` frame.
MAX_READ_BYTES: int = 5 * 1024 * 1024  # 5 MB

#: Sentinel error message used for binary files. The exact string is
#: load-bearing: ``file.result`` consumers on the server branch on
#: ``kind == "error"`` to surface "<binary>" as a clear "not text" cue.
_BINARY_MSG = "<binary>"


class FileBridge:
    """Bridge file-read / file-list envelopes from the relay server.

    One instance per agent process. Bound to a single :class:`RelayClient`
    and a single ``sandbox_root``. The bridge is stateless apart from
    the cached resolved root path, so multiple instances against the
    same sandbox are safe (each registers its own handlers, the
    :class:`RelayClient` contract says re-registering replaces the
    previous binding).
    """

    def __init__(self, client: RelayClient, sandbox_root: str) -> None:
        """Configure the bridge.

        Args:
            client: The relay client used to register the inbound
                ``file.read`` / ``file.list`` handlers and to push
                ``file.result`` replies back to the server.
            sandbox_root: The directory the bridge is allowed to serve
                files from. Stored as a resolved absolute
                :class:`pathlib.Path`; every inbound path is validated
                against this root.
        """
        self._client = client
        # Resolve once at construction so per-request validation can
        # just use ``is_relative_to`` (cheaper than a fresh resolve each
        # time, and the root is not allowed to change at runtime).
        self.root: pathlib.Path = pathlib.Path(sandbox_root).resolve()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Register the ``FILE_READ`` and ``FILE_LIST`` handlers."""
        self._client.register_handler(Op.FILE_READ, self.on_file_read)
        self._client.register_handler(Op.FILE_LIST, self.on_file_list)
        _log.info("file bridge started sandbox=%s", self.root)

    # ------------------------------------------------------------------
    # Path validation
    # ------------------------------------------------------------------

    def _validate_path(self, p: str) -> pathlib.Path:
        """Resolve ``p`` relative to ``self.root`` and confirm it is safe.

        Rejects:

        * absolute paths
        * paths containing a ``..`` segment anywhere
        * paths that resolve outside ``self.root``
        * paths in which any component is a symlink (a symlink may
          point at a target outside the sandbox)

        Returns:
            The resolved :class:`pathlib.Path` (always a child of
            ``self.root``).

        Raises:
            ValueError: with a short human-readable reason. Callers
                should map this to ``FileResult(kind="error", ...)``.
        """
        if not p:
            raise ValueError("empty path")

        # Reject absolute paths outright. We accept POSIX and Windows
        # forms: an absolute path either has a leading slash, or a
        # drive letter (e.g. ``C:\\`` / ``C:/``) anywhere in the string.
        if p.startswith("/") or p.startswith("\\"):
            raise ValueError("absolute paths are not allowed")
        # Windows drive-letter check: e.g. "C:" or "C:\\foo".
        if len(p) >= 2 and p[1] == ":":
            raise ValueError("absolute paths are not allowed")

        # Reject traversal segments BEFORE we touch the filesystem.
        # Split on the platform separator AND a forward slash so a
        # POSIX-style payload like "..\\..\\etc\\passwd" is also caught
        # on Windows (and vice versa).
        parts = [seg for seg in p.replace("/", "\\").split("\\") if seg]
        if ".." in parts:
            raise ValueError("path traversal is not allowed")

        # Resolve and re-check containment. We resolve lexically first
        # so the symlink check below operates on the unresolved chain
        # (the post-resolve Path collapses symlinks and we lose the
        # intermediate components).
        candidate = (self.root / p).resolve()

        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise ValueError("path escapes sandbox")

        # Symlink check: walk every component of the un-resolved path
        # chain, including the final segment. We use
        # :meth:`Path.is_symlink` on the joined path; if any component
        # is a symlink the request is rejected even if the symlink
        # target would also be inside the sandbox, because the
        # requested surface is the symlink itself, not its target, and
        # we don't want to silently follow links whose presence the
        # operator may not have intended to expose.
        ancestor = self.root
        for seg in parts:
            ancestor = ancestor / seg
            if ancestor.is_symlink():
                raise ValueError("symlink in path is not allowed")

        return candidate

    # ------------------------------------------------------------------
    # Inbound: file.read
    # ------------------------------------------------------------------

    async def on_file_read(self, envelope: Envelope, payload: FileRead) -> None:
        """Reply to ``file.read`` with a :class:`FileResult`.

        Behaviour:

        * Validation failure -> ``FileResult(kind="error", error=...)``.
        * File larger than :data:`MAX_READ_BYTES` -> error.
        * Binary (non-utf8) content -> ``FileResult(kind="error",
          error="<binary>")``.
        * Success -> ``FileResult(kind="file", content=<text>)``.
        """
        try:
            path = self._validate_path(payload.path)
        except ValueError as exc:
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error=str(exc),
                ),
            )
            return

        try:
            stat = path.stat()
        except FileNotFoundError:
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error="not found",
                ),
            )
            return
        except OSError as exc:
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error=f"stat failed: {exc}",
                ),
            )
            return

        if not path.is_file():
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error="not a regular file",
                ),
            )
            return

        if stat.st_size > MAX_READ_BYTES:
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error=f"file too large: {stat.st_size} bytes (max {MAX_READ_BYTES})",
                ),
            )
            return

        try:
            raw = path.read_bytes()
        except OSError as exc:
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error=f"read failed: {exc}",
                ),
            )
            return

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Binary content. The spec demands a specific marker so
            # the phone UI can branch on it.
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error=_BINARY_MSG,
                ),
            )
            return

        await self._client.respond(
            envelope,
            FileResult(
                path=payload.path,
                kind="file",
                content=content,
            ),
        )

    # ------------------------------------------------------------------
    # Inbound: file.list
    # ------------------------------------------------------------------

    async def on_file_list(self, envelope: Envelope, payload: FileList) -> None:
        """Reply to ``file.list`` with a :class:`FileResult`.

        The reply's ``kind`` is ``"dir"`` on success and ``entries`` is
        a list of ``{name, kind, size, mtime}`` dicts. ``kind`` is
        ``"dir"`` or ``"file"``; ``size`` is the byte size for files
        and 0 for directories; ``mtime`` is the entry's mtime as a
        float (or 0.0 if the stat call fails for a single entry).
        """
        try:
            path = self._validate_path(payload.path)
        except ValueError as exc:
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error=str(exc),
                ),
            )
            return

        try:
            scandir_iter = path.iterdir()
        except FileNotFoundError:
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error="not found",
                ),
            )
            return
        except NotADirectoryError:
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error="not a directory",
                ),
            )
            return
        except OSError as exc:
            await self._client.respond(
                envelope,
                FileResult(
                    path=payload.path,
                    kind="error",
                    error=f"list failed: {exc}",
                ),
            )
            return

        entries: list[dict[str, Any]] = []
        for entry in scandir_iter:
            name = entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            try:
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                is_file = False

            if is_dir:
                kind = "dir"
                size: int = 0
            elif is_file:
                kind = "file"
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
            else:
                # Socket, fifo, broken symlink, etc. -- expose it as a
                # file with size 0 so the operator can still see it.
                kind = "file"
                size = 0

            try:
                mtime: float = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                mtime = 0.0

            entries.append(
                {
                    "name": name,
                    "kind": kind,
                    "size": size,
                    "mtime": mtime,
                }
            )

        # Stable ordering by name makes the UI deterministic.
        entries.sort(key=lambda e: e["name"])

        await self._client.respond(
            envelope,
            FileResult(
                path=payload.path,
                kind="dir",
                entries=entries,
            ),
        )


__all__ = ["FileBridge", "MAX_READ_BYTES"]
