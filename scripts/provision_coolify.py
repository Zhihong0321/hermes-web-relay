"""Coolify provisioning CLI for hermes-web-relay.

Reads ``coolify.api_token`` and ``coolify.base_url`` from
``~/.hermes/vault.json``, then drives the Coolify v4 REST API.

Subcommands: ``inspect``, ``cleanup``, ``provision``, ``deploy``,
``full`` (cleanup -> provision -> deploy).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx


DEFAULT_VAULT_PATH = Path.home() / ".hermes" / "vault.json"
APP_NAME = "hermes-web-relay"
PROJECT_NAME = "hermes-web-relay"


# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------


def _load_vault(path: Path = DEFAULT_VAULT_PATH) -> dict[str, Any]:
    """Read and lightly validate ``~/.hermes/vault.json``."""
    if not path.exists():
        raise SystemExit(
            f"vault file not found at {path}. "
            "Run `python vault_service.py` to create it, or set "
            "COOLIFY_API_TOKEN and COOLIFY_BASE_URL in the environment."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"vault file at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "credentials" not in data:
        raise SystemExit(
            f"vault file at {path} is malformed: expected top-level "
            "'credentials' list. See vault-access-check skill."
        )
    return data


def find_credential(vault: dict[str, Any], id_substring: str) -> dict[str, Any] | None:
    """Return the first credential whose ``id`` contains ``id_substring``."""
    needle = id_substring.lower()
    for cred in vault.get("credentials", []):
        cred_id = str(cred.get("id", "")).lower()
        if needle in cred_id:
            return cred
    return None


def _resolve_coolify_creds(vault_path: Path = DEFAULT_VAULT_PATH) -> tuple[str, str]:
    """Return ``(api_token, base_url)`` for the Coolify API."""
    vault: dict[str, Any] = {}
    if vault_path.exists():
        try:
            vault = _load_vault(vault_path)
        except SystemExit:
            vault = {}
    api_token: str | None = None
    base_url: str | None = None
    if vault:
        token_cred = find_credential(vault, "coolify.api_token")
        url_cred = find_credential(vault, "coolify.base_url")
        if token_cred:
            api_token = str(token_cred.get("credential") or "").strip()
            if api_token.lower().startswith("bearer "):
                api_token = api_token[len("bearer "):].strip()
        if url_cred:
            base_url = str(url_cred.get("credential") or "").strip().rstrip("/")
    if not api_token:
        api_token = os.environ.get("COOLIFY_API_TOKEN")
    if not base_url:
        base_url = os.environ.get("COOLIFY_BASE_URL", "").rstrip("/")
    if not api_token:
        raise SystemExit(
            "Coolify API token not found. Add a credential with id "
            "'coolify.api_token' to ~/.hermes/vault.json, or set "
            "COOLIFY_API_TOKEN in the environment."
        )
    if not base_url:
        raise SystemExit(
            "Coolify base URL not found. Add a credential with id "
            "'coolify.base_url' to ~/.hermes/vault.json, or set "
            "COOLIFY_BASE_URL in the environment."
        )
    return api_token, base_url


# ---------------------------------------------------------------------------
# Coolify HTTP client
# ---------------------------------------------------------------------------


@dataclass
class CoolifyClient:
    """Thin wrapper around :class:`httpx.AsyncClient` for the Coolify API."""

    base_url: str
    api_token: str
    timeout: float =30.0
    _client: httpx.AsyncClient = field(default=None, init=False, repr=False)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def __aenter__(self) -> "CoolifyClient":
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v1",
            headers=self._headers(),
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("CoolifyClient used outside async-with")
        return self._client

    async def list_projects(self) -> list[dict[str, Any]]:
        r = await self._client.get("/projects")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def list_environments(self, project_uuid: str) -> list[dict[str, Any]]:
        r = await self._client.get(f"/projects/{project_uuid}/environments")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def create_project(self, name: str, description: str = "") -> dict[str, Any]:
        r = await self._client.post(
            "/projects", json={"name": name, "description": description}
        )
        r.raise_for_status()
        return r.json()

    async def list_applications(self) -> list[dict[str, Any]]:
        r = await self._client.get("/applications")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def list_databases(self) -> list[dict[str, Any]]:
        r = await self._client.get("/databases")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def list_services(self) -> list[dict[str, Any]]:
        r = await self._client.get("/services")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def list_servers(self) -> list[dict[str, Any]]:
        r = await self._client.get("/servers")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def create_public_app(
        self,
        *,
        project_uuid: str,
        server_uuid: str,
        environment_name: str,
        environment_uuid: str,
        git_repository: str,
        git_branch: str,
        build_pack: str,
        ports_mappings: str,
        name: str = APP_NAME,
    ) -> dict[str, Any]:
        payload = {
            "project_uuid": project_uuid,
            "server_uuid": server_uuid,
            "environment_name": environment_name,
            "environment_uuid": environment_uuid,
            "git_repository": git_repository,
            "git_branch": git_branch,
            "build_pack": build_pack,
            "ports_mappings": ports_mappings,
            "name": name,
        }
        r = await self._client.post("/applications/public", json=payload)
        r.raise_for_status()
        return r.json()

    async def create_dockerimage_app(
        self,
        *,
        project_uuid: str,
        server_uuid: str,
        environment_name: str,
        environment_uuid: str,
        docker_registry_image_name: str,
        ports_mappings: str,
        name: str = APP_NAME,
    ) -> dict[str, Any]:
        payload = {
            "project_uuid": project_uuid,
            "server_uuid": server_uuid,
            "environment_name": environment_name,
            "environment_uuid": environment_uuid,
            "docker_registry_image_name": docker_registry_image_name,
            "ports_mappings": ports_mappings,
            "name": name,
        }
        r = await self._client.post("/applications/dockerimage", json=payload)
        r.raise_for_status()
        return r.json()

    async def set_envs_bulk(self, app_uuid: str, envs: list[dict[str, Any]]) -> dict[str, Any]:
        r = await self._client.patch(
            f"/applications/{app_uuid}/envs/bulk", json={"data": envs}
        )
        r.raise_for_status()
        try:
            return r.json()
        except json.JSONDecodeError:
            return {"raw": r.text}

    async def delete_application(self, app_uuid: str) -> None:
        r = await self._client.delete(f"/applications/{app_uuid}")
        r.raise_for_status()

    async def delete_project(self, project_uuid: str) -> None:
        r = await self._client.delete(f"/projects/{project_uuid}")
        r.raise_for_status()

    async def deploy(self, app_uuid: str) -> list[str]:
        r = await self._client.post("/deploy", json={"uuid": app_uuid})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "deployments" in data:
            deps = data["deployments"]
            if isinstance(deps, list) and deps:
                uuid_ = deps[0].get("deployment_uuid")
                if uuid_:
                    return [str(uuid_)]
        return []

    async def deployment_status(self, run_uuid: str) -> dict[str, Any]:
        r = await self._client.get(f"/deployments/{run_uuid}")
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Helpers used by subcommands
# ---------------------------------------------------------------------------


def _project_resource_counts(
    project_uuid: str,
    apps: list[dict[str, Any]],
    dbs: list[dict[str, Any]],
    svcs: list[dict[str, Any]],
) -> tuple[int, int, int]:
    """Count how many apps / dbs / services belong to a project."""
    keys = ("project_uuid", "project_id", "project")

    def _belongs(item: dict[str, Any]) -> bool:
        return any(item.get(k) == project_uuid for k in keys)

    return (
        sum(1 for a in apps if _belongs(a)),
        sum(1 for d in dbs if _belongs(d)),
        sum(1 for s in svcs if _belongs(s)),
    )


async def _pick_environment(client: CoolifyClient, project: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(name, uuid)`` for the project's first environment, or ``None``."""
    envs = project.get("environments")
    if not envs:
        project_uuid = str(project.get("uuid", ""))
        envs = await client.list_environments(project_uuid)
    if not envs:
        return None
    first = envs[0]
    return str(first.get("name") or "production"), str(first.get("uuid") or "")


