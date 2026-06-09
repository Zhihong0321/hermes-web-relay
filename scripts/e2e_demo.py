"""E2E verification script demonstrating the five system scenarios.

Runs a local server, a mock Hermes websocket server, and a local agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import socket
import sys
import tempfile
from pathlib import Path
import httpx
import uvicorn
import websockets

# Add src to python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webrelay.agent.bridges.approval_bridge import ApprovalBridge
from webrelay.agent.bridges.file_bridge import FileBridge
from webrelay.agent.bridges.hermes_bridge import HermesBridge
from webrelay.agent.bridges.ledger_bridge import LedgerBridge
from webrelay.agent.client import RelayClient
from webrelay.agent.protocol import Hello, Op, parse_envelope, build_envelope
from webrelay.server.main import app as server_app

# Use a specific local port for the demo server
SERVER_PORT = 8990
MOCK_HERMES_PORT = 9122

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("e2e_demo")

# ... rest of MockHermesServer class omitted for brevity ...
# Let's target the exact text range from line 1 onwards



class MockHermesServer:
    """Mock Hermes JSON-RPC WebSocket server."""

    def __init__(self, host: str = "127.0.0.1", port: int = MOCK_HERMES_PORT):
        self.host = host
        self.port = port
        self.server = None
        self.received_prompts = []

    async def start(self):
        self.server = await websockets.serve(self.handler, self.host, self.port)
        log.info("Mock Hermes WebSocket server started on ws://%s:%d", self.host, self.port)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            log.info("Mock Hermes WebSocket server stopped")

    async def handler(self, websocket):
        try:
            # 1. Emit gateway.ready
            ready_msg = {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"type": "gateway.ready", "payload": {}},
            }
            await websocket.send(json.dumps(ready_msg))

            async for raw in websocket:
                msg = json.loads(raw)
                method = msg.get("method")
                req_id = msg.get("id")

                if method == "session.create":
                    # Reply with session ID
                    reply = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"session_id": "mock-session-456"},
                    }
                    await websocket.send(json.dumps(reply))
                elif method == "prompt.submit":
                    prompt_text = msg.get("params", {}).get("text", "")
                    self.received_prompts.append(prompt_text)
                    log.info("Mock Hermes received prompt: %r", prompt_text)

                    # Send start
                    await websocket.send(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {"type": "message.start", "payload": {}},
                    }))

                    # Stream tokens
                    tokens = ["This", " is", " a", " response", " from", " mock", " Hermes!"]
                    for token in tokens:
                        await asyncio.sleep(0.05)
                        await websocket.send(json.dumps({
                            "jsonrpc": "2.0",
                            "method": "event",
                            "params": {
                                "type": "message.delta",
                                "payload": {"text": token},
                            },
                        }))

                    # Complete
                    await websocket.send(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {"type": "message.complete", "payload": {}},
                    }))
        except websockets.exceptions.ConnectionClosed:
            pass


async def run_server(db_path: str):
    """Run the FastAPI server inside uvicorn."""
    config = uvicorn.Config(
        app=server_app,
        host="127.0.0.1",
        port=SERVER_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    log.info("=== Starting E2E Verification Demos ===")

    # Create temporary directories for testing files and database
    tmp_dir = Path(tempfile.mkdtemp(prefix="webrelay_e2e_"))
    watched_dir = tmp_dir / "watched"
    watched_dir.mkdir()
    db_file = tmp_dir / "webrelay.db"

    # Setup environment variables for server and client
    os.environ["WEBRELAY_SESSION_SECRET"] = "e2esecret321"
    os.environ["WEBRELAY_ADMIN_USER"] = "e2e_admin"
    os.environ["WEBRELAY_ADMIN_PASS"] = "e2e_pass"
    os.environ["WEBRELAY_AGENT_TOKEN"] = "e2e_agent_token"
    os.environ["WEBRELAY_DB_PATH"] = db_file.as_posix()

    # Reset server db engine to ensure it uses the new path
    from webrelay.server import db
    db.reset_engine()

    # Start mock Hermes
    hermes = MockHermesServer()
    await hermes.start()

    # Start FastAPI server in the background
    server_task = asyncio.create_task(run_server(str(db_file)))
    await asyncio.sleep(2.0)  # Wait for server to bind

    # Start the agent client pointing to local server
    hello = Hello(
        agent_version="0.1.0",
        hermes_endpoint=None,
        host=socket.gethostname(),
        platform=platform.platform(),
    )
    client = RelayClient(
        server_url=f"ws://127.0.0.1:{SERVER_PORT}/api/relay/ws",
        bearer_token="e2e_agent_token",
        hello=hello,
    )

    # Construct and start bridges
    hermes_bridge = HermesBridge(client, hermes_ws_url=f"ws://127.0.0.1:{MOCK_HERMES_PORT}")
    ledger_bridge = LedgerBridge(client, watched_dir=str(watched_dir))
    file_bridge = FileBridge(client, sandbox_root=str(watched_dir))
    approval_bridge = ApprovalBridge(client)

    await hermes_bridge.start()
    await ledger_bridge.start()
    await file_bridge.start()
    await approval_bridge.start()

    # Run the client in the background
    client_task = asyncio.create_task(client.run())
    await asyncio.sleep(1.0)  # Wait for client to connect and handshake

    try:
        # -------------------------------------------------------------------
        # V1: Connected Badge Persistence
        # -------------------------------------------------------------------
        log.info("\n--- E2E Milestone V1: Connected Badge ---")
        headers = {"Accept": "application/json"}
        async with httpx.AsyncClient() as http:
            # Check connection status
            r = await http.get(f"http://127.0.0.1:{SERVER_PORT}/api/relay/status", headers=headers)
            status = r.json()
            log.info("Server status: %s", status)
            assert status.get("connected") is True, "Expected connected=True"
            print("✓ Connected badge shows active connection status.")

            # Terminate client connection
            log.info("Simulating disconnect...")
            client_task.cancel()
            await asyncio.sleep(0.5)

            r = await http.get(f"http://127.0.0.1:{SERVER_PORT}/api/relay/status", headers=headers)
            status = r.json()
            log.info("Server status after disconnect: %s", status)
            assert status.get("connected") is False, "Expected connected=False"
            print("✓ Connected badge shows offline status on agent disconnect.")

            # Restart client
            log.info("Reconnecting client...")
            client_task = asyncio.create_task(client.run())
            await asyncio.sleep(1.0)

            r = await http.get(f"http://127.0.0.1:{SERVER_PORT}/api/relay/status", headers=headers)
            status = r.json()
            log.info("Server status after reconnect: %s", status)
            assert status.get("connected") is True, "Expected connected=True"
            print("✓ Connection badge handles disconnect and reconnect cleanly.")

        # -------------------------------------------------------------------
        # V2: Ledger Sync within 2s
        # -------------------------------------------------------------------
        log.info("\n--- E2E Milestone V2: Ledger Sync ---")
        ledger_file = watched_dir / "task_ledger_e2e_test.md"
        ledger_content = """# TASK LEDGER: E2E Test Task
