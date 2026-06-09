"""Tests for the file bridge.

Covers the happy paths (read text, list dir) and the four rejection
paths from the spec:

* path-traversal (``"../../etc/passwd"``) -> error
* absolute path (``"/etc/passwd"`` / ``"C:\\foo"``) -> error
* symlink in path -> error (skipped on Windows when symlink creation
  requires elevation; the test wraps the symlink creation in
  try/except ``OSError`` and ``pytest.skip``s on failure)
* file larger than 5 MB -> error
* binary file -> error (``<binary>`` marker)

A small ``RecordingClient`` stands in for :class:`RelayClient`. It
records every ``send`` and ``respond`` call so the tests can assert
on the frames the bridge produced.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from webrelay.agent.bridges.file_bridge import (
    MAX_READ_BYTES,
    FileBridge,
)
from webrelay.agent.protocol import Envelope, FileList, FileRead, FileResult, Op


# ---------------------------------------------------------------------------
# Recording fake for RelayClient
# ---------------------------------------------------------------------------


class RecordingClient:
    """Drop-in fake for :class:`webrelay.agent.client.RelayClient`.

    Captures every call to ``send`` and ``respond`` on two lists so the
    tests can assert on the frames the bridge produced.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[Op, Any]] = []
        self.responded: list[tuple[Envelope, Any]] = []
        # Handlers registered by the bridge, keyed by op, so tests can
        # also dispatch inbound frames manually if they want to.
        self.handlers: dict[Op, Any] = {}

    def register_handler(self, op: Op, handler: Any) -> None:
        self.handlers[op] = handler

    async def send(
        self, op: Op, payload: Any, *, correlation_id: str | None = None
    ) -> None:
        self.sent.append((op, payload))

    async def respond(self, envelope: Envelope, payload: Any) -> None:
        self.responded.append((envelope, payload))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """An empty sandbox directory used as the bridge's root."""
    d = tmp_path / "sandbox"
    d.mkdir()
    return d


@pytest.fixture
def client() -> RecordingClient:
    return RecordingClient()


@pytest.fixture
def bridge(client: RecordingClient, sandbox: Path) -> FileBridge:
    return FileBridge(client, str(sandbox))  # type: ignore[arg-type]


def _envelope_for(op: Op, envelope_id: str = "env-1") -> Envelope:
    """Build a real :class:`Envelope` for a given op with a known id."""
    return Envelope(op=op, id=envelope_id, ts=0.0, payload={})


def _result_for(envelope: Envelope, responses: list[tuple[Envelope, Any]]) -> Any:
    """Pull the single ``respond(envelope, payload)`` payload for ``envelope``."""
    matched = [p for env, p in responses if env.id == envelope.id]
    assert len(matched) == 1, (
        f"expected exactly 1 response for envelope id={envelope.id!r}, "
        f"got {len(matched)}: {matched!r}"
    )
    return matched[0]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_start_registers_handlers(client: RecordingClient, sandbox: Path) -> None:
    """start() registers FILE_READ and FILE_LIST handlers on the client."""
    bridge = FileBridge(client, str(sandbox))  # type: ignore[arg-type]
    await bridge.start()
    assert Op.FILE_READ in client.handlers
    assert Op.FILE_LIST in client.handlers
    # The handler map is keyed by Op, and each bound method identity
    # is unique, so an ``==`` (or ``is``) check both work; we just want
    # to confirm the bridge wired its own methods, not a foreign one.
    assert client.handlers[Op.FILE_READ] == bridge.on_file_read
    assert client.handlers[Op.FILE_LIST] == bridge.on_file_list


