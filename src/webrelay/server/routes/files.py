"""Read-only file-browser proxy route.

Exposes three HTMX-friendly endpoints under the ``/files`` prefix:

* ``GET /``              render the file-browser shell (breadcrumb + table)
* ``GET /list?path=...`` list a directory (HTMX partial: table of entries)
* ``GET /read?path=...`` read a file (HTMX partial: monospace <pre>)

All path arguments are checked *defense-in-depth* against path-traversal
patterns BEFORE we hand the path to the local agent's ``file.list`` /
``file.read`` bridge. The bridge does its own sandboxing, but refusing
obviously-bad paths at the route level gives a clean 400 to the browser
and a clear audit trail.

We do NOT mount this router on the FastAPI app here — that is the job
of ``server/main.py`` in Stage 3, which is the only file allowed to
import every route module.
"""

from __future__ import annotations

from html import escape
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from webrelay.server.protocol import FileList, FileRead, FileResult, Op


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/files", tags=["files"])


# ---------------------------------------------------------------------------
# Path validation (defense-in-depth)
# ---------------------------------------------------------------------------


def _validate_path(path: str) -> str:
    """Reject obvious path-traversal patterns.

    The local-agent ``file_bridge`` is the security boundary — it enforces
    the ``E:/hermes-agent`` sandbox. This helper is a defense-in-depth
    pre-check that surfaces a clean 400 for the most common attack
    shapes without burning a round trip to the agent.

    Rules:
      * Empty / whitespace-only paths are allowed (treated as root by the
        bridge; we forward as ``""`` so the bridge can decide).
      * The path must NOT start with ``/`` (would be absolute on POSIX;
        the sandbox is rooted at ``E:/hermes-agent``).
      * The path must NOT start with ``..`` (would escape upward).
      * The path must NOT contain a backslash — the bridge is
        forward-slash-oriented and a backslash is a hint that the
        client is trying to land on a Windows-only path the server
        should never see.
      * A null byte is always rejected.
    """
    if path is None:
        raise HTTPException(status_code=400, detail="path is required")
    if "\x00" in path:
        raise HTTPException(status_code=400, detail="path contains a null byte")
    if "\\" in path:
        raise HTTPException(
            status_code=400,
            detail="path must use forward slashes; backslashes are not allowed",
        )
    if path.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail="absolute paths are not allowed; pass a path relative to the sandbox root",
        )
    # Reject Windows drive letters (C:, D:, ...) — the sandbox is rooted
    # at E:/hermes-agent and the bridge is forward-slash-only.
    if len(path) >= 2 and path[1] == ":":
        raise HTTPException(
            status_code=400,
            detail="absolute paths are not allowed; pass a path relative to the sandbox root",
        )
    # Reject any segment that is "..", or a path that equals "..".
    segments = [s for s in path.split("/") if s]
    if any(seg == ".." for seg in segments):
        raise HTTPException(
            status_code=400,
            detail="path traversal is not allowed",
        )
    return path


# ---------------------------------------------------------------------------
# Sandbox root (used by the template for the breadcrumb + root listing)
# ---------------------------------------------------------------------------

# The bridge sandbox is fixed at ``E:/hermes-agent`` per ``agent/config.py``
# and the planning doc. We hard-code the same string here so the breadcrumb
# is consistent with what the bridge will actually resolve. The constant
# lives in two places by design — the route layer must not depend on the
# agent's config module.
SANDBOX_ROOT = "E:/hermes-agent"


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def _split_segments(path: str) -> list[tuple[str, str]]:
    """Split ``path`` into ``(label, href)`` breadcrumb pairs.

    The first pair is always ``(SANDBOX_ROOT, "/files?path=")`` (the root
    of the sandbox); subsequent pairs accumulate the path so far.
    """
    if not path:
        return [(SANDBOX_ROOT, "/files")]
    parts: list[tuple[str, str]] = [(SANDBOX_ROOT, "/files")]
    accumulated: list[str] = []
    for segment in path.split("/"):
        if not segment:
            continue
        accumulated.append(segment)
        href = "/files?path=" + "/".join(accumulated)
        parts.append((segment, href))
    return parts


