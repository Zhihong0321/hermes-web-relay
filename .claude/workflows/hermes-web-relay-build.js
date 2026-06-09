export const meta = {
  name: 'hermes-web-relay-build',
  description: 'Build hermes web-relay (Stages 0-5): bootstrap + contracts + 15 fan-out agents + wiring + verify + installer/skills. Pauses before Coolify deploy.',
  whenToUse: 'Initial build of the web-relay project after plan finalization and task-ledger creation.',
  phases: [
    { title: 'Bootstrap' },
    { title: 'Contracts' },
    { title: 'Fan-out' },
    { title: 'Wiring' },
    { title: 'Verify' },
    { title: 'Installer-Skills' },
  ],
}

const ROOT = args.webRelayRoot
const HERMES = args.hermesInstallRoot
const WS = args.hermesAgentRoot
const PLAN = args.planFile

const RULES = `
GLOBAL RULES (apply to every agent):
- Write ONLY the files listed in your WRITE list. Do NOT modify any other file, especially not pyproject.toml, server/main.py, agent/__main__.py, or files inside ${HERMES}/.
- Use absolute paths (Windows: forward-slashes are fine in Python/Node code; in Bash use Unix syntax).
- Python 3.11+, async/await throughout. Pydantic v2. SQLAlchemy 2.0 declarative.
- HTMX is loaded from CDN in base.html. No npm, no JS framework, no build step.
- Every code file you create must be importable (no syntax errors). Run \`python -c "import <module>"\` before finishing.
- If you write a test file, run it with \`python -m pytest <your_test> -v\` and confirm it passes.
- Return a structured summary of what you did.
`

const SCHEMA_RESULT = {
  type: 'object',
  required: ['agent_id', 'files_written', 'tests_pass', 'notes'],
  properties: {
    agent_id: { type: 'string' },
    files_written: { type: 'array', items: { type: 'string' } },
    tests_pass: { type: 'boolean' },
    notes: { type: 'string', description: 'One paragraph: what you did, any deviations, any blockers.' },
  },
}

// ─────────────────────────────────────────────────────────────────────
// STAGE 0 — Bootstrap
// ─────────────────────────────────────────────────────────────────────
phase('Bootstrap')
log('Stage 0: Bootstrap — creating package skeleton')

const bootstrap = await agent(`
You are AGENT BOOTSTRAP. Create the web-relay package skeleton at ${ROOT}/.

READ FIRST:
- ${PLAN} (read sections: "File layout", "Swarm execution plan", "Wire protocol")

WRITE EXACTLY THESE FILES (and no others):
1. ${ROOT}/pyproject.toml — pip-installable package "hermes-web-relay" with TWO console scripts:
   - "webrelay-server = webrelay.server.main:run"
   - "webrelay-agent = webrelay.agent.__main__:main"
   Dependencies (server + agent superset, all in one install):
     fastapi>=0.110, uvicorn[standard]>=0.27, jinja2>=3.1, python-multipart>=0.0.9,
     sqlalchemy[asyncio]>=2.0, aiosqlite>=0.19, itsdangerous>=2.1, websockets>=12,
     httpx>=0.27, pydantic>=2.6, watchfiles>=0.21, python-dotenv>=1.0,
     argon2-cffi>=23
   Dev deps: pytest>=8, pytest-asyncio>=0.23, respx>=0.21, ruff>=0.4
   Python 3.11+. Use src/ layout: src/webrelay/{server,agent}/.
2. ${ROOT}/README.md — short README explaining the two halves (server + agent), how to install, references task_ledger_webrelay.md.
3. ${ROOT}/.env.example — documented WEBRELAY_PASSWORD, WEBRELAY_SESSION_SECRET, WEBRELAY_RELAY_TOKEN_HASH, WEBRELAY_DB_PATH, WEBRELAY_BASE_URL.
4. ${ROOT}/.gitignore — Python standard + .env + *.db + .workflow-output/.
5. ${ROOT}/src/webrelay/__init__.py — empty package marker.
6. ${ROOT}/src/webrelay/server/__init__.py — empty.
7. ${ROOT}/src/webrelay/server/routes/__init__.py — empty.
8. ${ROOT}/src/webrelay/server/templates/.gitkeep — empty.
9. ${ROOT}/src/webrelay/server/static/.gitkeep — empty.
10. ${ROOT}/src/webrelay/agent/__init__.py — empty.
11. ${ROOT}/src/webrelay/agent/bridges/__init__.py — empty.
12. ${ROOT}/scripts/__init__.py — empty.
13. ${ROOT}/docker/.gitkeep — empty.
14. ${ROOT}/tests/__init__.py — empty.
15. ${ROOT}/tests/conftest.py — common pytest fixtures: a temp SQLite path fixture, a mocked httpx AsyncClient fixture, asyncio mode = auto.
16. ${ROOT}/pytest.ini — [pytest] asyncio_mode = auto, testpaths = tests.
17. ${ROOT}/ruff.toml — line-length 100, target-version py311.

Then run \`cd ${ROOT} && pip install -e . 2>&1 | tail -20\` to install the package in dev mode. If install fails because pip isn't on PATH, try \`python -m pip install -e .\` instead. Report success/failure.

${RULES}

Return: agent_id="bootstrap", files_written=[...], tests_pass=true (no tests yet, return true if package installed cleanly), notes=...
`, { phase: 'Bootstrap', label: 'bootstrap', schema: SCHEMA_RESULT })

if (!bootstrap || !bootstrap.tests_pass) {
  log(`Bootstrap failed: ${bootstrap?.notes ?? 'unknown error'} — aborting workflow`)
  return { aborted_at: 'bootstrap', bootstrap }
}

log(`Bootstrap done — ${bootstrap.files_written.length} files`)

// ─────────────────────────────────────────────────────────────────────
// STAGE 1 — Contracts (3 parallel)
// ─────────────────────────────────────────────────────────────────────
phase('Contracts')
log('Stage 1: Contracts — protocol + models + skeletons in parallel')

