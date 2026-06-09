"""Health + relay-connection status routes.

The relay-status route is polled by the top app-bar in ``base.html`` every
5 seconds via htmx. It must return a small HTML fragment (not JSON) so htmx
can swap it directly into the badge container.

Routes exposed (no router prefix):

* ``GET /healthz``            -> ``{"ok": true}``           (plain JSON, for ops checks)
* ``GET /api/relay/status``   -> HTML badge fragment        (polled by base.html)
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()



def _render_badge(*, connected: bool) -> str:
    """Build the small badge HTML snippet swapped into the top app-bar.

    The markup is intentionally inline (no template inheritance) — this
    endpoint is polled every 5s and we want the response to be a few
    hundred bytes tops. The class names match the styles in ``app.css``.
    """
    if connected:
        return (
            '<span class="badge is-connected" role="status" aria-live="polite">'
            '<span class="dot" aria-hidden="true"></span>'
            '<span>Connected</span>'
            "</span>"
        )
    return (
        '<span class="badge is-disconnected" role="status" aria-live="polite">'
        '<span class="dot" aria-hidden="true"></span>'
        '<span>Disconnected</span>'
        "</span>"
    )


@router.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Liveness probe — always 200 unless the process is wedged.

    Deliberately does NOT touch the hub: a wedged WS connection should
    not make the whole server look down to the Coolify healthcheck.
    """
    return JSONResponse({"ok": True})


@router.get("/api/relay/status")
async def relay_status(request: Request) -> Response:
    """Return the connected/disconnected status.

    Returns JSON if Accept header contains application/json (per blueprint spec),
    otherwise returns an HTML badge fragment polled by base.html.
    """
    hub = getattr(request.app.state, "hub", None)
    connected = bool(hub.is_connected()) if hub is not None else False

    if "application/json" in request.headers.get("accept", ""):
        agent_host = None
        if connected and getattr(hub, "_hello", None) is not None:
            agent_host = getattr(hub._hello, "host", None)
        return JSONResponse({"connected": connected, "agent_host": agent_host})

    return HTMLResponse(_render_badge(connected=connected))


__all__ = ["router"]
