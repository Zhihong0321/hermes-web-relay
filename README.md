# hermes-web-relay

Mobile-first HTMX UI that proxies a local **Hermes Agent** through a
Coolify-hosted FastAPI server over an outbound WebSocket. Reach your
task-ledger workflow, chat, file browser, and approval prompts from
your phone.

The package is **one install, two halves**:

| Half | Entry point | Runs where | Purpose |
|---|---|---|---|
| **Server** | `webrelay-server` (or `python -m webrelay.server.main`) | Coolify (always on) | FastAPI + Jinja + HTMX. Auth, SQLite, routes, SSE, WS hub. |
| **Agent** | `webrelay-agent` (or `python -m webrelay.agent`) | Local PC (Windows / macOS / Linux) | Dials outbound WS to server; bridges to local Hermes, task-ledger files, file sandbox, and Claude Code approval hooks. |

## Install (dev)

```bash
pip install -e .[dev]
```

## Configure

Copy `.env.example` to `.env` and fill in the values. See
`.env.example` for documentation on each variable. Secrets used by the
local agent (Coolify API token, bearer token) are stored in
`~/.hermes/vault.json` (see the `vault-access-check` skill).

## Run

Server (Coolify side):

```bash
webrelay-server
```

Local agent:

```bash
webrelay-agent
```

## Project layout

```
web-relay/
  src/webrelay/
    server/   # FastAPI app, routes, templates, WS hub
    agent/    # local WS client + bridges
  scripts/    # Coolify provisioning helpers
  docker/     # Dockerfile + compose for Coolify
  tests/      # pytest + respx
```

See `task_ledger_webrelay.md` for the workflow protocol that gates
this build (Pre-Planning, contracts, fan-out, wiring, verify).
