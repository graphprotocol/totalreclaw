"""Write-quota refusals reach the user and the agent, and facts are kept (#747).

Before this fix a ``/v1/bundler`` 403 ``quota_exceeded`` was an anonymous
``httpx.HTTPStatusError``. ``remember_batch`` turned it into a
``RuntimeError`` whose text carries no "quota" and no ``.response``, so
lifecycle's ``_is_write_quota_error`` never matched. Auto-extraction logged
"Fact not stored (batch failure)" and marked the messages processed: the
facts were gone and nothing was injected into the conversation.
``totalreclaw_remember`` returned the raw httpx string.

Relay contract (totalreclaw-relay ``src/routes/proxy.ts``): a write over the
monthly cap gets ``403 {"error": "quota_exceeded", "message", "upgrade_url"}``.
A batch is refused whole when it does not fit (``used + count > cap``), so an
earlier group of the same ``remember_batch`` can already have landed.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from totalreclaw.agent.extraction import ExtractedFact
from totalreclaw.relay import BillingStatus, RelayClient, RelayReadError

_BUNDLER_URL = "https://relay.invalid/v1/bundler"
_QUOTA_BODY = {
    "error": "quota_exceeded",
    "message": "Write limit reached for this month. Upgrade for higher limits.",
    "upgrade_url": "https://totalreclaw.xyz/pricing",
}


def _resp(status: int, body=None, *, text: Optional[str] = None) -> httpx.Response:
    req = httpx.Request("POST", _BUNDLER_URL)
    if text is not None:
        return httpx.Response(status, text=text, request=req)
    return httpx.Response(status, json=body, request=req)


def _quota_error(batch_results=None):
    """A real ``RelayWriteQuotaExceeded``, built by the real classifier."""
    from totalreclaw.relay import raise_for_bundler_status

    try:
        raise_for_bundler_status(_resp(403, _QUOTA_BODY))
    except Exception as e:  # noqa: BLE001
        err = e
    else:  # pragma: no cover
        raise AssertionError("403 quota_exceeded did not raise")
    if batch_results is not None:
        err.batch_results = batch_results
    return err


def _relay_with_handler(handler) -> RelayClient:
    transport = httpx.MockTransport(handler)
    rc = RelayClient(relay_url="https://relay.invalid")

    async def _mock_get_http() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=10.0)

    rc._get_http = _mock_get_http  # type: ignore[assignment]
    return rc


# ---------------------------------------------------------------------------
# 1. The bundler 403 body is parsed into a typed write error
# ---------------------------------------------------------------------------


class TestBundlerClassification:
    @pytest.mark.asyncio
    async def test_submit_userop_quota_403_raises_typed_write_error(self):
        from totalreclaw.relay import RelayWriteQuotaExceeded

        rc = _relay_with_handler(lambda req: httpx.Response(403, json=_QUOTA_BODY))
        with pytest.raises(RelayWriteQuotaExceeded) as exc_info:
            await rc.submit_userop({"jsonrpc": "2.0", "method": "eth_sendUserOperation"})
        err = exc_info.value
        # Still an HTTPStatusError, so existing handlers keep working.
        assert isinstance(err, httpx.HTTPStatusError)
        assert err.response.status_code == 403
        assert err.upgrade_url == "https://totalreclaw.xyz/pricing"
        assert err.relay_message.startswith("Write limit reached")
        # Readable, and string matchers that look for "quota" still match.
        assert "quota" in str(err).lower()
        # A write refusal is not a read denial and never pauses reads.
        assert not isinstance(err, RelayReadError)
        assert rc.read_block() is None

    @pytest.mark.asyncio
    async def test_userop_relay_rpc_quota_403_raises_typed_write_error(self):
        from totalreclaw.relay import RelayWriteQuotaExceeded
        from totalreclaw.userop import _relay_rpc

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(403, json=_QUOTA_BODY))
        )
        with pytest.raises(RelayWriteQuotaExceeded):
            await _relay_rpc(http, "https://relay.invalid", {}, "eth_sendUserOperation", [])

    @pytest.mark.parametrize(
        "resp",
        [
            _resp(403, {"error": "call_target_not_allowed", "message": "nope"}),
            _resp(403, text="<html>Forbidden</html>"),
            _resp(500, {"error": "quota_exceeded"}),
            _resp(403, {"error": "read_quota_exceeded"}),
        ],
        ids=["other-403-code", "non-json-403", "quota-code-on-500", "read-code"],
    )
    def test_other_failures_stay_plain_http_errors(self, resp):
        from totalreclaw.relay import RelayWriteQuotaExceeded, raise_for_bundler_status

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            raise_for_bundler_status(resp)
        assert not isinstance(exc_info.value, RelayWriteQuotaExceeded)

    @pytest.mark.asyncio
    async def test_full_batch_userop_flow_surfaces_typed_error_without_retry(self):
        """Nonce, gas price and sponsorship succeed; the relay refuses
        ``eth_sendUserOperation``. The typed error must come out of the
        userop retry loop untouched, after exactly one send."""
        from eth_account import Account

        from totalreclaw.relay import RelayWriteQuotaExceeded
        from totalreclaw.userop import (
            _reset_deployed_cache_for_tests,
            _reset_nonce_cache_for_tests,
            _reset_sender_locks_for_tests,
            build_and_send_userop_batch,
        )

        _reset_nonce_cache_for_tests()
        _reset_deployed_cache_for_tests()
        _reset_sender_locks_for_tests()
        Account.enable_unaudited_hdwallet_features()
        key = bytes(Account.from_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon about",
            account_path="m/44'/60'/0'/0/0",
        ).key)

        sends: list[int] = []

        def rpc(body, result):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id", 1), "result": result},
                                  request=httpx.Request("POST", "https://mock/v1/bundler"))

        def dispatch(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content or b"{}")
            method = body.get("method", "")
            if method == "eth_call":
                return rpc(body, "0x" + "0" * 64)
            if method == "eth_getCode":
                return rpc(body, "0x01")
            if method == "pimlico_getUserOperationGasPrice":
                return rpc(body, {"fast": {"maxFeePerGas": "0x77359400", "maxPriorityFeePerGas": "0x59682f00"}})
            if method == "pm_sponsorUserOperation":
                return rpc(body, {
                    "callGasLimit": "0x186a0", "verificationGasLimit": "0x30d40",
                    "preVerificationGas": "0xc350",
                    "paymaster": "0x0000000000000000000000000000000000000000",
                    "paymasterData": "0x", "paymasterVerificationGasLimit": "0x30d40",
                    "paymasterPostOpGasLimit": "0x30d40",
                })
            if method == "eth_sendUserOperation":
                sends.append(1)
                return httpx.Response(403, json=_QUOTA_BODY, request=request)
            pytest.fail(f"unexpected method {method}")

        transport = httpx.MockTransport(dispatch)
        original_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original_init(self, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "__init__", patched_init):
            with pytest.raises(RelayWriteQuotaExceeded):
                await build_and_send_userop_batch(
                    sender="0x2c0cf74b2b76110708ca431796367779e3738250",
                    eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
                    eoa_private_key=key,
                    protobuf_payloads=[b"fact1", b"fact2"],
                    relay_url="https://mock",
                    auth_key_hex="deadbeef",
                    wallet_address="0x2c0cf74b2b76110708ca431796367779e3738250",
                    chain_id=100,
                )
        assert len(sends) == 1

    def test_success_does_not_raise(self):
        from totalreclaw.relay import raise_for_bundler_status

        raise_for_bundler_status(_resp(200, {"jsonrpc": "2.0", "result": "0xabc"}))

    @pytest.mark.asyncio
    async def test_billing_status_parses_resets_at(self):
        body = {
            "tier": "free",
            "free_writes_used": 250,
            "free_writes_limit": 250,
            "resets_at": "2026-12-01T00:00:00.000Z",
        }
        rc = _relay_with_handler(lambda req: httpx.Response(200, json=body))
        status = await rc.get_billing_status()
        assert status.resets_at == "2026-12-01T00:00:00.000Z"


# ---------------------------------------------------------------------------
# 2. remember_batch keeps the type and reports which facts landed
# ---------------------------------------------------------------------------


def _shell_client():
    from totalreclaw.client import TotalReclaw

    tr = TotalReclaw.__new__(TotalReclaw)
    tr._ensure_address = AsyncMock()
    tr._ensure_registered = AsyncMock()
    tr._ensure_chain_id = AsyncMock(return_value=100)
    tr._wallet_context = MagicMock(return_value=MagicMock(name="wallet"))
    tr._get_lsh_hasher = MagicMock(return_value=None)
    tr._relay = MagicMock(name="relay")
    tr._data_edge_address = None
    return tr


@pytest.mark.asyncio
@patch("totalreclaw.client.store_fact_batch", new_callable=AsyncMock)
async def test_remember_batch_quota_mid_batch_reports_per_fact_results(mock_store):
    """20 facts = groups of 15 + 5. The first lands, the relay refuses the
    second: the caller gets the typed error and the 15 ids, in input order."""
    from totalreclaw.relay import RelayWriteQuotaExceeded

    first_ids = [f"id-{i}" for i in range(15)]
    mock_store.side_effect = [first_ids, _quota_error()]
    client = _shell_client()
    facts = [{"text": f"short fact number {i}"} for i in range(20)]

    with pytest.raises(RelayWriteQuotaExceeded) as exc_info:
        await client.remember_batch(facts, source="hermes-auto")

    assert exc_info.value.batch_results == first_ids + [None] * 5


@pytest.mark.asyncio
@patch("totalreclaw.client.store_fact_batch", new_callable=AsyncMock)
async def test_remember_batch_non_quota_failure_still_runtime_error(mock_store):
    from totalreclaw.relay import RelayWriteQuotaExceeded

    mock_store.side_effect = RuntimeError("Paymaster sponsorship failed: boom")
    client = _shell_client()
    with pytest.raises(RuntimeError) as exc_info:
        await client.remember_batch([{"text": "one fact"}], source="hermes-auto")
    assert not isinstance(exc_info.value, RelayWriteQuotaExceeded)


# ---------------------------------------------------------------------------
# 3. The retry queue: encrypted on disk, bounded, ordered
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    return tmp_path


def _queue(owner="0x000000000000000000000000000000000000cafe", key=None):
    from totalreclaw.agent.pending_store import PendingStoreQueue

    return PendingStoreQueue.for_owner(owner, key or secrets.token_bytes(32))


class TestPendingStoreQueue:
    def test_round_trip_is_encrypted_at_rest(self, state_dir):
        q = _queue()
        kept = q.add(
            [{"text": "User flies direct only", "embedding": [0.1] * 4, "fact_type": "preference"}],
            reason="write_quota",
        )
        assert kept == 1
        assert q.count() == 1
        raw = q.path.read_bytes()
        assert b"direct only" not in raw
        assert (q.path.stat().st_mode & 0o777) == 0o600

        taken = q.take(10)
        assert [e["fact"]["text"] for e in taken] == ["User flies direct only"]
        # The embedding is not stored; the drain recomputes it.
        assert "embedding" not in taken[0]["fact"]
        assert taken[0]["fact"]["fact_type"] == "preference"
        assert taken[0]["reason"] == "write_quota"
        assert q.count() == 0

    def test_other_key_cannot_read_it(self, state_dir):
        owner = "0x000000000000000000000000000000000000cafe"
        _queue(owner).add([{"text": "secret plan"}], reason="write_quota")
        assert _queue(owner).count() == 0

    def test_put_back_goes_to_the_front(self, state_dir):
        q = _queue()
        q.add([{"text": "a"}, {"text": "b"}, {"text": "c"}], reason="write_quota")
        first = q.take(2)
        q.put_back(first)
        assert [e["fact"]["text"] for e in q.take(10)] == ["a", "b", "c"]

    def test_bounded_drops_oldest(self, state_dir, monkeypatch):
        from totalreclaw.agent.pending_store import PendingStoreQueue

        monkeypatch.setattr(PendingStoreQueue, "MAX_ENTRIES", 3)
        q = _queue()
        q.add([{"text": t} for t in ("a", "b", "c", "d", "e")], reason="write_quota")
        assert [e["fact"]["text"] for e in q.take(10)] == ["c", "d", "e"]

    def test_concurrent_adds_do_not_lose_entries(self, state_dir):
        """Auto-extraction can run on the sync-loop thread and the host
        thread at once; the read-modify-write must not drop entries."""
        import threading

        owner = "0x000000000000000000000000000000000000cafe"
        key = secrets.token_bytes(32)

        def worker(n):
            q = _queue(owner, key)
            for i in range(25):
                q.add([{"text": f"thread {n} fact {i}"}], reason="write_quota")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _queue(owner, key).count() == 100

    def test_entities_dataclasses_are_serialised(self, state_dir):
        from totalreclaw.agent.extraction import ExtractedEntity

        q = _queue()
        q.add(
            [{"text": "User works at Acme", "entities": [ExtractedEntity(name="Acme", type="company")]}],
            reason="write_quota",
        )
        ent = q.take(1)[0]["fact"]["entities"][0]
        assert ent["name"] == "Acme"


# ---------------------------------------------------------------------------
# 4. Auto-extraction: keep, tell, retry
# ---------------------------------------------------------------------------


class QuotaClient:
    """Fake vault. ``remaining`` models the relay's monthly allowance: a batch
    that does not fit is refused whole with the typed error, like the relay."""

    resolved_chain_id = 100
    resolved_wallet_address = "0x000000000000000000000000000000000000beef"
    eoa_address = "0x000000000000000000000000000000000000cafe"

    def __init__(self, remaining: int, *, tier: str = "free", recent=None):
        self.remaining = remaining
        self.tier = tier
        self.batches: list[list[str]] = []
        self.stored: list[str] = []
        self.status_calls = 0
        self._recent = list(recent or [])
        self._n = 0
        self._queue = _queue(self.eoa_address)

    @property
    def pending_store(self):
        return self._queue

    async def remember_batch(self, facts, source="python-client"):
        texts = [f["text"] for f in facts]
        self.batches.append(texts)
        if len(facts) > self.remaining:
            raise _quota_error(batch_results=[None] * len(facts))
        self.remaining -= len(facts)
        ids = []
        for t in texts:
            self._n += 1
            ids.append(f"fact-{self._n}")
            self.stored.append(t)
        return ids

    async def status(self):
        self.status_calls += 1
        return BillingStatus(
            tier=self.tier,
            free_writes_used=250,
            free_writes_limit=250,
            resets_at="2026-12-01T00:00:00.000Z",
        )

    async def recall(self, *a, **kw):
        return []

    async def fetch_recent_facts(self, limit=10):
        return list(self._recent)

    async def forget(self, *a, **kw):
        return {"success": True}


def _state(client):
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {"TOTALRECLAW_STATE_DIR": os.environ["TOTALRECLAW_STATE_DIR"]}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    state._client = client
    return state


def _facts(*texts, action="ADD"):
    return [
        ExtractedFact(
            text=t, type="claim", importance=7, action=action,
            confidence=0.9, source="user", scope="unspecified",
        )
        for t in texts
    ]


def _run_extract(state, facts):
    from totalreclaw.agent.lifecycle import auto_extract

    async def fake_extract(*a, **kw):
        return facts

    async def passthrough(fs, *a, **kw):
        return fs

    state.add_message("user", "some conversation")
    state.add_message("assistant", "ok")
    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
         patch("totalreclaw.agent.lifecycle._fetch_recent_memories", return_value=[]), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):
        return auto_extract(state)


class TestAutoExtractQuota:
    def test_refused_facts_are_queued_and_the_user_is_told(self, state_dir):
        client = QuotaClient(remaining=0)
        state = _state(client)
        state.set_billing_cache({"tier": "free", "free_writes_used": 249, "free_writes_limit": 250})

        stored = _run_extract(state, _facts("User flies direct only", "User likes Sölvesborg"))

        assert stored == []
        assert client.pending_store.count() == 2
        notice = state.get_quota_warning() or ""
        assert "not being saved" in notice.lower()
        assert "250" in notice
        assert "2026-12-01" in notice
        assert "totalreclaw_upgrade" in notice
        assert "do not say" in notice.lower()
        # Stale usage numbers are dropped so the next session re-fetches.
        assert state.get_cached_billing() is None
        # Messages are consumed: the facts live in the queue now.
        assert state.get_unprocessed_messages() == []

    def test_pro_tier_notice_does_not_offer_upgrade(self, state_dir):
        client = QuotaClient(remaining=0, tier="pro")
        state = _state(client)
        _run_extract(state, _facts("User flies direct only"))
        notice = state.get_quota_warning() or ""
        assert "not being saved" in notice.lower()
        assert "totalreclaw_upgrade" not in notice

    def test_announced_once_per_session(self, state_dir):
        client = QuotaClient(remaining=0)
        state = _state(client)
        _run_extract(state, _facts("fact one"))
        assert state.get_quota_warning()
        state.clear_quota_warning()  # pre_llm_call injected it

        _run_extract(state, _facts("fact two"))
        assert state.get_quota_warning() is None
        assert client.pending_store.count() == 2
        assert client.status_calls == 1

    def test_after_a_refusal_later_chunks_are_not_sent(self, state_dir):
        client = QuotaClient(remaining=0)
        state = _state(client)
        state._max_facts = 30  # server-tuned cap above one chunk (15)
        _run_extract(state, _facts(*[f"distinct fact number {i} about topic {i}" for i in range(20)]))
        assert len(client.batches) == 1
        assert client.pending_store.count() == 20

    def test_partial_batch_keeps_stored_facts(self, state_dir):
        client = QuotaClient(remaining=0)

        async def partial(facts, source="python-client"):
            client.batches.append([f["text"] for f in facts])
            raise _quota_error(batch_results=["fact-1"] + [None] * (len(facts) - 1))

        client.remember_batch = partial
        state = _state(client)
        stored = _run_extract(state, _facts("landed fact", "refused fact"))
        assert stored == ["landed fact"]
        assert [e["fact"]["text"] for e in client.pending_store.take(10)] == ["refused fact"]

    def test_existing_warning_is_not_clobbered(self, state_dir):
        client = QuotaClient(remaining=0)
        state = _state(client)
        state.set_quota_warning("TotalReclaw: automatic memory extraction is DISABLED")
        _run_extract(state, _facts("fact one"))
        notice = state.get_quota_warning() or ""
        assert "not being saved" in notice.lower()
        assert "DISABLED" in notice


class TestDrain:
    def test_successful_write_drains_the_queue(self, state_dir):
        client = QuotaClient(remaining=100)
        client.pending_store.add(
            [{"text": "User flies direct only"}, {"text": "User likes Sölvesborg"}],
            reason="write_quota",
        )
        state = _state(client)

        stored = _run_extract(state, _facts("brand new fact"))

        assert stored == ["brand new fact"]
        assert client.batches[0] == ["brand new fact"]
        assert sorted(client.batches[1]) == ["User flies direct only", "User likes Sölvesborg"]
        assert client.pending_store.count() == 0

    def test_recovery_is_announced_after_an_outage(self, state_dir):
        client = QuotaClient(remaining=0)
        state = _state(client)
        _run_extract(state, _facts("queued fact"))
        state.clear_quota_warning()

        client.remaining = 100  # user upgraded
        _run_extract(state, _facts("brand new fact"))

        assert "queued fact" in client.stored
        notice = state.get_quota_warning() or ""
        assert "works again" in notice.lower()
        assert "1" in notice

    def test_drain_skips_what_is_already_in_the_vault(self, state_dir):
        from totalreclaw.agent.lifecycle import drain_pending_stores

        client = QuotaClient(
            remaining=100,
            recent=[{"id": "x1", "text": "User flies direct only", "embedding": None, "pinned": False}],
        )
        client.pending_store.add(
            [{"text": "User flies direct only."}, {"text": "User likes Sölvesborg"}],
            reason="write_quota",
        )
        state = _state(client)

        with patch("totalreclaw.embedding.get_embedding", return_value=None):
            n = drain_pending_stores(state)

        assert n == 1
        assert client.stored == ["User likes Sölvesborg"]
        assert client.pending_store.count() == 0

    def test_drain_skips_forget_notes(self, state_dir):
        from totalreclaw.agent.lifecycle import drain_pending_stores

        client = QuotaClient(remaining=100)
        client.pending_store.add(
            [{"text": "User asked to forget their home address"}, {"text": "User likes tea"}],
            reason="write_quota",
        )
        state = _state(client)
        with patch("totalreclaw.embedding.get_embedding", return_value=None):
            drain_pending_stores(state)
        assert client.stored == ["User likes tea"]

    def test_drain_still_refused_puts_everything_back(self, state_dir):
        from totalreclaw.agent.lifecycle import drain_pending_stores

        client = QuotaClient(remaining=0)
        client.pending_store.add([{"text": "a fact"}, {"text": "another fact"}], reason="write_quota")
        state = _state(client)
        with patch("totalreclaw.embedding.get_embedding", return_value=None):
            n = drain_pending_stores(state)
        assert n == 0
        assert [e["fact"]["text"] for e in client.pending_store.take(10)] == ["a fact", "another fact"]

    def test_drain_is_bounded(self, state_dir):
        from totalreclaw.agent.lifecycle import drain_pending_stores

        client = QuotaClient(remaining=1000)
        client.pending_store.add(
            [{"text": f"queued fact number {i} on subject {i}"} for i in range(40)],
            reason="write_quota",
        )
        state = _state(client)
        with patch("totalreclaw.embedding.get_embedding", return_value=None):
            n = drain_pending_stores(state, max_facts=15)
        assert n == 15
        assert client.pending_store.count() == 25


# ---------------------------------------------------------------------------
# 5. totalreclaw_remember returns a structured error
# ---------------------------------------------------------------------------


def test_remember_tool_returns_structured_quota_error(state_dir):
    from totalreclaw.agent.loop_runner import run_sync
    from totalreclaw.hermes import tools

    client = QuotaClient(remaining=0)

    async def refuse(text, **kw):
        raise _quota_error()

    client.remember = refuse
    state = _state(client)

    with patch("totalreclaw.embedding.get_embedding", return_value=None):
        out = json.loads(run_sync(tools.remember({"text": "User flies direct only"}, state)))

    assert out["stored"] is False
    assert out["error_code"] == "quota_exceeded"
    assert out["tier"] == "free"
    assert out["used"] == 250 and out["limit"] == 250
    assert out["resets_at"].startswith("2026-12-01")
    assert out["upgrade_url"] == "https://totalreclaw.xyz/pricing"
    assert "403" not in out["error"]
    assert "totalreclaw_upgrade" in out["instruction"]
    # The agent sees this result now; don't repeat it next turn.
    assert state.get_quota_warning() is None


def test_mirror_write_refusal_is_announced(state_dir):
    """The built-in memory mirror runs in the background: nobody sees its
    tool result, so a quota refusal there must reach the next turn."""
    from totalreclaw.hermes.memory_provider import _mirror_remember

    client = QuotaClient(remaining=0)

    async def refuse(text, **kw):
        raise _quota_error()

    client.remember = refuse
    state = _state(client)
    with patch("totalreclaw.embedding.get_embedding", return_value=None):
        assert _mirror_remember(state, {"text": "User flies direct only"}) is False
    assert "not being saved" in (state.get_quota_warning() or "").lower()


# ---------------------------------------------------------------------------
# 6. Session boundaries
# ---------------------------------------------------------------------------


def test_new_session_resets_the_once_per_session_latch(state_dir):
    client = QuotaClient(remaining=0)
    state = _state(client)
    _run_extract(state, _facts("fact one"))
    state.clear_quota_warning()

    state.reset_write_quota_announcement()  # what on_session_start does
    _run_extract(state, _facts("fact two"))
    assert "not being saved" in (state.get_quota_warning() or "").lower()


def test_on_session_start_resets_latch_and_drains_with_headroom(state_dir):
    from totalreclaw.hermes import hooks

    client = QuotaClient(remaining=100)
    client.pending_store.add([{"text": "User flies direct only"}], reason="write_quota")
    state = _state(client)
    state.announce_write_quota("[totalreclaw] stale notice")
    state.clear_quota_warning()
    state.set_billing_cache({"tier": "free", "free_writes_used": 3, "free_writes_limit": 250, "features": {}})

    with patch.object(hooks, "_eager_account_register"), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):
        hooks.on_session_start(state)

    assert state.write_quota_announced is False
    assert client.stored == ["User flies direct only"]


def test_on_session_start_does_not_drain_when_still_exhausted(state_dir):
    from totalreclaw.hermes import hooks

    client = QuotaClient(remaining=100)
    client.pending_store.add([{"text": "User flies direct only"}], reason="write_quota")
    state = _state(client)
    state.set_billing_cache({"tier": "free", "free_writes_used": 250, "free_writes_limit": 250, "features": {}})

    with patch.object(hooks, "_eager_account_register"), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):
        hooks.on_session_start(state)

    assert client.batches == []
    assert client.pending_store.count() == 1
