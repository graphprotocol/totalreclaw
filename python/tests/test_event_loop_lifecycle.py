"""Regression tests for Bug #2 — "Event loop is closed" across sync agent hooks.

QA reference: `docs/notes/QA-V1CLEAN-VPS-20260418.md`. On a fresh VPS install
of ``totalreclaw==2.0.1``, every call to ``totalreclaw_status`` and
``totalreclaw_export`` — and every first-turn ``pre_llm_call`` auto-recall —
returned ``RuntimeError: Event loop is closed``.

Root cause
----------
``totalreclaw.agent.recall.auto_recall`` and
``totalreclaw.agent.lifecycle.auto_extract`` each run synchronously from
Hermes's sync hook callbacks. Historically they used::

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(client.recall(...))
    finally:
        loop.close()

``client.recall`` eventually calls ``RelayClient._get_http()``, which caches
an ``httpx.AsyncClient`` on the RelayClient instance. ``httpx.AsyncClient``
binds to whichever event loop was running when it was constructed. After
the *first* hook call finished and its loop was closed, the cached
``httpx.AsyncClient`` was still held by the RelayClient — and on the *next*
hook call we created a fresh loop but reused the stale client, which still
had its underlying socket / connection pool referencing the closed loop.
That raises ``RuntimeError: Event loop is closed`` from inside anyio/httpx
on the first request attempted from the new loop.

Fix
---
Instead of per-call loops, run all per-instance async work through a single
long-lived background thread that owns one persistent event loop. The
httpx client is created on (and only used from) that one loop, so it is
never orphaned by a short-lived per-call loop. See
``totalreclaw.agent.loop_runner``.

These tests pin the invariant:

* ``_SyncLoopRunner`` survives repeated ``.run`` calls from different
  calling threads without raising.
* A ``RelayClient`` driven via ``_SyncLoopRunner`` can issue two HTTP
  requests (mocked transport) in sequence without tripping
  ``Event loop is closed``.
* Calling ``auto_recall(...)`` twice in a row against the same RelayClient
  does not raise.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Low-level: the loop runner itself must survive re-entry
# ---------------------------------------------------------------------------


def test_sync_loop_runner_survives_sequential_calls() -> None:
    """``_SyncLoopRunner.run`` must accept sequential awaitables.

    Before the fix, callers used a fresh ``asyncio.new_event_loop()`` each
    time and closed it at the end. That worked for single-call work but
    leaked any cached ``httpx.AsyncClient`` across loops. The new runner
    owns ONE loop in a background thread and runs every coroutine on it.
    """
    from totalreclaw.agent.loop_runner import get_sync_loop_runner

    runner = get_sync_loop_runner()

    async def _trivial(i: int) -> int:
        return i * 2

    assert runner.run(_trivial(3)) == 6
    assert runner.run(_trivial(5)) == 10
    assert runner.run(_trivial(7)) == 14


def test_sync_loop_runner_is_process_wide_singleton() -> None:
    """Multiple calls return the same runner — one loop for the process."""
    from totalreclaw.agent.loop_runner import get_sync_loop_runner

    a = get_sync_loop_runner()
    b = get_sync_loop_runner()
    assert a is b


# ---------------------------------------------------------------------------
# Mid-level: RelayClient re-entry must not blow up across sync calls
# ---------------------------------------------------------------------------


def test_relay_client_survives_sequential_sync_calls() -> None:
    """Two sequential calls to a RelayClient method from sync code must work.

    This is the exact pattern that broke in v2.0.1: ``totalreclaw_status``
    was called twice in a row by a user, and the second call returned
    ``Event loop is closed``. We mock the httpx transport so we don't
    actually hit a relay.
    """
    from totalreclaw.agent.loop_runner import get_sync_loop_runner
    from totalreclaw.relay import RelayClient

    # Mock httpx.AsyncClient so each .post() returns a canned billing response.
    # Importantly, the mock honors the httpx_mock.Response contract so that
    # the code path exercises the same async execution flow as production.
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tier": "free",
                "free_writes_used": 3,
                "free_writes_limit": 500,
                "expires_at": None,
            },
        )

    transport = httpx.MockTransport(_handler)

    # Patch the RelayClient's _get_http so it returns a mock-transport client
    # on the long-lived loop.
    rc = RelayClient(
        relay_url="https://api-staging.totalreclaw.xyz",
        auth_key_hex="00" * 32,
        wallet_address="0x" + "ab" * 20,
    )

    runner = get_sync_loop_runner()

    # Replace the lazy async-client constructor with one bound to the runner's
    # loop (this is how the production fix wires things up).
    async def _mock_get_http() -> httpx.AsyncClient:
        if rc._http is None or rc._http.is_closed:
            rc._http = httpx.AsyncClient(transport=transport, timeout=10.0)
        return rc._http

    rc._get_http = _mock_get_http  # type: ignore[assignment]

    # Call twice — the regression is that the second call raises
    # "Event loop is closed".
    status1 = runner.run(rc.get_billing_status())
    status2 = runner.run(rc.get_billing_status())

    assert status1.tier == "free"
    assert status2.tier == "free"

    runner.run(rc.close())


# ---------------------------------------------------------------------------
# High-level: lifecycle functions must not create and tear down loops
# ---------------------------------------------------------------------------


def _make_state_with_fake_client():
    """Build a PluginState with a fake client. Mirrors _make_state from
    test_v1_hooks_integration.py but keeps the imports local so failure of
    the loop-runner module doesn't break test collection."""
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()

    fake_client = MagicMock()
    fake_client.remember = AsyncMock(return_value="fact-uuid-abc")
    fake_client.recall = AsyncMock(return_value=[])
    fake_client.forget = AsyncMock(return_value=True)
    state._client = fake_client
    return state, fake_client