def _make_relay_token() -> str:
    """Generate a fresh32-byte hex token used for ``WEBRELAY_RELAY_TOKEN``."""
    return secrets.token_hex(32)


def _hash_token(token: str) -> str:
    """SHA256 hex digest -- what the server compares against."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _render_projects_table(rows: list[tuple[int, str, str, int, int, int]]) -> str:
    """Render a numbered table for ``inspect`` / pre-cleanup summary."""
    if not rows:
        return " (no projects)\n"
    header = f" {'#':>3} {'UUID':<36} {'NAME':<24} {'APPS':>4} {'DBS':>4} {'SVCS':>4}"
    sep = " " + "-" * (len(header) -2)
    out = [header, sep]
    for idx, uuid_, name, a, d, s in rows:
        out.append(
            f" {idx:>3} {uuid_:<36} {name[:24]:<24} {a:>4} {d:>4} {s:>4}"
        )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def cmd_inspect(client: CoolifyClient) -> int:
    """Print a numbered table of every project and its resource counts."""
    projects = await client.list_projects()
    apps = await client.list_applications()
    dbs = await client.list_databases()
    svcs = await client.list_services()
    rows: list[tuple[int, str, str, int, int, int]] = []
    for i, p in enumerate(projects, start=1):
        uuid_ = str(p.get("uuid", ""))
        name = str(p.get("name", ""))
        a, d, s = _project_resource_counts(uuid_, apps, dbs, svcs)
        rows.append((i, uuid_, name, a, d, s))
    sys.stdout.write(_render_projects_table(rows))
    return 0


async def cmd_cleanup(
    client: CoolifyClient,
    *,
    confirm: bool,
    dry_run: bool,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Interactively delete projects."""
    if not confirm:
        sys.stderr.write(
            "REFUSING to delete: pass --confirm to enable deletion. "
            "(dry-run mode is the default.)\n"
        )
        return 2

    projects = await client.list_projects()
    apps = await client.list_applications()
    dbs = await client.list_databases()
    svcs = await client.list_services()

    sys.stdout.write(
        f"Found {len(projects)} project(s) on {client.base_url}.\n"
    )
    rows: list[tuple[int, str, str, int, int, int]] = []
    for i, p in enumerate(projects, start=1):
        uuid_ = str(p.get("uuid", ""))
        name = str(p.get("name", ""))
        a, d, s = _project_resource_counts(uuid_, apps, dbs, svcs)
        rows.append((i, uuid_, name, a, d, s))
    sys.stdout.write(_render_projects_table(rows))

    for idx, uuid_, name, a, d, s in rows:
        n_resources = a + d + s
        prompt = (
            f"Delete project '{name}' ({uuid_}) and its "
            f"{n_resources} resource(s)? [y/N] "
        )
        try:
            answer = input_fn(prompt)
        except EOFError:
            sys.stderr.write("\nInput closed; aborting cleanup.\n")
            return 1
        if answer.strip().lower() != "y":
            sys.stdout.write(f" skipping {name!r}\n")
            continue
        if dry_run:
            sys.stdout.write(
                f" [dry-run] would delete apps in {name!r} then project {uuid_}\n"
            )
            continue
        sys.stdout.write(f" deleting {a} app(s) in {name!r}...\n")
        for app in apps:
            if app.get("project_uuid") == uuid_:
                await client.delete_application(str(app.get("uuid", "")))
        sys.stdout.write(f" deleting project {name!r} ({uuid_})...\n")
        await client.delete_project(uuid_)
        sys.stdout.write(f" deleted {name!r}.\n")
    return 0


