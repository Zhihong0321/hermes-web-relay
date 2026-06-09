# Web-Relay MVP Blueprint

## Goal
Mobile-first WebUI hosted on Coolify that lets you chat with your local Hermes Agent and monitor task ledgers from anywhere.

---

## Architecture

```
[Browser / Phone]
       |
       | HTTPS
       v
[Cooify: webrelay-server]  <-- FastAPI + HTMX + Jinja2
       |
       | WebSocket (outbound from local PC)
       v
[Local PC: webrelay-agent]  <-- Dials server, bridges to Hermes
       |
       | JSON-RPC over WS
       v
[Local Hermes]  <-- ws://127.0.0.1:8765/ws (TUI gateway)
```

**Key constraint:** The agent initiates an outbound WebSocket to the server. No inbound firewall holes needed. The server is always on via Coolify. The agent runs on your local PC and reconnects after sleep.

---

## What's Built (Existing)

| Component | Status |
|---|---|
| `src/webrelay/server/main.py` | FastAPI app factory, session auth gate, Jinja, optional router loading |
| `src/webrelay/server/relay_hub.py` | WS hub — one hub, tracks connected agents, broadcasts |
| `src/webrelay/server/db.py` | SQLite session store |
| `src/webrelay/agent/client.py` | RelayClient — WS client to server, send/receive envelopes |
| `src/webrelay/agent/bridges/hermes_bridge.py` | HermesBridge — chat forwarding to Hermes TUI gateway (fully working) |
| `src/webrelay/agent/protocol.py` | Op enum + Pydantic models for all wire protocol ops |
| `src/webrelay/agent/bridges/ledger_bridge.py` | Stub only |
| `src/webrelay/agent/bridges/file_bridge.py` | Stub only |
| `src/webrelay/agent/bridges/approval_bridge.py` | Stub only |
| `src/webrelay/server/routes/status.py` | `/healthz` — works |
| `src/webrelay/server/routes/files.py` | Partially built |
| `src/webrelay/server/routes/approvals.py` | Partially built |

**Missing entirely (needed for MVP):**
- `src/webrelay/server/routes/auth.py` — login page + session handling
- `src/webrelay/server/routes/chat.py` — chat UI + SSE/WS streaming
- `src/webrelay/server/routes/ledgers.py` — ledger list + read pages
- `src/webrelay/server/routes/relay.py` — WS upgrade endpoint `/api/relay/ws`
- `src/webrelay/agent/__main__.py` — agent entry point + runs all bridges
- `src/webrelay/server/static/` — CSS + minimal JS

---

## Wire Protocol (Already Defined)

```json
// Chat: server -> agent -> hermes
{ "op": "chat.send",  "id": "...", "ts": 1234, "payload": { "thread_id": "...", "text": "hello" } }

// Token stream: hermes -> agent -> server -> browser (SSE)
{ "op": "chat.token", "id": "...", "ts": 1234, "payload": { "thread_id": "...", "text": "hi", "seq": 1 } }

// Done: hermes -> agent -> server
{ "op": "chat.done",  "id": "...", "ts": 1234, "payload": { "thread_id": "...", "task_ledger_id": null } }

// Ledger list: server -> agent
{ "op": "ledger.list", "id": "...", "ts": 1234, "payload": {} }

// Ledger result: agent -> server
{ "op": "ledger.result", "id": "...", "ts": 1234, "payload": { "ledger_id": "...", "content": "...", "mtime": 1234 } }
```

All ops defined in `src/webrelay/agent/protocol.py`. Server and agent both import this file (byte-identical copies).

---

## MVP Build Checklist

### Phase 1 — Auth + Relay WS Endpoint

- [ ] **`/auth/login`** — GET shows login form (username + password), POST validates against env credentials, sets `session["sid"]`
- [ ] **`/api/relay/ws`** — WebSocket upgrade endpoint, accepts agent connections with bearer token auth
- [ ] **`relay_hub.py`** — Confirm it handles agent WS connections, stores them, can route messages
- [ ] **`/api/relay/status`** — Returns `{ "connected": true/false, "agent_host": "..." }`

### Phase 2 — Agent Entry Point

- [ ] **`src/webrelay/agent/__main__.py`** — `python -m webrelay.agent` runs the agent
- [ ] Agent connects to `wss://<server>/api/relay/ws` using bearer token from env
- [ ] Sends `hello` op on connect
- [ ] Runs `HermesBridge`, `LedgerBridge`, `FileBridge`, `ApprovalBridge` concurrently
- [ ] Auto-reconnects on disconnect with exponential backoff

### Phase 3 — Chat UI