def _build_real_client_with_mock_transport(transport: httpx.MockTransport):
    """Build a real TotalReclaw client whose RelayClient uses a mock transport.

    Using a real RelayClient + real httpx.AsyncClient (not an AsyncMock) is
    essential to reproduce the ``Event loop is closed`` bug: the bug only
    surfaces when the same httpx client is carried across two different
    event loops, which MagicMock does not simulate.
    """
    from totalreclaw.client import TotalReclaw

    test_mnemonic = (
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )
    client = TotalReclaw(
        recovery_phrase=test_mnemonic,
        wallet_address="0x2c0cf74b2b76110708ca431796367779e3738250",
    )
    client._registered = True

    # Monkeypatch _get_http so it returns a real httpx.AsyncClient with the
    # mock transport. The RelayClient still owns the reference, so if the
    # code creates a loop, tears it down, and then reuses the cached
    # AsyncClient from a new loop, we will hit "Event loop is closed".
    rc = client._relay
    original_get_http = rc._get_http

    async def _patched() -> httpx.AsyncClient:
        if rc._http is None or rc._http.is_closed:
            rc._http = httpx.AsyncClient(transport=transport, timeout=10.0)
        return rc._http

    rc._get_http = _patched  # type: ignore[assignment]
    return client


def test_sync_auto_recall_does_not_hit_event_loop_is_closed() -> None:
    """Reproduces the QA bug: two sync calls to auto_recall against the
    same TotalReclaw instance used to raise ``Event loop is closed`` on the
    second call. Must not raise any more.
    """
    from totalreclaw.agent.recall import auto_recall
    from totalreclaw.hermes.state import PluginState

    # Minimal empty-search response: no facts found.
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"blindIndexes": [], "facts": []}},
        )

    transport = httpx.MockTransport(_handler)
    client = _build_real_client_with_mock_transport(transport)

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    state._client = client

    out1 = auto_recall("hello", state)
    out2 = auto_recall("world", state)
    assert out1 is None
    assert out2 is None


def test_sync_client_status_does_not_hit_event_loop_is_closed() -> None:
    """Reproduces the QA bug: calling client.status() from two different
    short-lived loops used to raise ``Event loop is closed`` on the second.

    This is the ``totalreclaw_status`` path — the single most visible break
    for users in the v2.0.1 QA because it's the first sanity tool a user
    reaches for.
    """
    from totalreclaw.agent.loop_runner import get_sync_loop_runner

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tier": "free",
                "free_writes_used": 2,
                "free_writes_limit": 500,
            },
        )

    transport = httpx.MockTransport(_handler)
    client = _build_real_client_with_mock_transport(transport)

    runner = get_sync_loop_runner()
    s1 = runner.run(client.status())
    s2 = runner.run(client.status())
    assert s1.tier == "free"
    assert s2.tier == "free"


