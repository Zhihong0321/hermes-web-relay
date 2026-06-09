"""Chat routes for the Web UI.

Handles rendering the chat history, posting new user messages, and streaming
assistant responses via Server-Sent Events (SSE).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webrelay.server.models import ChatMessage, ChatThread
from webrelay.server.protocol import ChatSend, Op

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


async def get_session(request: Request) -> AsyncSession:
    """Resolve the database session from the application state."""
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="database not configured")
    async with factory() as session:
        yield session


@router.get("/", response_class=HTMLResponse)
async def chat_get(
    request: Request,
    thread_id: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the chat page, loading or creating the active thread."""
    # Fetch all active threads (newest updated first)
    threads_stmt = select(ChatThread).where(ChatThread.is_archived == False).order_by(ChatThread.updated_at.desc())
    threads = list((await session.execute(threads_stmt)).scalars().all())

    active_thread = None
    if thread_id:
        active_thread = await session.get(ChatThread, thread_id)

    # If no specific or valid thread is requested, pick the newest active thread
    if not active_thread and threads:
        active_thread = threads[0]

    # If no threads exist at all, create one
    if not active_thread:
        new_id = str(uuid.uuid4())
        active_thread = ChatThread(id=new_id, title="New Chat")
        session.add(active_thread)
        await session.commit()
        # Refresh the threads list
        threads = [active_thread]

    # Load message history for the active thread
    msg_stmt = select(ChatMessage).where(ChatMessage.thread_id == active_thread.id).order_by(ChatMessage.id.asc())
    messages = list((await session.execute(msg_stmt)).scalars().all())

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "chat/index.html",
        {
            "threads": threads,
            "active_thread_id": active_thread.id,
            "messages": messages,
        },
    )


@router.post("/", response_class=HTMLResponse)
async def chat_post(
    request: Request,
    text: Annotated[str, Form()],
    thread_id: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Submit a user message, save it, and notify the agent."""
    if not thread_id:
        raise HTTPException(status_code=400, detail="thread_id is required")

    thread = await session.get(ChatThread, thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")

    # Get message count to determine if this is the first message (to set a title)
    count_stmt = select(ChatMessage).where(ChatMessage.thread_id == thread_id)
    existing_messages = (await session.execute(count_stmt)).scalars().all()

    # Update thread title if it's the default and this is the first message
    if not existing_messages or thread.title == "New Chat":
        clean_text = text.strip()
        thread.title = clean_text[:40] + "..." if len(clean_text) > 40 else clean_text

    # Save user message to database
    user_msg = ChatMessage(
        thread_id=thread_id,
        role="user",
        content=text,
    )
    session.add(user_msg)
    thread.updated_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    await session.commit()

    # Push to agent
    hub = request.app.state.hub
    try:
        await hub.push(
            Op.CHAT_SEND,
            ChatSend(thread_id=thread_id, text=text),
        )
    except ConnectionError:
        _log.warning("Failed to push chat message to agent: no agent connected")
        # Return an error indicator along with user message so the UI knows
        # the agent is disconnected.
        user_bubble_html = (
            f'<div class="flex justify-end mb-4">'
            f'  <div class="chat-bubble user max-w-[75%] rounded-2xl px-4 py-2.5 bg-indigo-600 text-white text-sm shadow-md font-sans leading-relaxed">'
            f'    {text}'
            f'  </div>'
            f'</div>'
            f'<div class="flex justify-start mb-4 text-xs text-rose-400 font-mono italic px-2">'
            f'  Error: Local agent is disconnected.'
            f'</div>'
        )
        return HTMLResponse(user_bubble_html)

    # Return only the user message bubble to be appended by HTMX
    user_bubble_html = (
        f'<div class="flex justify-end mb-4">'
        f'  <div class="chat-bubble user max-w-[75%] rounded-2xl px-4 py-2.5 bg-indigo-600 text-white text-sm shadow-md font-sans leading-relaxed">'
        f'    {text}'
        f'  </div>'
        f'</div>'
    )
    return HTMLResponse(user_bubble_html)


@router.get("/stream/{thread_id}")
async def chat_stream(
    request: Request,
    thread_id: str,
) -> StreamingResponse:
    """Stream assistant response tokens via Server-Sent Events (SSE)."""
    hub = request.app.state.hub
    factory = request.app.state.db_session_factory

    async def event_generator():
        queue = asyncio.Queue()

        # Helper tasks to consume agent tokens and done signals concurrently
        async def read_gen(gen, name):
            try:
                async for item in gen:
                    await queue.put((name, item))
            except Exception as e:
                _log.debug("SSE consumer task finished: %s", e)

        task_token = asyncio.create_task(read_gen(hub.subscribe(Op.CHAT_TOKEN), "token"))
        task_done = asyncio.create_task(read_gen(hub.subscribe(Op.CHAT_DONE), "done"))

        accumulated_text = []

        try:
            while True:
                # Wait for the next token or done frame
                name, item = await queue.get()

                if name == "token":
                    if item.thread_id == thread_id:
                        accumulated_text.append(item.text)
                        # Yield standard SSE format
                        yield f"event: chat.token\ndata: {item.model_dump_json()}\n\n"

                elif name == "done":
                    if item.thread_id == thread_id:
                        full_text = "".join(accumulated_text)

                        # Write completed assistant reply to database
                        async with factory() as session:
                            msg = ChatMessage(
                                thread_id=thread_id,
                                role="assistant",
                                content=full_text,
                                task_ledger_id=item.task_ledger_id,
                            )
                            session.add(msg)
                            # Update thread update time
                            thread = await session.get(ChatThread, thread_id)
                            if thread:
                                thread.updated_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                            await session.commit()

                        yield f"event: chat.done\ndata: {item.model_dump_json()}\n\n"
                        break
        except Exception as exc:
            _log.warning("Error in SSE event stream: %s", exc)
        finally:
            task_token.cancel()
            task_done.cancel()
            try:
                await asyncio.gather(task_token, task_done, return_exceptions=True)
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
