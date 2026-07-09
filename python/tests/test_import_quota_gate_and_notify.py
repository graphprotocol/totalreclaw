"""Tests for the rc9 import additions:

#1 — Pro-only tier gate in ``totalreclaw_import_from`` (free tier hits the
     upgrade wall before any extraction; fails open if billing is unreachable).
#2 — Proactive import-completion notification (one-shot context injection in
     ``pre_llm_call`` so the agent tells the user the import finished).

All pure/unit: no network, no LLM, no on-chain writes. The import-state dir is
redirected to a tmp dir so the real ``~/.totalreclaw`` is never touched.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import totalreclaw.import_state as ist
from totalreclaw.import_state import (
    ImportState,
    write_import_state,
    read_import_state,
    read_completed_unannounced_imports,
    mark_import_announced,
    _coerce_state,
)


def _redirect_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", tmp_path / "import-state")


# ---------------------------------------------------------------------------
# import_state helpers (#2 backbone)
# ---------------------------------------------------------------------------

def test_coerce_tolerates_legacy_and_unknown_keys():
    s = _coerce_state({
        "import_id": "a", "source": "gemini", "status": "completed",
        "started_at": "t", "last_updated": "t",
        "some_future_field": 123,  # must not blow up
    })
    assert s.import_id == "a"
    assert s.announced is False  # defaulted


def test_completed_unannounced_then_marked(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    write_import_state(ImportState(
        import_id="i1", source="gemini", status="completed",
        started_at="2026-06-07T00:00:00+00:00", last_updated="x",
        facts_stored=7, dups_skipped=2,
    ))
    pending = read_completed_unannounced_imports()
    assert [s.import_id for s in pending] == ["i1"]
    assert pending[0].facts_stored == 7

    mark_import_announced("i1")
    assert read_completed_unannounced_imports() == []
    assert read_import_state("i1").announced is True


def test_running_import_is_not_reported_as_complete(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    write_import_state(ImportState(
        import_id="r", source="chatgpt", status="running",
        started_at="t", last_updated="t",
    ))
    assert read_completed_unannounced_imports() == []


# ---------------------------------------------------------------------------
# #2 — pre_llm_call proactive completion injection
# ---------------------------------------------------------------------------

def _configured_state(monkeypatch):
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    monkeypatch.setattr(state, "is_configured", lambda: True)
    return state


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def test_pre_llm_call_injects_completion_then_dedupes(tmp_path, monkeypatch):
    """#401: first turn injects; consecutive turns dedupe (no re-inject).

    Pre-#401 the hook latched ``announced=True`` immediately after the
    one-shot injection, so the notification was fire-and-forget. #401 keeps
    the notification PERSISTENT (``announced`` stays False) and instead
    dedupes re-injection on a turn interval. So after the first inject we
    expect: no re-inject on the very next turn, AND ``announced`` still
    False (not latched).
    """
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import hooks

    write_import_state(ImportState(
        import_id="done1", source="gemini", status="completed",
        started_at=_now_iso(), last_updated=_now_iso(),
        facts_stored=5, dups_skipped=1, batch_done=4, batch_total=4,
    ))

    state = _configured_state(monkeypatch)
    out = hooks.pre_llm_call(state, user_message="hello", is_first_turn=False)
    assert out and "context" in out
    ctx = out["context"]
    assert "finished" in ctx and "5 memories" in ctx and "gemini" in ctx
    # Persistence: NOT latched on injection (#401).
    assert read_import_state("done1").announced is False

    # Same turn window — deduped, must NOT re-inject.
    out2 = hooks.pre_llm_call(state, user_message="hello", is_first_turn=False)
    ctx2 = (out2 or {}).get("context", "") if out2 else ""
    assert "finished" not in ctx2
    # Still not announced — the notification persists across turns.
    assert read_import_state("done1").announced is False


def test_pre_llm_call_re_injects_after_turn_interval(tmp_path, monkeypatch):
    """#401: after the dedup turn interval elapses, the nudge re-injects.

    The agent may have skipped the first nudge; the persistent notification
    retries every _IMPORT_ANNOUNCE_TURN_INTERVAL turns without spamming
    every turn.
    """
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import hooks

    write_import_state(ImportState(
        import_id="done2", source="chatgpt", status="completed",
        started_at=_now_iso(), last_updated=_now_iso(), facts_stored=3,
    ))
    state = _configured_state(monkeypatch)

    # First turn: inject.
    hooks.pre_llm_call(state, user_message="hi", is_first_turn=False)
    # Advance past the dedup interval and re-run: should inject again.
    interval = hooks._IMPORT_ANNOUNCE_TURN_INTERVAL
    state._turn_count = interval + 1
    out = hooks.pre_llm_call(state, user_message="hi", is_first_turn=False)
    ctx = (out or {}).get("context", "") if out else ""
    assert "finished" in ctx  # re-injected after the interval
    assert read_import_state("done2").announced is False


def test_pre_llm_call_auto_retires_after_grace(tmp_path, monkeypatch):
    """#401: past the 24h grace window the notification auto-latches.

    Guarantees the persistent nudge eventually stops even if the agent never
    acknowledges by querying import_status.
    """
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import hooks
    from totalreclaw import import_state as ist
    from datetime import datetime, timezone, timedelta
    from dataclasses import asdict

    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    # write_import_state() overwrites last_updated=now; write raw JSON to
    # plant a 30h-old timestamp so the 24h grace auto-retire fires.
    ist.IMPORT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    raw = asdict(ImportState(
        import_id="done3", source="gemini", status="completed",
        started_at=old, last_updated=old, facts_stored=9,
    ))
    (ist.IMPORT_STATE_DIR / "done3.json").write_text(json.dumps(raw))

    state = _configured_state(monkeypatch)
    out = hooks.pre_llm_call(state, user_message="hi", is_first_turn=False)
    # Past grace → no injection, and latched retired.
    ctx = (out or {}).get("context", "") if out else ""
    assert "finished" not in ctx
    assert read_import_state("done3").announced is True


# ---------------------------------------------------------------------------
# #1 — Pro-only tier gate in import_from
# ---------------------------------------------------------------------------

def _state_with_tier(tier, *, status_raises=False):
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    client = MagicMock()
    if status_raises:
        client.status = AsyncMock(side_effect=RuntimeError("billing down"))
    else:
        client.status = AsyncMock(return_value=MagicMock(tier=tier))
    state._client = client
    return state, client


def _patch_engine(monkeypatch, *, process=None):
    import totalreclaw.import_engine as ie
    monkeypatch.setattr(
        ie.ImportEngine, "estimate",
        lambda self, **k: {
            "total_chunks": 2, "estimated_facts": 50,
            "estimated_minutes": 3, "num_batches": 1, "batch_size": 25,
        },
    )
    if process is not None:
        monkeypatch.setattr(ie.ImportEngine, "process_batch", process)


@pytest.mark.asyncio
async def test_free_tier_is_blocked_with_upgrade_message(monkeypatch):
    from totalreclaw.hermes import tools
    _patch_engine(monkeypatch)
    state, client = _state_with_tier("free")

    res = json.loads(await tools.import_from(
        {"source": "gemini", "content": "irrelevant — estimate is patched"}, state,
    ))
    assert res.get("blocked") is True
    assert res["tier"] == "free"
    assert "Pro" in res["message"]
    client.status.assert_awaited()  # tier WAS checked


@pytest.mark.asyncio
async def test_pro_tier_is_not_blocked(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    monkeypatch.setattr(tools, "_extraction_provider_label", lambda: "z.ai (GLM)")
    called = AsyncMock(return_value=MagicMock(
        facts_stored=3, facts_extracted=3, is_complete=True,
    ))
    # process_batch returns a stub so we don't run real extraction.
    _patch_engine(monkeypatch, process=lambda self, **k: called(**k))
    state, _client = _state_with_tier("pro")

    # rc13 (#421): consent needs the tool-minted token or persisted state.
    # internal#418: persisted consent must carry the matching provider label.
    write_import_state(ImportState(
        import_id="consent-seed", source="gemini", status="completed",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, announced=True, disclosure_provider="z.ai (GLM)",
    ))
    res = json.loads(await tools.import_from(
        {"source": "gemini", "content": "x"}, state,
    ))
    assert res.get("blocked") is not True
    assert called.await_count >= 1  # import actually proceeded


@pytest.mark.asyncio
async def test_billing_unreachable_fails_open(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    monkeypatch.setattr(tools, "_extraction_provider_label", lambda: "z.ai (GLM)")
    called = AsyncMock(return_value=MagicMock(
        facts_stored=1, facts_extracted=1, is_complete=True,
    ))
    _patch_engine(monkeypatch, process=lambda self, **k: called(**k))
    state, _client = _state_with_tier("free", status_raises=True)

    write_import_state(ImportState(
        import_id="consent-seed", source="gemini", status="completed",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, announced=True, disclosure_provider="z.ai (GLM)",
    ))
    res = json.loads(await tools.import_from(
        {"source": "gemini", "content": "x"}, state,
    ))
    # Billing down -> do NOT block (self-hosted / offline must still import).
    assert res.get("blocked") is not True
    assert called.await_count >= 1


# ---------------------------------------------------------------------------
# #401 — import_status() fallback + acknowledgment latch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_status_falls_back_to_recent_completed(tmp_path, monkeypatch):
    """No active import + a recent completed import -> report it (#401).

    Was: returned ``{"status": "no_active_import"}`` after completion, leaving
    the agent unable to tell "completed" from "never existed".
    """
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    from datetime import datetime, timezone
    from totalreclaw.agent.state import AgentState

    now = datetime.now(timezone.utc).isoformat()
    # Pre-mark announced so the import_status() acknowledgment latch is a
    # no-op (it would otherwise rewrite last_updated via write_import_state,
    # racing the completed_at assertion). The latch is covered by its own
    # dedicated test below.
    write_import_state(ImportState(
        import_id="done-fb", source="gemini", status="completed",
        started_at=now, last_updated=now, announced=True,
        total_chunks=100, batch_done=4, batch_total=4,
        facts_stored=287, facts_extracted=2345,
    ))
    stored = read_import_state("done-fb")

    state = AgentState()
    res = json.loads(await tools.import_status({}, state))
    assert res["status"] == "completed"
    assert res["import_id"] == "done-fb"
    assert res["facts_stored"] == 287
    # #401 new fields:
    assert "elapsed_seconds" in res and res["elapsed_seconds"] >= 0
    # completed_at is derived from the stored last_updated for terminal state.
    assert res["completed_at"] == stored.last_updated


@pytest.mark.asyncio
async def test_import_status_no_import_found_when_empty(tmp_path, monkeypatch):
    """No active + no recent -> the new ``no_import_found`` status (#401)."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    from totalreclaw.agent.state import AgentState

    res = json.loads(await tools.import_status({}, AgentState()))
    assert res["status"] == "no_import_found"


@pytest.mark.asyncio
async def test_import_status_latches_announced_on_query(tmp_path, monkeypatch):
    """#401: querying a completed import retires the persistent nudge.

    The agent checking on a completed import is the natural acknowledgment
    signal (no new tool needed): ``import_status`` latches ``announced`` so
    ``pre_llm_call`` stops re-injecting.
    """
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    from totalreclaw.agent.state import AgentState

    now = _now_iso()
    write_import_state(ImportState(
        import_id="done-ack", source="gemini", status="completed",
        started_at=now, last_updated=now, facts_stored=10,
    ))
    assert read_import_state("done-ack").announced is False

    state = AgentState()
    await tools.import_status({}, state)
    assert read_import_state("done-ack").announced is True


# ---------------------------------------------------------------------------
# #401 — _persist_import_memory (import_id survives context compaction)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_import_memory_noop_when_unconfigured(monkeypatch):
    """#401: graceful no-op when TotalReclaw is not configured."""
    from totalreclaw.hermes import tools
    from totalreclaw.agent.state import AgentState

    state = AgentState()  # not configured
    # Must not raise, and must not attempt a write.
    await tools._persist_import_memory(state, text="anything", importance=0.7)


@pytest.mark.asyncio
async def test_persist_import_memory_writes_via_remember_when_configured(monkeypatch):
    """#401: configured state -> routes through remember(force=True).

    The issue spec proposed ``provider.remember_sync``; that API does not
    exist, so the closest equivalent is the shared ``tools.remember`` handler.
    This pins that routing + the force=True bypass (internal bookkeeping
    write, not a duplicate of a pending user fact).
    """
    from totalreclaw.hermes import tools
    from totalreclaw.agent.state import AgentState

    state = AgentState()
    monkeypatch.setattr(state, "is_configured", lambda: True)

    seen = {}

    async def _fake_remember(args, st, **kwargs):
        seen["args"] = args
        return '{"stored": true}'

    monkeypatch.setattr(tools, "remember", _fake_remember)

    await tools._persist_import_memory(
        state, text="Active background import: id=abc", importance=0.7,
    )
    assert seen["args"]["text"] == "Active background import: id=abc"
    assert seen["args"]["force"] is True
    assert seen["args"]["importance"] == 0.7


@pytest.mark.asyncio
async def test_persist_import_memory_swallows_remember_failure(monkeypatch):
    """#401: a failing remember must not propagate (best-effort only)."""
    from totalreclaw.hermes import tools
    from totalreclaw.agent.state import AgentState

    state = AgentState()
    monkeypatch.setattr(state, "is_configured", lambda: True)

    async def _boom(args, st, **kwargs):
        raise RuntimeError("vault write exploded")

    monkeypatch.setattr(tools, "remember", _boom)
    # Must not raise.
    await tools._persist_import_memory(state, text="x", importance=0.7)
