"""rc5 — import entry-point hardening (rc4 re-QA NO-GO, #457/#437).

The rc4 store path is fixed; the NO-GO moved to the default entry point:

* #457a — a typo'd path returned status:completed / 0 chunks / 0 facts /
  errors:[] — silent failure presented as success, which leaked a phantom
  "import finished, 0 memories" note into agent context. import_from must FAIL
  LOUD (adapter errors verbatim; failed state; never completed-empty).
* #457b — in one-shot `hermes chat -q` the background task dies with the
  process, leaving a `running` record stuck at 0/N for 28+ min. Two guards: an
  atexit reaper marks this process's still-running imports failed, and
  import_status treats a running/0-progress record older than 10 min as
  stale-failed.
* #437 — the pending disclosure token was the FILENAME, so a shell-capable
  agent could read it off disk and self-assert consent. Now stored hashed with
  a 1h TTL.
* accounting — session Crystals (derived_facts) count in facts_stored but not
  facts_extracted; surface derived_facts so the numbers reconcile.

All pure/unit: no network, no LLM, no on-chain writes.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import totalreclaw.import_state as ist
from totalreclaw.import_state import (
    ImportState, write_import_state, read_import_state, is_import_early_stale,
    EARLY_STALE_THRESHOLD_SECONDS,
)
from totalreclaw.hermes import tools


def _redirect_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", tmp_path / "import-state")


def _state_with_tier(tier="pro"):
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    client = MagicMock()
    client.status = AsyncMock(return_value=MagicMock(tier=tier))
    state._client = client
    return state


def _patch_estimate(monkeypatch, estimate: dict):
    import totalreclaw.import_engine as ie
    monkeypatch.setattr(ie.ImportEngine, "estimate", lambda self, **k: dict(estimate))


# ── #457a fail-loud ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_nonexistent_file_fails_loud_not_completed(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    process = AsyncMock()
    import totalreclaw.import_engine as ie
    monkeypatch.setattr(ie.ImportEngine, "process_batch", lambda self, **k: process(**k))
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "file_path": "/home/pdioho/typo.json"}, state,
    ))
    # Loud error, adapter message surfaced, NOT a completed-empty success.
    assert res.get("error") == "import_failed"
    assert res.get("status") == "failed"
    assert any("No such file" in e or "Failed to read" in e for e in res["errors"])
    assert res.get("facts_stored") != 0 or "facts_stored" not in res  # no phantom count
    # No extraction attempted.
    assert process.await_count == 0
    # State (if written) is failed — never completed.
    s = read_import_state(res["import_id"])
    assert s is None or s.status == "failed"


@pytest.mark.asyncio
async def test_empty_but_valid_parse_fails_loud(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    # Adapter parsed cleanly but found nothing: 0 chunks AND 0 facts, no errors.
    _patch_estimate(monkeypatch, {
        "total_chunks": 0, "total_facts": 0, "errors": [],
        "estimated_facts": 0, "num_batches": 1, "batch_size": 25,
    })
    process = AsyncMock()
    import totalreclaw.import_engine as ie
    monkeypatch.setattr(ie.ImportEngine, "process_batch", lambda self, **k: process(**k))
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "mem0", "content": "{}"}, state,
    ))
    assert res.get("error") == "import_failed"
    assert "No importable content" in " ".join(res["errors"])
    assert process.await_count == 0
    s = read_import_state(res["import_id"])
    assert s is None or s.status == "failed"


@pytest.mark.asyncio
async def test_dry_run_bad_path_fails_loud(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    state = _state_with_tier("pro")
    res = json.loads(await tools.import_from(
        {"source": "gemini", "file_path": "/nope/missing.html", "dry_run": True}, state,
    ))
    assert res.get("error") == "import_failed"
    assert res["errors"]


@pytest.mark.asyncio
async def test_valid_import_still_succeeds(tmp_path, monkeypatch):
    """Regression: a usable estimate is not tripped by the fail-loud guard."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.import_adapters import BatchImportResult
    _patch_estimate(monkeypatch, {
        "total_chunks": 2, "total_facts": 0, "errors": [],
        "estimated_facts": 5, "estimated_minutes": 1, "num_batches": 1, "batch_size": 25,
    })
    import totalreclaw.import_engine as ie
    process = AsyncMock(return_value=BatchImportResult(
        success=True, batch_offset=0, batch_size=25, chunks_processed=2,
        total_chunks=2, facts_extracted=3, facts_stored=4, remaining_chunks=0,
        is_complete=True, derived_facts=1,
    ))
    monkeypatch.setattr(ie.ImportEngine, "process_batch", lambda self, **k: process(**k))
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "mem0", "content": "[{}]"}, state,
    ))
    assert res.get("error") != "import_failed"
    assert res.get("facts_stored") == 4
    assert res.get("derived_facts") == 1  # accounting surfaced
    s = read_import_state(res["import_id"])
    assert s is not None and s.status == "completed"


