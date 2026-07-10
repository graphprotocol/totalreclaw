"""rc13 fix wave — QA NO-GO findings on 2.4.5rc12 (internal umbrella #420).

F1 (#421)  disclosure_token handshake: consent is impossible without first
           receiving the tool's provider-naming disclosure. rc12 QA showed the
           agent composing its OWN consent prompt and self-setting
           disclosure_confirmed=true — the provider name never reached the user.
F4 (#424)  import_batch persists ImportState so import_status tracks
           batch-driven imports (rc12 QA: "no_import_found" after completion).
F6 (#426)  _SMALL_IMPORT_THRESHOLD 50 -> 5: a 34-chunk import blocked the agent
           turn for 7+ minutes. Anything above a trivial import backgrounds.
gate       import_batch enforces the Pro tier gate (rc12 QA: import ran on the
           Free account with no upgrade wall; flagged in the #431 review too).
F5 (#425)  recall + export surface provenance: source, metadata.import_source,
           session_id. Write-side tags them since #356/#363; read side dropped.
"""
from __future__ import annotations

import asyncio
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


def _patch_engine(monkeypatch, *, total_chunks=2, process=None):
    import totalreclaw.import_engine as ie
    from totalreclaw.import_adapters import BatchImportResult
    monkeypatch.setattr(
        ie.ImportEngine, "estimate",
        lambda self, **k: {
            "total_chunks": total_chunks, "estimated_facts": 50,
            "estimated_minutes": 3, "num_batches": 1, "batch_size": 25,
        },
    )
    if process is None:
        process = AsyncMock(return_value=BatchImportResult(
            success=True, batch_offset=0, batch_size=25, chunks_processed=total_chunks,
            total_chunks=total_chunks, facts_extracted=3, facts_stored=3,
            remaining_chunks=0, is_complete=True,
        ))
    monkeypatch.setattr(ie.ImportEngine, "process_batch", lambda self, **k: process(**k))
    return process


def _patch_provider(monkeypatch, label="zai (glm-4.6)"):
    from totalreclaw.hermes import tools
    monkeypatch.setattr(tools, "_extraction_provider_label", lambda: label)


# ---------------------------------------------------------------------------
# F1 — disclosure_token handshake
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disclosure_response_carries_token(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x"}, state,
    ))
    assert res["disclosure_required"] is True
    assert res.get("disclosure_token"), "disclosure must mint a one-time token"
    assert "zai (glm-4.6)" in res["message"]


@pytest.mark.asyncio
async def test_consent_without_token_is_rejected(tmp_path, monkeypatch):
    """Self-asserted consent (the rc12 bypass) no longer works: the agent must
    have received the disclosure_required response to obtain the token."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x", "disclosure_confirmed": True}, state,
    ))
    assert res.get("disclosure_required") is True
    assert res.get("disclosure_token")
    assert process.await_count == 0


@pytest.mark.asyncio
async def test_consent_with_token_proceeds_and_persists(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    first = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x"}, state,
    ))
    token = first["disclosure_token"]

    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x",
         "disclosure_confirmed": True, "disclosure_token": token}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert process.await_count >= 1
    s = read_import_state(res["import_id"])
    assert s is not None and s.disclosure_confirmed is True


@pytest.mark.asyncio
async def test_wrong_token_is_rejected(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    json.loads(await tools.import_from({"source": "chatgpt", "content": "x"}, state))
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x",
         "disclosure_confirmed": True, "disclosure_token": "bogus"}, state,
    ))
    assert res.get("disclosure_required") is True
    assert process.await_count == 0


@pytest.mark.asyncio
async def test_persisted_consent_still_skips_disclosure(tmp_path, monkeypatch):
    """Resume path: consent recorded in an ImportState never re-prompts."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    write_import_state(ImportState(
        import_id="resume-1", source="chatgpt", status="failed",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider="zai (glm-4.6)",
    ))
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x", "resume_id": "resume-1"}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert process.await_count >= 1


# ---------------------------------------------------------------------------
# import_batch: token + tier gate + state persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_import_batch_rejects_selfasserted_consent(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    res = json.loads(await tools.import_batch(
        {"source": "chatgpt", "content": "x", "disclosure_confirmed": True}, state,
    ))
    assert res.get("disclosure_required") is True
    assert process.await_count == 0


