"""Setup and autostart installation helper for the local agent.

Exposes run() for setup and uninstall() for symmetric teardown.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

# Task / Service names
TASK_NAME_WIN = "HermesWebRelayAgent"
PLIST_LABEL_MAC = "com.hermes.webrelay.agent"
SERVICE_NAME_LINUX = "webrelay-agent.service"


def _get_vault_path(args_path: str | None = None) -> Path:
    """Resolve vault path, defaulting to ~/.hermes/vault.json."""
    if args_path:
        return Path(args_path)
    return Path.home() / ".hermes" / "vault.json"


def normalize_ws_url(url: str) -> str:
    """Normalize HTTP/HTTPS URLs to WebSocket WS/WSS URLs with appropriate suffix."""
    url = url.strip()
    if url.startswith("https://"):
        url = "wss://" + url[8:]
    elif url.startswith("http://"):
        url = "ws://" + url[7:]
    elif not url.startswith("ws://") and not url.startswith("wss://"):
        url = "wss://" + url

    url = url.rstrip("/")
    if not url.endswith("/api/relay/ws"):
        url = url + "/api/relay/ws"
    return url


def normalize_token(token: str) -> str:
    """Strip 'Bearer ' prefix if present."""
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _find_vault_credential(vault: dict, cred_id: str) -> str | None:
    """Search for a credential in the vault."""
    # Try nested object layout
    if "." in cred_id:
        head, tail = cred_id.split(".", 1)
        nested = vault.get(head)
        if isinstance(nested, dict) and tail in nested:
            val = nested[tail]
            if isinstance(val, str):
                return val

    # Try credentials array layout
    creds = vault.get("credentials")
    if isinstance(creds, list):
        for record in creds:
            if isinstance(record, dict):
                for key in ("id", "field", "name", "key"):
                    if record.get(key) == cred_id:
                        # try value then credential
                        for val_key in ("value", "credential"):
                            val = record.get(val_key)
                            if isinstance(val, str) and val:
                                return val
    return None


def run() -> int:
    """Configure credentials and install autostart helper."""
    parser = argparse.ArgumentParser(
        prog="webrelay-agent setup",
        description="Configure credentials and set up autostart for the local agent.",
    )
    parser.add_argument("--server-url", help="Relay server WebSocket URL (wss://...)")
    parser.add_argument("--token", help="Bearer token for agent authentication")
    parser.add_argument(
        "--vault-path", help="Path to ~/.hermes/vault.json (override for tests/dev)"
    )

    args = parser.parse_args(sys.argv[1:])
    vault_path = _get_vault_path(args.vault_path)

    # 1. Load existing vault data
    vault_data: dict = {"credentials": []}
    if vault_path.exists():
        try:
            vault_data = json.loads(vault_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Warning: Could not parse existing vault file: {exc}. Starting fresh.")
            vault_data = {"credentials": []}

    # 2. Resolve server_url
    server_url = args.server_url
    if not server_url:
        # Check vault values
        server_url = (
            _find_vault_credential(vault_data, "webrelay.server_url")
            or _find_vault_credential(vault_data, "web-relay.base_url")
        )
    if not server_url:
        print("Error: --server-url must be provided or configured in ~/.hermes/vault.json.")
        return 1

    # 3. Resolve token
    token = args.token
    if not token:
        # Check vault values
        token = (
            _find_vault_credential(vault_data, "webrelay.bearer_token")
            or _find_vault_credential(vault_data, "web-relay.relay_token")
        )
    if not token:
        print("Error: --token must be provided or configured in ~/.hermes/vault.json.")
        return 1

    server_url = normalize_ws_url(server_url)
    token = normalize_token(token)

    # 4. Save back to vault
    # Layout 3: nested webrelay dictionary
    vault_data["webrelay"] = {
        "server_url": server_url,
        "bearer_token": token,
    }

    # Layout 2: credentials array
    if "credentials" not in vault_data or not isinstance(vault_data["credentials"], list):
        vault_data["credentials"] = []

    # Update or add server_url
    found_url = False
    for rec in vault_data["credentials"]:
        if isinstance(rec, dict) and rec.get("id") == "webrelay.server_url":
            rec["value"] = server_url
            rec["credential"] = server_url
            found_url = True
            break
    if not found_url:
        vault_data["credentials"].append({
            "id": "webrelay.server_url",
            "type": "url",
            "subtype": "webrelay",
            "value": server_url,
            "credential": server_url,
            "remark": "Relay server WebSocket URL",
        })

    # Update or add bearer_token
    found_token = False
    for rec in vault_data["credentials"]:
        if isinstance(rec, dict) and rec.get("id") == "webrelay.bearer_token":
            rec["value"] = token
            rec["credential"] = token
            found_token = True
            break
    if not found_token:
        vault_data["credentials"].append({
            "id": "webrelay.bearer_token",
            "type": "api_token",
            "subtype": "webrelay",
            "value": token,
            "credential": token,
            "remark": "Relay server bearer token",
        })

    try:
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        vault_path.write_text(json.dumps(vault_data, indent=2), encoding="utf-8")
        print(f"Credentials written to {vault_path}")
    except Exception as exc:
        print(f"Error: Could not write vault file: {exc}")
        return 1

    # 5. Set up autostart
    os_name = platform.system().lower()
    py_executable = sys.executable

    if os_name == "windows":
        print("Configuring Windows Scheduled Task...")
        cmd_args = [
            "schtasks", "/create",
            "/tn", TASK_NAME_WIN,
            "/tr", f'"{py_executable}" -m webrelay.agent run',
            "/sc", "ONLOGON",
            "/f"
        ]
        res = subprocess.run(cmd_args, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"Error configuring Windows Scheduled Task: {res.stderr}")
            return res.returncode
        print("Windows Scheduled Task configured successfully.")
        
        # Start immediately
        print("Starting task immediately...")
        subprocess.run(["schtasks", "/run", "/tn", TASK_NAME_WIN], capture_output=True)

    elif os_name == "darwin":
        print("Configuring macOS LaunchAgent...")
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_path = plist_dir / f"{PLIST_LABEL_MAC}.plist"
        
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL_MAC}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py_executable}</string>
        <string>-m</string>
        <string>webrelay.agent</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{os.path.expanduser('~/.hermes/webrelay-agent.log')}</string>
    <key>StandardErrorPath</key>
    <string>{os.path.expanduser('~/.hermes/webrelay-agent.log')}</string>
</dict>
</plist>
"""
        try:
            plist_dir.mkdir(parents=True, exist_ok=True)
            plist_path.write_text(plist_content, encoding="utf-8")
            
            # Ensure log dir exists
            Path(os.path.expanduser('~/.hermes')).mkdir(parents=True, exist_ok=True)
            
            # Try unloading first defensively
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            # Load the agent
            res = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True)
            if res.returncode != 0:
                print(f"Warning: launchctl load returned code {res.returncode}: {res.stderr}")
            else:
                print("macOS LaunchAgent registered and loaded.")
        except Exception as exc:
            print(f"Error configuring macOS LaunchAgent: {exc}")
            return 1

    elif os_name == "linux":
        print("Configuring Linux systemd user service...")
        service_dir = Path.home() / ".config" / "systemd" / "user"
        service_path = service_dir / SERVICE_NAME_LINUX
        
        service_content = f"""[Unit]
Description=Hermes Web-Relay Agent
After=network.target

[Service]
ExecStart="{py_executable}" -m webrelay.agent run
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""
        try:
            service_dir.mkdir(parents=True, exist_ok=True)
            service_path.write_text(service_content, encoding="utf-8")
            
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", SERVICE_NAME_LINUX], capture_output=True)
            res = subprocess.run(["systemctl", "--user", "start", SERVICE_NAME_LINUX], capture_output=True, text=True)
            if res.returncode != 0:
                print(f"Warning: systemctl start returned code {res.returncode}: {res.stderr}")
            else:
                print("Linux systemd user service registered and started.")
        except Exception as exc:
            print(f"Error configuring Linux systemd service: {exc}")
            return 1
    else:
        print(f"Platform {os_name!r} not recognized for autostart helper setup.")

    print("Setup completed successfully.")
    return 0


def uninstall() -> int:
    """Uninstall the autostart configuration and clean up vault entries."""
    parser = argparse.ArgumentParser(
        prog="webrelay-agent uninstall",
        description="Uninstall autostart helpers and clean up credentials.",
    )
    parser.add_argument(
        "--vault-path", help="Path to ~/.hermes/vault.json (override for tests/dev)"
    )
    args = parser.parse_args(sys.argv[2:])  # argv[0]="setup", argv[1]="uninstall"

    # 1. autostart cleanup
    os_name = platform.system().lower()

    if os_name == "windows":
        print("Deleting Windows Scheduled Task...")
        res = subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME_WIN, "/f"], capture_output=True, text=True)
        if res.returncode == 0:
            print("Windows Scheduled Task deleted successfully.")
        else:
            print(f"Note: Scheduled task delete returned code {res.returncode}. (It might not have been registered.)")

    elif os_name == "darwin":
        print("Deleting macOS LaunchAgent...")
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL_MAC}.plist"
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            try:
                plist_path.unlink()
                print("macOS LaunchAgent plist deleted.")
            except Exception as exc:
                print(f"Error deleting macOS plist file: {exc}")
        else:
            print("macOS LaunchAgent plist not found.")

    elif os_name == "linux":
        print("Deleting Linux systemd user service...")
        service_path = Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME_LINUX
        subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME_LINUX], capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", SERVICE_NAME_LINUX], capture_output=True)
        if service_path.exists():
            try:
                service_path.unlink()
                subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
                print("Linux systemd service file deleted.")
            except Exception as exc:
                print(f"Error deleting Linux service file: {exc}")
        else:
            print("Linux systemd service file not found.")

    # 2. Vault Cleanup
    vault_path = _get_vault_path(args.vault_path)
    if vault_path.exists():
        try:
            vault_data = json.loads(vault_path.read_text(encoding="utf-8"))
            dirty = False

            # Delete nested dict
            if "webrelay" in vault_data:
                del vault_data["webrelay"]
                dirty = True

            # Delete credential array records
            creds = vault_data.get("credentials")
            if isinstance(creds, list):
                new_creds = [
                    rec for rec in creds
                    if not (isinstance(rec, dict) and str(rec.get("id")).startswith("webrelay."))
                ]
                if len(new_creds) != len(creds):
                    vault_data["credentials"] = new_creds
                    dirty = True

            if dirty:
                vault_path.write_text(json.dumps(vault_data, indent=2), encoding="utf-8")
                print(f"Cleaned up webrelay credentials from {vault_path}")
        except Exception as exc:
            print(f"Note: Could not clean up credentials from vault: {exc}")

    print("Uninstall completed successfully.")
    return 0