# ── #457b orphaned background imports ───────────────────────────────────────
def test_is_import_early_stale():
    old = (datetime.now(timezone.utc) - timedelta(seconds=EARLY_STALE_THRESHOLD_SECONDS + 60)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    # running, 0 progress, old → early-stale.
    assert is_import_early_stale(ImportState(
        import_id="a", source="chatgpt", status="running",
        started_at=old, last_updated=old, batch_done=0, batch_total=3))
    # running but has made progress → NOT early-stale (2h guard governs).
    assert not is_import_early_stale(ImportState(
        import_id="b", source="chatgpt", status="running",
        started_at=old, last_updated=old, batch_done=1, batch_total=3))
    # recent → not stale yet.
    assert not is_import_early_stale(ImportState(
        import_id="c", source="chatgpt", status="running",
        started_at=recent, last_updated=recent, batch_done=0, batch_total=3))
    # not running → never early-stale.
    assert not is_import_early_stale(ImportState(
        import_id="d", source="chatgpt", status="completed",
        started_at=old, last_updated=old, batch_done=0, batch_total=3))


def _write_state_raw(import_id: str, **fields):
    """Write a state file directly, preserving the given last_updated (the
    public write_import_state stamps last_updated=now, defeating age tests)."""
    from dataclasses import asdict as _asdict
    d = ist.IMPORT_STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    st = ImportState(import_id=import_id, **fields)
    (d / f"{import_id}.json").write_text(json.dumps(_asdict(st)), encoding="utf-8")


@pytest.mark.asyncio
async def test_import_status_reports_orphaned_running(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    _write_state_raw(
        "orph-1", source="chatgpt", status="running",
        started_at=old, last_updated=old, batch_done=0, batch_total=3)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_status({"import_id": "orph-1"}, state))
    assert res["status"] == "failed"
    assert res.get("orphaned") is True
    assert res.get("resume_id") == "orph-1"
    # Persisted as failed with a resume hint.
    s = read_import_state("orph-1")
    assert s.status == "failed"
    assert any("exited before making progress" in e for e in s.errors)


def test_atexit_reaper_fails_running_spawned_imports(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    write_import_state(ImportState(
        import_id="bg-1", source="gemini", status="running",
        started_at="2026-07-06T00:00:00+00:00", last_updated="2026-07-06T00:00:00+00:00",
        batch_done=0, batch_total=5))
    # A completed spawned import must NOT be touched.
    write_import_state(ImportState(
        import_id="bg-done", source="gemini", status="completed",
        started_at="2026-07-06T00:00:00+00:00", last_updated="2026-07-06T00:05:00+00:00",
        batch_done=5, batch_total=5))
    monkeypatch.setattr(tools, "_SPAWNED_IMPORT_IDS", {"bg-1", "bg-done"})

    tools._fail_orphaned_imports_on_exit()

    running = read_import_state("bg-1")
    assert running.status == "failed"
    assert any("process exited before the import completed" in e for e in running.errors)
    assert "resume_id bg-1" in " ".join(running.errors)
    # Completed import untouched.
    assert read_import_state("bg-done").status == "completed"


# ── #457b review Finding 1: heartbeat + reap-safe background loop ───────────
def _bg_result(**over):
    from totalreclaw.import_adapters import BatchImportResult
    base = dict(
        success=True, batch_offset=0, batch_size=25, chunks_processed=25,
        total_chunks=100, facts_extracted=5, facts_stored=6, remaining_chunks=75,
        is_complete=False, derived_facts=1,
    )
    base.update(over)
    return BatchImportResult(**base)


@pytest.mark.asyncio
async def test_heartbeat_keeps_live_batch0_fresh(tmp_path, monkeypatch):
    """A live-but-slow batch 0 (last_updated frozen at spawn) must NOT read as
    early-stale — the heartbeat refreshes last_updated while the batch runs."""
    _redirect_state_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "_HEARTBEAT_INTERVAL_S", 0.02)
    old = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    _write_state_raw(
        "live-1", source="gemini", status="running",
        started_at=old, last_updated=old, batch_done=0, batch_total=5)
    # Confirm it WOULD be early-stale before the heartbeat runs.
    assert is_import_early_stale(read_import_state("live-1"))

    class _SlowEngine:
        async def process_batch(self, **k):
            await asyncio.sleep(0.15)  # ~7 heartbeat ticks
            return _bg_result(is_complete=True)

    await tools._process_batch_with_heartbeat(
        _SlowEngine(), "live-1", source="gemini", offset=0, batch_size=25)

    s = read_import_state("live-1")
    # batch_done still 0 (helper doesn't checkpoint) but last_updated is fresh.
    assert s.batch_done == 0
    assert not is_import_early_stale(s)


@pytest.mark.asyncio
async def test_background_loop_stops_and_stays_failed_on_reap(tmp_path, monkeypatch):
    """If the record is reaped (status→failed) mid-run, the loop must stop, do
    no further store calls, and NOT resurrect the record."""
    _redirect_state_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "_HEARTBEAT_INTERVAL_S", 10)  # no heartbeat interference
    monkeypatch.setattr(tools, "_persist_import_memory", AsyncMock())
    start = datetime.now(timezone.utc)
    _write_state_raw(
        "reap-1", source="gemini", status="running",
        started_at=start.isoformat(), last_updated=start.isoformat(),
        batch_done=0, batch_total=4)

    calls = {"n": 0}

    class _ReapingEngine:
        async def process_batch(self, **k):
            calls["n"] += 1
            # Simulate an external reaper flipping the record to failed during
            # the first batch.
            from dataclasses import asdict as _ad
            s = read_import_state("reap-1")
            write_import_state(ImportState(**{**_ad(s), "status": "failed",
                                              "errors": ["reaped externally"]}))
            return _bg_result(is_complete=False)

    await tools._run_import_background(
        engine=_ReapingEngine(), state=MagicMock(), import_id="reap-1",
        source="gemini", file_path=None, content="x",
        estimate={"batch_size": 25}, total_items=100, num_batches=4, start_dt=start)

    # Only ONE batch ran — the reap halted the loop before batch 2.
    assert calls["n"] == 1
    s = read_import_state("reap-1")
    # Not resurrected to running/completed.
    assert s.status == "failed"
    assert any("reaped externally" in e for e in s.errors)