- [ ] **`/chat`** — HTMX form: text input + submit, SSE stream from `/chat/stream/<thread_id>`
- [ ] **`/chat/stream/<thread_id>`** — SSE endpoint that watches relay hub for `chat.token` + `chat.done` on that thread
- [ ] `hermes_bridge.py` — verify `on_chat_send` is wired to `client.register_handler(Op.CHAT_SEND, self.on_chat_send)` in agent startup
- [ ] Multiple chat threads supported — each `thread_id` is independent

### Phase 4 — Task Ledger Viewer

- [ ] **`/ledgers`** — Page listing all `task_ledger_*.md` in `E:/hermes-agent/`, polled via HTMX every 5s
- [ ] **`/ledgers/<id>`** — Page showing full ledger content, polled every 5s
- [ ] **`LedgerBridge`** — handles `ledger.list` and `ledger.read` ops from server, reads files from disk
- [ ] **No file watcher** — polling only, keep it simple

### Phase 5 — File Browser

- [ ] **`/files`** — HTMX page listing `E:/hermes-agent/` directory
- [ ] **`FileBridge`** — handles `file.list` and `file.read` ops, sandboxed to `E:/hermes-agent/`
- [ ] Breadcrumb nav, file size, click to read

### Phase 6 — Approval Prompts

- [ ] **`/approvals`** — List pending approvals from Hermes
- [ ] **`Approve/Deny` buttons** — POST to server, server sends `approval.respond` to agent via WS
- [ ] **`ApprovalBridge`** — intercepts Hermes, queues pending approvals, waits for user response
- [ ] **Simplified MVP** — show only tool name + "allow/deny", no command context capture

### Phase 7 — Styling + Polish

- [ ] **`/static/app.css`** — Mobile-first CSS, dark mode, clean layout
- [ ] Root `/` redirects to `/chat`
- [ ] Nav bar: Chat | Ledgers | Files | Approvals
- [ ] Session timeout handling

---

## File Structure (Target)

```
web-relay/
  src/webrelay/
    server/
      __init__.py
      main.py              # FastAPI app, already exists
      relay_hub.py         # WS hub, already exists
      db.py                # SQLite, already exists
      models.py            # Pydantic models, already exists
      protocol.py          # Byte-identical copy of agent/protocol.py
      routes/
        __init__.py
        auth.py            # MISSING — login
        chat.py            # MISSING — chat UI + SSE
        ledgers.py         # MISSING — ledger list/read
        files.py           # PARTIAL — needs FileBridge
        approvals.py       # PARTIAL — needs ApprovalBridge
        relay.py           # MISSING — WS endpoint
        status.py          # EXISTS — /healthz
      templates/           # MISSING — Jinja2 templates
        auth/login.html
        chat/index.html
        ledgers/index.html
        ledgers/detail.html
        files/index.html
        approvals/index.html
        base.html
      static/
        app.css            # MISSING
    agent/
      __init__.py
      __main__.py         # MISSING — entry point
      client.py            # EXISTS — RelayClient
      protocol.py         # EXISTS — full Op enum + models
      reconnect.py        # EXISTS — reconnect logic
      config.py           # EXISTS
      bridges/
        __init__.py
        hermes_bridge.py  # EXISTS — fully working
        ledger_bridge.py  # STUB — needs implementation
        file_bridge.py    # STUB — needs implementation
        approval_bridge.py # STUB — needs implementation
      hooks/
        __init__.py
        preToolUse.py     # STUB — needs Hermes hook setup
```

---

## What NOT to build (MVP out of scope)

- File watcher for ledger changes — polling only
- Full approval context capture — just tool name + allow/deny
- Docker / CI/CD / deployment scripts — deploy manually on Coolify
- Database migrations — SQLite schema is created on startup
- Push notifications
- Multiple user / team support
- Hermes configuration from WebUI

---

## Config / Env Vars

```env
# Server side (.env on Coolify)
WEBRELAY_SESSION_SECRET=     # Required, random string
WEBRELAY_ADMIN_USER=        # Username for WebUI login
WEBRELAY_ADMIN_PASS=        # Password for WebUI login
WEBRELAY_AGENT_TOKEN=       # Bearer token agents use to connect

# Agent side (.env on local PC)
WEBRELAY_SERVER_URL=        # wss://your-coolify-domain.com
WEBRELAY_AGENT_TOKEN=       # Must match server side
HERMES_WS_URL=              # ws://127.0.0.1:8765/ws (default)
```

---

## Running It

**Server (Coolify):**
```bash
webrelay-server
# or
python -m webrelay.server.main
```

**Agent (local PC):**
```bash
webrelay-agent
# or
python -m webrelay.agent
```

Agent auto-reconnects. Set it as a startup item or systemd service on your local PC.

---

## Dependencies

Already in `pyproject.toml`:
- fastapi
- uvicorn
- pydantic
- websockets
- httpx (for health checks)
- jinja2
- starlette

Dev:
- pytest
- ruff