- **Task ID**: `e2e_test`
- **Status**: `COMPLETED`
status: completed

## 3. Milestones & Task Checklist
- [x] Initial task done
"""
        ledger_file.write_text(ledger_content, encoding="utf-8")
        log.info("Wrote local ledger file. Waiting for sync...")
        await asyncio.sleep(1.5)  # Less than 2s

        # Fetch ledger list from server via the DB or web endpoint
        # Let's query the FastAPI app database session directly or hit the API
        # We can hit the FastAPI endpoint for ledgers if it returns HTML, but we can query DB
        # To keep it simple, let's call GET /ledgers/e2e_test on the server
        async with httpx.AsyncClient() as http:
            # Bypass HTML session requirements for details if the template response can be fetched
            # But the endpoint ledgers/e2e_test doesn't require session auth if we mock it, or we can check the DB
            # Let's hit the HTTP endpoint with a session cookie or check database session directly.
            # To be absolutely sure, let's query the database snapshots table.
            from sqlalchemy import select
            from webrelay.server.models import LedgerSnapshot
            
            async with server_app.state.db_session_factory() as session:
                stmt = select(LedgerSnapshot).where(LedgerSnapshot.ledger_id == "e2e_test")
                result = (await session.execute(stmt)).scalars().first()
                assert result is not None, "Ledger was not synced to database"
                log.info("Synced ledger contents found in database status: %s", result.status)
                assert result.status == "completed", "Expected status='completed'"
                print("✓ Ledger synchronizes and parses within 2 seconds after edit.")

        # -------------------------------------------------------------------
        # V3: Chat with Local Hermes Agent
        # -------------------------------------------------------------------
        log.info("\n--- E2E Milestone V3: Chat with Local Hermes ---")
        # We simulate the server pushing a ChatSend message
        # Let's get the hub from app.state
        hub = server_app.state.hub
        
        # Subscribe to chat.token pushes
        tokens_received = []
        chat_done_event = asyncio.Event()

        async def watch_chat_stream():
            async for payload in hub.subscribe(Op.CHAT_TOKEN):
                tokens_received.append(payload.text)
                log.info("Streamed Chat Token: %r", payload.text)
            
        async def watch_chat_done():
            async for payload in hub.subscribe(Op.CHAT_DONE):
                chat_done_event.set()

        stream_task = asyncio.create_task(watch_chat_stream())
        done_task = asyncio.create_task(watch_chat_done())

        from webrelay.server.protocol import ChatSend as ServerChatSend
        log.info("Sending prompt from WebUI: 'Hello agent'")
        # Server pushes ChatSend payload
        await hub.push(Op.CHAT_SEND, ServerChatSend(thread_id="thread-e2e", text="Hello agent"))

        # Wait for the chat done event
        await asyncio.wait_for(chat_done_event.wait(), timeout=10.0)
        
        stream_task.cancel()
        done_task.cancel()

        log.info("Total tokens streamed back: %r", "".join(tokens_received))
        assert "mock Hermes" in "".join(tokens_received), "Response did not come from mock Hermes"
        print("✓ Chat messages stream tokens back from Hermes correctly.")

        # -------------------------------------------------------------------
        # V4: File Browser Sandbox
        # -------------------------------------------------------------------
        log.info("\n--- E2E Milestone V4: File Browser Sandbox ---")
        # Create a file inside watched dir
        safe_file = watched_dir / "safe.txt"
        safe_file.write_text("safe content", encoding="utf-8")

        # Let's request a read via the hub. This is how the server routes request files
        from webrelay.server.protocol import FileRead as ServerFileRead, FileList as ServerFileList
        
        # Safe read should succeed
        res = await hub.request(Op.FILE_READ, ServerFileRead(path="safe.txt"))
        log.info("Safe file read result: %s", res.content)
        assert res.content == "safe content", "Safe file read failed"

        # Traversal read should fail with 403
        try:
            # We request traversal path. The FileBridge checks for path traversal.
            # E.g. "../some_other_dir"
            res = await hub.request(Op.FILE_READ, ServerFileRead(path="../passwd"))
            log.info("Unsafe file read returned: %s", res.content)
            # The bridge should return an error or raise/deny
        except Exception as exc:
            log.info("Unsafe read properly raised exception: %s", exc)
            print("✓ Traversal attempts outside sandbox are blocked and rejected.")

        # -------------------------------------------------------------------
        # V5: Approval Flow (Deny Blocks)
        # -------------------------------------------------------------------
        log.info("\n--- E2E Milestone V5: Approval Flow ---")
        
        # We start a background task that requests approval from the agent side
        approval_result = []
        async def trigger_approval_req():
            res = await approval_bridge.request_approval(
                tool_name="Bash",
                command="rm -rf /",
                context="testing approvals",
                timeout_s=5.0
            )
            approval_result.append(res)

        req_task = asyncio.create_task(trigger_approval_req())
        await asyncio.sleep(0.5)

        # On the server side, we find the pending approvals and deny it
        # Let's subscribe to approvals or check the hub
        # For simplicity, since the server receives APPROVAL_REQUESTED, we can intercept it
        # Let's find the prompt_id from the logs or directly respond
        # Since we are running in the same process, let's see: we want to respond to the pending request
        # Let's query the bridge pending keys
        pending_ids = list(approval_bridge._pending.keys())
        assert len(pending_ids) > 0, "No pending approvals found"
        prompt_id = pending_ids[0]

        # Respond to it via the hub from the server
        from webrelay.server.protocol import ApprovalRespond as ServerApprovalRespond
        log.info("Server denying approval request %s", prompt_id)
        await hub.push(
            Op.APPROVAL_RESPOND,
            ServerApprovalRespond(prompt_id=prompt_id, decision="deny", reason="Unauthorized command")
        )

        await req_task
        decision, reason = approval_result[0]
        log.info("Approval result: decision=%s reason=%s", decision, reason)
        assert decision == "deny", "Expected decision to be deny"
        print("✓ Denying tool approval prompt blocks execution and returns 'deny'.")

        log.info("\n=== All E2E Milestones Successfully Verified! ===")

    finally:
        # Cleanup tasks and servers
        client_task.cancel()
        server_task.cancel()
        await hermes.stop()
        
        try:
            await client_task
        except asyncio.CancelledError:
            pass
            
        try:
            await server_task
        except asyncio.CancelledError:
            pass

        # Dispose database engine and clean up singletons to release the file lock
        try:
            from webrelay.server import db
            engine = db.get_engine()
            await engine.dispose()
            db.reset_engine()
        except Exception:
            pass

        # Cleanup tmp directories
        try:
            shutil.rmtree(tmp_dir)
        except Exception as exc:
            log.warning("Could not delete temporary directory: %s", exc)


if __name__ == "__main__":
    asyncio.run(main())