@pytest.mark.asyncio
async def test_background_loop_completes_when_healthy(tmp_path, monkeypatch):
    """Regression: an uninterrupted loop still flips the record to completed."""
    _redirect_state_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "_HEARTBEAT_INTERVAL_S", 10)
    monkeypatch.setattr(tools, "_persist_import_memory", AsyncMock())
    start = datetime.now(timezone.utc)
    _write_state_raw(
        "ok-1", source="gemini", status="running",
        started_at=start.isoformat(), last_updated=start.isoformat(),
        batch_done=0, batch_total=1)

    class _Engine:
        async def process_batch(self, **k):
            return _bg_result(facts_stored=6, facts_extracted=5, derived_facts=1,
                              is_complete=True)

    await tools._run_import_background(
        engine=_Engine(), state=MagicMock(), import_id="ok-1",
        source="gemini", file_path=None, content="x",
        estimate={"batch_size": 25}, total_items=25, num_batches=1, start_dt=start)

    s = read_import_state("ok-1")
    assert s.status == "completed"
    assert s.facts_stored == 6 and s.facts_extracted == 5 and s.derived_facts == 1


@pytest.mark.asyncio
async def test_reimport_all_skipped_is_success_not_failed(tmp_path, monkeypatch):
    """A re-import where the registry skips EVERY conversation yields 0
    extracted / 0 stored but conversations_skipped>0 — that is a SUCCESS, not
    an import_failed (fail-loud keys on the ESTIMATE, not the empty result)."""
    _redirect_state_dir(tmp_path, monkeypatch)
    # Estimate shows real content (the file has conversations) → no fail-loud.
    _patch_estimate(monkeypatch, {
        "total_chunks": 2, "total_facts": 0, "errors": [],
        "estimated_facts": 5, "num_batches": 1, "batch_size": 25,
    })
    import totalreclaw.import_engine as ie
    process = AsyncMock(return_value=_bg_result(
        facts_extracted=0, facts_stored=0, derived_facts=0,
        conversations_skipped=2, is_complete=True))
    monkeypatch.setattr(ie.ImportEngine, "process_batch", lambda self, **k: process(**k))
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x", "disclosure_confirmed": True,
         "disclosure_token": "unused"}, state,
    ))
    # First call returns disclosure_required; drive the consented call.
    if res.get("disclosure_required"):
        res = json.loads(await tools.import_from(
            {"source": "chatgpt", "content": "x", "disclosure_confirmed": True,
             "disclosure_token": res["disclosure_token"]}, state,
        ))
    assert res.get("error") != "import_failed"
    assert res.get("conversations_skipped") == 2
    assert res.get("facts_stored") == 0
    s = read_import_state(res["import_id"])
    assert s is not None and s.status == "completed"


