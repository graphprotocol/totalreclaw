"""imp-2 (#244) — privacy disclosure, URL allowlist, onboarding nudge.

PRD-IMP G-4: 100% of extraction-based imports (ChatGPT / Gemini / Claude)
show the LLM-provider disclosure BEFORE any extraction; cancelling at the
disclosure leaves nothing processed; consent persists in the import state
file so resume never re-prompts.

Design doc §2.5: URL input fetches server-side only for allowlisted export
hosts; anything else requires an explicit user confirmation first.

All pure/unit: no network, no LLM, no on-chain writes.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import totalreclaw.import_state as ist
from totalreclaw.import_state import ImportState, write_import_state, read_import_state


def _redirect_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", tmp_path / "import-state")


def _state_with_tier(tier="pro"):
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    client = MagicMock()
    client.status = AsyncMock(return_value=MagicMock(tier=tier))
    state._client = client
    return state


def _patch_engine(monkeypatch, *, process=None):
    import totalreclaw.import_engine as ie
    from totalreclaw.import_adapters import BatchImportResult
    monkeypatch.setattr(
        ie.ImportEngine, "estimate",
        lambda self, **k: {
            "total_chunks": 2, "estimated_facts": 50,
            "estimated_minutes": 3, "num_batches": 1, "batch_size": 25,
        },
    )
    if process is None:
        # A real dataclass: the small-import path round-trips the result
        # through dataclasses.asdict().
        process = AsyncMock(return_value=BatchImportResult(
            success=True, batch_offset=0, batch_size=25, chunks_processed=2,
            total_chunks=2, facts_extracted=3, facts_stored=3,
            remaining_chunks=0, is_complete=True,
        ))
    monkeypatch.setattr(ie.ImportEngine, "process_batch", lambda self, **k: process(**k))
    return process


def _patch_provider(monkeypatch, provider="zai", model="glm-4.6"):
    from totalreclaw.hermes import tools
    monkeypatch.setattr(
        tools, "_extraction_provider_label",
        lambda: f"{provider} ({model})",
    )


# ---------------------------------------------------------------------------
# Privacy disclosure gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extraction_import_requires_disclosure(tmp_path, monkeypatch):
    """chatgpt/gemini/claude without disclosure_confirmed -> blocked BEFORE
    any extraction, message names the LLM provider."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x"}, state,
    ))
    assert res.get("disclosure_required") is True
    assert "zai (glm-4.6)" in res["message"]
    assert process.await_count == 0  # nothing was extracted


@pytest.mark.asyncio
async def test_disclosure_confirmed_proceeds_and_persists(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    first = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x"}, state,
    ))
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x", "disclosure_confirmed": True,
         "disclosure_token": first["disclosure_token"]}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert process.await_count >= 1
    # Consent persisted so resume never re-prompts (issue #244 done-criterion).
    s = read_import_state(res["import_id"])
    assert s is not None and s.disclosure_confirmed is True


@pytest.mark.asyncio
async def test_dry_run_needs_no_disclosure(tmp_path, monkeypatch):
    """Estimating parses locally — nothing goes to an LLM, no gate."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    _patch_engine(monkeypatch)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x", "dry_run": True}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert "estimated_facts" in res


@pytest.mark.asyncio
async def test_resume_with_prior_consent_skips_disclosure(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    write_import_state(ImportState(
        import_id="resume-1", source="chatgpt", status="failed",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True,
    ))
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x", "resume_id": "resume-1"}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert process.await_count >= 1


@pytest.mark.asyncio
async def test_prestructured_sources_are_not_gated(tmp_path, monkeypatch):
    """mem0 facts are pre-structured — no conversation text goes to an LLM."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "mem0", "content": "x"}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert process.await_count >= 1


# ---------------------------------------------------------------------------
# URL fetch + allowlist
# ---------------------------------------------------------------------------

def _patch_fetch(monkeypatch, tmp_path):
    from totalreclaw.hermes import tools
    fetched = []

    def fake_fetch(url, **_kw):
        fetched.append(url)
        p = tmp_path / "downloaded-export.json"
        p.write_text("[]")
        return str(p)

    monkeypatch.setattr(tools, "_fetch_export_url", fake_fetch)
    return fetched


@pytest.mark.parametrize("url", [
    "https://chatgpt.com/backup/export-123.zip",
    "https://cdn.openai.com/exports/export.zip",
    "https://takeout.google.com/exports/gemini.zip",
    "https://claude.ai/exports/data.zip",
    "https://files.anthropic.com/export.zip",
])
def test_allowlisted_hosts(url):
    from totalreclaw.hermes.tools import _is_allowlisted_export_host
    assert _is_allowlisted_export_host(url) is True


@pytest.mark.parametrize("url", [
    "https://evil.example.com/export.zip",
    "https://chatgpt.com.evil.io/x.zip",       # suffix spoof
    "https://notopenai.com/export.zip",        # substring spoof
    "http://chatgpt.com/x.zip",                # non-https
    # multi-tenant object storage: anyone can host a bucket there, so it
    # must NOT be trusted implicitly (PR #431 review finding 2)
    "https://storage.googleapis.com/attacker-bucket/fake-export.zip",
])
def test_non_allowlisted_hosts(url):
    from totalreclaw.hermes.tools import _is_allowlisted_export_host
    assert _is_allowlisted_export_host(url) is False


