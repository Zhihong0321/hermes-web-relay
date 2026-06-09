"""Configuration loader for the local agent.

A frozen dataclass holds the runtime config and :func:`load_config`
reads it from the appropriate platform-native vault path. The function
is intentionally flexible about vault layout — the ``setup`` command
may write the credentials under a nested ``webrelay`` key, as a flat
``webrelay.bearer_token`` id, or as a top-level ``webrelay.bearer_token``
field. All three are recognised.

Stage 1 (this file) is a SKELETON. Stage 2 agent L1 fills in the body
and the I/O. The default values are documented here so feature agents
can refer to them without reading the Stage-2 code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


# Default URLs and paths. Kept here (not in ``__main__``) so bridges can
# import them as constants when they need the same defaults.
DEFAULT_HERMES_WS_URL = "ws://127.0.0.1:9119/api/ws"
DEFAULT_WATCHED_LEDGER_DIR = "E:/hermes-agent"
DEFAULT_FILE_SANDBOX_ROOT = "E:/hermes-agent"


# Environment variable names. Env vars always win over vault values.
ENV_SERVER_URL = "WEBRELAY_SERVER_URL"
ENV_BEARER_TOKEN = "WEBRELAY_BEARER_TOKEN"
ENV_HERMES_WS_URL = "WEBRELAY_HERMES_WS_URL"
ENV_WATCHED_DIR = "WEBRELAY_WATCHED_DIR"
ENV_SANDBOX_ROOT = "WEBRELAY_SANDBOX_ROOT"


# Credential identifiers (id field) accepted by the vault.
CRED_ID_SERVER_URL = "webrelay.server_url"
CRED_ID_BEARER_TOKEN = "webrelay.bearer_token"


@dataclass(frozen=True)
class AgentConfig:
    """Immutable runtime configuration for the local agent.

    The dataclass is frozen so that bridges and the client can stash a
    reference at startup and trust it never mutates underneath them.

    Attributes:
        server_url: Coolify WebSocket URL the agent dials, e.g.
            ``wss://hermes.example.com/api/relay/ws``. Required — the
            ``setup`` command writes this to the vault.
        bearer_token: Pre-shared token presented as
            ``Authorization: Bearer <token>`` on the upgrade request.
            Stored in ``~/.hermes/vault.json`` under
            ``webrelay.bearer_token`` (never on disk elsewhere).
        hermes_ws_url: Local JSON-RPC endpoint of the installed
            ``hermes-agent`` TUI gateway. The
            ``hermes_bridge`` connects here. Default:
            ``ws://127.0.0.1:9119/api/ws``.
        watched_ledger_dir: Directory the ``ledger_bridge`` watches
            with ``watchfiles`` for ``task_ledger_*.md`` changes. Must
            be the same directory Claude Code writes ledgers to.
            Default: ``E:/hermes-agent`` (Windows laptop; the Mac-mini
            installer overrides this via ``setup``).
        file_sandbox_root: Root of the read-only file tree the
            ``file_bridge`` exposes to the phone. Any path-traversal
            outside this root is rejected with 403. Default:
            ``E:/hermes-agent``.
    """

    server_url: str
    bearer_token: str
    hermes_ws_url: str = DEFAULT_HERMES_WS_URL
    watched_ledger_dir: str = DEFAULT_WATCHED_LEDGER_DIR
    file_sandbox_root: str = DEFAULT_FILE_SANDBOX_ROOT


def _search_vault_for(vault: dict, cred_id: str) -> str | None:
    """Search a parsed vault JSON for a credential matching ``cred_id``.

    Recognises the three layouts the ``setup`` command may produce:

    1. ``vault[cred_id]`` — flat top-level key.
    2. ``vault["credentials"]`` is a list of ``{"id": ..., "value": ...}``
       records and we look up the record whose ``id`` is exactly
       ``cred_id`` (or whose ``field`` / ``name`` / ``key`` matches).
    3. ``vault[cred_id.split(".", 1)[0]][cred_id.split(".", 1)[1]]`` —
       the nested ``{"webrelay": {"bearer_token": "..."}}`` form.

    Returns the string value, or ``None`` if no match was found.
    """
    # Layout 1: flat top-level key.
    if cred_id in vault and isinstance(vault[cred_id], str):
        return vault[cred_id]

    # Layout 2: list of credential records.
    creds = vault.get("credentials")
    if isinstance(creds, list):
        for record in creds:
            if not isinstance(record, dict):
                continue
            # Match on id / field / name / key — whichever the setup
            # script happens to write.
            for key in ("id", "field", "name", "key"):
                value = record.get(key)
                if isinstance(value, str) and value == cred_id:
                    inner = record.get("value")
                    if isinstance(inner, str):
                        return inner

    # Layout 3: nested object with a dotted key like "webrelay.bearer_token".
    if "." in cred_id:
        head, tail = cred_id.split(".", 1)
        nested = vault.get(head)
        if isinstance(nested, dict):
            inner = nested.get(tail)
            if isinstance(inner, str):
                return inner

    return None


def _resolve_vault_path(vault_path: str | None) -> Path:
    """Resolve the vault file path, defaulting to ``~/.hermes/vault.json``."""
    if vault_path is not None and vault_path != "":
        return Path(vault_path)
    return Path.home() / ".hermes" / "vault.json"


def load_config(vault_path: str | None = None) -> AgentConfig:
    """Read ``AgentConfig`` from the platform-native vault.

    Args:
        vault_path: Override the vault file location. If ``None``
            (the default), the function reads
            ``~/.hermes/vault.json`` (i.e. ``Path.home() / '.hermes' /
            'vault.json'``). The vault must contain at minimum::

                {
                  "webrelay": {
                    "server_url": "wss://...",
                    "bearer_token": "..."
                  }
                }

            The loader also accepts the two flat-key layouts described
            in :func:`_search_vault_for`. Optional keys ``hermes_ws_url``,
            ``watched_ledger_dir`` and ``file_sandbox_root`` override the
            dataclass defaults when present in the vault.

            The following environment variables override vault values
            (env always wins)::

                WEBRELAY_SERVER_URL
                WEBRELAY_BEARER_TOKEN
                WEBRELAY_HERMES_WS_URL
                WEBRELAY_WATCHED_DIR
                WEBRELAY_SANDBOX_ROOT

    Returns:
        A fully-populated :class:`AgentConfig`.

    Raises:
        FileNotFoundError: If the vault file does not exist (the user
            has not run ``python -m webrelay_agent setup`` yet).
        KeyError: If a required key is missing from the vault.
    """
    resolved = _resolve_vault_path(vault_path)
    if not resolved.exists():
        raise FileNotFoundError(
            f"vault file not found at {resolved}. "
            "Run `python -m webrelay_agent setup` to create it."
        )

    try:
        vault_data = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FileNotFoundError(
            f"vault file at {resolved} is not valid JSON: {exc.msg}"
        ) from exc

    if not isinstance(vault_data, dict):
        raise FileNotFoundError(
            f"vault file at {resolved} is not a JSON object"
        )

    # Required values: read from vault, then from env, then error.
    server_url = (
        os.environ.get(ENV_SERVER_URL)
        or _search_vault_for(vault_data, CRED_ID_SERVER_URL)
    )
    bearer_token = (
        os.environ.get(ENV_BEARER_TOKEN)
        or _search_vault_for(vault_data, CRED_ID_BEARER_TOKEN)
    )

    missing: list[str] = []
    if not server_url:
        missing.append(CRED_ID_SERVER_URL)
    if not bearer_token:
        missing.append(CRED_ID_BEARER_TOKEN)
    if missing:
        raise KeyError(
            "missing required credential(s) in vault "
            f"{resolved}: {', '.join(missing)}. "
            "Run `python -m webrelay_agent setup` to add them."
        )

    # Optional values: env > vault > default.
    hermes_ws_url = (
        os.environ.get(ENV_HERMES_WS_URL)
        or _search_vault_for(vault_data, "webrelay.hermes_ws_url")
        or DEFAULT_HERMES_WS_URL
    )
    watched_ledger_dir = (
        os.environ.get(ENV_WATCHED_DIR)
        or _search_vault_for(vault_data, "webrelay.watched_ledger_dir")
        or DEFAULT_WATCHED_LEDGER_DIR
    )
    file_sandbox_root = (
        os.environ.get(ENV_SANDBOX_ROOT)
        or _search_vault_for(vault_data, "webrelay.file_sandbox_root")
        or DEFAULT_FILE_SANDBOX_ROOT
    )

    # ``server_url`` and ``bearer_token`` are typed as str, so the
    # ``or`` chains above guarantee non-empty values (the empty string
    # is falsy). The cast keeps type-checkers quiet.
    return AgentConfig(
        server_url=str(server_url),
        bearer_token=str(bearer_token),
        hermes_ws_url=str(hermes_ws_url),
        watched_ledger_dir=str(watched_ledger_dir),
        file_sandbox_root=str(file_sandbox_root),
    )


__all__ = [
    "AgentConfig",
    "DEFAULT_FILE_SANDBOX_ROOT",
    "DEFAULT_HERMES_WS_URL",
    "DEFAULT_WATCHED_LEDGER_DIR",
    "load_config",
]
