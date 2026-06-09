"""Task Ledger routes for the Web UI.

Exposes endpoints to list and view task ledgers, and starts a background
listener to sync Op.LEDGER_CHANGED pushes from the agent.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webrelay.server.models import LedgerSnapshot
from webrelay.server.protocol import LedgerList, LedgerRead, Op

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/ledgers", tags=["ledgers"])


async def get_session(request: Request) -> AsyncSession:
    """Resolve the database session from the application state."""
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="database not configured")
    async with factory() as session:
        yield session


async def sync_ledgers(hub: Any, session: AsyncSession) -> None:
    """Sync ledger catalog with the agent's files on disk."""
    if not hub.is_connected():
        return
    try:
        list_result = await hub.request(Op.LEDGER_LIST, LedgerList())
        if list_result.ledger_id == "__list__":
            ids = json.loads(list_result.content)

            # Load existing snapshots
            stmt = select(LedgerSnapshot)
            db_snapshots = {s.ledger_id: s for s in (await session.execute(stmt)).scalars().all()}

            # Deletion: remove files from DB that are no longer on the agent
            list_snapshots = list(db_snapshots.keys())
            for lid in list_snapshots:
                if lid not in ids:
                    await session.delete(db_snapshots[lid])
            await session.commit()

            # Read missing ones
            to_fetch = [lid for lid in ids if lid not in db_snapshots]

            if to_fetch:
                async def fetch_one(lid):
                    try:
                        res = await hub.request(Op.LEDGER_READ, LedgerRead(ledger_id=lid))
                        return lid, res
                    except Exception:
                        return lid, None

                results = await asyncio.gather(*(fetch_one(lid) for lid in to_fetch))
                for lid, res in results:
                    if res and res.content:
                        status = _parse_status(res.content)
                        chat_thread_id = _parse_thread_id(res.content)

                        snapshot = LedgerSnapshot(
                            ledger_id=lid,
                            filename=f"task_ledger_{lid}.md",
                            content=res.content,
                            status=status,
                            chat_thread_id=chat_thread_id,
                            mtime=res.mtime,
                        )
                        session.add(snapshot)
                await session.commit()
    except Exception as exc:
        _log.warning("Failed to sync ledgers with agent: %s", exc)


@router.get("/", response_class=HTMLResponse)
async def ledgers_get(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """List all task ledger snapshots."""
    hub = request.app.state.hub
    await sync_ledgers(hub, session)

    # Fetch snapshots
    stmt = select(LedgerSnapshot).order_by(LedgerSnapshot.updated_at.desc())
    ledgers = list((await session.execute(stmt)).scalars().all())

    # Return partial for HTMX polling, or full page
    templates = request.app.state.templates
    if request.headers.get("HX-Request", "").lower() == "true":
        return templates.TemplateResponse(
            "ledgers/partials/list.html",
            {"request": request, "ledgers": ledgers},
        )

    return templates.TemplateResponse(
        "ledgers/index.html",
        {"request": request, "ledgers": ledgers},
    )


@router.get("/{ledger_id}", response_class=HTMLResponse)
async def ledger_detail(
    request: Request,
    ledger_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """View content of a single task ledger."""
    hub = request.app.state.hub

    # Try refreshing from agent
    if hub.is_connected():
        try:
            res = await hub.request(Op.LEDGER_READ, LedgerRead(ledger_id=ledger_id))
            if res and res.content:
                status = _parse_status(res.content)
                chat_thread_id = _parse_thread_id(res.content)

                snapshot = await session.get(LedgerSnapshot, ledger_id)
                if snapshot:
                    snapshot.content = res.content
                    snapshot.mtime = res.mtime
                    snapshot.status = status
                    snapshot.chat_thread_id = chat_thread_id
                else:
                    snapshot = LedgerSnapshot(
                        ledger_id=ledger_id,
                        filename=f"task_ledger_{ledger_id}.md",
                        content=res.content,
                        status=status,
                        chat_thread_id=chat_thread_id,
                        mtime=res.mtime,
                    )
                    session.add(snapshot)
                await session.commit()
        except Exception as exc:
            _log.warning("Failed to refresh ledger %s from agent: %s", ledger_id, exc)

    snapshot = await session.get(LedgerSnapshot, ledger_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Ledger not found")

    templates = request.app.state.templates
    if request.headers.get("HX-Request", "").lower() == "true":
        return templates.TemplateResponse(
            "ledgers/partials/detail_content.html",
            {"request": request, "ledger": snapshot},
        )

    return templates.TemplateResponse(
        "ledgers/detail.html",
        {"request": request, "ledger": snapshot},
    )


# ---------------------------------------------------------------------------
# Background Change Listener
# ---------------------------------------------------------------------------

async def register_ledger_watcher(app: Any) -> None:
    """Persistent background task to sync Op.LEDGER_CHANGED pushes into SQLite."""
    hub = app.state.hub
    factory = app.state.db_session_factory
    _log.info("Ledger background watcher started")

    while True:
        try:
            async for change in hub.subscribe(Op.LEDGER_CHANGED):
                async with factory() as session:
                    status = _parse_status(change.content)
                    chat_thread_id = _parse_thread_id(change.content)
                    filename = f"task_ledger_{change.ledger_id}.md"

                    snapshot = await session.get(LedgerSnapshot, change.ledger_id)
                    if snapshot:
                        snapshot.content = change.content
                        snapshot.mtime = change.mtime
                        snapshot.status = status
                        snapshot.chat_thread_id = chat_thread_id
                        snapshot.filename = filename
                        snapshot.updated_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                    else:
                        snapshot = LedgerSnapshot(
                            ledger_id=change.ledger_id,
                            filename=filename,
                            content=change.content,
                            status=status,
                            chat_thread_id=chat_thread_id,
                            mtime=change.mtime,
                        )
                        session.add(snapshot)
                    await session.commit()
                    _log.debug("Saved ledger snapshot for id %s", change.ledger_id)
        except asyncio.CancelledError:
            _log.info("Ledger background watcher cancelled")
            break
        except Exception as exc:
            _log.error("Error in ledger watcher background loop: %s", exc)
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_status(content: str) -> str:
    """Parse status from ledger frontmatter (defaulting to 'active')."""
    status = "active"
    for line in content.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip().lower() in ("status", "state"):
                status = v.strip().lower()
                break
    return status


def _parse_thread_id(content: str) -> str | None:
    """Parse thread_id from ledger frontmatter."""
    for line in content.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip().lower() in ("thread_id", "thread"):
                return v.strip()
    return None