@pytest.mark.asyncio
async def test_import_batch_accepts_token(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    first = json.loads(await tools.import_batch(
        {"source": "chatgpt", "content": "x"}, state,
    ))
    token = first["disclosure_token"]
    res = json.loads(await tools.import_batch(
        {"source": "chatgpt", "content": "x",
         "disclosure_confirmed": True, "disclosure_token": token}, state,
    ))
    assert res.get("disclosure_required") is not True
    assert process.await_count >= 1


@pytest.mark.asyncio
async def test_import_batch_writes_state_for_import_status(tmp_path, monkeypatch):
    """F4: after batch-driven import completes, import_status finds it."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    write_import_state(ImportState(
        import_id="consented", source="chatgpt", status="running",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider="zai (glm-4.6)",
    ))
    await tools.import_batch(
        {"source": "chatgpt", "file_path": "/tmp/e.zip", "offset": 0}, state,
    )
    status = json.loads(await tools.import_status({}, state))
    assert status.get("status") != "no_import_found"
    assert status.get("source") == "chatgpt"


@pytest.mark.asyncio
async def test_import_batch_enforces_pro_gate(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    state = _state_with_tier("free")

    write_import_state(ImportState(
        import_id="consented", source="chatgpt", status="running",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider="zai (glm-4.6)",
    ))
    res = json.loads(await tools.import_batch(
        {"source": "chatgpt", "content": "x", "offset": 0}, state,
    ))
    assert res.get("blocked") is True
    assert process.await_count == 0


@pytest.mark.asyncio
async def test_import_batch_fails_open_when_billing_unreachable(tmp_path, monkeypatch):
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    process = _patch_engine(monkeypatch)
    _patch_provider(monkeypatch)
    from totalreclaw.hermes.state import PluginState
    state = PluginState()
    client = MagicMock()
    client.status = AsyncMock(side_effect=RuntimeError("billing down"))
    state._client = client

    write_import_state(ImportState(
        import_id="consented", source="chatgpt", status="running",
        started_at="2026-07-05T00:00:00+00:00", last_updated="x",
        disclosure_confirmed=True, disclosure_provider="zai (glm-4.6)",
    ))
    res = json.loads(await tools.import_batch(
        {"source": "chatgpt", "content": "x", "offset": 0}, state,
    ))
    assert res.get("blocked") is not True
    assert process.await_count >= 1


# ---------------------------------------------------------------------------
# F6 — small-import threshold
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_six_chunks_runs_in_background(tmp_path, monkeypatch):
    """34 chunks blocked >7min on rc12; anything above 5 chunks backgrounds."""
    _redirect_state_dir(tmp_path, monkeypatch)
    from totalreclaw.hermes import tools
    _patch_engine(monkeypatch, total_chunks=6)
    _patch_provider(monkeypatch)
    state = _state_with_tier("pro")

    first = json.loads(await tools.import_from({"source": "chatgpt", "content": "x"}, state))
    res = json.loads(await tools.import_from(
        {"source": "chatgpt", "content": "x",
         "disclosure_confirmed": True, "disclosure_token": first["disclosure_token"]},
        state,
    ))
    assert res.get("status") == "running", f"6 chunks must background, got {res}"
    assert res.get("import_id")
    # allow the spawned background task to finish so the loop closes clean
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# F5 — provenance surfaced in recall + export
# ---------------------------------------------------------------------------

def test_reranker_result_carries_metadata():
    from totalreclaw.reranker import RerankerCandidate, rerank
    cands = [
        RerankerCandidate(
            id="a", text="User visited Bergen with Ingrid",
            embedding=[0.1] * 8, source="external",
            metadata={"import_source": "chatgpt", "session_id": "0198-x"},
        ),
        RerankerCandidate(id="b", text="unrelated", embedding=[0.9] * 8),
    ]
    out = rerank("bergen trip", [0.1] * 8, cands, top_k=2)
    by_id = {r.id: r for r in out}
    assert by_id["a"].metadata == {"import_source": "chatgpt", "session_id": "0198-x"}
    assert by_id["b"].metadata is None


@pytest.mark.asyncio
async def test_recall_tool_surfaces_provenance(monkeypatch):
    from totalreclaw.hermes import tools
    from totalreclaw.reranker import RerankerResult

    state = _state_with_tier("pro")
    state._client.recall = AsyncMock(return_value=[
        RerankerResult(
            id="f1", text="User visited Bergen", category="episode",
            rrf_score=0.9, source="external",
            metadata={"import_source": "chatgpt", "session_id": "0198-x"},
        ),
        RerankerResult(id="f2", text="likes tea", category="preference", rrf_score=0.5),
    ])
    monkeypatch.setattr(state, "get_recall_top_k", lambda: 8, raising=False)
    monkeypatch.setattr(state, "get_max_candidate_pool", lambda: 50, raising=False)

    res = json.loads(await tools.recall({"query": "bergen"}, state))
    m = res["memories"][0]
    assert m["source"] == "external"
    assert m["import_source"] == "chatgpt"
    assert m["session_id"] == "0198-x"
    # untagged memory: keys absent or null, never wrong values
    m2 = res["memories"][1]
    assert not m2.get("import_source")


def test_export_facts_include_provenance(monkeypatch):
    """operations.export_facts surfaces category/source/import_source/session_id."""
    import totalreclaw.operations as ops

    blob_doc = {
        "text": "User visited Bergen",
        "category": "episode",
        "metadata": {
            "source": "external", "import_source": "chatgpt", "session_id": "0198-x",
        },
    }
    monkeypatch.setattr(ops, "decrypt", lambda b64, key: b"blob")
    monkeypatch.setattr(ops, "is_digest_blob", lambda blob: False)
    monkeypatch.setattr(ops, "read_claim_from_blob", lambda blob: blob_doc)

    relay = MagicMock()
    relay.query_subgraph = AsyncMock(side_effect=[
        {"data": {"facts": [{
            "id": "0xf1", "encryptedBlob": "0xdead", "createdAt": "1751700000",
            "decayScore": "0.8",
        }]}},
        {"data": {"facts": []}},
    ])
    keys = MagicMock(encryption_key=b"k" * 32)

    facts = asyncio.run(ops.export_facts(keys, "0xOWNER", relay, page_size=1))
    assert facts[0]["type"] == "episode"
    assert facts[0]["source"] == "external"
    assert facts[0]["import_source"] == "chatgpt"
    assert facts[0]["session_id"] == "0198-x"


# ---------------------------------------------------------------------------
# F2 (#422) — client-side pre-write dedup
# ---------------------------------------------------------------------------

class _DedupBatchClient:
    """Gnosis client whose vault already contains the 'known' fact."""

    def __init__(self, known_texts):
        self.known = set(known_texts)
        self.batches = []
        self._n = 0

    async def _ensure_chain_id(self):
        return 100

    async def find_duplicate_texts(self, texts):
        return [t in self.known for t in texts]

    async def remember_batch(self, payloads, source=None):
        self.batches.append(list(payloads))
        ids = [f"f{self._n + i}" for i in range(len(payloads))]
        self._n += len(payloads)
        return ids


@pytest.mark.asyncio
async def test_store_skips_crossvault_and_intracall_duplicates():
    from totalreclaw.import_engine import ImportEngine

    client = _DedupBatchClient(known_texts={"User lives in Lisbon"})
    engine = ImportEngine(client=client, llm_extract=None)

    facts = [
        {"text": "User lives in Lisbon", "type": "claim", "importance": 8},   # cross-vault dup
        {"text": "User visited Bergen", "type": "episode", "importance": 7},
        {"text": "User visited  bergen", "type": "episode", "importance": 7}, # intra-call dup (normalized)
        {"text": "User is vegetarian", "type": "preference", "importance": 8},
    ]
    stored, errors, dups, _conv = await engine._store_facts_chunked(facts)
    assert not errors
    assert dups == 2
    assert stored == 2
    stored_texts = [p["text"] for b in client.batches for p in b]
    assert stored_texts == ["User visited Bergen", "User is vegetarian"]


@pytest.mark.asyncio
async def test_dedup_fails_open_when_lookup_errors():
    from totalreclaw.import_engine import ImportEngine

    class _Brittle(_DedupBatchClient):
        async def find_duplicate_texts(self, texts):
            raise RuntimeError("subgraph down")

    client = _Brittle(known_texts=set())
    engine = ImportEngine(client=client, llm_extract=None)
    stored, errors, dups, _conv = await engine._store_facts_chunked(
        [{"text": "unique fact", "type": "claim", "importance": 8}],
    )
    assert stored == 1 and dups == 0 and not errors


@pytest.mark.asyncio
async def test_find_duplicate_texts_maps_fingerprints():
    from totalreclaw.client import TotalReclaw
    import totalreclaw.client as client_mod

    tr = TotalReclaw.__new__(TotalReclaw)  # no real init — no phrase involved
    tr._keys = MagicMock(dedup_key=b"k" * 32)
    tr._wallet_address = "0xOWNER"
    tr._relay = MagicMock()

    async def _noop():
        return None
    tr._ensure_address = _noop
    tr._ensure_registered = _noop

    import totalreclaw.crypto as crypto_mod
    fp_map = {"a": "fp-a", "b": "fp-b"}

    def fake_fp(text, key):
        return fp_map[text]

    orig = crypto_mod.generate_content_fingerprint
    crypto_mod.generate_content_fingerprint = fake_fp
    try:
        async def fake_find(keys, owner, relay, fps):
            return {"fp-a"}
        orig_find = client_mod.find_existing_content_fps
        client_mod.find_existing_content_fps = fake_find
        try:
            flags = await tr.find_duplicate_texts(["a", "b"])
        finally:
            client_mod.find_existing_content_fps = orig_find
    finally:
        crypto_mod.generate_content_fingerprint = orig
    assert flags == [True, False]