const contracts = await parallel([
  () => agent(`
You are AGENT C1 (PROTOCOL). Define the wire-protocol message schemas.

READ FIRST:
- ${PLAN} (section "Wire protocol (JSON over the single relay WebSocket)" — copy the table verbatim into your schemas)

WRITE EXACTLY THESE FILES:
1. ${ROOT}/src/webrelay/server/protocol.py
2. ${ROOT}/src/webrelay/agent/protocol.py
(Both files MUST be byte-identical — agent and server use the same wire format.)

CONTENT — each file contains:
- A string-valued enum class \`Op\` with members: CHAT_SEND, CHAT_TOKEN, CHAT_DONE, FILE_READ, FILE_LIST, FILE_RESULT, LEDGER_LIST, LEDGER_READ, LEDGER_RESULT, LEDGER_CHANGED, APPROVAL_REQUESTED, APPROVAL_RESPOND, PING, PONG, HELLO, ERROR. Values are the dotted strings ("chat.send" etc.).
- A pydantic v2 BaseModel \`Envelope\` with fields: op: Op, id: str (correlation), ts: float (unix seconds), payload: dict.
- Pydantic v2 BaseModel classes for every payload, named after the op in PascalCase: ChatSend, ChatToken, ChatDone, FileRead, FileList, FileResult, LedgerList, LedgerRead, LedgerResult, LedgerChanged, ApprovalRequested, ApprovalRespond, Ping, Pong, Hello, ErrorPayload.
- A \`PAYLOAD_MAP: dict[Op, type[BaseModel]]\` mapping each op to its payload class.
- A helper \`parse_envelope(raw: str | bytes) -> tuple[Envelope, BaseModel]\` that JSON-parses and returns (envelope, validated payload).
- A helper \`build_envelope(op: Op, payload: BaseModel, id: str | None = None) -> str\` returning JSON. If id is None, use uuid4 hex.

Field shapes (match the plan):
- ChatSend: thread_id: str, text: str
- ChatToken: thread_id: str, text: str (partial token), seq: int
- ChatDone: thread_id: str, task_ledger_id: str | None
- FileRead: path: str (relative to ${WS})
- FileList: path: str
- FileResult: path: str, kind: str ("file" | "dir" | "error"), content: str | None, entries: list[dict] | None, error: str | None
- LedgerList: (empty)
- LedgerRead: ledger_id: str
- LedgerResult: ledger_id: str, content: str, mtime: float
- LedgerChanged: ledger_id: str, content: str, mtime: float
- ApprovalRequested: prompt_id: str, tool_name: str, command: str, context: str
- ApprovalRespond: prompt_id: str, decision: str ("allow" | "deny"), reason: str | None
- Ping: (empty); Pong: (empty)
- Hello: agent_version: str, hermes_endpoint: str | None, host: str, platform: str
- ErrorPayload: code: str, message: str, original_op: str | None

ALSO write ${ROOT}/tests/test_protocol.py with:
- A test that every Op has an entry in PAYLOAD_MAP
- A test that build_envelope -> parse_envelope round-trips losslessly for each op
- A test that parse_envelope rejects unknown ops with a clear error

Run \`cd ${ROOT} && python -m pytest tests/test_protocol.py -v\` and confirm pass.

${RULES}
Return agent_id="C1".
`, { phase: 'Contracts', label: 'C1-protocol', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT C2 (MODELS). Define SQLAlchemy 2.0 declarative ORM models for the server.

READ FIRST:
- ${PLAN} (sections "Task-ledger workflow changes" + "Wire protocol" + "File layout")
- ${WS}/task_ledger_template.md (to understand ledger file shape)
- ${WS}/task_ledger_copilot_m3.md (real example)

WRITE EXACTLY THIS FILE:
${ROOT}/src/webrelay/server/models.py

CONTENT — using SQLAlchemy 2.0 declarative_base, AsyncAttrs, Mapped/mapped_column:

class Base(AsyncAttrs, DeclarativeBase): pass

Tables:
- RelayClient: id (pk autoincrement), token_hash (str, unique, indexed), host (str), platform (str), agent_version (str), first_seen (datetime utcnow default), last_seen (datetime, nullable). Tracks which local agent is registered/connected.
- ChatThread: id (str uuid pk), title (str, nullable), created_at (datetime), updated_at (datetime), is_archived (bool default false).
- ChatMessage: id (int autoincrement pk), thread_id (fk -> ChatThread.id, indexed), role (str: "user" | "assistant" | "system"), content (text), task_ledger_id (str, nullable, indexed — fk to LedgerSnapshot.ledger_id when this assistant turn spawned a task), created_at (datetime), token_count (int, nullable). Relationship to thread.
- LedgerSnapshot: ledger_id (str pk — the file basename without "task_ledger_" prefix and without ".md" suffix, e.g. "webrelay"), filename (str — full basename), content (text), status (str — "PLANNING" | "IN_PROGRESS" | "COMPLETED" | "BLOCKED" parsed from ledger), chat_thread_id (str, nullable, fk -> ChatThread.id), mtime (float — file mtime from local), updated_at (datetime utcnow). Latest snapshot wins; we don't keep history.
- ApprovalRequest: prompt_id (str pk), tool_name (str), command (text), context (text), decision (str, nullable — "allow" | "deny" | null=pending), reason (text, nullable), requested_at (datetime), responded_at (datetime, nullable), responded_by_session (str, nullable — session id of browser that responded). Indexed by (decision IS NULL) for fast pending lookup.

ALSO write at the BOTTOM of the same file:
- An async function \`init_db(engine: AsyncEngine) -> None\` that creates all tables (Base.metadata.create_all).
- An async function \`get_pending_approvals(session) -> list[ApprovalRequest]\` returning all where decision IS NULL ordered by requested_at desc.
- An async function \`get_latest_ledgers(session, limit: int = 50) -> list[LedgerSnapshot]\` ordered by updated_at desc.

WRITE TEST: ${ROOT}/tests/test_models.py
- Use the conftest sqlite_path fixture to create an engine
- Test create_all works
- Test insert + query for each table
- Test get_pending_approvals returns only pending
- Test get_latest_ledgers ordering

Run \`cd ${ROOT} && python -m pytest tests/test_models.py -v\` and confirm pass.

${RULES}
Return agent_id="C2".
`, { phase: 'Contracts', label: 'C2-models', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT C3 (SKELETONS). Write the public API of the server-side relay hub and the local agent client. Bodies are stubs that raise NotImplementedError — the IMPL agents in Stage 2 fill them in. The signatures you choose are the contract everyone codes against.

READ FIRST:
- ${PLAN} (sections "Architecture" + "Wire protocol" + "Swarm execution plan")
- ${ROOT}/src/webrelay/server/protocol.py (you may not see this yet if C1 hasn't finished; the wire protocol table in the plan is your source of truth either way)

WRITE EXACTLY THESE FILES:
1. ${ROOT}/src/webrelay/server/relay_hub.py
2. ${ROOT}/src/webrelay/agent/client.py
3. ${ROOT}/src/webrelay/agent/reconnect.py
4. ${ROOT}/src/webrelay/agent/config.py

server/relay_hub.py — class RelayHub:
  def __init__(self, *, request_timeout_s: float = 30.0) -> None
  async def attach(self, websocket, hello_payload) -> None  # called when a local agent connects with a valid bearer
  async def detach(self, websocket) -> None
  def is_connected(self) -> bool
  async def request(self, op: Op, payload: BaseModel) -> BaseModel  # send op, await correlated response within timeout
  async def push(self, op: Op, payload: BaseModel) -> None  # fire-and-forget
  async def on_inbound(self, raw: str) -> None  # called by the WS endpoint for every received frame; routes to either request-correlation futures, push handlers, or queues an event for SSE subscribers
  def subscribe(self, op: Op) -> AsyncIterator[BaseModel]  # async generator yielding inbound pushes of a given op (for SSE)
All bodies raise NotImplementedError("filled in by S2 in Stage 2"). Include docstrings explaining what each must do.

agent/client.py — class RelayClient:
  def __init__(self, server_url: str, bearer_token: str, hello: Hello) -> None
  async def run(self) -> None  # connect-loop with backoff; never returns unless cancelled
  def register_handler(self, op: Op, handler: Callable[[Envelope, BaseModel], Awaitable[None]]) -> None
  async def send(self, op: Op, payload: BaseModel, *, correlation_id: str | None = None) -> None
  async def respond(self, envelope: Envelope, payload: BaseModel) -> None  # respond to a request, reusing the correlation id
All bodies raise NotImplementedError("filled in by L1 in Stage 2"). Include docstrings.

agent/reconnect.py — async generator function reconnect_backoff(initial=1.0, max=60.0, jitter=0.3) yielding floats. Body: raise NotImplementedError.

agent/config.py:
  @dataclass(frozen=True)
  class AgentConfig:
    server_url: str
    bearer_token: str
    hermes_ws_url: str  # default "ws://127.0.0.1:9119/api/ws"
    watched_ledger_dir: str  # default ${WS}
    file_sandbox_root: str  # default ${WS}
  def load_config(vault_path: str | None = None) -> AgentConfig
Bodies raise NotImplementedError.

WRITE TEST: ${ROOT}/tests/test_skeletons.py
- Import each module
- Assert each class can be instantiated where signatures allow
- Assert calling a NotImplementedError-stub method raises NotImplementedError (proves the signatures are wired)

Run pytest on this file and confirm pass.

${RULES}
Return agent_id="C3".
`, { phase: 'Contracts', label: 'C3-skeletons', schema: SCHEMA_RESULT }),
])

const contractsFailed = contracts.filter(r => !r || !r.tests_pass)
if (contractsFailed.length) {
  log(`Contracts stage had ${contractsFailed.length} failures — aborting`)
  return { aborted_at: 'contracts', contracts }
}
log(`Contracts done: ${contracts.map(c => c.agent_id).join(', ')}`)

// ─────────────────────────────────────────────────────────────────────
// STAGE 2 — Fan-out (15 parallel)
// ─────────────────────────────────────────────────────────────────────
phase('Fan-out')
log('Stage 2: Fan-out — 15 feature agents in parallel')

const CONTRACTS_READ_LIST = `
READ FIRST (these are the contracts; do NOT modify them):
- ${ROOT}/src/webrelay/server/protocol.py — op schemas (or agent/protocol.py for bridge agents — they are identical)
- ${ROOT}/src/webrelay/server/models.py — DB tables (server-side agents only)
- ${ROOT}/src/webrelay/server/relay_hub.py — RelayHub public API (use \`hub.request\` / \`hub.push\` / \`hub.subscribe\`)
- ${ROOT}/src/webrelay/agent/client.py — RelayClient public API (bridge agents register handlers via \`client.register_handler\`)
- ${PLAN} — full plan with wire protocol table, file layout, decisions
`

const fanout = await parallel([
  () => agent(`
You are AGENT R1 (CHAT ROUTE). Build the chat-thread route + templates.

${CONTRACTS_READ_LIST}

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/server/routes/chat.py — APIRouter prefix="/chat":
    GET "/" -> list threads (newest first), render chat_list.html
    POST "/" -> create new thread (title from form), redirect to /chat/{id}
    GET "/{thread_id}" -> render chat.html with thread + messages
    POST "/{thread_id}/messages" -> form-encoded {text: str}; save user ChatMessage; call \`await hub.request(Op.CHAT_SEND, ChatSend(thread_id=..., text=...))\`; save assistant ChatMessage with response; return HTMX-friendly partial
    GET "/{thread_id}/stream" -> SSE endpoint yielding chat.token events as they arrive via \`hub.subscribe(Op.CHAT_TOKEN)\` filtered by thread_id
  Use FastAPI dependencies for DB session and \`require_session\`.
- ${ROOT}/src/webrelay/server/templates/chat_list.html — extends base.html; mobile-first list of threads with "+ new" button
- ${ROOT}/src/webrelay/server/templates/chat.html — extends base.html; message log + bottom-fixed compose box (HTMX form, target=#messages); hx-ext="sse" for stream
- ${ROOT}/src/webrelay/server/templates/_bubble.html — single chat-bubble partial; takes a message variable
- ${ROOT}/tests/test_route_chat.py — pytest using FastAPI TestClient. Monkeypatch the RelayHub to return canned responses. Test: list empty, create thread, send message, get reply.

Run pytest on your test file. ${RULES}
Return agent_id="R1".
`, { phase: 'Fan-out', label: 'R1-chat', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT R2 (LEDGERS ROUTE). Build the task-ledger viewer route.

${CONTRACTS_READ_LIST}

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/server/routes/ledgers.py — APIRouter prefix="/ledgers":
    GET "/" -> list LedgerSnapshot rows ordered by updated_at desc; render ledger_list.html. Show status badge ("PLANNING"/"IN_PROGRESS"/"COMPLETED"/"BLOCKED") with color.
    GET "/{ledger_id}" -> render ledger_view.html with parsed markdown of the snapshot's content. Use Python's stdlib (\`markdown\` is NOT in deps; do simple html-escape + <pre> for now — note in docstring that we may add a markdown lib later).
    GET "/{ledger_id}/stream" -> SSE yielding new content whenever a LedgerChanged event arrives via \`hub.subscribe(Op.LEDGER_CHANGED)\` filtered by ledger_id
    POST "/refresh" -> trigger \`hub.request(Op.LEDGER_LIST, ...)\` to pull all ledgers from local agent (initial sync after a fresh server start)
- ${ROOT}/src/webrelay/server/templates/ledger_list.html — extends base; mobile-first list; each row: badge + title + last-updated relative time
- ${ROOT}/src/webrelay/server/templates/ledger_view.html — extends base; live-updates section subscribed to /ledgers/{id}/stream; "Refresh" button hits POST /ledgers/refresh
- ${ROOT}/tests/test_route_ledgers.py — TestClient + monkeypatched hub. Test: empty list, populated list, view a snapshot.

Run pytest. ${RULES}
Return agent_id="R2".
`, { phase: 'Fan-out', label: 'R2-ledgers', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT R3 (FILES ROUTE). Build the read-only file-browser proxy.

${CONTRACTS_READ_LIST}

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/server/routes/files.py — APIRouter prefix="/files":
    GET "/" -> render file_browser.html with no path (root listing)
    GET "/list?path=..." -> call \`await hub.request(Op.FILE_LIST, FileList(path=path))\`; render or return an HTMX partial table of dir entries
    GET "/read?path=..." -> call \`await hub.request(Op.FILE_READ, FileRead(path=path))\`; render the content as monospace <pre> escaped HTML. For binary files (no UTF-8 decode possible), show "<binary file, X bytes>".
  Reject absolute paths starting with anything that escapes ${WS} — the bridge re-validates, but defense in depth: the route should refuse any path that starts with ".." or "/" or contains "\\\\". Return 400.
- ${ROOT}/src/webrelay/server/templates/file_browser.html — extends base; mobile-first; breadcrumb path + table of entries (folders first, then files, alphabetical); clicking a folder loads its listing via hx-get into the table area; clicking a file loads its content into a viewer pane.
- ${ROOT}/tests/test_route_files.py — TestClient + monkeypatched hub. Test happy path (list, read) + path-traversal rejection (../etc/passwd -> 400) + symlink path is rejected by the bridge mock.

Run pytest. ${RULES}
Return agent_id="R3".
`, { phase: 'Fan-out', label: 'R3-files', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT R4 (APPROVALS ROUTE). Build the approval-prompt UI.

${CONTRACTS_READ_LIST}

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/server/routes/approvals.py — APIRouter prefix="/approvals":
    GET "/" -> render approvals.html with all pending ApprovalRequest rows (decision IS NULL), ordered by requested_at desc
    POST "/{prompt_id}/decision" -> form-encoded {decision: "allow"|"deny", reason?: str}; update row (decision, responded_at, responded_by_session=request.session.get("sid")); call \`await hub.push(Op.APPROVAL_RESPOND, ApprovalRespond(...))\` to unblock the local-side handler; return HTMX partial removing the card.
    GET "/badge" -> returns a small HTMX partial with the count of pending approvals (used by base.html nav badge).
- ${ROOT}/src/webrelay/server/templates/approvals.html — extends base; mobile-first card list. Each card: tool_name, command (code block), context, large Allow + Deny buttons (POSTing the decision via hx-post). hx-swap="outerHTML" so the card removes itself.
- ${ROOT}/tests/test_route_approvals.py — TestClient + monkeypatched hub. Test: list empty, list with pending, allow a request, deny a request, badge count.

Run pytest. ${RULES}
Return agent_id="R4".
`, { phase: 'Fan-out', label: 'R4-approvals', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT R5 (RELAY-WS + AUTH). Build the WebSocket endpoint the local agent dials into, and the browser-auth flow.

${CONTRACTS_READ_LIST}

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/server/routes/relay.py — APIRouter at prefix="/api/relay":
    WS "/ws" — accept connection, read Authorization: Bearer header from the WS upgrade request, hash with SHA256, compare to env var WEBRELAY_RELAY_TOKEN_HASH (constant-time). If mismatch, close with code 4401. Otherwise:
      - Read first frame, expect a Hello envelope
      - \`await hub.attach(websocket, hello_payload)\`
      - Loop: \`await hub.on_inbound(await websocket.receive_text())\` until disconnect; finally \`await hub.detach(websocket)\`
- ${ROOT}/src/webrelay/server/routes/auth.py — APIRouter at prefix="/auth":
    GET "/login" -> render login.html
    POST "/login" -> form-encoded {password: str}; compare with env var WEBRELAY_PASSWORD using argon2 if hash, or constant-time str compare otherwise. On match, set signed cookie session "sid" = random hex; redirect to "/". On mismatch, re-render with error and HTTP 401.
    POST "/logout" -> clear cookie; redirect to /auth/login.
- ${ROOT}/src/webrelay/server/auth.py — module with:
    \`get_session_serializer() -> itsdangerous.URLSafeTimedSerializer\` reading WEBRELAY_SESSION_SECRET
    \`def require_session(request: Request) -> str\` — FastAPI dependency raising HTTPException(401, redirect=...) if no valid session cookie, else returning sid
    \`def set_session(response: Response, sid: str) -> None\` writes cookie HttpOnly+Secure+SameSite=Lax, max_age=30d
    \`def clear_session(response: Response) -> None\`
- ${ROOT}/src/webrelay/server/templates/login.html — extends base; mobile-first password form. If error param set, show red banner.
- ${ROOT}/tests/test_auth.py — test serializer round-trip, require_session dep with valid/invalid cookie, login flow happy + bad password.
- ${ROOT}/tests/test_relay_ws.py — pytest with httpx + a small in-memory hub. Test: WS connect with bad token -> 4401; good token + hello -> attach called; subsequent frames -> on_inbound called.

Run pytest on both your test files. ${RULES}
Return agent_id="R5".
`, { phase: 'Fan-out', label: 'R5-relay-auth', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT S1 (DB IMPL). Implement the SQLAlchemy async engine + session factory.

${CONTRACTS_READ_LIST}

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/server/db.py:
    Read DB path from env var WEBRELAY_DB_PATH (default "./webrelay.db" for local dev)
    \`create_engine(db_path: str) -> AsyncEngine\` returning a create_async_engine with sqlite+aiosqlite URL
    Module-level lazy singleton _engine and _session_maker (use a \`get_engine()\` and \`get_session_maker()\` functions, NOT module-import-time side effects)
    \`async_session()\` context-manager dependency for FastAPI endpoints
    \`async def init_db() -> None\` calls models.init_db with the engine
    A startup hook \`async def on_startup() -> None\` that calls init_db; designed to be passed to FastAPI lifespan
- ${ROOT}/tests/test_db.py:
    Use a temp file path
    Test get_engine returns the same instance twice
    Test on_startup creates tables (verify by inserting a row and selecting it back)
    Test async_session yields a usable AsyncSession

Run pytest. ${RULES}
Return agent_id="S1".
`, { phase: 'Fan-out', label: 'S1-db', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT S2 (RELAY HUB IMPL). Fill in the body of RelayHub (currently raises NotImplementedError).

${CONTRACTS_READ_LIST}
Read the existing skeleton at ${ROOT}/src/webrelay/server/relay_hub.py to keep the SAME public signatures. ONLY replace the bodies; do not change the class signature.

REQUIREMENTS:
- Single connected local agent at any time (multi-host comes later). attach() rejects (raises ValueError) if one is already attached, after logging.
- request(op, payload):
    Generate uuid4 hex correlation id
    Create asyncio.Future and store in _pending[id]
    Send Envelope via the active websocket
    \`await asyncio.wait_for(future, timeout=self.request_timeout_s)\`
    On timeout: pop from _pending, raise TimeoutError
- push(op, payload): build envelope with fresh id and send; no future
- on_inbound(raw):
    Parse envelope via protocol.parse_envelope
    If envelope.id in _pending -> set_result on the future
    Else: route to all subscribers of envelope.op via internal asyncio.Queues
- subscribe(op): create a new asyncio.Queue, register it; return an async generator that yields queue.get() forever. Make sure unsubscribing on generator close removes the queue (use try/finally).
- detach(): cancel all pending futures with ConnectionError; close all subscriber queues with a sentinel; clear state.
- is_connected(): return self._websocket is not None

ALSO update ${ROOT}/tests/test_skeletons.py: remove or update the "raises NotImplementedError" assertion for RelayHub (it should now work end-to-end). Add a new ${ROOT}/tests/test_relay_hub_impl.py with:
- Test request/response with a fake websocket (collects sent frames, lets you manually call on_inbound to simulate a response)
- Test request timeout
- Test push (no response expected)
- Test subscribe yields events
- Test detach cancels pending

Run pytest. ${RULES}
Return agent_id="S2".
`, { phase: 'Fan-out', label: 'S2-hub-impl', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT S3 (BASE TEMPLATE + CSS + PWA). Build the shared mobile-first shell.

${CONTRACTS_READ_LIST}

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/server/templates/base.html:
    HTML5, mobile-viewport meta, dark theme by default
    <head> loads HTMX 1.9+ from CDN, htmx-ext-sse, our app.css
    Body has: top app-bar with "Connected" badge that polls /api/relay/status every 5s (hx-get, hx-trigger="every 5s"), and a bottom fixed nav with 4 tabs: Chat, Ledgers, Files, Approvals (with badge count)
    {% block content %} in the middle scrollable area
    Link to /auth/logout in app-bar
    PWA: <link rel="manifest" href="/static/manifest.json">, <meta name="theme-color" content="#0a0a0a">
- ${ROOT}/src/webrelay/server/static/app.css:
    Tailwind via CDN: <script src="https://cdn.tailwindcss.com"></script> in base.html (don't write a tailwind.config; the few customizations can be inline tailwind classes)
    Dark theme color palette (use Tailwind dark: classes); zinc-950 background, zinc-100 text, accent indigo-500
    Bottom-nav fixed, height 56px, tap-target >= 44px
    Chat bubbles: user right-aligned indigo-700, assistant left-aligned zinc-800
    SAFE-AREA insets (env(safe-area-inset-bottom)) for iPhone home-bar
- ${ROOT}/src/webrelay/server/static/manifest.json:
    Standalone PWA, name "Hermes Relay", short_name "Hermes", theme #0a0a0a, background #0a0a0a, single 192px icon path /static/icon-192.png (we'll add the icon later; reference it now)
- DO NOT write any other templates.

Also write a tiny health/status route file:
- ${ROOT}/src/webrelay/server/routes/status.py — APIRouter at no prefix, GET "/healthz" -> {"ok": True}, GET "/api/relay/status" -> returns "connected" or "disconnected" badge HTML based on hub.is_connected().

ALSO write ${ROOT}/tests/test_base_template.py:
- Render base.html with Jinja; assert it contains "Hermes", references /static/app.css, contains htmx CDN.
- TestClient GET /healthz -> 200 {"ok": true}

Run pytest. ${RULES}
Return agent_id="S3".
`, { phase: 'Fan-out', label: 'S3-base-pwa', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT B1 (HERMES BRIDGE). Bridge chat ops to the local hermes-agent's tui_gateway WebSocket.

${CONTRACTS_READ_LIST}
Also READ (to understand the hermes-side wire protocol):
- ${HERMES}/tui_gateway/ws.py
- ${HERMES}/tui_gateway/server.py (look for dispatch() and the RPC methods)

WRITE EXACTLY THIS FILE:
- ${ROOT}/src/webrelay/agent/bridges/hermes_bridge.py:
    class HermesBridge:
      def __init__(self, client: RelayClient, hermes_ws_url: str): ...
      async def start(self) -> None: registers handlers on client (Op.CHAT_SEND -> on_chat_send)
      async def on_chat_send(self, envelope, payload): 
        - Lazily connect to hermes via websockets.connect(self.hermes_ws_url)
        - Read first frame from hermes (expect a "gateway.ready" event); discard
        - Send JSON-RPC \`{"jsonrpc":"2.0","id":"<uuid>","method":"chat.send","params":{"text": payload.text, "thread_id": payload.thread_id}}\`
        - Loop reading frames; for each, parse JSON-RPC; if it's a token event (method like "chat.token") forward via \`client.send(Op.CHAT_TOKEN, ChatToken(...))\`; when "chat.done" arrives, send Op.CHAT_DONE and close.
        - Be DEFENSIVE: if hermes is unreachable (connection refused, timeout), send a single CHAT_TOKEN with text="[Local hermes is not running. Please start hermes-agent on this machine.]" and then CHAT_DONE.
    NOTE: the exact JSON-RPC method names ("chat.send" etc.) are GUESSES based on the plan — your job is to read the actual hermes tui_gateway/server.py to find the real RPC method names. If you can't find chat-specific methods, fall back to a generic method like "prompt.send" or whatever exists, and document your choice in a docstring. If NOTHING usable exists, raise NotImplementedError with a clear message in the docstring and return canned text — better to be honest than silently broken.

- ${ROOT}/tests/test_bridge_hermes.py:
    Use a fake JSON-RPC server (asyncio + websockets.serve) that simulates hermes
    Test happy path: chat_send -> tokens streamed -> done
    Test connection refused: bridge sends error CHAT_TOKEN + CHAT_DONE

Run pytest. ${RULES}
Return agent_id="B1". In notes, REPORT what RPC method name(s) you found in hermes server.py and used.
`, { phase: 'Fan-out', label: 'B1-hermes-bridge', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT B2 (LEDGER BRIDGE). Watch task_ledger_*.md files and push changes to server.

${CONTRACTS_READ_LIST}

WRITE EXACTLY THIS FILE:
- ${ROOT}/src/webrelay/agent/bridges/ledger_bridge.py:
    class LedgerBridge:
      def __init__(self, client: RelayClient, watched_dir: str): ...
      async def start(self) -> None:
        - Register handlers for Op.LEDGER_LIST and Op.LEDGER_READ
        - Spawn a background task watching the dir for task_ledger_*.md changes via watchfiles.awatch
      async def on_ledger_list(self, envelope, payload): list all files matching task_ledger_*.md, return LedgerResult-style listing in payload (use a list inside the response; you may extend protocol with a helper if needed BUT do NOT modify protocol.py — instead, send the listing as a JSON-encoded string in LedgerResult.content with ledger_id="__list__")
      async def on_ledger_read(self, envelope, payload): read the file (basename "task_ledger_{payload.ledger_id}.md"), return LedgerResult with content + mtime
      async def _watch_loop(self): use watchfiles.awatch(dir, recursive=False, step=200); for each change, debounce 300ms per file, then read content and send LedgerChanged via client.send.
    Parsing ledger_id from filename: strip "task_ledger_" prefix and ".md" suffix.

- ${ROOT}/tests/test_bridge_ledger.py:
    Use tmp_path fixture
    Test on_ledger_list returns expected ids
    Test on_ledger_read returns content + mtime
    Test _watch_loop: write a file, wait, assert client.send was called with LedgerChanged
    (Mock client.send by passing a recording fake)

Run pytest. ${RULES}
Return agent_id="B2".
`, { phase: 'Fan-out', label: 'B2-ledger-bridge', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT B3 (FILE BRIDGE). Sandboxed file reads.

${CONTRACTS_READ_LIST}
Also briefly read ${HERMES}/agent/file_safety.py to mirror its path-sandbox conventions.

WRITE EXACTLY THIS FILE:
- ${ROOT}/src/webrelay/agent/bridges/file_bridge.py:
    class FileBridge:
      def __init__(self, client: RelayClient, sandbox_root: str): self.root = pathlib.Path(sandbox_root).resolve()
      async def start(self): registers handlers for FILE_READ and FILE_LIST
      def _validate_path(self, p: str) -> pathlib.Path:
        - Reject absolute paths
        - Reject paths containing ".." segments
        - Resolve (sandbox_root / p).resolve()
        - Reject if not is_relative_to(self.root)
        - Reject if any component is a symlink (use Path.is_symlink on each parent)
        - Return the resolved Path
      async def on_file_read(self, env, payload):
        - try _validate_path; on FileResult with kind="error"
        - read bytes; try utf-8 decode; on success return kind="file" with content; on binary return kind="error" with msg "<binary>"
        - Limit: refuse files >5 MB (return error)
      async def on_file_list(self, env, payload):
        - validate dir; list entries; return entries=[{name, kind: "dir"|"file", size, mtime}, ...]

- ${ROOT}/tests/test_bridge_file.py:
    Test happy paths (read text, list dir)
    Test path-traversal: "../../etc/passwd" -> error
    Test absolute path -> error  
    Test symlink in path -> error (skip on Windows if symlink creation requires admin — use try/except OSError around symlink creation)
    Test >5 MB file -> error
    Test binary file -> error

Run pytest. ${RULES}
Return agent_id="B3".
`, { phase: 'Fan-out', label: 'B3-file-bridge', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT B4 (APPROVAL BRIDGE). Forward Claude Code PreToolUse approval requests to the web UI.

${CONTRACTS_READ_LIST}

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/agent/bridges/approval_bridge.py:
    class ApprovalBridge:
      def __init__(self, client: RelayClient): ...
      async def start(self) -> None: register handler for Op.APPROVAL_RESPOND
      async def on_approval_respond(self, env, payload): pop the pending future at self._pending[payload.prompt_id] and set its result to (payload.decision, payload.reason)
      async def request_approval(self, tool_name: str, command: str, context: str, *, timeout_s: float = 300.0) -> tuple[str, str | None]:
        - generate uuid prompt_id
        - register a Future in self._pending
        - send Op.APPROVAL_REQUESTED via client.send
        - wait_for the future; on timeout return ("deny", "timeout — defaulted to deny")
        - return (decision, reason)
      def cli_entry(self) -> None: a synchronous wrapper that the Claude Code PreToolUse hook calls. Reads JSON from stdin (Claude Code's hook input format: {"tool_name": str, "command_string": str, "context": str}), calls asyncio.run on a tiny helper that uses an EXISTING running ApprovalBridge instance via a TCP loopback socket (127.0.0.1:15999). If no running agent, default deny.

- ${ROOT}/src/webrelay/agent/hooks/preToolUse.py:
    A standalone CLI script that:
      - reads Claude Code hook input from stdin (JSON)
      - tries to connect to 127.0.0.1:15999 (the running approval IPC server inside the relay-agent)
      - sends the request, awaits a decision
      - prints the Claude Code hook response JSON: {"continue": true} or {"continue": false, "stop_reason": "..."}
      - if connect fails, prints {"continue": true} and exits 0 (don't block work just because relay isn't running)
- ${ROOT}/src/webrelay/agent/hooks/__init__.py — empty

- ${ROOT}/tests/test_bridge_approval.py:
    Test request_approval -> on_approval_respond round-trip
    Test timeout returns ("deny", "timeout — ...")
    Test the preToolUse CLI: connect fails -> outputs {"continue": true}

Run pytest. ${RULES}
Return agent_id="B4".
`, { phase: 'Fan-out', label: 'B4-approval-bridge', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT L1 (RELAY CLIENT IMPL + CONFIG). Fill in agent/client.py, reconnect.py, config.py bodies.

${CONTRACTS_READ_LIST}

The skeletons exist at:
- ${ROOT}/src/webrelay/agent/client.py
- ${ROOT}/src/webrelay/agent/reconnect.py
- ${ROOT}/src/webrelay/agent/config.py

ONLY replace the NotImplementedError bodies; keep the same public signatures.

REQUIREMENTS:

reconnect.py reconnect_backoff:
- Yield delays: 1, 2, 4, 8, 16, 32, 60, 60, 60, ... seconds
- Apply jitter +/- 30% per yield using Python's \`random.uniform(-jitter, jitter)\` (with random.seed seeded from a counter so it stays deterministic in tests — actually simpler: accept an optional \`jitter_fn\` callable param defaulting to random.uniform; tests pass a deterministic fn)
- The function should be a regular generator (def, not async def) so callers can drive it however they want

client.py RelayClient.run():
- Loop forever:
  - try: websockets.connect(server_url, additional_headers={"Authorization": "Bearer " + bearer_token})
    - On connect: send Hello envelope as the first frame
    - Start a task that periodically sends Ping every 30s
    - Read loop: for each frame, parse envelope, route to a registered handler (if op has one)
  - except ConnectionClosed / OSError / TimeoutError: log, then \`await asyncio.sleep(next(reconnect_backoff_iter))\`
- register_handler stores in dict op -> handler; multiple handlers per op are allowed (call all)
- send(op, payload, correlation_id=None): build envelope (use provided correlation_id if given, else fresh uuid), send over current websocket. If no websocket, queue and warn (do NOT crash).
- respond(envelope, payload): like send but reuses envelope.id

config.py load_config():
- Default vault_path = ~/.hermes/vault.json (use pathlib.Path.home())
- Read JSON, find credential with id == "webrelay.bearer_token" OR field "webrelay.bearer_token" — be flexible (search both id field and substring of credential field). If absent, raise FileNotFoundError with clear message pointing to the setup script.
- Same for "webrelay.server_url"
- Defaults: hermes_ws_url="ws://127.0.0.1:9119/api/ws", watched_ledger_dir="${WS}", file_sandbox_root="${WS}"
- Allow env overrides: WEBRELAY_SERVER_URL, WEBRELAY_BEARER_TOKEN, WEBRELAY_HERMES_WS_URL, WEBRELAY_WATCHED_DIR, WEBRELAY_SANDBOX_ROOT (env wins over vault)
- Return AgentConfig dataclass

Update ${ROOT}/tests/test_skeletons.py to remove now-passing NotImplementedError assertions for these modules.
Write ${ROOT}/tests/test_client_impl.py:
- Test reconnect_backoff yields the expected sequence (pass a deterministic jitter_fn)
- Test load_config from a temp vault file
- Test load_config env-var overrides
- Test register_handler + send (use a fake websocket)

Run pytest. ${RULES}
Return agent_id="L1".
`, { phase: 'Fan-out', label: 'L1-client-impl', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT I1 (DOCKER + CI). Build the Coolify-server image + dev compose + GitHub Actions.

${CONTRACTS_READ_LIST}
Briefly read ${HERMES}/docker-compose.yml for security-posture inspiration.

WRITE EXACTLY THESE FILES:
- ${ROOT}/docker/Dockerfile:
    Multi-stage: builder stage uses python:3.11-slim, installs build deps + pyproject deps into a venv; runtime stage copies the venv + src/ ; final EXPOSE 8000 ; CMD ["python", "-m", "uvicorn", "webrelay.server.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
    Use a non-root user (uid 10000) named "webrelay"
    Set WEBRELAY_DB_PATH=/data/webrelay.db default; declare VOLUME ["/data"]
- ${ROOT}/docker/compose.yml (LOCAL DEV ONLY — Coolify will use the Dockerfile directly):
    Single service "server" building from ../, env from ../.env, ports 8000:8000, volume named "webrelay-data" mounted at /data
    Comment: "Coolify deploys via the Dockerfile and its own env-var mgmt; this compose is for local testing only."
- ${ROOT}/.dockerignore:
    .git, .venv, __pycache__, *.db, .env, .pytest_cache, tests/, docker/compose.yml, .workflow-output/
- ${ROOT}/.github/workflows/test.yml:
    On push to main + pull_request: ubuntu-latest, python 3.11, pip install -e .[dev], run \`python -m pytest -v\`
    Also run \`ruff check src/ tests/\`

Run a smoke test: \`cd ${ROOT} && docker build -f docker/Dockerfile -t webrelay-server-test . 2>&1 | tail -30\` if docker is available; if not, skip and note in your output that the smoke wasn't run.
${RULES}
Return agent_id="I1".
`, { phase: 'Fan-out', label: 'I1-docker-ci', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT I2 (PROVISION COOLIFY SCRIPT). Build the Coolify provisioning script with cleanup-with-confirm and respx-mocked tests.

${CONTRACTS_READ_LIST}
Also READ:
- ${WS}/COOLIFY_API_AGENT_DOC.md — the API reference

WRITE EXACTLY THESE FILES:
- ${ROOT}/scripts/provision_coolify.py:
    CLI using argparse with subcommands: \`inspect\` (list current projects), \`cleanup\` (interactive delete), \`provision\` (create/update hermes-web-relay project + app + envs), \`deploy\` (trigger and poll a deploy). Plus a \`full\` command that does cleanup -> provision -> deploy in sequence.
    Reads coolify.api_token and coolify.base_url from ~/.hermes/vault.json via a helper that searches credentials by id field substring.
    All HTTP via httpx.AsyncClient with auth header pre-set.
    Cleanup MUST: list projects, for each project list its apps/dbs/services, print a numbered table, then iterate per-project asking "Delete project '{name}' and its N resources? [y/N] " — only delete on explicit y. Use \`--dry-run\` (default) and \`--confirm\` flags so the script REFUSES to delete unless --confirm is passed.
    Provision: create project "hermes-web-relay" if absent; create application using POST /applications/public when --git-url provided, ELSE POST /applications/dockerimage when --docker-image provided. Set env vars via PATCH /applications/{uuid}/envs/bulk: WEBRELAY_PASSWORD, WEBRELAY_SESSION_SECRET (random hex), WEBRELAY_RELAY_TOKEN_HASH (SHA256 of a token printed at the end), WEBRELAY_DB_PATH=/data/webrelay.db.
    Deploy: POST /deploy with uuid, poll /deployments/{run_uuid} every 5s until status finished/failed (timeout 10 min). Stream the logs to stdout.
    Print at the end: the deployed URL, the bearer token to put in vault on the local-agent machine.
- ${ROOT}/tests/test_provision_coolify.py:
    Use respx to mock the entire Coolify API
    Test inspect: GET /projects -> renders table
    Test cleanup without --confirm: refuses
    Test cleanup with --confirm and a "y" response: makes the right DELETE calls in the right order
    Test cleanup with "n" response: no DELETE calls
    Test provision: creates project, creates app, sets envs (capture the PATCH body)
    Test deploy poll: simulates status progression queued -> in_progress -> finished
    Use monkeypatch on builtins.input to feed responses to confirm prompts

Run pytest on test_provision_coolify.py. ${RULES}
Return agent_id="I2".
`, { phase: 'Fan-out', label: 'I2-provision', schema: SCHEMA_RESULT }),
])

const fanoutFailed = fanout.filter(r => !r || !r.tests_pass)
log(`Fan-out done: ${fanout.filter(Boolean).length}/15 ok, ${fanoutFailed.length} test-failures`)

// ─────────────────────────────────────────────────────────────────────
// STAGE 3 — Wiring
// ─────────────────────────────────────────────────────────────────────
phase('Wiring')
log('Stage 3: Wiring — composing main.py and __main__.py')

const wiring = await agent(`
You are AGENT WIRING. Compose the server FastAPI app and the agent process entry point.

READ:
- ${ROOT}/src/webrelay/server/routes/ (every .py file there)
- ${ROOT}/src/webrelay/agent/bridges/ (every .py file there)
- ${ROOT}/src/webrelay/server/relay_hub.py
- ${ROOT}/src/webrelay/server/db.py
- ${ROOT}/src/webrelay/server/auth.py
- ${ROOT}/src/webrelay/agent/client.py
- ${ROOT}/src/webrelay/agent/config.py

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/server/main.py:
    Create FastAPI app with lifespan that calls db.on_startup() and instantiates a module-level RelayHub
    Mount session middleware (use SessionMiddleware from starlette with secret from WEBRELAY_SESSION_SECRET)
    Mount StaticFiles at /static -> server/static/
    Mount templates via Jinja2Templates at server/templates/
    Include every router: status.router, auth.router, chat.router, ledgers.router, files.router, approvals.router, relay.router
    Add a root route GET "/" -> redirect to /chat
    Add app.middleware to redirect to /auth/login if no session for non-auth routes
    def run(): uvicorn.run("webrelay.server.main:app", host="0.0.0.0", port=8000, proxy_headers=True)
- ${ROOT}/src/webrelay/agent/__main__.py:
    def main(): 
      Parse argv for "setup" / "run" / "uninstall" subcommands (setup/uninstall are stubs that import from setup.py if it exists; "run" is the default)
      For "run": load_config(); build Hello payload; create RelayClient; instantiate all 4 bridges (HermesBridge, LedgerBridge, FileBridge, ApprovalBridge), call each .start(); asyncio.run(client.run()) — this blocks forever
    if __name__ == "__main__": main()

Then write ${ROOT}/tests/test_main_wiring.py:
- TestClient against the FastAPI app: GET / -> 302 to /auth/login (no session)
- TestClient with set cookie: GET / -> 302 to /chat
- TestClient: GET /healthz -> 200

Run the FULL test suite: \`cd ${ROOT} && python -m pytest -v 2>&1 | tail -80\`. Report pass/fail counts in notes.
${RULES}
Return agent_id="WIRING".
`, { phase: 'Wiring', label: 'wiring', schema: SCHEMA_RESULT })

log(`Wiring done — ${wiring?.notes ?? 'no notes'}`)

// ─────────────────────────────────────────────────────────────────────
// STAGE 4 — Verify loop (up to 3 attempts)
// ─────────────────────────────────────────────────────────────────────
phase('Verify')
log('Stage 4: Verify — running full pytest, fan out per failure if needed')

let verifyAttempt = 0
let verifyResult = null
while (verifyAttempt < 3) {
  verifyAttempt++
  verifyResult = await agent(`
You are AGENT VERIFY-RUNNER (attempt ${verifyAttempt}/3).
Run \`cd ${ROOT} && python -m pytest -v --tb=short 2>&1 | tail -120\` and report:
- total pass / fail counts (parse from pytest summary line)
- a list of FAILING test names with their first-line error message
- exit code (0 = green)

Return as agent_id="VERIFY-${verifyAttempt}", files_written=[], tests_pass=(exit code == 0), notes=JSON-string of {pass:N, fail:N, failures:[{name, error}, ...]}.
`, { phase: 'Verify', label: 'verify-run-' + verifyAttempt, schema: SCHEMA_RESULT })

  if (verifyResult?.tests_pass) {
    log(`Verify GREEN on attempt ${verifyAttempt}`)
    break
  }

  let failures = []
  try {
    const parsed = JSON.parse(verifyResult?.notes ?? '{}')
    failures = parsed.failures ?? []
  } catch (e) {
    log('Could not parse verify notes — bailing out of verify loop')
    break
  }

  if (!failures.length) {
    log(`No failures reported but tests_pass is false — bailing`)
    break
  }

  if (verifyAttempt >= 3) {
    log(`Hit max verify attempts; ${failures.length} failures remain`)
    break
  }

  log(`Attempt ${verifyAttempt}: ${failures.length} failures, fanning out fix agents`)
  await parallel(failures.slice(0, 12).map(f => () => agent(`
You are a FIX agent. A test failed:
  Test name: ${f.name}
  Error: ${f.error}

READ the test file and the module(s) under test to find the bug. Fix the bug — touch ONLY files mentioned in the error trace. Do NOT modify any test that asserts important behavior (you may relax a test only if it tested an incorrect spec).

After your fix, run JUST that test: \`cd ${ROOT} && python -m pytest "${f.name}" -v\`. Report pass/fail.

Return agent_id="fix-${f.name.replace(/[^a-z0-9]/gi, '_').slice(0, 40)}".
`, { phase: 'Verify', label: 'fix-' + f.name.slice(0, 30), schema: SCHEMA_RESULT })))
}

// ─────────────────────────────────────────────────────────────────────
// STAGE 5 — Installer + Skill updates (3 parallel)
// ─────────────────────────────────────────────────────────────────────
phase('Installer-Skills')
log('Stage 5: Installer + skill updates in parallel')

const installerSkills = await parallel([
  () => agent(`
You are AGENT P1 (INSTALLER). Build the cross-platform local-agent installer.

WRITE EXACTLY THESE FILES:
- ${ROOT}/src/webrelay/agent/setup.py:
    def main(argv=None):
      argparse: --coolify-url, --coolify-token, --server-url, --hermes-ws-url, --non-interactive
      Steps:
        1. Detect platform; pick vault path (~/.hermes/vault.json — same on all OS per the project convention).
        2. Read vault. If coolify.api_token missing, prompt.
        3. Probe local hermes at hermes_ws_url (websockets.connect with 3s timeout). If unreachable, ask user to enter custom URL or skip.
        4. Generate bearer token (secrets.token_urlsafe(32)). Compute SHA256 hex.
        5. POST hash to <server-url>/api/internal/register-relay with header X-Coolify-Token: <api_token> — server stores the hash. (NOTE: this endpoint may not yet exist on server; if 404, fall back to printing the hash and instructing the user to set WEBRELAY_RELAY_TOKEN_HASH env var on the Coolify app.)
        6. Write bearer token to vault under id "webrelay.bearer_token".
        7. Write server_url to vault under id "webrelay.server_url".
        8. Install platform autostart entry (call into autostart_macos / _linux / _windows depending on sys.platform).
        9. Start the agent in the background (subprocess.Popen with appropriate detach flags per platform).
        10. Print success message with the deployed URL and next steps.
- ${ROOT}/src/webrelay/agent/uninstall.py: removes autostart entry; optionally removes vault entries (--purge flag).
- ${ROOT}/src/webrelay/agent/autostart_macos.py: writes ~/Library/LaunchAgents/com.hermes.webrelay-agent.plist; uses launchctl load/unload
- ${ROOT}/src/webrelay/agent/autostart_linux.py: writes ~/.config/systemd/user/webrelay-agent.service; uses systemctl --user enable/start
- ${ROOT}/src/webrelay/agent/autostart_windows.py: uses schtasks /Create to register a per-user task that runs at logon; /Delete on uninstall

Write ${ROOT}/tests/test_setup.py:
- Test setup dry-run: monkeypatch vault path, run with --non-interactive flags, assert vault was written
- Test each autostart module's install() / uninstall() function generates the expected file content (don't actually invoke launchctl/systemctl/schtasks)

Run pytest. ${RULES}
Return agent_id="P1".
`, { phase: 'Installer-Skills', label: 'P1-installer', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT P2 (SKILL UPDATE). Update the task-ledger-workflow skill to support chat-thread linkage.

READ:
- ${WS}/.claude/skills/task-ledger-workflow/SKILL.md (current content)
- ${WS}/task_ledger_template.md
- ${PLAN} (section "Task-ledger workflow changes (skill update)")

EDIT ONLY:
- ${WS}/.claude/skills/task-ledger-workflow/SKILL.md

Changes:
1. Add a "Phase 0.5: Chat-thread linkage" section between Phase 0 and Phase 1. Content:
   "If invoked from a web-relay chat thread (env var WEBRELAY_CHAT_THREAD_ID is set), record the originating chat_thread_id in Section 2 of the ledger under a new bullet 'Origin chat thread: <id>'. This lets the web UI link the ledger back to its conversation."
2. In Phase 2 (Development & Self-Healing Loop), add a bullet:
   "If env WEBRELAY_AGENT_RUNNING is set, when blocked, add an Approval-style note to Section 5 with format 'APPROVAL_NEEDED: <action>' — the web-relay agent surfaces these as push prompts on the user's phone."

Run a grep to confirm the new text is in place: \`grep -c "Phase 0.5" "${WS}/.claude/skills/task-ledger-workflow/SKILL.md"\` should be >= 1.
${RULES}
Return agent_id="P2", files_written=["${WS}/.claude/skills/task-ledger-workflow/SKILL.md"], tests_pass=true if grep confirms.
`, { phase: 'Installer-Skills', label: 'P2-skill-update', schema: SCHEMA_RESULT }),

  () => agent(`
You are AGENT P3 (LAUNCHER UPDATE). Update launch_task.py for chat-thread support and relay notification.

READ:
- ${WS}/launch_task.py (current content)
- ${PLAN} (section "Task-ledger workflow changes (skill update)" — point about WEBRELAY_SOCKET and --chat-thread-id)

EDIT ONLY:
- ${WS}/launch_task.py

Changes:
1. Argparse: accept new optional flag --chat-thread-id <id>. If provided, write it into the created ledger file (insert a line in Section 2 like "- Origin chat thread: <id>").
2. After creating the ledger file: if env WEBRELAY_SOCKET is set (path to a TCP loopback host:port like "127.0.0.1:15998"), open a brief TCP connection and send a single JSON line: {"event": "ledger_created", "path": "<absolute_path>"}. On any error, silently ignore. This lets the running relay-agent watch the new file immediately instead of polling.
3. Keep all existing behavior intact (Claude Code subprocess invocation + retry loop).

Run \`python -c "import ast; ast.parse(open('${WS}/launch_task.py').read())"\` to confirm syntax is valid.
${RULES}
Return agent_id="P3", files_written=["${WS}/launch_task.py"], tests_pass=true if ast.parse succeeded.
`, { phase: 'Installer-Skills', label: 'P3-launch-task', schema: SCHEMA_RESULT }),
])

const installerFailed = installerSkills.filter(r => !r || !r.tests_pass)
log(`Stage 5 done: ${installerSkills.filter(Boolean).length}/3 ok, ${installerFailed.length} failures`)

return {
  bootstrap: bootstrap?.tests_pass ?? false,
  contracts: {
    C1: contracts[0],
    C2: contracts[1],
    C3: contracts[2],
  },
  fanout_agents: fanout.length,
  fanout_passing: fanout.filter(r => r?.tests_pass).length,
  fanout_failing: fanoutFailed.map(r => r?.agent_id ?? '<null>'),
  wiring: wiring?.tests_pass ?? false,
  verify_attempts: verifyAttempt,
  verify_final_green: verifyResult?.tests_pass ?? false,
  installer_skills_passing: installerSkills.filter(r => r?.tests_pass).length,
  paused_before_stage_6: true,
  next_step: 'Fill coolify.base_url in vault, then run scripts/provision_coolify.py --confirm cleanup; review per-project deletion prompts; then provision + deploy.',
}
