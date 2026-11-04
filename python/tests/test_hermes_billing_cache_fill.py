"""Hermes fills its billing cache, so server-tuned settings apply.

Before this fix ``AgentState.set_billing_cache`` had no production caller:
``on_session_start`` read ``state.get_cached_billing()``, which was always
``None``, so ``update_from_billing`` never ran. The relay's
``recall_top_k`` / ``extraction_interval`` / ``max_facts_per_extraction`` /
``max_candidate_pool``, the >80% quota warning and the update notice were
all dead.

Now the eager account register (first session of the process) caches the
``client.status()`` response it already fetches, and once that copy has
expired ``on_session_start`` makes one refresh bounded by
``_BILLING_REFRESH_TIMEOUT_S``. A slow or failing relay never breaks a
session: settings stay at their current values (defaults on a fresh
process).

These tests drive the real ``on_session_start`` with a stubbed client.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from totalreclaw.relay import BillingFeatures, BillingStatus

# BIP-39 test vector (public, all-"abandon"); never a real account.
_TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)

_SERVER_FEATURES = dict(
    recall_top_k=12,
    extraction_interval=5,
    max_facts_per_extraction=20,
    max_candidate_pool=2000,
)


def _status(used: int = 0, limit: int = 250, tier: str = "free", **features) -> BillingStatus:
    feats = dict(_SERVER_FEATURES)
    feats.update(features)
    return BillingStatus(
        tier=tier,
        free_writes_used=used,
        free_writes_limit=limit,
        features=BillingFeatures(**feats),
    )


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setenv("HOME", str(tmp))
    monkeypatch.setattr(Path, "home", lambda: tmp)
    for var in (
        "TOTALRECLAW_RECOVERY_PHRASE",
        "TOTALRECLAW_RECALL_TOP_K",
        "TOTALRECLAW_EXTRACT_INTERVAL",
        "TOTALRECLAW_MAX_FACTS_PER_EXTRACTION",
        "CANDIDATE_POOL_MAX_FREE",
        "CANDIDATE_POOL_MAX_PRO",
    ):
        monkeypatch.delenv(var, raising=False)
    # Keep the update-notice nudge out of the quota assertions.
    monkeypatch.setenv("TOTALRECLAW_DISABLE_UPDATE_NOTICE", "1")
    creds = tmp / ".totalreclaw"
    creds.mkdir(parents=True, mode=0o700)
    (creds / "credentials.json").write_text(json.dumps({"mnemonic": _TEST_MNEMONIC}))
    return tmp


def _make_client_factory(status_impl):
    """Return a ``TotalReclaw`` stand-in whose ``status()`` runs *status_impl*.

    ``calls`` records each status() call so tests can count relay requests.
    """
    calls: list[int] = []

    def factory(*_, **__):
        c = MagicMock()
        c._eoa_address = "0xeoatest"
        c._sa_address = None
        c.smart_account_address = None
        c.wallet_address = "0xeoatest"

        async def _status():
            calls.append(1)
            return await status_impl()

        c.status = _status
        return c

    return factory, calls


def _returns(value):
    async def impl():
        return value

    return impl


def _raises(exc):
    async def impl():
        raise exc

    return impl


def _new_state(factory):
    from totalreclaw.hermes.state import PluginState

    with patch("totalreclaw.client.TotalReclaw", side_effect=factory):
        state = PluginState()
    assert state.is_configured()
    return state


def _run_session_start(state, factory, session_id="s1"):
    from totalreclaw.hermes import hooks

    with patch("totalreclaw.client.TotalReclaw", side_effect=factory):
        hooks.on_session_start(state, session_id=session_id)


# ---------------------------------------------------------------------------
# Cache gets filled on the real path, and the server values apply
# ---------------------------------------------------------------------------


def test_session_start_fills_cache_and_applies_server_settings(home):
    factory, calls = _make_client_factory(_returns(_status()))
    state = _new_state(factory)

    # Defaults before any billing data.
    assert state.get_cached_billing() is None
    assert state.get_recall_top_k() == 16
    assert state.get_extraction_interval() == 3
    assert state.get_max_facts_per_extraction() == 15
    assert state.get_max_candidate_pool() == 250

    _run_session_start(state, factory)

    cached = state.get_cached_billing()
    assert cached is not None, "on_session_start must fill the billing cache"
    assert cached["tier"] == "free"
    assert cached["features"]["recall_top_k"] == 12
    # One relay request: the eager register's status() response is reused.
    assert len(calls) == 1

    assert state.get_recall_top_k() == 12
    assert state.get_extraction_interval() == 5
    assert state.get_max_facts_per_extraction() == 20
    assert state.get_max_candidate_pool() == 2000


def test_second_session_within_ttl_makes_no_extra_request(home):
    factory, calls = _make_client_factory(_returns(_status()))
    state = _new_state(factory)

    _run_session_start(state, factory, "s1")
    _run_session_start(state, factory, "s2")

    assert len(calls) == 1, "a cached copy within its TTL must not trigger a refresh"
    assert state.get_recall_top_k() == 12


def test_expired_cache_is_refreshed_once_at_session_start(home):
    factory, calls = _make_client_factory(_returns(_status()))
    state = _new_state(factory)
    _run_session_start(state, factory, "s1")
    assert len(calls) == 1

    # Age the cache past its TTL; the next session must re-fetch once.
    state._billing_cache_time = time.time() - 3 * 3600
    assert state.get_cached_billing() is None

    # The relay now advertises a different value.
    factory2, calls2 = _make_client_factory(_returns(_status(recall_top_k=10)))
    state._client = factory2()
    _run_session_start(state, factory2, "s2")

    assert len(calls2) == 1
    assert state.get_cached_billing() is not None
    assert state.get_recall_top_k() == 10


def test_billing_status_to_cache_shape():
    from totalreclaw.hermes.hooks import _billing_status_to_cache

    data = _billing_status_to_cache(_status(used=7, limit=250))
    assert data["free_writes_used"] == 7
    assert data["free_writes_limit"] == 250
    # None-valued features are dropped so update_from_billing ignores them.
    assert "min_extract_interval" not in data["features"]
    assert data["features"]["max_candidate_pool"] == 2000
    # Non-BillingStatus objects (e.g. a bare MagicMock) are not cached.
    assert _billing_status_to_cache(MagicMock()) is None
    assert _billing_status_to_cache({"no": "tier"}) is None


# ---------------------------------------------------------------------------
# Relay failure leaves defaults and never breaks the session
# ---------------------------------------------------------------------------


def test_relay_failure_leaves_defaults(home):
    factory, calls = _make_client_factory(_raises(httpx.ConnectError("relay down")))
    state = _new_state(factory)

    _run_session_start(state, factory)  # must not raise

    assert state.get_cached_billing() is None
    assert state.get_recall_top_k() == 16
    assert state.get_extraction_interval() == 3
    assert state.get_max_facts_per_extraction() == 15
    assert state.get_max_candidate_pool() == 250
    # The eager register failed, so no second (refresh) request this session.
    assert len(calls) == 1


def test_slow_relay_refresh_times_out_and_keeps_settings(home, monkeypatch):
    from totalreclaw.hermes import hooks

    factory, _ = _make_client_factory(_returns(_status()))
    state = _new_state(factory)
    _run_session_start(state, factory, "s1")
    assert state.get_recall_top_k() == 12

    # Expire the cache; the relay now hangs far longer than the timeout.
    state._billing_cache_time = time.time() - 3 * 3600
    monkeypatch.setattr(hooks, "_BILLING_REFRESH_TIMEOUT_S", 0.05)

    async def _hang():
        await asyncio.sleep(5)
        return _status(recall_top_k=4)

    slow_factory, slow_calls = _make_client_factory(_hang)
    state._client = slow_factory()

    started = time.monotonic()
    _run_session_start(state, slow_factory, "s2")
    elapsed = time.monotonic() - started

    assert len(slow_calls) == 1
    assert elapsed < 2.0, f"session start waited {elapsed:.2f}s on a slow relay"
    assert state.get_cached_billing() is None
    # Previously applied server settings stay in effect.
    assert state.get_recall_top_k() == 12


# ---------------------------------------------------------------------------
# Env override still beats the server
# ---------------------------------------------------------------------------


def test_env_overrides_beat_server_values(home, monkeypatch):
    monkeypatch.setenv("TOTALRECLAW_RECALL_TOP_K", "20")
    monkeypatch.setenv("TOTALRECLAW_EXTRACT_INTERVAL", "2")
    monkeypatch.setenv("TOTALRECLAW_MAX_FACTS_PER_EXTRACTION", "9")
    monkeypatch.setenv("CANDIDATE_POOL_MAX_FREE", "300")

    factory, _ = _make_client_factory(_returns(_status()))
    state = _new_state(factory)
    _run_session_start(state, factory)

    assert state.get_cached_billing() is not None
    assert state.get_recall_top_k() == 20
    assert state.get_extraction_interval() == 2
    assert state.get_max_facts_per_extraction() == 9
    assert state.get_max_candidate_pool() == 300


# ---------------------------------------------------------------------------
# Quota warning
# ---------------------------------------------------------------------------


def test_quota_warning_fires_above_80_percent(home):
    factory, _ = _make_client_factory(_returns(_status(used=210, limit=250)))
    state = _new_state(factory)
    _run_session_start(state, factory)

    warning = state.get_quota_warning()
    assert warning is not None
    assert "210/250" in warning
    assert "84%" in warning


def test_quota_warning_silent_at_or_below_80_percent(home):
    factory, _ = _make_client_factory(_returns(_status(used=200, limit=250)))
    state = _new_state(factory)
    _run_session_start(state, factory)

    warning = state.get_quota_warning() or ""
    assert "memories used" not in warning


# ---------------------------------------------------------------------------
# 403 on write invalidates the cache
# ---------------------------------------------------------------------------


def test_write_quota_error_detection():
    from totalreclaw.agent.lifecycle import _is_write_quota_error

    req = httpx.Request("POST", "https://relay.invalid/v1/bundler")
    forbidden = httpx.HTTPStatusError(
        "403 Forbidden", request=req, response=httpx.Response(403, request=req)
    )
    server_error = httpx.HTTPStatusError(
        "500", request=req, response=httpx.Response(500, request=req)
    )
    assert _is_write_quota_error(forbidden)
    assert _is_write_quota_error(RuntimeError("Quota exceeded for this month"))
    assert not _is_write_quota_error(server_error)
    assert not _is_write_quota_error(RuntimeError("connection reset"))


def test_invalidate_billing_cache_forces_refetch(home):
    factory, calls = _make_client_factory(_returns(_status()))
    state = _new_state(factory)
    _run_session_start(state, factory, "s1")
    assert state.get_cached_billing() is not None

    state.invalidate_billing_cache()
    assert state.get_cached_billing() is None
    # Settings already applied stay until the next fetch.
    assert state.get_recall_top_k() == 12

    _run_session_start(state, factory, "s2")
    assert len(calls) == 2, "an invalidated cache is re-fetched at the next session start"
    assert state.get_cached_billing() is not None
