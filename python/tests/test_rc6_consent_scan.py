"""rc6 — #460: a registry ledger in the state dir must never crash a scan.

rc5 re-QA: 3/4 entry-point fixes PASS and the store path holds, but ONE new
deterministic crash bricked completion. The #436 conversation registry
(``imported-conversations-<source>.json``) is a JSON LIST living in the same
dir as import-state records. ``_prior_disclosure_consent`` globbed ``*.json``
and did ``data.get("source")`` — ``list.get`` raises AttributeError, which the
narrow ``except (OSError, ValueError)`` missed → import crashed on batch 2
(once the ledger existed) and on ALL re-imports.

Fix: every glob-based scan routes through ``iter_import_state_records`` /
``_iter_state_files``, which exclude the registry ledgers and skip non-dict
payloads. All pure/unit: no network, no LLM.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import totalreclaw.import_state as ist
from totalreclaw.import_state import (
    ImportState, write_import_state, read_import_state,
    read_most_recent_active_import, read_most_recent_import,
    read_completed_unannounced_imports, any_import_exists,
    iter_import_state_records, record_imported_conversations,
)
from totalreclaw.hermes import tools


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", tmp_path / "import-state")


def _write_registry(source="chatgpt", ids=("c1", "c2")):
    # A real #436 ledger: a JSON LIST, written by record_imported_conversations.
    record_imported_conversations(source, list(ids))


def _pin_provider(monkeypatch, label="z.ai (GLM)"):
    # internal#418: persisted consent is honored only when disclosure_provider
    # matches the current label — pin both in tests that write consent.
    monkeypatch.setattr(tools, "_extraction_provider_label", lambda: label)
    return label


def _state_with_tier(tier="pro"):
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    client = MagicMock()
    client.status = AsyncMock(return_value=MagicMock(tier=tier))
    state._client = client
    return state


# ── the crash site ─────────────────────────────────────────────────────────
def test_prior_consent_with_registry_present_does_not_crash(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    _write_registry("chatgpt")  # the JSON-list ledger that used to crash .get
    # No consented state yet → False, and crucially NO AttributeError.
    assert tools._prior_disclosure_consent("chatgpt") is False


def test_prior_consent_finds_real_record_despite_registry(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    label = _pin_provider(monkeypatch)
    write_import_state(ImportState(
        import_id="s1", source="chatgpt", status="failed",
        started_at="2026-07-06T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider=label))
    _write_registry("chatgpt")
    assert tools._prior_disclosure_consent("chatgpt") is True


def test_disclosure_consent_ok_with_registry_present(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    label = _pin_provider(monkeypatch)
    write_import_state(ImportState(
        import_id="s2", source="chatgpt", status="running",
        started_at="2026-07-06T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider=label))
    _write_registry("chatgpt")
    assert tools._disclosure_consent_ok("chatgpt", {}) is True


# ── every audited reader is unaffected by a registry file ──────────────────
def test_iter_records_skips_registry(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    write_import_state(ImportState(
        import_id="rec-1", source="gemini", status="running",
        started_at="2026-07-06T00:00:00+00:00", last_updated="x"))
    _write_registry("chatgpt")
    _write_registry("claude")
    records = list(iter_import_state_records())
    assert [r.import_id for r in records] == ["rec-1"]


def test_readers_unaffected_by_registry(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    write_import_state(ImportState(
        import_id="active-1", source="gemini", status="running",
        started_at="2026-07-06T00:00:00+00:00",
        last_updated="2026-07-06T00:01:00+00:00"))
    write_import_state(ImportState(
        import_id="done-1", source="gemini", status="completed",
        started_at="2026-07-06T00:00:00+00:00",
        last_updated="2026-07-06T00:02:00+00:00", announced=False))
    _write_registry("chatgpt")

    assert read_most_recent_active_import().import_id == "active-1"
    assert read_most_recent_import().import_id == "done-1"
    assert [s.import_id for s in read_completed_unannounced_imports()] == ["done-1"]
    assert any_import_exists() is True


def test_any_import_exists_false_with_only_registry(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    _write_registry("chatgpt")  # only a ledger, no real import records
    # The ledger must NOT read as "an import exists" (would suppress the nudge).
    assert any_import_exists() is False


def test_readers_do_not_crash_on_registry_only(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    _write_registry("chatgpt")
    _write_registry("gemini")
    # None of these raise; all return the empty/None result.
    assert read_most_recent_active_import() is None
    assert read_most_recent_import() is None
    assert read_completed_unannounced_imports() == []
    assert list(iter_import_state_records()) == []


# ── the exact #460 repro: two-batch import, registry written after batch 1 ──
@pytest.mark.asyncio
async def test_two_batch_import_proceeds_after_registry_written(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    from totalreclaw.import_adapters import BatchImportResult
    import totalreclaw.import_engine as ie

    # Prior consent so import_batch's disclosure gate is satisfied.
    label = _pin_provider(monkeypatch)
    write_import_state(ImportState(
        import_id="batch-src", source="chatgpt", status="running",
        started_at="2026-07-06T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider=label))

    calls = {"n": 0}

    async def _fake_pb(self, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # #436 writes the conversation ledger after the first batch.
            record_imported_conversations("chatgpt", ["c1", "c2"])
        return BatchImportResult(
            success=True, batch_offset=k.get("offset", 0), batch_size=25,
            chunks_processed=25, total_chunks=50, facts_extracted=5,
            facts_stored=6, remaining_chunks=25 if calls["n"] == 1 else 0,
            is_complete=(calls["n"] >= 2), derived_facts=1)

    monkeypatch.setattr(ie.ImportEngine, "process_batch", _fake_pb)
    state = _state_with_tier("pro")

    r1 = json.loads(await tools.import_batch(
        {"source": "chatgpt", "content": "x", "offset": 0}, state))
    # Batch 2 runs AFTER the ledger exists — the consent scan must not crash.
    r2 = json.loads(await tools.import_batch(
        {"source": "chatgpt", "content": "x", "offset": 25}, state))

    assert "error" not in r1, r1
    assert "error" not in r2, r2
    assert calls["n"] == 2
    assert r2.get("is_complete") is True
