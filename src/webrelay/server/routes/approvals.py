"""HTTP routes for the Approvals tab.

Renders the list of pending sensitive-tool prompts the local agent has
forwarded to the server, accepts allow/deny decisions from the phone,
and fans the decision out to the local agent over the relay WS so the
blocked PreToolUse hook can unblock.

Routes (prefix: ``/approvals``):

* ``GET  /``                       — full page (list + decision cards)
* ``POST /{prompt_id}/decision``   — form-encoded {decision, reason?}
* ``GET  /badge``                  — HTMX partial with the pending count

Decisions are recorded against the row in ``approval_requests`` (decision,
responded_at, responded_by_session) and then pushed to the local agent via
``hub.push(Op.APPROVAL_RESPOND, ApprovalRespond(...))``. The push is the
notification path that releases the agent-side blocking wait.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from webrelay.server.models import (
    ApprovalRequest,
    get_pending_approvals,
)
from webrelay.server.protocol import ApprovalRespond, Op


# ---------------------------------------------------------------------------
# Router + dependency helpers
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/approvals", tags=["approvals"])


async def get_session(request: Request) -> AsyncSession:
    """Resolve the per-request :class:`AsyncSession` placed on app.state.

    The wiring layer (Stage 3) is expected to attach ``request.app.state.db_session_factory``
    on startup. Tests inject their own factory by overriding the
    dependency directly on the FastAPI app.
    """
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="database not configured")
    async with factory() as session:
        yield session


async def get_hub(request: Request):
    """Resolve the connected :class:`RelayHub`, or raise 503.

    The hub may legitimately be detached (no local agent connected).
    In that case we still want to allow the user to *record* their
    decision against the row, so we yield ``None`` and let the route
    decide whether to skip the push.
    """
    return getattr(request.app.state, "hub", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_naive() -> dt.datetime:
    """Naive UTC ``datetime`` matching ``models._utcnow``."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# GET / — render pending approvals page
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def list_pending(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the approvals page with all pending rows newest-first."""
    pending = await get_pending_approvals(session)
    template = request.app.state.jinja_env.get_template("approvals.html")
    html = template.render(
        request=request,
        pending=pending,
        pending_count=len(pending),
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# POST /{prompt_id}/decision — record a decision and unblock the local hook
# ---------------------------------------------------------------------------

@router.post("/{prompt_id}/decision", response_class=HTMLResponse)
async def submit_decision(
    request: Request,
    prompt_id: str,
    decision: Annotated[str, Form()],
    reason: Annotated[str | None, Form()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Apply an allow/deny decision to a pending row and notify the agent.

    * Updates ``decision``, ``responded_at``, and ``responded_by_session``.
    * Pushes ``approval.respond`` to the local agent (best-effort; the
      decision is still recorded if the agent is offline so a later
      reconnect can pick it up).
    * Returns an HTMX partial that the calling card uses to remove
      itself from the list (``hx-swap="outerHTML"``).
    """
    decision_norm = decision.strip().lower()
    if decision_norm not in ("allow", "deny"):
        raise HTTPException(
            status_code=400,
            detail="decision must be 'allow' or 'deny'",
        )

    row = await session.get(ApprovalRequest, prompt_id)
    if row is None:
        raise HTTPException(status_code=404, detail="prompt not found")
    if row.decision is not None:
        # Idempotent: a decision has already been recorded. Still emit
        # the HTMX partial so the card disappears from the list.
        return HTMLResponse(_resolved_card_partial(prompt_id), status_code=200)

    row.decision = decision_norm
    row.reason = reason or None
    row.responded_at = _utcnow_naive()
    row.responded_by_session = request.session.get("sid")
    await session.commit()

    # Fan the decision out to the local agent so its blocking
    # PreToolUse handler can return. The push itself is fire-and-forget;
    # if the agent is offline the decision row is still canonical and
    # the bridge can reconcile on reconnect.
    hub = await get_hub(request)
    if hub is not None:
        try:
            await hub.push(
                Op.APPROVAL_RESPOND,
                ApprovalRespond(
                    prompt_id=prompt_id,
                    decision=decision_norm,
                    reason=row.reason,
                ),
            )
        except Exception:
            # Recording the decision is the source of truth; the push
            # is a best-effort unblock. Don't 500 the user's tap.
            pass

    return HTMLResponse(_resolved_card_partial(prompt_id), status_code=200)


def _resolved_card_partial(prompt_id: str) -> str:
    """HTMX partial that removes the card from the list (outerHTML swap)."""
    # Intentionally empty — ``hx-swap="outerHTML"`` replaces the card
    # element with this empty string, removing it from the DOM.
    return f'<!-- resolved:{prompt_id} -->'


# ---------------------------------------------------------------------------
# GET /badge — nav badge count for the base template
# ---------------------------------------------------------------------------

@router.get("/badge", response_class=HTMLResponse)
async def badge(
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Small HTMX partial: just the pending count, for the nav badge.

    Rendered as a plain text string (e.g. ``"3"``) so the base template
    can swap it into a ``<span id="approvals-badge">`` with no extra
    HTML wrapping.
    """
    pending = await get_pending_approvals(session)
    return HTMLResponse(str(len(pending)))


__all__ = ["router"]
