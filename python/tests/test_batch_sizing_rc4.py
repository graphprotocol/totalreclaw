"""rc4 (internal#435) — byte-capped batching + halve-on-sim-revert.

rc3 re-QA NO-GO'd on F3 again. Instrumented staging repro
(https://github.com/p-diogo/totalreclaw-internal/issues/435#issuecomment-4895421400):
Pimlico's ``-32500 "Sender does not implement validateUserOp or factory is not
deployed"`` is a CATCH-ALL for executeBatch simulation failure on oversized
calldata. Realistic import facts (~600-char text + encrypted 640-dim embedding)
≈ 4.5KB each; simulation reverts between 15 (~67KB, passes) and 20 (~85KB,
fails) facts. The #454 deploy-state fixes were correct but not this bug.

Fixes exercised here:
  * ``_group_payloads_by_size`` chunks by BOTH a count ceiling (≤15) and an
    estimated calldata-byte cap (≤32KB).
  * ``ImportEngine._store_group_adaptive`` halves-and-retries a group that
    still sim-reverts, floor 1.
  * ``userop._await_batch_receipt`` waits up to 240s (poll 5s) for inclusion.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import totalreclaw.import_engine as ie
from totalreclaw.import_engine import (
    ImportEngine,
    IMPORT_MAX_BATCH_SIZE,
    _MAX_BATCH_BYTES,
    _estimate_payload_bytes,
    _group_payloads_by_size,
)
from totalreclaw import userop


# ── (a)+(b) grouping helper ───────────────────────────────────────────────
def test_count_ceiling_is_15():
    # rc4 restored the count ceiling to 15 (was 30, never staging-validated).
    assert IMPORT_MAX_BATCH_SIZE == 15


def test_realistic_facts_group_within_both_caps():
    # ~600-char text + a 640-dim embedding ≈ 4.56KB estimated each.
    payloads = [
        {"text": "x" * 600, "embedding": [0.0] * 640}
        for _ in range(40)
    ]
    groups = list(_group_payloads_by_size(
        payloads, IMPORT_MAX_BATCH_SIZE, _MAX_BATCH_BYTES
    ))
    # No fact dropped or duplicated.
    assert sum(len(g) for g in groups) == 40
    for g in groups:
        assert len(g) <= IMPORT_MAX_BATCH_SIZE
        assert sum(_estimate_payload_bytes(p) for p in g) <= _MAX_BATCH_BYTES
    # The byte cap (not the count cap) binds for realistic payloads: groups are
    # well under 15.
    assert max(len(g) for g in groups) < IMPORT_MAX_BATCH_SIZE


def test_short_facts_group_up_to_count_ceiling():
    # Tiny text, no embedding → the count ceiling binds before the byte cap.
    payloads = [{"text": "short fact"} for _ in range(40)]
    groups = list(_group_payloads_by_size(
        payloads, IMPORT_MAX_BATCH_SIZE, _MAX_BATCH_BYTES
    ))
    assert sum(len(g) for g in groups) == 40
    assert [len(g) for g in groups] == [15, 15, 10]


def test_single_oversize_fact_still_forms_a_group():
    # A lone fact larger than the byte cap is never dropped — it forms its own
    # group (adaptive halving is the backstop if it still sim-reverts).
    payloads = [{"text": "z" * 100_000, "embedding": [0.0] * 640}]
    groups = list(_group_payloads_by_size(
        payloads, IMPORT_MAX_BATCH_SIZE, _MAX_BATCH_BYTES
    ))
    assert groups == [payloads]


# ── (c)+(d) adaptive halving via _store_facts_chunked ─────────────────────
# internal#448 single-pass: the import engine calls the REAL store
# (store_fact_batch) directly through ONE group_and_store_adaptive pass — it no
# longer wraps client.remember_batch (wrapping it would nest two halving
# cascades: remember_batch's inner pass re-raises a RuntimeError wrapping the
# inner floor-1 -32500, which the OUTER pass mis-detects as a fresh sim-revert
# → re-halves → re-stores already-stored facts). So these cascade assertions
# now observe store_fact_batch — the single layer that owns the halving —
# reached via a fake client whose store-setup privates are stubbed. The
# [10,5,5] / [4,2,2] / [4] expectations are UNCHANGED: the grouping + halving
# logic is identical, just observed at the layer that now runs it.
def _gnosis_client() -> MagicMock:
    """Fake client resolved on Gnosis (chain 100) with the store-setup privates
    the engine assembles its WalletContext from stubbed (no crypto / relay)."""
    client = MagicMock()
    client._ensure_chain_id = AsyncMock(return_value=100)
    client._ensure_address = AsyncMock()
    client._ensure_registered = AsyncMock()
    client._wallet_context = MagicMock(return_value=MagicMock(name="wallet"))
    client._relay = MagicMock(name="relay")
    client._data_edge_address = None
    client._get_lsh_hasher = MagicMock(return_value=None)
    # Pre-write dedup fails open / no-op so all facts reach the store.
    client.find_duplicate_texts = AsyncMock(return_value=None)
    return client


def _wire_store(monkeypatch, fail_pred=lambda n: False) -> list[int]:
    """Patch the engine's ``store_fact_batch`` with a recorder that raises a
    sim-revert when ``fail_pred(group_size)`` is true. Returns the list of
    observed group sizes (in call order) so a test can assert the cascade."""
    calls: list[int] = []

    async def _store(facts, *a, **kw):
        calls.append(len(facts))
        if fail_pred(len(facts)):
            raise RuntimeError(
                "UserOperation reverted during simulation with reason: -32500 "
                "Sender does not implement validateUserOp or factory is not deployed"
            )
        return [f"id{i}" for i in range(len(facts))]

    monkeypatch.setattr("totalreclaw.import_engine.store_fact_batch", _store)
    return calls


@pytest.fixture(autouse=True)
def _no_embedding(monkeypatch):
    # Keep payloads small (no embedding) so a 10-fact group stays one group,
    # and avoid loading the real embedding model.
    import totalreclaw.embedding as emb
    monkeypatch.setattr(emb, "get_embedding", lambda t: None)
    yield


def _facts(n):
    return [
        {"text": f"distinct fact number {i} about something", "type": "fact", "importance": 8}
        for i in range(n)
    ]


def test_sim_revert_halves_and_stores_all(monkeypatch):
    # A 10-fact group sim-reverts; halves (5+5) succeed → all 10 stored, no
    # errors. Store sees group sizes [10, 5, 5].
    calls = _wire_store(monkeypatch, fail_pred=lambda n: n > 5)
    engine = ImportEngine(client=_gnosis_client(), llm_extract=None)
    stored, errors, dups, _conv = asyncio.run(engine._store_facts_chunked(_facts(10)))
    assert stored == 10
    assert errors == []
    assert calls == [10, 5, 5]


def test_sim_revert_at_single_fact_floor_surfaces_error(monkeypatch):
    # Every group (down to 1 fact) sim-reverts → the single-fact floor surfaces
    # an error rather than silently dropping the fact.
    calls = _wire_store(monkeypatch, fail_pred=lambda n: True)
    engine = ImportEngine(client=_gnosis_client(), llm_extract=None)
    stored, errors, dups, _conv = asyncio.run(engine._store_facts_chunked(_facts(1)))
    assert stored == 0
    assert errors
    assert "Batch store failed" in errors[0]


def test_sim_revert_partial_floor_failure_stores_the_rest(monkeypatch):
    # 4 facts: the full group reverts, halves to 2+2; one 2-group succeeds,
    # the other reverts and halves to 1+1 which both fail at the floor.
    # Deterministic on group SIZE: fail any group of size != 2. So [4]→halve,
    # [2],[2] succeed → actually stores all. Use a size-based fail that still
    # exercises a floor error: fail sizes 4 and 1.
    calls = _wire_store(monkeypatch, fail_pred=lambda n: n in (4, 1))
    engine = ImportEngine(client=_gnosis_client(), llm_extract=None)
    stored, errors, dups, _conv = asyncio.run(engine._store_facts_chunked(_facts(4)))
    # [4] reverts → [2],[2] both succeed (size 2 not in fail set).
    assert stored == 4
    assert errors == []
    assert calls == [4, 2, 2]


def test_aa25_with_32500_code_does_not_halve(monkeypatch):
    """Review Finding 2: an AA25 that exhausted the userop-layer retry
    propagates carrying a ``-32500`` code. That is NOT a size revert — it must
    surface immediately (one store call, no halving)."""
    calls: list[int] = []

    async def _raise_aa25(facts, *a, **kw):
        calls.append(len(facts))
        raise RuntimeError(
            "UserOperation reverted during validation: AA25 invalid account "
            "nonce (code -32500)"
        )

    monkeypatch.setattr("totalreclaw.import_engine.store_fact_batch", _raise_aa25)
    engine = ImportEngine(client=_gnosis_client(), llm_extract=None)
    stored, errors, _, _conv = asyncio.run(engine._store_facts_chunked(_facts(4)))
    assert stored == 0
    assert errors  # surfaced
    assert calls == [4]  # exactly one attempt — NOT halved


def test_sim_revert_without_code_still_halves(monkeypatch):
    """A "reverted during simulation" message with no -32500 code still
    triggers halving (the phrase is sufficient)."""
    calls: list[int] = []

    async def _store(facts, *a, **kw):
        calls.append(len(facts))
        if len(facts) > 2:
            raise RuntimeError("UserOperation reverted during simulation with reason: out of gas")
        return [f"id{i}" for i in range(len(facts))]

    monkeypatch.setattr("totalreclaw.import_engine.store_fact_batch", _store)
    engine = ImportEngine(client=_gnosis_client(), llm_extract=None)
    stored, errors, _, _conv = asyncio.run(engine._store_facts_chunked(_facts(4)))
    assert stored == 4
    assert errors == []
    assert calls == [4, 2, 2]


# ── (Finding 1) non-circular calibration: est ≥ REAL encode_fact_protobuf ──
def _real_protobuf_bytes(text, embedding=None, extra_metadata=None):
    """Build a fact the way operations.store_fact_batch does and measure its
    actual on-chain protobuf byte length — the ground truth the estimate must
    bound. Non-circular: measures encode_fact_protobuf, not the estimator."""
    import base64
    import os
    from totalreclaw.protobuf import (
        FactPayload, encode_fact_protobuf, PROTOBUF_VERSION_V4,
    )
    from totalreclaw.crypto import (
        encrypt, encrypt_embedding, generate_blind_indices,
        generate_content_fingerprint,
    )
    from totalreclaw.lsh import LSHHasher
    from totalreclaw.claims_helper import build_canonical_claim_v1

    key = os.urandom(32)
    dedup = os.urandom(32)
    claim = build_canonical_claim_v1(
        {"text": text, "type": "fact", "source": "external", "scope": "personal"},
        importance=8, claim_id="id-" + "x" * 32, extra_metadata=extra_metadata,
    )
    enc_hex = base64.b64decode(encrypt(claim, key)).hex()
    indices = generate_blind_indices(text)
    enc_emb = None
    if embedding:
        indices = indices + LSHHasher(os.urandom(32), len(embedding)).hash(embedding)
        enc_emb = encrypt_embedding(embedding, key)
    fp = FactPayload(
        id="id-" + "x" * 32, timestamp="2026-05-14T09:21:03.512Z",
        owner="0x" + "a" * 40, encrypted_blob=enc_hex, blind_indices=indices,
        decay_score=0.8, source="import",
        content_fp=generate_content_fingerprint(text, dedup),
        agent_id="python-client", encrypted_embedding=enc_emb,
        version=PROTOBUF_VERSION_V4,
    )
    return len(encode_fact_protobuf(fp))


def _payload(text, embedding=None, extra_metadata=None):
    p = {"text": text}
    if embedding is not None:
        p["embedding"] = embedding
    if extra_metadata is not None:
        p["extra_metadata"] = extra_metadata
    return p


@pytest.mark.parametrize("nchars", [300, 600, 900])
@pytest.mark.parametrize("with_embedding", [False, True])
def test_estimate_bounds_real_protobuf_bytes(nchars, with_embedding):
    # Representative extracted-memory prose (natural word repetition).
    base = (
        "The user moved to Berlin in May 2026 for a new engineering role at a "
        "startup. They are looking for an apartment in Prenzlauer Berg or Mitte "
        "with a budget around 1500 euros per month and want good public transport."
    )
    text = base
    while len(text) < nchars:
        text += " " + base
    text = text[:nchars]
    embedding = [0.01 * i for i in range(640)] if with_embedding else None

    real = _real_protobuf_bytes(text, embedding)
    est = _estimate_payload_bytes(_payload(text, embedding))
    assert est >= real, (
        f"estimate {est} under-counts real {real} for {nchars} chars "
        f"embedding={with_embedding}"
    )


def test_estimate_bounds_real_for_all_unique_tokens():
    # Adversarial: every token unique → maximal blind-index count. A char-linear
    # estimate under-counts this; the computed-index estimate must still bound it.
    text = " ".join(f"tok{i}word" for i in range(120))
    embedding = [0.01 * i for i in range(640)]
    real = _real_protobuf_bytes(text, embedding)
    est = _estimate_payload_bytes(_payload(text, embedding))
    assert est >= real, f"estimate {est} under-counts real {real} for all-unique tokens"


# Dense non-ASCII scripts: the encrypted blob stores UTF-8 BYTES
# (ensure_ascii=False), so a code-point estimate under-counts (CJK ~3B/char,
# emoji ~4B/cp, RTL ~2B/char) — PR #461 re-review Finding A.
_NONASCII = {
    "cjk": "这是一个关于搬到柏林并寻找住房的对话摘要用户需要完成登记并购买保险还要办理居留许可",
    "emoji": "Trip recap 🎉🏙️🚆🏡💶📝✈️🗺️🎊🥳 moving to Berlin ",
    "rtl": "ملخص المحادثة حول الانتقال إلى برلين والبحث عن سكن والتسجيل والتأمين وتصريح الإقامة ",
}


@pytest.mark.parametrize("script", ["cjk", "emoji", "rtl"])
@pytest.mark.parametrize("nchars", [300, 1000, 2000])
@pytest.mark.parametrize("with_embedding", [False, True])
def test_estimate_bounds_real_non_ascii(script, nchars, with_embedding):
    seed = _NONASCII[script]
    text = seed
    while len(text) < nchars:
        text += seed
    text = text[:nchars]
    embedding = [0.01 * i for i in range(640)] if with_embedding else None
    real = _real_protobuf_bytes(text, embedding)
    est = _estimate_payload_bytes(_payload(text, embedding))
    assert est >= real, (
        f"estimate {est} under-counts real {real} for {script} {nchars} chars "
        f"({len(text.encode('utf-8'))} utf-8 bytes) embedding={with_embedding}"
    )


def test_estimate_bounds_real_for_crystal_metadata():
    # Crystal-shaped fact: extra_metadata carries key_outcomes/open_threads/etc.
    text = "Session summary about relocating to Berlin and finding housing."
    meta = {
        "key_outcomes": ["moved to Berlin", "signed a lease", "started job"],
        "open_threads": ["find a school", "set up insurance"],
        "topics_discussed": ["relocation", "housing", "work", "transport"],
        "session_title": "Moving to Berlin for a new job",
        "subtype": "session_crystal",
        "import_source": "chatgpt",
        "session_id": "s" * 36,
    }
    embedding = [0.01 * i for i in range(640)]
    real = _real_protobuf_bytes(text, embedding, meta)
    est = _estimate_payload_bytes(_payload(text, embedding, meta))
    assert est >= real, f"estimate {est} under-counts real {real} for a Crystal fact"


# ── (e) receipt-wait constant ─────────────────────────────────────────────
def test_receipt_wait_constants_lifted():
    assert userop._BATCH_RECEIPT_TIMEOUT_S == 240.0
    assert userop._BATCH_RECEIPT_POLL_S == 5.0