def test_redirects_are_revalidated():
    """PR #431 review finding 3 — an open redirect on a trusted host must
    not escape the allowlist, and confirmed fetches must not downgrade to
    cleartext http."""
    import urllib.error
    from totalreclaw.hermes.tools import _make_redirect_validator

    strict = _make_redirect_validator(True)()
    with pytest.raises(urllib.error.URLError):
        strict.redirect_request(
            MagicMock(), None, 302, "Found", {}, "https://evil.example.com/x.zip",
        )

    confirmed = _make_redirect_validator(False)()
    with pytest.raises(urllib.error.URLError):
        confirmed.redirect_request(
            MagicMock(), None, 302, "Found", {}, "http://evil.example.com/x.zip",
        )


@pytest.mark.asyncio
async def test_allowlisted_url_is_fetched(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    fetched = _patch_fetch(monkeypatch, tmp_path)
    state = _state_with_tier("pro")

    first = json.loads(await tools.import_from(
        {"source": "chatgpt", "url": "https://chatgpt.com/backup/e.zip"}, state,
    ))
    assert first["disclosure_required"] is True and fetched == []
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "url": "https://chatgpt.com/backup/e.zip",
         "disclosure_confirmed": True,
         "disclosure_token": first["disclosure_token"]}, state,
    ))
    assert fetched == ["https://chatgpt.com/backup/e.zip"]
    assert res.get("url_confirmation_required") is not True
    assert process.await_count >= 1


@pytest.mark.asyncio
async def test_unknown_host_requires_confirmation(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    fetched = _patch_fetch(monkeypatch, tmp_path)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "url": "https://evil.example.com/e.zip",
         "disclosure_confirmed": True}, state,
    ))
    assert res.get("url_confirmation_required") is True
    assert "evil.example.com" in res["message"]
    assert fetched == []  # nothing downloaded before consent


@pytest.mark.asyncio
async def test_unknown_host_with_confirmation_proceeds(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    fetched = _patch_fetch(monkeypatch, tmp_path)
    state = _state_with_tier("pro")

    first = json.loads(await tools.import_from(
        {"source": "chatgpt", "url": "https://evil.example.com/e.zip",
         "url_confirmed": True}, state,
    ))
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "url": "https://evil.example.com/e.zip",
         "url_confirmed": True, "disclosure_confirmed": True,
         "disclosure_token": first["disclosure_token"]}, state,
    ))
    assert fetched == ["https://evil.example.com/e.zip"]
    assert res.get("url_confirmation_required") is not True
    assert process.await_count >= 1


@pytest.mark.asyncio
async def test_url_is_not_downloaded_before_disclosure(tmp_path, monkeypatch):
    """PR #431 review finding 4 — a gated source given a URL must return the
    disclosure BEFORE spending the download."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    fetched = _patch_fetch(monkeypatch, tmp_path)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "url": "https://chatgpt.com/backup/e.zip"}, state,
    ))
    assert res.get("disclosure_required") is True
    assert fetched == []  # nothing downloaded pre-consent


# ---------------------------------------------------------------------------
# import_batch must honor the same gate (PR #431 review finding 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_batch_is_disclosure_gated(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_batch(
        {"source": "chatgpt", "content": "x", "offset": 0}, state,
    ))
    assert res.get("disclosure_required") is True
    assert process.await_count == 0


@pytest.mark.asyncio
async def test_import_batch_honors_persisted_consent(tmp_path, monkeypatch):
    """The documented flow — import_from records consent, import_batch loops
    without re-prompting."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    state = _state_with_tier("pro")

    write_import_state(ImportState(
        import_id="consented", source="chatgpt", status="running",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True,
    ))
    res = json.loads(await tools.import_batch(
        {"source": "chatgpt", "content": "x", "offset": 0}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert process.await_count >= 1


@pytest.mark.asyncio
async def test_import_batch_prestructured_not_gated(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_batch(
        {"source": "mem0", "content": "x", "offset": 0}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert process.await_count >= 1


# ---------------------------------------------------------------------------
# One-time onboarding nudge
# ---------------------------------------------------------------------------

def _configured_state(monkeypatch):
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    monkeypatch.setattr(state, "is_configured", lambda: True)
    return state


def test_import_nudge_fires_once(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import hooks

    state = _configured_state(monkeypatch)
    out = hooks.pre_llm_call(state, user_message="hi", is_first_turn=True)
    ctx = (out or {}).get("context", "") if out else ""
    assert "import" in ctx.lower(), "first configured turn should mention import"

    # Second call: one-shot — never again.
    state2 = _configured_state(monkeypatch)
    out2 = hooks.pre_llm_call(state2, user_message="hi again", is_first_turn=True)
    ctx2 = (out2 or {}).get("context", "") if out2 else ""
    assert "chatgpt" not in ctx2.lower()


def test_import_nudge_suppressed_after_an_import_exists(tmp_path, monkeypatch):
    """A user who already imported doesn't need discovery."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import hooks

    write_import_state(ImportState(
        import_id="prior", source="chatgpt", status="completed",
        started_at="2026-07-01T00:00:00+00:00", last_updated="x", announced=True,
    ))
    state = _configured_state(monkeypatch)
    out = hooks.pre_llm_call(state, user_message="hi", is_first_turn=True)
    ctx = (out or {}).get("context", "") if out else ""
    assert "chatgpt" not in ctx.lower()


# ---------------------------------------------------------------------------
# SKILL.md guidance (attachment-first + disclosure workflow)
# ---------------------------------------------------------------------------

def test_skill_md_documents_disclosure_and_attachments():
    from pathlib import Path
    import totalreclaw.hermes as hermes_pkg
    skill = (Path(hermes_pkg.__file__).parent / "SKILL.md").read_text().lower()
    assert "disclosure" in skill
    assert "attach" in skill or "drop" in skill