async def test_read_text_file_returns_kind_file(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """A utf-8 text file inside the sandbox is returned as kind='file'."""
    target = sandbox / "hello.txt"
    # ``newline=""`` so Windows doesn't translate "\n" to "\r\n"
    # underneath us -- the bridge reads raw bytes and the test compares
    # the exact content.
    with target.open("w", encoding="utf-8", newline="") as f:
        f.write("hello world\n")

    env = _envelope_for(Op.FILE_READ, "env-read-text")
    await bridge.on_file_read(
        env, FileRead(path="hello.txt")
    )

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "file"
    assert result.path == "hello.txt"
    assert result.content == "hello world\n"
    assert result.error is None
    assert result.entries is None


async def test_read_text_file_in_nested_directory(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """A nested path (no traversal) resolves and reads cleanly."""
    nested = sandbox / "deep" / "nested"
    nested.mkdir(parents=True)
    target = nested / "leaf.md"
    target.write_text("# leaf", encoding="utf-8")

    env = _envelope_for(Op.FILE_READ, "env-nested")
    await bridge.on_file_read(
        env, FileRead(path="deep/nested/leaf.md")
    )

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "file"
    assert result.content == "# leaf"


async def test_list_directory_returns_entries(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """Listing a directory returns sorted entries with name/kind/size/mtime."""
    (sandbox / "a.txt").write_text("aaa", encoding="utf-8")
    (sandbox / "b.txt").write_text("bb", encoding="utf-8")
    (sandbox / "sub").mkdir()
    (sandbox / "sub" / "c.md").write_text("c", encoding="utf-8")

    env = _envelope_for(Op.FILE_LIST, "env-list")
    await bridge.on_file_list(env, FileList(path="."))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "dir"
    assert result.path == "."
    assert result.content is None
    assert result.error is None

    assert result.entries is not None
    by_name = {e["name"]: e for e in result.entries}
    # Sorted alphabetically (stable for the test).
    assert [e["name"] for e in result.entries] == sorted(by_name)

    assert by_name["a.txt"]["kind"] == "file"
    assert by_name["a.txt"]["size"] == 3
    assert isinstance(by_name["a.txt"]["mtime"], float)
    assert by_name["a.txt"]["mtime"] > 0.0

    assert by_name["b.txt"]["kind"] == "file"
    assert by_name["b.txt"]["size"] == 2

    assert by_name["sub"]["kind"] == "dir"
    assert by_name["sub"]["size"] == 0


async def test_list_empty_directory_returns_empty_entries(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """An empty directory returns kind='dir' with an empty entries list."""
    env = _envelope_for(Op.FILE_LIST, "env-empty")
    await bridge.on_file_list(env, FileList(path="."))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "dir"
    assert result.entries == []


# ---------------------------------------------------------------------------
# Path-traversal rejection
# ---------------------------------------------------------------------------


async def test_path_traversal_dotdot_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
) -> None:
    """``../../etc/passwd`` is rejected with kind='error'."""
    env = _envelope_for(Op.FILE_READ, "env-traverse")
    await bridge.on_file_read(
        env, FileRead(path="../../etc/passwd")
    )

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"
    assert result.error is not None
    assert "traversal" in result.error.lower() or ".." in result.error


async def test_path_traversal_mid_segment_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
) -> None:
    """A ``..`` segment in the middle of a path is also rejected."""
    env = _envelope_for(Op.FILE_READ, "env-traverse-mid")
    await bridge.on_file_read(
        env, FileRead(path="safe/../../../etc/passwd")
    )

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"


async def test_path_traversal_list_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
) -> None:
    """``file.list`` also rejects traversal attempts."""
    env = _envelope_for(Op.FILE_LIST, "env-traverse-list")
    await bridge.on_file_list(env, FileList(path="../../"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"


# ---------------------------------------------------------------------------
# Absolute path rejection
# ---------------------------------------------------------------------------


async def test_absolute_path_posix_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
) -> None:
    """A POSIX absolute path is rejected with kind='error'."""
    env = _envelope_for(Op.FILE_READ, "env-abs-posix")
    await bridge.on_file_read(env, FileRead(path="/etc/passwd"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"
    assert result.error is not None
    assert "absolute" in result.error.lower()


async def test_absolute_path_drive_letter_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
) -> None:
    """A Windows-style absolute path (drive letter) is rejected."""
    env = _envelope_for(Op.FILE_READ, "env-abs-drive")
    await bridge.on_file_read(env, FileRead(path="C:\\Windows\\System32"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"
    assert "absolute" in (result.error or "").lower()


async def test_absolute_path_list_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
) -> None:
    """``file.list`` also rejects absolute paths."""
    env = _envelope_for(Op.FILE_LIST, "env-abs-list")
    await bridge.on_file_list(env, FileList(path="/"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"


# ---------------------------------------------------------------------------
# Symlink rejection
# ---------------------------------------------------------------------------


async def test_symlink_in_path_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """A symlink in any component of the path is rejected.

    On Windows, ``os.symlink`` typically requires either admin or
    developer mode. If symlink creation fails with ``OSError`` we skip
    rather than fail the test -- the rejection logic is identical
    across platforms and exercised on the Linux CI.
    """
    target = sandbox / "real.txt"
    target.write_text("real", encoding="utf-8")
    link = sandbox / "link.txt"
    try:
        os.symlink("real.txt", str(link))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation not supported here: {exc!r}")

    env = _envelope_for(Op.FILE_READ, "env-symlink")
    await bridge.on_file_read(env, FileRead(path="link.txt"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"
    assert result.error is not None
    assert "symlink" in result.error.lower()


async def test_symlink_in_parent_directory_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """A symlink in a *parent* component of the path is also rejected.

    Create a symlinked directory inside the sandbox, then a file under
    it, then ask the bridge to read the file. The validation walks
    every parent component, so the symlinked directory should be
    flagged.
    """
    real_sub = sandbox / "real_sub"
    real_sub.mkdir()
    (real_sub / "leaf.txt").write_text("leaf", encoding="utf-8")

    link_sub = sandbox / "link_sub"
    try:
        os.symlink(str(real_sub), str(link_sub))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation not supported here: {exc!r}")

    env = _envelope_for(Op.FILE_READ, "env-symlink-parent")
    await bridge.on_file_read(
        env, FileRead(path="link_sub/leaf.txt")
    )

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"
    assert "symlink" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Oversize file rejection
# ---------------------------------------------------------------------------


async def test_oversize_file_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """A file larger than ``MAX_READ_BYTES`` is rejected with kind='error'."""
    big = sandbox / "big.bin"
    # Write MAX_READ_BYTES + 1 bytes -- a single byte over the limit
    # is enough to trigger the rejection.
    big.write_bytes(b"x" * (MAX_READ_BYTES + 1))

    env = _envelope_for(Op.FILE_READ, "env-big")
    await bridge.on_file_read(env, FileRead(path="big.bin"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"
    assert result.error is not None
    assert "too large" in result.error.lower() or "large" in result.error.lower()


async def test_file_at_exact_limit_is_read(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """A file at exactly ``MAX_READ_BYTES`` is allowed (boundary)."""
    # Build a file that is exactly the limit, all-ASCII so it decodes
    # as utf-8 cleanly. We do NOT compare the full content (it is 5 MB
    # and the assertion is on size, not bytes), we just confirm kind
    # is "file".
    boundary = sandbox / "boundary.txt"
    boundary.write_bytes(b"a" * MAX_READ_BYTES)

    env = _envelope_for(Op.FILE_READ, "env-boundary")
    await bridge.on_file_read(env, FileRead(path="boundary.txt"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "file"
    assert result.content is not None
    assert len(result.content) == MAX_READ_BYTES


# ---------------------------------------------------------------------------
# Binary file rejection
# ---------------------------------------------------------------------------


async def test_binary_file_returns_binary_error(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """A file that is not valid utf-8 returns kind='error' with the binary marker."""
    binary = sandbox / "blob.bin"
    # 0xff 0xfe is invalid as a utf-8 start byte sequence (it's a
    # UTF-16 BOM, not valid UTF-8). Anything non-utf-8 will do.
    binary.write_bytes(b"\xff\xfe\x00\x01\x02 random binary \x80\x81\x82")

    env = _envelope_for(Op.FILE_READ, "env-binary")
    await bridge.on_file_read(env, FileRead(path="blob.bin"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"
    assert result.error == "<binary>"
    assert result.content is None


async def test_empty_text_file_returns_empty_content(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """A zero-byte text file is returned as kind='file' with empty content."""
    (sandbox / "empty.txt").write_bytes(b"")

    env = _envelope_for(Op.FILE_READ, "env-empty")
    await bridge.on_file_read(env, FileRead(path="empty.txt"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "file"
    assert result.content == ""


# ---------------------------------------------------------------------------
# Misc / non-existent paths
# ---------------------------------------------------------------------------


async def test_read_nonexistent_file_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
) -> None:
    """A valid relative path that does not exist returns 'not found'."""
    env = _envelope_for(Op.FILE_READ, "env-missing")
    await bridge.on_file_read(env, FileRead(path="does_not_exist.txt"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"
    assert "not found" in (result.error or "").lower()


async def test_list_nonexistent_directory_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
) -> None:
    """Listing a directory that does not exist returns 'not found'."""
    env = _envelope_for(Op.FILE_LIST, "env-list-missing")
    await bridge.on_file_list(env, FileList(path="nope"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"
    assert "not found" in (result.error or "").lower()


async def test_list_a_file_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """Listing a *file* (not a directory) returns 'not a directory'."""
    (sandbox / "a_file.txt").write_text("hi", encoding="utf-8")

    env = _envelope_for(Op.FILE_LIST, "env-list-file")
    await bridge.on_file_list(env, FileList(path="a_file.txt"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"


async def test_read_a_directory_returns_error(
    bridge: FileBridge,
    client: RecordingClient,
    sandbox: Path,
) -> None:
    """Reading a *directory* returns 'not a regular file'."""
    (sandbox / "a_dir").mkdir()

    env = _envelope_for(Op.FILE_READ, "env-read-dir")
    await bridge.on_file_read(env, FileRead(path="a_dir"))

    result = _result_for(env, client.responded)
    assert isinstance(result, FileResult)
    assert result.kind == "error"


# ---------------------------------------------------------------------------
# Bridge surface (small import sanity check)
# ---------------------------------------------------------------------------


def test_bridge_module_surface() -> None:
    """The bridge module exposes the documented names."""
    from webrelay.agent.bridges import file_bridge

    assert hasattr(file_bridge, "FileBridge")
    assert hasattr(file_bridge, "MAX_READ_BYTES")
    assert file_bridge.MAX_READ_BYTES == 5 * 1024 * 1024