def _build_envs(
    *,
    relay_token: str,
    db_path: str = "/data/webrelay.db",
) -> list[dict[str, Any]]:
    """Compose the four env vars the relay server expects."""
    password = secrets.token_urlsafe(24)
    session_secret = secrets.token_hex(32)
    return [
        {
            "key": "WEBRELAY_PASSWORD",
            "value": password,
            "is_preview": False,
            "is_buildtime": False,
            "is_literal": True,
        },
        {
            "key": "WEBRELAY_SESSION_SECRET",
            "value": session_secret,
            "is_preview": False,
            "is_buildtime": False,
            "is_literal": True,
        },
        {
            "key": "WEBRELAY_RELAY_TOKEN_HASH",
            "value": _hash_token(relay_token),
            "is_preview": False,
            "is_buildtime": False,
            "is_literal": True,
        },
        {
            "key": "WEBRELAY_DB_PATH",
            "value": db_path,
            "is_preview": False,
            "is_buildtime": False,
            "is_literal": True,
        },
    ]


async def _ensure_project(client: CoolifyClient, name: str) -> dict[str, Any]:
    """Find a project by name, creating it when absent."""
    projects = await client.list_projects()
    for p in projects:
        if p.get("name") == name:
            return p
    created = await client.create_project(
        name=name, description=f"Provisioned by {APP_NAME} provisioner."
    )
    refreshed = await client.list_projects()
    for p in refreshed:
        if p.get("uuid") == created.get("uuid"):
            return p
    return {
        "uuid": created.get("uuid"),
        "name": name,
        "environments": [],
    }