def _render_list_partial(
    request: Request,
    path: str,
    entries: list[dict[str, Any]],
    error: str | None = None,
) -> str:
    """Render the directory-listing fragment used by ``GET /list``.

    The fragment is intentionally small (just the breadcrumb + table) so
    HTMX can swap it in cheaply when the user clicks a folder. The full
    page shell (nav, header, etc.) lives in ``file_browser.html`` and is
    rendered by ``_render_browser``.
    """
    breadcrumbs = _split_segments(path)
    rows: list[str] = []
    for entry in entries:
        name = entry.get("name") or ""
        kind = entry.get("kind") or ""
        size = entry.get("size")
        if kind in ("dir", "directory"):
            # Use the entry's own path if the bridge provided one;
            # otherwise join it onto the current path. The bridge does
            # the same and the planning doc pins the contract.
            child_path = entry.get("path") or (
                f"{path.rstrip('/')}/{name}" if path else name
            )
            icon = (
                '<svg xmlns="http://www.w3.org/2000/svg" class="inline w-4 h-4 '
                'text-indigo-400 mr-1.5" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round" aria-hidden="true">'
                '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'
                "</svg>"
            )
            row = (
                '<tr class="border-b border-zinc-800/60 hover:bg-zinc-800/40">'
                f'<td class="py-2 pl-2">{icon}'
                f'<a class="text-indigo-300 hover:text-indigo-200 font-mono text-sm" '
                f'hx-get="/files/list?path={escape(child_path, quote=True)}" '
                f'hx-target="#file-table" hx-swap="innerHTML" hx-push-url="true">'
                f"{escape(name)}</a></td>"
                '<td class="py-2 pr-2 text-xs text-zinc-500 text-right">—</td>'
                "</tr>"
            )
        else:
            icon = (
                '<svg xmlns="http://www.w3.org/2000/svg" class="inline w-4 h-4 '
                'text-zinc-500 mr-1.5" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
                'stroke-linejoin="round" aria-hidden="true">'
                '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
                '<polyline points="14 2 14 8 20 8"/>'
                "</svg>"
            )
            size_str = "" if size is None else f"{size:,} B"
            row = (
                '<tr class="border-b border-zinc-800/60 hover:bg-zinc-800/40">'
                f'<td class="py-2 pl-2">{icon}'
                f'<a class="text-zinc-200 hover:text-zinc-50 font-mono text-sm" '
                f'hx-get="/files/read?path={escape(name, quote=True) if not path else escape(f"{path.rstrip(chr(47))}/{name}", quote=True)}" '
                f'hx-target="#file-viewer" hx-swap="innerHTML" hx-push-url="false">'
                f"{escape(name)}</a></td>"
                f'<td class="py-2 pr-2 text-xs text-zinc-500 text-right">{size_str}</td>'
                "</tr>"
            )
        rows.append(row)

    # Build breadcrumb
    crumbs: list[str] = []
    for i, (label, href) in enumerate(breadcrumbs):
        sep = "" if i == 0 else (
            '<span class="mx-1 text-zinc-600">/</span>'
        )
        crumbs.append(
            f'{sep}<a class="text-indigo-300 hover:text-indigo-200" '
            f'hx-get="/files/list?path={escape(href.split("path=", 1)[-1], quote=True) if "path=" in href else ""}" '
            f'hx-target="#file-table" hx-swap="innerHTML" '
            f'hx-push-url="true">{escape(label)}</a>'
        )
    breadcrumb_html = (
        '<nav class="text-sm font-mono text-zinc-400 mb-3 break-all">'
        + "".join(crumbs)
        + "</nav>"
    )

    if error:
        body = (
            f'<p class="text-rose-400 text-sm mb-2">{escape(error)}</p>'
        )
    elif not rows:
        body = '<p class="text-zinc-500 text-sm italic">empty directory</p>'
    else:
        body = (
            '<table class="w-full text-left">'
            "<thead>"
            '<tr class="text-xs text-zinc-500 border-b border-zinc-800">'
            '<th class="py-1 pl-2 font-medium">Name</th>'
            '<th class="py-1 pr-2 font-medium text-right">Size</th>'
            "</tr>"
            "</thead>"
            "<tbody>"
            + "".join(rows)
            + "</tbody>"
            "</table>"
        )

    return f'<div id="file-table">{breadcrumb_html}{body}</div>'


