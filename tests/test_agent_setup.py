"""Tests for the agent setup and uninstall module."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from webrelay.agent import setup


@pytest.fixture
def temp_vault(tmp_path: Path) -> Path:
    """Return a temporary vault path."""
    return tmp_path / "vault.json"


def test_normalize_ws_url() -> None:
    """Verify URL normalization to WebSocket format."""
    assert setup.normalize_ws_url("http://example.com") == "ws://example.com/api/relay/ws"
    assert setup.normalize_ws_url("https://example.com/") == "wss://example.com/api/relay/ws"
    assert setup.normalize_ws_url("wss://my-relay.com/api/relay/ws") == "wss://my-relay.com/api/relay/ws"
    assert setup.normalize_ws_url("localhost:8000") == "wss://localhost:8000/api/relay/ws"


def test_normalize_token() -> None:
    """Verify bearer token normalization."""
    assert setup.normalize_token("Bearer my-token-123") == "my-token-123"
    assert setup.normalize_token(" bearer my-token-123 ") == "my-token-123"
    assert setup.normalize_token("my-token-123") == "my-token-123"


@patch("platform.system")
@patch("subprocess.run")
def test_setup_runs_windows(mock_run: MagicMock, mock_system: MagicMock, temp_vault: Path) -> None:
    """Verify setup on Windows writes vault and configures schtasks."""
    mock_system.return_value = "Windows"
    mock_run.return_value = MagicMock(returncode=0)

    # We mock sys.argv
    test_argv = [
        "webrelay.agent.setup",
        "--server-url",
        "http://relay-url.internal",
        "--token",
        "Bearer abc123xyz",
        "--vault-path",
        str(temp_vault),
    ]

    with patch.object(sys, "argv", test_argv):
        rc = setup.run()
        assert rc == 0

    # Check vault was written
    assert temp_vault.exists()
    vault_content = json.loads(temp_vault.read_text(encoding="utf-8"))
    assert vault_content["webrelay"]["server_url"] == "ws://relay-url.internal/api/relay/ws"
    assert vault_content["webrelay"]["bearer_token"] == "abc123xyz"

    # Check schtasks was called
    mock_run.assert_any_call(
        [
            "schtasks",
            "/create",
            "/tn",
            "HermesWebRelayAgent",
            "/tr",
            f'"{sys.executable}" -m webrelay.agent run',
            "/sc",
            "ONLOGON",
            "/f",
        ],
        capture_output=True,
        text=True,
    )


@patch("platform.system")
@patch("subprocess.run")
def test_setup_runs_darwin(mock_run: MagicMock, mock_system: MagicMock, temp_vault: Path, tmp_path: Path) -> None:
    """Verify setup on macOS writes plist and loads launchctl."""
    mock_system.return_value = "Darwin"
    mock_run.return_value = MagicMock(returncode=0)

    # Mock Path.home() so plist is written to a tmp dir
    mock_home = tmp_path / "home"
    mock_home.mkdir()

    test_argv = [
        "webrelay.agent.setup",
        "--server-url",
        "https://relay-url.internal",
        "--token",
        "abc123xyz",
        "--vault-path",
        str(temp_vault),
    ]

    with patch.object(sys, "argv", test_argv), patch("pathlib.Path.home", return_value=mock_home):
        rc = setup.run()
        assert rc == 0

    plist_path = mock_home / "Library" / "LaunchAgents" / "com.hermes.webrelay.agent.plist"
    assert plist_path.exists()
    plist_text = plist_path.read_text(encoding="utf-8")
    assert "<string>com.hermes.webrelay.agent</string>" in plist_text
    assert "<string>run</string>" in plist_text

    # Check launchctl was called
    mock_run.assert_any_call(
        ["launchctl", "load", "-w", str(plist_path)],
        capture_output=True,
        text=True,
    )


@patch("platform.system")
@patch("subprocess.run")
def test_setup_runs_linux(mock_run: MagicMock, mock_system: MagicMock, temp_vault: Path, tmp_path: Path) -> None:
    """Verify setup on Linux writes systemd service and triggers systemctl."""
    mock_system.return_value = "Linux"
    mock_run.return_value = MagicMock(returncode=0)

    mock_home = tmp_path / "home"
    mock_home.mkdir()

    test_argv = [
        "webrelay.agent.setup",
        "--server-url",
        "wss://relay-url.internal",
        "--token",
        "abc123xyz",
        "--vault-path",
        str(temp_vault),
    ]

    with patch.object(sys, "argv", test_argv), patch("pathlib.Path.home", return_value=mock_home):
        rc = setup.run()
        assert rc == 0

    service_path = mock_home / ".config" / "systemd" / "user" / "webrelay-agent.service"
    assert service_path.exists()
    service_text = service_path.read_text(encoding="utf-8")
    assert "Description=Hermes Web-Relay Agent" in service_text

    mock_run.assert_any_call(
        ["systemctl", "--user", "start", "webrelay-agent.service"],
        capture_output=True,
        text=True,
    )


@patch("platform.system")
@patch("subprocess.run")
def test_uninstall_windows(mock_run: MagicMock, mock_system: MagicMock, temp_vault: Path) -> None:
    """Verify uninstall cleans up Windows task and deletes vault records."""
    mock_system.return_value = "Windows"
    mock_run.return_value = MagicMock(returncode=0)

    # Pre-populate vault
    vault_data = {
        "webrelay": {"server_url": "wss://x", "bearer_token": "y"},
        "credentials": [
            {"id": "webrelay.server_url", "value": "wss://x"},
            {"id": "webrelay.bearer_token", "value": "y"},
            {"id": "other.credential", "value": "keep-me"},
        ],
    }
    temp_vault.write_text(json.dumps(vault_data), encoding="utf-8")

    test_argv = ["webrelay.agent.setup", "uninstall", "--vault-path", str(temp_vault)]
    with patch.object(sys, "argv", test_argv):
        rc = setup.uninstall()
        assert rc == 0

    # Check schtasks was deleted
    mock_run.assert_any_call(
        ["schtasks", "/delete", "/tn", "HermesWebRelayAgent", "/f"],
        capture_output=True,
        text=True,
    )

    # Check vault was cleaned up
    vault_content = json.loads(temp_vault.read_text(encoding="utf-8"))
    assert "webrelay" not in vault_content
    assert len(vault_content["credentials"]) == 1
    assert vault_content["credentials"][0]["id"] == "other.credential"