async def cmd_provision(
    client: CoolifyClient,
    *,
    git_url: str | None,
    git_branch: str,
    docker_image: str | None,
    server_uuid: str | None,
    ports_mappings: str,
    fqdn: str | None,
    project_name: str = PROJECT_NAME,
) -> int:
    """Create (or update) the project + application + envs."""
    if not git_url and not docker_image:
        raise SystemExit(
            "provision requires --git-url or --docker-image "
            "(exactly one of the two)."
        )
    if git_url and docker_image:
        raise SystemExit(
            "provision accepts --git-url OR --docker-image, not both."
        )

    project = await _ensure_project(client, project_name)
    project_uuid = str(project.get("uuid", ""))
    env = await _pick_environment(client, project)
    if env is None:
        sys.stdout.write(f"Project '{project_name}' has no environment. Creating 'production'...\n")
        r = await client.client.post(f"/projects/{project_uuid}/environments", json={"name": "production"})
        r.raise_for_status()
        refreshed_projects = await client.list_projects()
        for p in refreshed_projects:
            if p.get("uuid") == project_uuid:
                project = p
                break
        env = await _pick_environment(client, project)
        if env is None:
            raise SystemExit(
                f"project {project_name!r} has no environment even after attempting to create one."
            )
    env_name, env_uuid = env

    if not server_uuid:
        servers = await client.list_servers()
        if not servers:
            raise SystemExit(
                "no servers registered on the Coolify instance; pass "
                "--server-uuid or register one in the UI."
            )
        server_uuid = str(servers[0]["uuid"])

    if git_url:
        created = await client.create_public_app(
            project_uuid=project_uuid,
            server_uuid=server_uuid,
            environment_name=env_name,
            environment_uuid=env_uuid,
            git_repository=git_url,
            git_branch=git_branch,
            build_pack="dockerfile",
            ports_mappings=ports_mappings,
            name=APP_NAME,
        )
        app_uuid = str(created.get("uuid", ""))
        r = await client.client.patch(f"/applications/{app_uuid}", json={"dockerfile_location": "/docker/Dockerfile"})
        r.raise_for_status()
    else:
        created = await client.create_dockerimage_app(
            project_uuid=project_uuid,
            server_uuid=server_uuid,
            environment_name=env_name,
            environment_uuid=env_uuid,
            docker_registry_image_name=docker_image or "",
            ports_mappings=ports_mappings,
            name=APP_NAME,
        )

    app_uuid = str(created.get("uuid", ""))
    fqdn = created.get("domains") or created.get("fqdn") or fqdn or ""

    relay_token = _make_relay_token()
    envs = _build_envs(relay_token=relay_token)
    await client.set_envs_bulk(app_uuid, envs)

    sys.stdout.write(
        "\nProvisioned application:\n"
        f" uuid: {app_uuid}\n"
        f" project: {project_name} ({project_uuid})\n"
        f" environment: {env_name} ({env_uuid})\n"
        f" fqdn: {fqdn or '(none)'}\n"
    )
    sys.stdout.write(
        "\n=== PASTE THE FOLLOWING INTO THE LOCAL-AGENT VAULT ===\n"
        f" id: web-relay.relay_token\n"
        f" bearer: {relay_token}\n"
        f" token_hash: {_hash_token(relay_token)}\n"
        "======================================================\n"
    )
    state_dir = Path(os.environ.get("HERMES_PROVISION_STATE", "/tmp"))
    state_path = state_dir / "hermes_provision_state.json"
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"app_uuid": app_uuid, "fqdn": fqdn or ""}),
            encoding="utf-8",
        )
    except OSError:
        pass
    return 0