# ---------------------------------------------------------------------------
# Cross-loop RelayClient regression
# ---------------------------------------------------------------------------


def test_relay_client_per_loop_caching() -> None:
    """``RelayClient._get_http`` must return different clients on different loops.

    This pins the core invariant behind the fix for Bug #2 and Bug #3:
    each event loop gets its own ``httpx.AsyncClient`` so nothing is ever
    orphaned across loop boundaries. Before this fix, a single cached
    client on ``RelayClient._http`` was shared across loops and blew up
    the moment a different loop tried to use it.
    """
    import asyncio
    from totalreclaw.relay import RelayClient

    rc = RelayClient(
        relay_url="https://api-staging.totalreclaw.xyz",
        auth_key_hex="00" * 32,
        wallet_address="0x" + "ab" * 20,
    )

    async def _grab_client():
        return await rc._get_http()

    # Loop A.
    loop_a = asyncio.new_event_loop()
    try:
        client_a = loop_a.run_until_complete(_grab_client())
    finally:
        # Drop the reference to the client bound to loop_a and don't close
        # loop_a yet, so the test mirrors reality: the loop stays alive
        # until something closes it, and we never touch client_a again
        # from loop_b.
        loop_a.close()

    # Loop B. This MUST get a fresh client, not the orphaned one from A.
    loop_b = asyncio.new_event_loop()
    try:
        client_b = loop_b.run_until_complete(_grab_client())
    finally:
        loop_b.run_until_complete(rc.close())
        loop_b.close()

    assert client_a is not client_b, (
        "RelayClient._get_http returned the same client across two distinct "
        "event loops — this is the Bug #2/#3 regression. Each loop must get "
        "its own httpx.AsyncClient."
    )


def test_hermes_sync_hook_then_async_tool_does_not_orphan_httpx() -> None:
    """Full QA scenario: sync hook runs first, async tool runs after.

    In v2.0.1, a single httpx client got cached on the RelayClient during
    the sync hook's short-lived loop, then Hermes reused it on its own
    async loop when invoking the async ``totalreclaw_export`` tool —
    which failed with ``RuntimeError: Event loop is closed``.

    With the per-loop cache this no longer happens. We prove it by:

    1. Running ``auto_recall`` through the sync loop runner (hook path).
    2. Running ``client.export_all`` / ``client.status`` through an
       independent asyncio.run call (tool path).

    Both must succeed without "Event loop is closed".
    """
    import asyncio
    from totalreclaw.agent.recall import auto_recall
    from totalreclaw.hermes.state import PluginState

    def _handler(request: httpx.Request) -> httpx.Response:
        # Handle both subgraph queries (GraphQL POST) and billing (GET).
        if request.url.path.endswith("/v1/billing/status"):
            return httpx.Response(
                200,
                json={
                    "tier": "free",
                    "free_writes_used": 3,
                    "free_writes_limit": 500,
                },
            )
        if request.url.path.endswith("/v1/register"):
            return httpx.Response(200, json={"user_id": "test-user"})
        # Default: subgraph query, no facts.
        return httpx.Response(
            200,
            json={"data": {"blindIndexes": [], "facts": []}},
        )

    transport = httpx.MockTransport(_handler)
    client = _build_real_client_with_mock_transport(transport)

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    state._client = client

    # Step 1: sync hook path (auto_recall) runs on the loop runner.
    out = auto_recall("what do i prefer?", state)
    assert out is None  # No results, no error.

    # Step 2: async tool path (status + export) runs on a fresh
    # asyncio.run call — exactly what Hermes does when it invokes an
    # ``is_async=True`` tool handler.
    async def _invoke_tools():
        s = await client.status()
        facts = await client.export_all()
        return s, facts

    s, facts = asyncio.run(_invoke_tools())
    assert s.tier == "free"
    assert facts == []  # 0 on-chain in our mock, not an error state.


