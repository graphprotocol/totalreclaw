"""internal#418 — import disclosure consent must bind to the disclosed provider.

The privacy disclosure names the LLM provider that will read the user's past
conversations in cleartext. Persisted consent (``ImportState.disclosure_confirmed``)
previously authorized extraction indefinitely regardless of which provider is
later configured — so consenting while on provider A silently authorized provider
B after a config switch, with no re-disclosure. The fix stamps the resolved
provider label into ``disclosure_provider`` when consent is recorded, and
``_prior_disclosure_consent`` only honors a persisted consent whose recorded
provider matches the CURRENT provider. An absent/None provider (a pre-#418
record) is treated as a mismatch → re-prompt once (never silently authorize).

Pure/unit: no network, no LLM, no on-chain writes.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import totalreclaw.import_state as ist
from totalreclaw.import_state import (
    ImportState, _coerce_state, write_import_state, read_import_state,
)


def _redirect_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", tmp_path / "import-state")


def _state_with_tier(tier="pro"):
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    client = MagicMock()
    client.status = AsyncMock(return_value=MagicMock(tier=tier))
    state._client = client
    return state


def _patch_engine(monkeypatch):
    import totalreclaw.import_engine as ie
    from totalreclaw.import_adapters import BatchImportResult
    process = AsyncMock(return_value=BatchImportResult(
        success=True, batch_offset=0, batch_size=25, chunks_processed=2,
        total_chunks=2, facts_extracted=3, facts_stored=3,
        remaining_chunks=0, is_complete=True,
    ))
    monkeypatch.setattr(
        ie.ImportEngine, "estimate",
        lambda self, **k: {
            "total_chunks": 2, "estimated_facts": 50,
            "estimated_minutes": 3, "num_batches": 1, "batch_size": 25,
        },
    )
    monkeypatch.setattr(ie.ImportEngine, "process_batch", lambda self, **k: process(**k))
    return process


def _patch_provider(monkeypatch, label):
    from totalreclaw.hermes import tools
    monkeypatch.setattr(tools, "_extraction_provider_label", lambda: label)


# ---------------------------------------------------------------------------
# State field: round-trip + back-compat coercion
# ---------------------------------------------------------------------------

def test_import_state_roundtrips_disclosure_provider(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    write_import_state(ImportState(
        import_id="rt-1", source="chatgpt", status="running",
        started_at="2026-07-08T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider="z.ai (GLM)",
    ))
    s = read_import_state("rt-1")
    assert s is not None
    assert s.disclosure_provider == "z.ai (GLM)"


def test_coerce_state_defaults_disclosure_provider_none_for_old_record():
    """A pre-#418 on-disk record has no disclosure_provider key; loading it
    must not crash and the field must default to None."""
    legacy = {
        "import_id": "old-1", "source": "chatgpt", "status": "completed",
        "started_at": "2026-01-01T00:00:00+00:00", "last_updated": "x",
        "disclosure_confirmed": True,
        # NOTE: no disclosure_provider key — pre-#418 shape
    }
    s = _coerce_state(legacy)
    assert s.disclosure_provider is None


# ---------------------------------------------------------------------------
# Provider drift revokes consent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_drift_revokes_persisted_consent(tmp_path, monkeypatch):
    """Consent recorded for provider A must NOT authorize extraction once the
    current provider is B — the disclosure must re-fire."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch, "OpenAI (gpt-4.1)")
    state = _state_with_tier("pro")

    write_import_state(ImportState(
        import_id="resume-1", source="chatgpt", status="failed",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider="z.ai (GLM)",
    ))
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x", "resume_id": "resume-1"}, state,
    ))
    assert res.get("disclosure_required") is True
    # The re-fired disclosure names the CURRENT provider, not the old one.
    assert "OpenAI (gpt-4.1)" in res["message"]
    assert process.await_count == 0  # nothing extracted


@pytest.mark.asyncio
async def test_absent_provider_re_prompts(tmp_path, monkeypatch):
    """A pre-#418 record (disclosure_provider absent → None) is treated as a
    mismatch and re-prompts; never silently authorized."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch, "z.ai (GLM)")
    state = _state_with_tier("pro")

    write_import_state(ImportState(
        import_id="resume-2", source="chatgpt", status="failed",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True,
        # disclosure_provider deliberately omitted → None
    ))
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x", "resume_id": "resume-2"}, state,
    ))
    assert res.get("disclosure_required") is True
    assert process.await_count == 0


# ---------------------------------------------------------------------------
# Same provider honors consent (no spurious re-prompt)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_provider_honors_persisted_consent(tmp_path, monkeypatch):
    """Consent recorded for provider A, current provider still A → honored,
    no re-prompt."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch, "z.ai (GLM)")
    state = _state_with_tier("pro")

    write_import_state(ImportState(
        import_id="resume-3", source="chatgpt", status="failed",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider="z.ai (GLM)",
    ))
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x", "resume_id": "resume-3"}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert process.await_count >= 1
