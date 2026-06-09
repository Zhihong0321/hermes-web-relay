"""Stage-1 skeleton tests for the relay hub and agent client.

These tests verify the **contract** of the C3 public API:

* Every module imports cleanly (syntax + name resolution).
* Every public class is defined and has the documented methods with
  the correct sync/async/generator kind.
* Every stub body raises ``NotImplementedError`` when invoked —
  proving the signature is wired and the body is a placeholder waiting
  for Stage 2.

Because the ``__init__`` of every class is itself a stub that raises
``NotImplementedError``, we **cannot construct instances** to introspect
methods. Instead we inspect the **unbound methods on the class object**
— this works regardless of whether ``__init__`` runs successfully.

Stage-2 implementation agents (S2 for ``relay_hub.py``, L1 for the
agent modules) are expected to DELETE the corresponding ``pytest.raises``
blocks from this file as they replace the stubs with real bodies.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

# --- Imports under test (the SKELETON modules) --------------------------

from webrelay.agent.client import RelayClient
from webrelay.agent.config import (
    DEFAULT_FILE_SANDBOX_ROOT,
    DEFAULT_HERMES_WS_URL,
    DEFAULT_WATCHED_LEDGER_DIR,
    AgentConfig,
    load_config,
)
from webrelay.agent.reconnect import reconnect_backoff
from webrelay.server.relay_hub import RelayHub


# --- Helpers ------------------------------------------------------------


class _HelloPayload(BaseModel):
    """Stand-in for the C1 Hello schema — used only to satisfy signatures.

    C1's real Hello model lives in ``webrelay.server.protocol`` /
    ``webrelay.agent.protocol``. The L1 RelayClient constructor accepts
    any ``BaseModel`` for the ``hello`` arg.
    """


# --- relay_hub ----------------------------------------------------------


def test_relay_hub_module_imports() -> None:
    """The relay_hub module exposes the RelayHub class."""
    assert inspect.isclass(RelayHub)


def test_relay_hub_construct_signature() -> None:
    """RelayHub.__init__ has one keyword-only timeout argument.

    We cannot call the constructor (the body is a stub) so we inspect
    the unbound method's signature instead.
    """
    sig = inspect.signature(RelayHub.__init__)
    params = list(sig.parameters.values())
    # self + one kw-only request_timeout_s
    assert len(params) == 2
    assert params[0].name == "self"
    assert params[1].name == "request_timeout_s"
    assert params[1].default == 30.0
    assert params[1].kind == inspect.Parameter.KEYWORD_ONLY


def test_relay_hub_attach_is_async() -> None:
    """attach is an async method (must be awaited)."""
    assert inspect.iscoroutinefunction(RelayHub.attach)


def test_relay_hub_detach_is_async() -> None:
    """detach is an async method."""
    assert inspect.iscoroutinefunction(RelayHub.detach)


def test_relay_hub_is_connected_is_sync() -> None:
    """is_connected is a cheap, synchronous boolean (used by nav badge)."""
    assert not inspect.iscoroutinefunction(RelayHub.is_connected)


def test_relay_hub_request_is_async() -> None:
    """request is an async method (returns the correlated reply)."""
    assert inspect.iscoroutinefunction(RelayHub.request)


def test_relay_hub_request_signature() -> None:
    """request(op, payload) takes both args positionally; no defaults."""
    sig = inspect.signature(RelayHub.request)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    assert [p.name for p in params] == ["op", "payload"]


def test_relay_hub_push_is_async() -> None:
    """push is an async method (fire-and-forget)."""
    assert inspect.iscoroutinefunction(RelayHub.push)


def test_relay_hub_on_inbound_is_async() -> None:
    """on_inbound is an async method (called per received frame)."""
    assert inspect.iscoroutinefunction(RelayHub.on_inbound)


def test_relay_hub_subscribe_returns_async_iterator() -> None:
    """subscribe must return an async iterator (for SSE).

    We cannot call it (body is a stub) but we can inspect the return
    annotation — it should be ``AsyncIterator[BaseModel]``.
    """
    sig = inspect.signature(RelayHub.subscribe)
    anno = sig.return_annotation
    # Either the string form "AsyncIterator[BaseModel]" or the
    # evaluated typing.AsyncIterator. We accept any iterable annotation
    # whose name contains "AsyncIterator" or "AsyncGenerator".
    assert "AsyncIterator" in str(anno) or "AsyncGenerator" in str(anno), (
        f"subscribe return annotation must be an async iterator, got {anno!r}"
    )


# Skeleton-body assertions for RelayHub were removed by S2 when the
# hub body was filled in. The implementation now constructs and runs
# end-to-end; coverage of its behaviour lives in test_relay_hub_impl.py.


# --- agent.client.RelayClient ------------------------------------------


def test_relay_client_module_imports() -> None:
    assert inspect.isclass(RelayClient)


def test_relay_client_construct_signature() -> None:
    """RelayClient.__init__ takes (server_url, bearer_token, hello) — all required."""
    sig = inspect.signature(RelayClient.__init__)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    assert [p.name for p in params] == ["server_url", "bearer_token", "hello"]
    for p in params:
        assert p.default is inspect.Parameter.empty, (
            f"{p.name} must be required (no default)"
        )


def test_relay_client_run_is_async() -> None:
    assert inspect.iscoroutinefunction(RelayClient.run)


def test_relay_client_register_handler_is_sync() -> None:
    """register_handler is synchronous; the handler itself is async."""
    assert not inspect.iscoroutinefunction(RelayClient.register_handler)


def test_relay_client_register_handler_signature() -> None:
    """register_handler(op, handler) — both positional, no defaults."""
    sig = inspect.signature(RelayClient.register_handler)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    assert [p.name for p in params] == ["op", "handler"]


def test_relay_client_send_is_async() -> None:
    assert inspect.iscoroutinefunction(RelayClient.send)


def test_relay_client_send_signature() -> None:
    """send(op, payload, *, correlation_id=None) — correlation_id is kw-only."""
    sig = inspect.signature(RelayClient.send)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    assert [p.name for p in params] == ["op", "payload", "correlation_id"]
    cid = params[2]
    assert cid.default is None
    assert cid.kind == inspect.Parameter.KEYWORD_ONLY


def test_relay_client_respond_is_async() -> None:
    assert inspect.iscoroutinefunction(RelayClient.respond)


def test_relay_client_respond_signature() -> None:
    """respond(envelope, payload) — both positional, no defaults."""
    sig = inspect.signature(RelayClient.respond)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    assert [p.name for p in params] == ["envelope", "payload"]


# --- Skeleton-body assertions for RelayClient (removed by L1) ----------


def test_relay_client_constructor_body_runs() -> None:
    """The constructor stores its arguments and does not raise.

    Stage-2 L1 implementation note: ``__init__`` is now a real body
    that just stashes the args in instance attributes.
    """
    client = RelayClient("wss://x", "t", _HelloPayload())
    # The private attrs are an implementation detail, but checking that
    # the construction succeeded and the object is a RelayClient is
    # enough to prove the body is no longer a stub.
    assert isinstance(client, RelayClient)


def test_relay_client_run_is_async() -> None:
    # run() is now a real coroutine; ``inspect.iscoroutinefunction``
    # still holds.
    assert inspect.iscoroutinefunction(RelayClient.run)


def test_relay_client_register_handler_runs() -> None:
    """register_handler stores the handler and does not raise.

    Stage-2 L1 implementation note: register_handler appends to an
    internal list keyed by op. We verify the body is no longer a stub
    by constructing a real client and registering a handler against it.
    """
    import asyncio

    async def _handler(envelope: object, payload: object) -> None:
        return None

    client = RelayClient("wss://x", "t", _HelloPayload())
    client.register_handler("chat.token", _handler)
    # Multiple registrations for the same op should be allowed; we
    # don't introspect the internal list (it's an implementation
    # detail) but a second call must not raise either.
    client.register_handler("chat.token", _handler)
    # Touch asyncio so the import above is not flagged as unused by
    # linters that look for module-level references.
    assert asyncio.iscoroutinefunction(_handler)


def test_relay_client_send_is_async() -> None:
    # send() is now a real coroutine.
    assert inspect.iscoroutinefunction(RelayClient.send)


def test_relay_client_respond_is_async() -> None:
    # respond() is now a real coroutine.
    assert inspect.iscoroutinefunction(RelayClient.respond)


# --- agent.reconnect -----------------------------------------------------


def test_reconnect_backoff_imports() -> None:
    assert callable(reconnect_backoff)


def test_reconnect_backoff_is_regular_generator() -> None:
    """reconnect_backoff is a regular (sync) generator function.

    Stage-2 L1 implementation note: per the agent L1 spec the
    backoff schedule is decoupled from the asyncio event loop, so
    ``run`` drives it with ``next(...)`` + ``await asyncio.sleep(...)``.
    We assert that the function is a generator function but NOT an
    async one.
    """
    assert inspect.isgeneratorfunction(reconnect_backoff)
    assert not inspect.isasyncgenfunction(reconnect_backoff)


def test_reconnect_backoff_signature() -> None:
    """reconnect_backoff(initial=1.0, max=60.0, jitter=0.3, *, jitter_fn=None).

    L1 added a keyword-only ``jitter_fn`` parameter so tests can pass a
    deterministic jitter function. The three existing parameters keep
    their documented defaults.
    """
    sig = inspect.signature(reconnect_backoff)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["initial", "max", "jitter", "jitter_fn"]
    assert params[0].default == 1.0
    assert params[1].default == 60.0
    assert params[2].default == 0.3
    assert params[3].default is None
    assert params[3].kind == inspect.Parameter.KEYWORD_ONLY


def test_reconnect_backoff_body_runs() -> None:
    """The body yields sleep durations instead of raising.

    Calling the function returns a generator iterator. Advancing it
    with ``next()`` returns a float in the configured jitter band.
    """
    iterator = reconnect_backoff()
    # Regular (sync) iterator — must have __next__, not __anext__.
    assert hasattr(iterator, "__next__")
    assert not hasattr(iterator, "__anext__")
    first = next(iterator)
    assert isinstance(first, float)
    # With jitter=0.3 the first yield is in [0.7, 1.3].
    assert 0.7 <= first <= 1.3


# --- agent.config -------------------------------------------------------


def test_agent_config_module_imports() -> None:
    assert inspect.isclass(AgentConfig)
    assert callable(load_config)


def test_agent_config_defaults() -> None:
    """Dataclass defaults match the constants in the source file."""
    cfg = AgentConfig(server_url="wss://x", bearer_token="t")
    assert cfg.hermes_ws_url == DEFAULT_HERMES_WS_URL == "ws://127.0.0.1:9119/api/ws"
    assert cfg.watched_ledger_dir == DEFAULT_WATCHED_LEDGER_DIR == "E:/hermes-agent"
    assert cfg.file_sandbox_root == DEFAULT_FILE_SANDBOX_ROOT == "E:/hermes-agent"


def test_agent_config_frozen() -> None:
    """Frozen dataclass: assignment is forbidden after construction."""
    cfg = AgentConfig(server_url="wss://x", bearer_token="t")
    with pytest.raises((AttributeError, Exception)):
        cfg.server_url = "wss://other"  # type: ignore[misc]


def test_agent_config_overrides() -> None:
    """All four fields can be overridden at construction time."""
    cfg = AgentConfig(
        server_url="wss://x",
        bearer_token="t",
        hermes_ws_url="ws://localhost:9999/api/ws",
        watched_ledger_dir="/tmp/ledgers",
        file_sandbox_root="/tmp/sandbox",
    )
    assert cfg.hermes_ws_url == "ws://localhost:9999/api/ws"
    assert cfg.watched_ledger_dir == "/tmp/ledgers"
    assert cfg.file_sandbox_root == "/tmp/sandbox"


def test_load_config_signature() -> None:
    """load_config is a regular function with one optional arg."""
    sig = inspect.signature(load_config)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "vault_path"
    assert params[0].default is None


def test_load_config_missing_vault_raises() -> None:
    """load_config raises FileNotFoundError when the vault does not exist.

    Stage-2 L1 implementation note: the body now does real I/O. The
    contract the skeleton was guarding is that calling load_config with
    no vault file produces a clear FileNotFoundError pointing to the
    setup script.
    """
    with pytest.raises(FileNotFoundError):
        load_config(vault_path="Z:/definitely/does/not/exist.json")