async def cmd_deploy(
    client: CoolifyClient,
    *,
    app_uuid: str,
    poll_seconds: float =5.0,
    timeout_seconds: float =600.0,
) -> int:
    """Trigger a deploy and poll until finished / failed."""
    sys.stdout.write(f"Triggering deploy for {app_uuid}...\n")
    runs = await client.deploy(app_uuid)
    if not runs:
        sys.stderr.write("Coolify returned no deployment run uuid.\n")
        return 1
    run_uuid = str(runs[0])
    sys.stdout.write(f" run_uuid: {run_uuid}\n")

    deadline = time.monotonic() + timeout_seconds
    last_log_lines =0
    while True:
        if time.monotonic() > deadline:
            sys.stderr.write(
                f"Deploy {run_uuid} timed out after {timeout_seconds:.0f}s.\n"
            )
            return 1
        info = await client.deployment_status(run_uuid)
        status = str(info.get("status", "")).lower()
        logs = str(info.get("logs", "") or "")
        log_lines = logs.splitlines()
        for line in log_lines[last_log_lines:]:
            sys.stdout.write(f" | {line}\n")
            last_log_lines = len(log_lines)
            sys.stdout.flush()
        if status in ("finished", "success", "succeeded"):
            sys.stdout.write(f"Deploy {run_uuid} finished.\n")
            return 0
        if status in ("failed", "error", "cancelled", "canceled"):
            sys.stderr.write(f"Deploy {run_uuid} ended with status {status!r}.\n")
            return 1
        await asyncio.sleep(poll_seconds)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="provision_coolify",
        description="Provision and manage the hermes-web-relay Coolify deployment.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("inspect", help="List projects and their resource counts.")

    p_cleanup = sub.add_parser(
        "cleanup", help="Interactively delete projects (requires --confirm)."
    )
    p_cleanup.add_argument(
        "--confirm", action="store_true",
        help="Enable deletion. Without this flag, cleanup refuses to delete.",
    )
    p_cleanup.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Print what would be deleted without making DELETE calls (default).",
    )
    p_cleanup.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false",
        help="Actually perform DELETEs. Implies --confirm.",
    )

    p_provision = sub.add_parser(
        "provision",
        help="Create the hermes-web-relay project + application + envs.",
    )
    p_provision.add_argument("--git-url")
    p_provision.add_argument("--git-branch", default="main")
    p_provision.add_argument("--docker-image")
    p_provision.add_argument("--server-uuid", dest="server_uuid")
    p_provision.add_argument("--ports-mappings", default="8000:8000")
    p_provision.add_argument("--fqdn", default=None)
    p_provision.add_argument("--project-name", default=PROJECT_NAME)

    p_deploy = sub.add_parser(
        "deploy",
        help="Trigger and poll a deploy for an app uuid.",
    )
    p_deploy.add_argument(
        "app_uuid",
        nargs="?",
        help="Application uuid. If omitted, uses the one stashed by 'provision'.",
    )
    p_deploy.add_argument("--poll-seconds", type=float, default=5.0)
    p_deploy.add_argument("--timeout-seconds", type=float, default=600.0)

    p_full = sub.add_parser(
        "full",
        help="cleanup --confirm -> provision -> deploy.",
    )
    p_full.add_argument("--git-url")
    p_full.add_argument("--git-branch", default="main")
    p_full.add_argument("--docker-image")
    p_full.add_argument("--server-uuid", dest="server_uuid")
    p_full.add_argument("--ports-mappings", default="8000:8000")
    p_full.add_argument("--fqdn", default=None)
    p_full.add_argument("--project-name", default=PROJECT_NAME)
    p_full.add_argument("--poll-seconds", type=float, default=5.0)
    p_full.add_argument("--timeout-seconds", type=float, default=600.0)

    return p


async def _run(args: argparse.Namespace) -> int:
    api_token, base_url = _resolve_coolify_creds()
    async with CoolifyClient(base_url=base_url, api_token=api_token) as client:
        if args.command == "inspect":
            return await cmd_inspect(client)
        if args.command == "cleanup":
            return await cmd_cleanup(
                client, confirm=args.confirm, dry_run=args.dry_run
            )
        if args.command == "provision":
            return await cmd_provision(
                client,
                git_url=args.git_url,
                git_branch=args.git_branch,
                docker_image=args.docker_image,
                server_uuid=args.server_uuid,
                ports_mappings=args.ports_mappings,
                fqdn=args.fqdn,
                project_name=args.project_name,
            )
        if args.command == "deploy":
            app_uuid = args.app_uuid
            if not app_uuid:
                state_path = Path(
                    os.environ.get("HERMES_PROVISION_STATE", "/tmp")
                ) / "hermes_provision_state.json"
                if not state_path.exists():
                    sys.stderr.write(
                        f"No app_uuid supplied and no state file at "
                        f"{state_path}; run `provision` first.\n"
                    )
                    return 2
                state = json.loads(state_path.read_text(encoding="utf-8"))
                app_uuid = state.get("app_uuid", "")
            return await cmd_deploy(
                client,
                app_uuid=app_uuid,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )
        if args.command == "full":
            rc = await cmd_cleanup(client, confirm=True, dry_run=False)
            if rc !=0:
                return rc
            rc = await cmd_provision(
                client,
                git_url=args.git_url,
                git_branch=args.git_branch,
                docker_image=args.docker_image,
                server_uuid=args.server_uuid,
                ports_mappings=args.ports_mappings,
                fqdn=args.fqdn,
                project_name=args.project_name,
            )
            if rc !=0:
                return rc
            state_path = Path(
                os.environ.get("HERMES_PROVISION_STATE", "/tmp")
            ) / "hermes_provision_state.json"
            app_uuid = ""
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                app_uuid = state.get("app_uuid", "")
            if not app_uuid:
                sys.stderr.write(
                    "Provision did not stash an app_uuid; cannot deploy.\n"
                )
                return 1
            return await cmd_deploy(
                client,
                app_uuid=app_uuid,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )
        raise SystemExit(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
