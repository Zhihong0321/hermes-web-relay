"""Authentication routes for the Web UI.

Handles login page rendering, session management, and logout.
"""

from __future__ import annotations

import os
import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, next: str = "/chat") -> HTMLResponse:
    """Render the login form."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "auth/login.html",
        {"request": request, "next": next, "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/chat",
) -> Response:
    """Validate credentials and create a session."""
    admin_user = os.environ.get("WEBRELAY_ADMIN_USER", "admin")
    admin_pass = os.environ.get("WEBRELAY_ADMIN_PASS", "admin")

    if username == admin_user and password == admin_pass:
        request.session["sid"] = uuid.uuid4().hex
        return RedirectResponse(url=next, status_code=302)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "next": next,
            "error": "Invalid username or password.",
        },
    )


@router.get("/logout", response_class=RedirectResponse)
async def logout(request: Request) -> RedirectResponse:
    """Clear session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/auth/login", status_code=302)
