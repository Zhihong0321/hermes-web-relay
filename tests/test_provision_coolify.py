"""Tests for scripts.provision_coolify.

Every test mocks the Coolify API with respx so no network is
touched. Cleanup tests use ``monkeypatch.setattr(builtins,
"input", ...)`` to feed y/N replies, exercising the
doubly-gated safety mechanism.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

# Make the scripts/ directory importable.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import provision_coolify as pc # noqa: E402


BASE_URL = "https://coolify.test/api/v1"
TOKEN = "fake-token-xyz"


def _make_client():
    """Build a CoolifyClient pre-pointed at the mocked base URL."""
    return pc.CoolifyClient(base_url="https://coolify.test", api_token=TOKEN)


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------


SAMPLE_PROJECTS = [
    {
        "id":1,
        "uuid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "name":"hermes-web-relay",
        "description":"",
        "environments":[
            {
                "id":1,
                "uuid":"eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                "name":"production",
                "project_id":1,
            }
        ],
    },
    {
        "id":2,
        "uuid":"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "name":"Other Project",
        "description":"",
        "environments":[
            {
                "id":2,
                "uuid":"ffffffff-ffff-ffff-ffff-ffffffffffff",
                "name":"production",
                "project_id":2,
            }
        ],
    },
]


SAMPLE_APPLICATIONS = [
    {
        "uuid":"11111111-1111-1111-1111-111111111111",
        "name":"hermes-web-relay",
        "project_uuid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    },
]


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inspect_renders_table(mocked_httpx):
    """``inspect`` should fetch projects/apps/dbs/services and render a table."""
    mocked_httpx.get("/api/v1/projects").mock(
        return_value=httpx.Response(200, json=SAMPLE_PROJECTS)
    )
    mocked_httpx.get("/api/v1/applications").mock(
        return_value=httpx.Response(200, json=SAMPLE_APPLICATIONS)
    )
    mocked_httpx.get("/api/v1/databases").mock(return_value=httpx.Response(200, json=[]))
    mocked_httpx.get("/api/v1/services").mock(return_value=httpx.Response(200, json=[]))

    async with _make_client() as client:
        rc = await pc.cmd_inspect(client)
    assert rc ==0
    # The table is written to sys.stdout; details checked in the next test.


@pytest.mark.asyncio
async def test_inspect_calls_all_four_endpoints(mocked_httpx, capsys):
    """``inspect`` must hit /api/v1/projects, /api/v1/applications, /api/v1/databases, /api/v1/services."""
    projects = mocked_httpx.get("/api/v1/projects").mock(
        return_value=httpx.Response(200, json=SAMPLE_PROJECTS)
    )
    apps = mocked_httpx.get("/api/v1/applications").mock(
        return_value=httpx.Response(200, json=SAMPLE_APPLICATIONS)
    )
    dbs = mocked_httpx.get("/api/v1/databases").mock(
        return_value=httpx.Response(200, json=[])
    )
    svcs = mocked_httpx.get("/api/v1/services").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with _make_client() as client:
        rc = await pc.cmd_inspect(client)
    assert rc ==0
    assert projects.called
    assert apps.called
    assert dbs.called
    assert svcs.called

    out = capsys.readouterr().out
    assert "hermes-web-relay" in out
    assert "Other Project" in out


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_without_confirm_refuses(
    mocked_httpx, capsys, monkeypatch
):
    """Cleanup without --confirm must NOT touch the API and must not call input()."""
    project_route = mocked_httpx.get("/api/v1/projects").mock(
        return_value=httpx.Response(200, json=SAMPLE_PROJECTS)
    )

    called = []

    def _fail(prompt):
        called.append(prompt)
        raise AssertionError(f"input() should not be called: {prompt!r}")

    monkeypatch.setattr("builtins.input", _fail)

    async with _make_client() as client:
        rc = await pc.cmd_cleanup(client, confirm=False, dry_run=False)
    assert rc ==2
    assert not project_route.called
    assert called == []

    err = capsys.readouterr().err
    assert "REFUSING" in err


@pytest.mark.asyncio
async def test_cleanup_with_y_response_makes_delete_calls(
    mocked_httpx, monkeypatch, capsys
):
    """Cleanup with --confirm + y must DELETE apps then projects, in order."""
    mocked_httpx.get("/api/v1/projects").mock(
        return_value=httpx.Response(200, json=SAMPLE_PROJECTS)
    )
    mocked_httpx.get("/api/v1/applications").mock(
        return_value=httpx.Response(200, json=SAMPLE_APPLICATIONS)
    )
    mocked_httpx.get("/api/v1/databases").mock(
        return_value=httpx.Response(200, json=[])
    )
    mocked_httpx.get("/api/v1/services").mock(
        return_value=httpx.Response(200, json=[])
    )

    delete_app = mocked_httpx.delete(
        "/api/v1/applications/11111111-1111-1111-1111-111111111111"
    ).mock(return_value=httpx.Response(200, json={}))
    delete_proj_a = mocked_httpx.delete(
        "/api/v1/projects/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    ).mock(return_value=httpx.Response(200, json={}))
    delete_proj_b = mocked_httpx.delete(
        "/api/v1/projects/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    ).mock(return_value=httpx.Response(200, json={}))

    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    async with _make_client() as client:
        rc = await pc.cmd_cleanup(client, confirm=True, dry_run=False, input_fn=lambda _p: next(answers))
    assert rc ==0
    assert delete_app.called
    assert delete_proj_a.called
    assert not delete_proj_b.called


@pytest.mark.asyncio
async def test_cleanup_with_n_response_makes_no_delete_calls(
    mocked_httpx, monkeypatch, capsys
):
    """Cleanup with --confirm + n must NOT DELETE anything."""
    mocked_httpx.get("/api/v1/projects").mock(
        return_value=httpx.Response(200, json=SAMPLE_PROJECTS)
    )
    mocked_httpx.get("/api/v1/applications").mock(
        return_value=httpx.Response(200, json=SAMPLE_APPLICATIONS)
    )
    mocked_httpx.get("/api/v1/databases").mock(
        return_value=httpx.Response(200, json=[])
    )
    mocked_httpx.get("/api/v1/services").mock(
        return_value=httpx.Response(200, json=[])
    )

    delete_app = mocked_httpx.delete(
        "/api/v1/applications/11111111-1111-1111-1111-111111111111"
    ).mock(return_value=httpx.Response(200, json={}))
    delete_proj_a = mocked_httpx.delete(
        "/api/v1/projects/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    ).mock(return_value=httpx.Response(200, json={}))

    answers = iter(["n", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    async with _make_client() as client:
        rc = await pc.cmd_cleanup(client, confirm=True, dry_run=False, input_fn=lambda _p: next(answers))
    assert rc ==0
    assert not delete_app.called
    assert not delete_proj_a.called


@pytest.mark.asyncio
async def test_cleanup_dry_run_makes_no_delete_calls(
    mocked_httpx, monkeypatch, capsys
):
    """Cleanup with --confirm + y + dry-run must print, but not DELETE."""
    mocked_httpx.get("/api/v1/projects").mock(
        return_value=httpx.Response(200, json=SAMPLE_PROJECTS)
    )
    mocked_httpx.get("/api/v1/applications").mock(
        return_value=httpx.Response(200, json=SAMPLE_APPLICATIONS)
    )
    mocked_httpx.get("/api/v1/databases").mock(
        return_value=httpx.Response(200, json=[])
    )
    mocked_httpx.get("/api/v1/services").mock(
        return_value=httpx.Response(200, json=[])
    )

    delete_app = mocked_httpx.delete(
        "/api/v1/applications/11111111-1111-1111-1111-111111111111"
    ).mock(return_value=httpx.Response(200, json={}))
    delete_proj_a = mocked_httpx.delete(
        "/api/v1/projects/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    ).mock(return_value=httpx.Response(200, json={}))

    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    async with _make_client() as client:
        rc = await pc.cmd_cleanup(client, confirm=True, dry_run=True, input_fn=lambda _p: next(answers))
    assert rc ==0
    assert not delete_app.called
    assert not delete_proj_a.called
    out = capsys.readouterr().out
    assert "dry-run" in out


# ---------------------------------------------------------------------------
# provision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_creates_project_app_and_envs(
    mocked_httpx, monkeypatch, tmp_path, capsys
):
    """provision must: ensure project, create app, PATCH envs with the right keys."""
    monkeypatch.setenv("HERMES_PROVISION_STATE", str(tmp_path))

    mocked_httpx.get("/api/v1/projects").mock(
        return_value=httpx.Response(200, json=SAMPLE_PROJECTS)
    )
    mocked_httpx.get("/api/v1/applications").mock(
        return_value=httpx.Response(200, json=[])
    )
    mocked_httpx.get("/api/v1/databases").mock(
        return_value=httpx.Response(200, json=[])
    )
    mocked_httpx.get("/api/v1/services").mock(
        return_value=httpx.Response(200, json=[])
    )
    mocked_httpx.get("/api/v1/servers").mock(
        return_value=httpx.Response(200, json=[
            {
                "uuid":"ssssssss-ssss-ssss-ssss-ssssssssssss",
                "name":"server1",
                "is_usable":True,
            }
        ])
    )
    mocked_httpx.post("/api/v1/applications/public").mock(
        return_value=httpx.Response(201, json={
            "uuid":"99999999-9999-9999-9999-999999999999"
        })
    )

    env_route = mocked_httpx.patch(
        "/api/v1/applications/99999999-9999-9999-9999-999999999999/envs/bulk"
    ).mock(return_value=httpx.Response(200, json={"ok": True}))

    async with _make_client() as client:
        rc = await pc.cmd_provision(
        client,
        git_url="https://github.com/me/hermes-web-relay",
        git_branch="main",
        docker_image=None,
        server_uuid=None,
        ports_mappings="8000:8000",
        fqdn="https://relay.example.com",
    )
    assert rc ==0
    assert env_route.called

    body = env_route.calls.last.request.content
    payload = json.loads(body)
    data = payload["data"]
    keys = {e["key"] for e in data}
    assert "WEBRELAY_PASSWORD" in keys
    assert "WEBRELAY_SESSION_SECRET" in keys
    assert "WEBRELAY_RELAY_TOKEN_HASH" in keys
    assert "WEBRELAY_DB_PATH" in keys
    db_entry = next(e for e in data if e["key"] == "WEBRELAY_DB_PATH")
    assert db_entry["value"] == "/data/webrelay.db"
    for e in data:
        assert e["is_literal"] is True

    out = capsys.readouterr().out
    assert "bearer:" in out
    assert "token_hash:" in out


# ---------------------------------------------------------------------------
# deploy poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_polls_until_finished(mocked_httpx, monkeypatch, capsys):
    """deploy must POST /api/v1/deploy, then poll /api/v1/deployments/{run} until status=finished."""
    import asyncio

    mocked_httpx.post("/api/v1/deploy").mock(
        return_value=httpx.Response(200, json=["rrrrrrrr-rrrr-rrrr-rrrr-rrrrrrrrrrrr"])
    )
    mocked_httpx.get(
        "/api/v1/deployments/rrrrrrrr-rrrr-rrrr-rrrr-rrrrrrrrrrrr"
    ).mock(side_effect=[
        httpx.Response(200, json={"status":"queued", "logs":"Starting...\n"}),
        httpx.Response(200, json={"status":"in_progress", "logs":"Starting...\nBuilding...\n"}),
        httpx.Response(200, json={"status":"finished", "logs":"Starting...\nBuilding...\nDone.\n"}),
    ])

    async def _fast_sleep(_seconds):
        return None
    monkeypatch.setattr(pc.asyncio, "sleep", _fast_sleep)

    async with _make_client() as client:
        rc = await pc.cmd_deploy(
        client,
        app_uuid="99999999-9999-9999-9999-999999999999",
        poll_seconds=0.01,
        timeout_seconds=5.0,
    )
    assert rc ==0
    out = capsys.readouterr().out
    assert "Building" in out
    assert "finished" in out


@pytest.mark.asyncio
async def test_deploy_propagates_failure(mocked_httpx, monkeypatch, capsys):
    """A failed status should make cmd_deploy return non-zero."""
    import asyncio

    async def _fast_sleep(_seconds):
        return None
    monkeypatch.setattr(pc.asyncio, "sleep", _fast_sleep)

    mocked_httpx.post("/api/v1/deploy").mock(
        return_value=httpx.Response(200, json=["rrrrrrrr-rrrr-rrrr-rrrr-rrrrrrrrrrrr"])
    )
    mocked_httpx.get(
        "/api/v1/deployments/rrrrrrrr-rrrr-rrrr-rrrr-rrrrrrrrrrrr"
    ).mock(return_value=httpx.Response(200, json={"status":"failed", "logs":"oops\n"}))

    async with _make_client() as client:
        rc = await pc.cmd_deploy(
        client,
        app_uuid="99999999-9999-9999-9999-999999999999",
        poll_seconds=0.01,
        timeout_seconds=5.0,
    )
    assert rc ==1
    err = capsys.readouterr().err
    assert "failed" in err