# ── #437 disclosure token hashed at rest ────────────────────────────────────
def test_token_stored_hashed_not_plaintext(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    token = tools._mint_disclosure_token("chatgpt")
    files = list((tmp_path / "import-state").glob("disclosure-*.pending"))
    assert len(files) == 1
    # The raw token must NOT appear in the filename.
    assert token not in files[0].name
    assert files[0].name == f"disclosure-{tools._disclosure_token_hash(token)}.pending"
    # The file holds only source + minted_at — never the raw token.
    data = json.loads(files[0].read_text())
    assert data == {"source": "chatgpt", "minted_at": data["minted_at"]}
    assert token not in files[0].read_text()


def test_token_redeem_once_then_fails(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    token = tools._mint_disclosure_token("chatgpt")
    assert tools._redeem_disclosure_token("chatgpt", token) is True
    # One-time use.
    assert tools._redeem_disclosure_token("chatgpt", token) is False


def test_token_wrong_source_rejected(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    token = tools._mint_disclosure_token("chatgpt")
    assert tools._redeem_disclosure_token("gemini", token) is False
    # Still redeemable for the correct source (wrong-source didn't consume it).
    assert tools._redeem_disclosure_token("chatgpt", token) is True


def test_token_expired_not_redeemable_and_cleaned(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    d = tmp_path / "import-state"
    d.mkdir(parents=True, exist_ok=True)
    token = "abc123def456aaaa"
    stale_minted = (datetime.now(timezone.utc) - timedelta(seconds=tools._DISCLOSURE_TOKEN_TTL_S + 60)).isoformat()
    (d / f"disclosure-{tools._disclosure_token_hash(token)}.pending").write_text(
        json.dumps({"source": "chatgpt", "minted_at": stale_minted}))
    assert tools._redeem_disclosure_token("chatgpt", token) is False
    # The expired sidecar was cleaned up on the failed redeem.
    assert not list(d.glob("disclosure-*.pending"))


def test_mint_cleans_expired_tokens(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    d = tmp_path / "import-state"
    d.mkdir(parents=True, exist_ok=True)
    stale_minted = (datetime.now(timezone.utc) - timedelta(seconds=tools._DISCLOSURE_TOKEN_TTL_S + 60)).isoformat()
    (d / "disclosure-deadbeefdeadbeef.pending").write_text(
        json.dumps({"source": "chatgpt", "minted_at": stale_minted}))
    tools._mint_disclosure_token("gemini")
    # The stale one is gone; only the fresh mint remains.
    remaining = list(d.glob("disclosure-*.pending"))
    assert len(remaining) == 1
    assert "deadbeefdeadbeef" not in remaining[0].name


# ── accounting: derived_facts reconciles ────────────────────────────────────
def test_derived_facts_reconciles_stored_vs_extracted(monkeypatch):
    """A multi-turn import emits one session Crystal — a DERIVED fact that
    counts in facts_stored but not facts_extracted. facts_stored ==
    facts_extracted + derived_facts (dups 0)."""
    import totalreclaw.embedding as emb
    monkeypatch.setattr(emb, "get_embedding", lambda t: [0.1, 0.2, 0.3])
    from totalreclaw.import_engine import ImportEngine

    class _BatchClient:
        def __init__(self):
            self.n = 0

        async def _ensure_chain_id(self):
            return 100

        async def remember_batch(self, payloads, source=None):
            ids = [f"f{self.n + i}" for i in range(len(payloads))]
            self.n += len(payloads)
            return ids

    convo = json.dumps([
        {"header": "Gemini Apps", "title": "Prompted I just moved to Berlin",
         "time": "2026-05-14T09:21:03.512Z", "products": ["Gemini Apps"],
         "subtitles": [{"name": "Congrats on the move!"}]},
        {"header": "Gemini Apps", "title": "Prompted best neighbourhoods in Berlin?",
         "time": "2026-05-14T09:24:00.000Z", "products": ["Gemini Apps"],
         "subtitles": [{"name": "Prenzlauer Berg and Mitte."}]},
    ])

    async def fake_extract(messages, timestamp):
        return [{"text": "User moved to Berlin for a job", "type": "fact", "importance": 8}]

    async def fake_completion(prompt):
        return ('{"title": "Moving to Berlin", "summary": "s", "key_outcomes": [], '
                '"open_threads": [], "topics_discussed": []}')

    engine = ImportEngine(client=_BatchClient(), llm_extract=fake_extract,
                          llm_completion=fake_completion)
    result = asyncio.run(engine.process_batch(source="gemini", content=convo))
    assert result.derived_facts >= 1
    assert result.facts_stored == result.facts_extracted + result.derived_facts - result.dups_skipped


# ── SKILL.md one-shot guidance ──────────────────────────────────────────────
def test_skill_md_has_one_shot_synchronous_guidance():
    from pathlib import Path
    import totalreclaw.hermes as h
    skill = Path(h.__file__).parent / "SKILL.md"
    text = skill.read_text().lower()
    assert "one-shot" in text or "short-lived" in text
    assert "import_batch" in text and "synchronous" in text
