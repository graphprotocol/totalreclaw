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


def test_pre_llm_call_injects_completion_once(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import hooks

    write_import_state(ImportState(
        import_id="done1", source="gemini", status="completed",
        started_at="t", last_updated="t", facts_stored=5, dups_skipped=1,
    ))

    state = _configured_state(monkeypatch)
    out = hooks.pre_llm_call(state, user_message="hello", is_first_turn=False)
    assert out and "context" in out
    ctx = out["context"]
    assert "finished" in ctx and "5 memories" in ctx and "gemini" in ctx

    # Latched — a second turn must NOT re-announce the same import.
    out2 = hooks.pre_llm_call(state, user_message="hello", is_first_turn=False)
    ctx2 = (out2 or {}).get("context", "") if out2 else ""
    assert "finished" not in ctx2
    assert read_import_state("done1").announced is True


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
    called = AsyncMock(return_value=MagicMock(
        facts_stored=3, facts_extracted=3, is_complete=True,
    ))
    # process_batch returns a stub so we don't run real extraction.
    _patch_engine(monkeypatch, process=lambda self, **k: called(**k))
    state, _client = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "gemini", "content": "x"}, state,
    ))
    assert res.get("blocked") is not True
    assert called.await_count >= 1  # import actually proceeded


@pytest.mark.asyncio
async def test_billing_unreachable_fails_open(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    called = AsyncMock(return_value=MagicMock(
        facts_stored=1, facts_extracted=1, is_complete=True,
    ))
    _patch_engine(monkeypatch, process=lambda self, **k: called(**k))
    state, _client = _state_with_tier("free", status_raises=True)

    res = json.loads(await tools.import_from(
        {"source": "gemini", "content": "x"}, state,
    ))
    # Billing down -> do NOT block (self-hosted / offline must still import).
    assert res.get("blocked") is not True
    assert called.await_count >= 1