def test_export_returns_decrypted_facts_after_sync_hook_ran() -> None:
    """QA Bug #3: export returned count=0 despite 7 facts on-chain.

    Root cause was the same cross-loop httpx caching that caused Bug #2 —
    the subgraph GraphQL query in ``export_facts`` hit "Event loop is
    closed" and the outer ``except Exception: break`` silently returned
    an empty list.

    This test simulates the full scenario:

    1. Sync hook (``auto_recall``) runs on the sync loop runner — primes
       any cached httpx client.
    2. Hermes invokes ``totalreclaw_export`` on its own async loop, which
       triggers a subgraph ``EXPORT_QUERY`` returning 2 mock-encrypted
       facts (mocked so we don't need real crypto).
    3. Decryption is mocked so we only exercise the transport + pagination
       path. The result MUST contain 2 entries.
    """
    import asyncio
    from totalreclaw.agent.recall import auto_recall
    from totalreclaw.hermes.state import PluginState

    # Route by GraphQL query shape so the auto_recall "prime" doesn't
    # exhaust the export facts. The auto_recall path sends
    # ``SearchByBlindIndex`` and ``BroadenedSearch`` queries; the export
    # path sends ``ExportFacts``. Only the last returns real fact rows
    # here.
    import json as _json

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/register"):
            return httpx.Response(200, json={"user_id": "test-user"})
        if request.url.path.endswith("/v1/subgraph"):
            payload = _json.loads(request.content.decode("utf-8"))
            query = payload.get("query", "")
            if "ExportFacts" in query:
                skip = payload.get("variables", {}).get("skip", 0)
                if skip == 0:
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "facts": [
                                    {
                                        "id": "fact-1",
                                        "encryptedBlob": "0x" + "ab" * 32,
                                        "encryptedEmbedding": None,
                                        "decayScore": "0.8",
                                        "timestamp": "1713456789",
                                        "createdAt": "1713456800",
                                        "isActive": True,
                                        "contentFp": "fp1",
                                    },
                                    {
                                        "id": "fact-2",
                                        "encryptedBlob": "0x" + "cd" * 32,
                                        "encryptedEmbedding": None,
                                        "decayScore": "0.6",
                                        "timestamp": "1713456790",
                                        "createdAt": "1713456801",
                                        "isActive": True,
                                        "contentFp": "fp2",
                                    },
                                ]
                            }
                        },
                    )
                return httpx.Response(200, json={"data": {"facts": []}})
            # Any other subgraph query (search, broadened search, by id):
            # return an empty shell so auto_recall completes cleanly.
            if "BroadenedSearch" in query:
                return httpx.Response(200, json={"data": {"facts": []}})
            return httpx.Response(200, json={"data": {"blindIndexes": []}})
        return httpx.Response(200, json={"data": {"facts": []}})

    transport = httpx.MockTransport(_handler)
    client = _build_real_client_with_mock_transport(transport)

    # Prime the RelayClient via the sync hook path.
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    state._client = client

    # Fake decrypt + blob parser so we don't need real crypto.
    with patch(
        "totalreclaw.operations.decrypt",
        side_effect=lambda b, k: '{"text":"mocked","type":"claim","schema_version":"1.0","importance":8,"source":"user","scope":"unspecified","confidence":0.9}',
    ), patch(
        "totalreclaw.operations.is_digest_blob",
        return_value=False,
    ):
        # Step 1: sync hook (sync loop runner).
        auto_recall("prime", state)

        # Step 2: async tool path (Hermes-style) — MUST get 2 results.
        facts = asyncio.run(client.export_all())

    assert len(facts) == 2, (
        f"export_all returned {len(facts)} facts; expected 2. Bug #3 regression — "
        "the cross-loop httpx issue is silently returning empty."
    )
    assert {f["id"] for f in facts} == {"fact-1", "fact-2"}
    # Timestamps should be formatted strings, not raw.
    assert all(isinstance(f["timestamp"], str) for f in facts)