def _render_browser(
    request: Request,
    path: str,
    error: str | None = None,
    viewer_html: str | None = None,
    table_html: str | None = None,
) -> Response:
    """Render the full file-browser page used by ``GET /`` and as the
    fallback when the partial is fetched as a top-level navigation.
    """
    templates = request.app.state.templates
    return templates.TemplateResponse(  # type: ignore[union-attr]
        request,
        "file_browser.html",
        {
            "path": path or "",
            "breadcrumbs": _split_segments(path),
            "error": error,
            "viewer_html": viewer_html,
            "table_html": table_html,
        },
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def browser_root(request: Request) -> Response:
    """Render the file-browser shell with the root listing.

    We pass an empty ``path`` so the template renders the sandbox-root
    breadcrumb; the user clicks a folder to drill in. The actual listing
    is fetched lazily by HTMX, so the page loads fast even when the
    remote agent is slow to reply.
    """
    return _render_browser(request, path="")


@router.get("/list", response_class=HTMLResponse)
async def list_path(
    request: Request,
    path: str = Query("", description="Path relative to the sandbox root"),
) -> Response:
    """List a directory and return an HTMX partial with the table of entries.

    Validation:
        * ``path`` is checked for traversal patterns first.
        * The path is then forwarded to the local agent via
          ``hub.request(Op.FILE_LIST, FileList(path=path))``.
        * A successful response (kind="dir") yields a sorted list of
          entries; anything else (kind="error" or an unexpected kind)
          surfaces as a 502 with the bridge's error message.
    """
    path = _validate_path(path)
    hub = request.app.state.hub

    result: FileResult = await hub.request(Op.FILE_LIST, FileList(path=path))

    if result.kind == "error":
        raise HTTPException(status_code=502, detail=result.error or "file list failed")

    if result.kind != "dir" or result.entries is None:
        raise HTTPException(
            status_code=502,
            detail=f"unexpected file.list response kind={result.kind!r}",
        )

    # Sort: folders first, then files, alphabetical (case-insensitive).
    entries = sorted(
        result.entries,
        key=lambda e: (
            0 if (e.get("kind") == "dir" or e.get("kind") == "directory") else 1,
            (e.get("name") or "").lower(),
        ),
    )

    # ``HX-Request: true`` means the caller is an HTMX partial swap;
    # we return the small table partial. Otherwise (e.g. someone hitting
    # /list?path=... in a fresh tab) we return the full page so the
    # browser has the base shell + nav. The full page bakes the listing
    # in directly so the user doesn't have to wait for a second HTMX
    # round-trip — and so curl/disable-JS still works.
    if request.headers.get("HX-Request", "").lower() == "true":
        return HTMLResponse(_render_list_partial(request, path, entries))
    return _render_browser(
        request,
        path=path,
        table_html=_render_list_partial(request, path, entries),
    )


@router.get("/read", response_class=HTMLResponse)
async def read_path(
    request: Request,
    path: str = Query("", description="Path relative to the sandbox root"),
) -> Response:
    """Read a file and render it as a monospace ``<pre>`` block.

    Binary detection: if the bridge delivers content we cannot decode
    as UTF-8, we surface a small ``<binary file, X bytes>`` notice
    instead of mojibake.
    """
    path = _validate_path(path)
    hub = request.app.state.hub

    result: FileResult = await hub.request(Op.FILE_READ, FileRead(path=path))

    if result.kind == "error":
        raise HTTPException(status_code=502, detail=result.error or "file read failed")

    if result.kind != "file" or result.content is None:
        raise HTTPException(
            status_code=502,
            detail=f"unexpected file.read response kind={result.kind!r}",
        )

    # Binary detection: the bridge returns ``content`` as a str. If the
    # bridge sent raw bytes (e.g. a non-UTF-8 file) it'll have already
    # been rejected upstream — we mirror that by trying to detect
    # control bytes in the returned string.
    is_binary = any(ord(c) < 0x09 and c not in "\n\t" for c in result.content[:4096])
    size = len(result.content.encode("utf-8", errors="replace"))

    if is_binary:
        body = f'<p class="text-zinc-400 italic">&lt;binary file, {size} bytes&gt;</p>'
    else:
        body = (
            '<pre class="font-mono text-xs leading-snug whitespace-pre '
            'overflow-x-auto p-3 bg-zinc-900 border border-zinc-800 rounded">'
            + escape(result.content)
            + "</pre>"
        )

    # The read view swaps into a "viewer" region; we keep the wrapping
    # container here so the partial is self-contained.
    html = (
        '<div id="file-viewer" class="px-3 py-2">'
        '<div class="flex items-center justify-between mb-2">'
        f'<span class="text-xs text-zinc-500 font-mono break-all">{escape(path or "/")}</span>'
        '<a class="text-xs text-indigo-400 hover:text-indigo-300" '
        'href="/files">&larr; back</a>'
        "</div>"
        f"{body}"
        "</div>"
    )

    if request.headers.get("HX-Request", "").lower() == "true":
        return HTMLResponse(html)
    return _render_browser(request, path=path, viewer_html=html)


__all__ = [
    "PurePosixPath",  # re-exported for tests that import the module
    "router",
    "SANDBOX_ROOT",
]
