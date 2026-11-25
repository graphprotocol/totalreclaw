"""#704: importance scale handling (1-10 tool scale vs legacy 0-1 floats)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.crypto import derive_keys_from_mnemonic
from totalreclaw.operations import WalletContext, normalize_importance, store_fact
from totalreclaw.relay import RelayClient

from tests.test_operations import EOA_ADDRESS, EOA_PRIVATE_KEY, TEST_MNEMONIC


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, 1),       # int 1 is 1 on the 1-10 scale (was 10)
        (1.0, 1),     # float 1.0 = 1 on 1-10 (internal producers now send ints; legacy queued floats are decoded at the drain)
        (0.7, 7),     # legacy 0-1 float
        (0.5, 5),
        (5, 5),
        (10, 10),
        (10.0, 10),
        (0, 1),       # clamped
        (99, 10),     # clamped
        (True, 5),    # bool is not a score
        ("x", 5),
        (None, 5),
    ],
)
def test_normalize_importance(value, expected):
    assert normalize_importance(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("given,stored", [(1, 1), (0.7, 7), (5, 5), (10, 10), (1.0, 1)])
async def test_store_fact_blob_importance_and_active_decay_score(given, stored):
    keys = derive_keys_from_mnemonic(TEST_MNEMONIC)
    relay = AsyncMock(spec=RelayClient)
    relay._relay_url = "https://api-staging.totalreclaw.xyz"
    wallet = WalletContext(
        keys=keys, owner="0x1234", eoa_private_key=EOA_PRIVATE_KEY,
        eoa_address=EOA_ADDRESS, sender="0x1234",
    )
    seen = {}

    import totalreclaw.operations as ops

    real_build = ops.build_canonical_claim_v1

    def _build(*a, **kw):
        seen["importance"] = kw.get("importance")
        return real_build(*a, **kw)

    def _encode(payload):
        seen["decay"] = payload.decay_score
        return b"x"

    with patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock), \
         patch("totalreclaw.operations.build_canonical_claim_v1", side_effect=_build), \
         patch("totalreclaw.operations.encode_fact_protobuf", side_effect=_encode):
        await store_fact(text="Test fact", wallet=wallet, relay=relay, importance=given)

    assert seen["importance"] == stored
    # decayScore < 0.3 marks a fact inactive in the subgraph: it must stay
    # at or above that for every explicit importance (decayScore is unchanged).
    assert seen["decay"] >= 0.3


def _configured_state():
    from totalreclaw.hermes.tools import PluginState  # noqa: F401
    state = MagicMock()
    state.is_configured.return_value = True
    state.get_client.return_value = MagicMock()
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("given,passed", [(1, 1), (5, 5), (10, 10), (7, 7), (0, 1), (99, 10)])
async def test_remember_tool_passes_1_to_10_int(given, passed):
    from totalreclaw.hermes import tools

    state = MagicMock()
    state.is_configured.return_value = True
    client = MagicMock()
    client.remember = AsyncMock(return_value="fid")
    state.get_client.return_value = client

    with patch.object(tools, "_embed_for_remember", MagicMock(return_value=[0.1] * 4)), \
         patch("totalreclaw.write_gate.record_write", MagicMock()):
        raw = await tools.remember({"text": "likes tea", "importance": given, "force": True}, state)

    payload = json.loads(raw)
    assert payload.get("stored") is True, raw
    kwargs = client.remember.await_args.kwargs
    assert kwargs["importance"] == passed
    assert isinstance(kwargs["importance"], int)
    assert payload["importance"] == passed


@pytest.mark.asyncio
@pytest.mark.parametrize("default,expected", [(None, 7), (7, 7), (8, 8), (1, 1), (10, 10)])
async def test_import_progress_note_not_stored_at_10(default, expected):
    from totalreclaw.hermes import tools

    state = MagicMock()
    state.is_configured.return_value = True
    seen = {}

    async def _fake_remember(args, st, **kw):
        seen.update(args)
        return "{}"

    with patch.object(tools, "remember", _fake_remember):
        if default is None:
            await tools._persist_import_memory(state, text="n")
        else:
            await tools._persist_import_memory(state, text="n", importance=default)
    assert seen["importance"] == expected
    assert normalize_importance(seen["importance"]) == expected


# ---------------------------------------------------------------------------
# Producer conventions (#704 follow-up): internal producers pass an int 1-10,
# and the stored blob importance + batch decayScore match what main stored for
# the old ``score / 10.0`` float form (for 6-10; 1-5 differ only in decay of 1).
# ---------------------------------------------------------------------------


def _batch_decay(raw):
    # the exact expression store_fact_batch uses for decayScore
    return raw if raw <= 1 else raw / 10.0


@pytest.mark.parametrize("score", [6, 7, 8, 9, 10])
def test_int_producer_matches_old_float_producer(score):
    old = score / 10.0
    old_blob = max(1, min(10, int(round(old * 10)) if old <= 1 else int(old)))
    assert normalize_importance(score) == old_blob == score
    assert _batch_decay(score) == pytest.approx(_batch_decay(old))


def test_llm_score_10_is_not_stored_as_1():
    assert normalize_importance(10) == 10
    # the legacy float form of "10" still decodes the old way at the drain
    from totalreclaw.agent.lifecycle import _decode_legacy_queued_importance

    fact = {"importance": 1.0}
    _decode_legacy_queued_importance(fact)
    assert fact["importance"] == 10


def test_normalize_importance_nan_inf_fall_back():
    assert normalize_importance(float("nan")) == 5
    assert normalize_importance(float("inf")) == 5
    assert normalize_importance(float("-inf")) == 5


def test_import_engine_producer_passes_int():
    from totalreclaw.imports.engine import ImportEngine

    for score in (6, 8, 10, 1):
        p = ImportEngine._prepare_fact_payload({"text": "t", "importance": score})
        assert p["importance"] == score and isinstance(p["importance"], int)
        assert normalize_importance(p["importance"]) == score


def test_auto_extraction_producer_passes_int():
    from tests.test_auto_extract_uses_batch import _make_state_and_client
    from totalreclaw.agent.extraction import ExtractedFact
    from totalreclaw.agent.lifecycle import auto_extract

    state, client = _make_state_and_client()
    facts = [
        ExtractedFact(text=f"Distinct fact {i}: detail", type="claim", importance=imp,
                      action="ADD", confidence=0.9, source="user", scope="unspecified")
        for i, imp in enumerate((10, 8, 6))
    ]

    async def fake_extract(*a, **k):
        return facts

    async def passthrough(fs, *a, **k):
        return fs

    with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
         patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
         patch("totalreclaw.agent.lifecycle._fetch_recent_memories", return_value=[]), \
         patch("totalreclaw.agent.lifecycle._is_near_duplicate", return_value=False), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):
        state.add_message("user", "I like things")
        state.add_message("assistant", "ok")
        auto_extract(state)

    sent = [d["importance"] for d in client.remember_batch.call_args.args[0]]
    assert sent == [10, 8, 6]
    assert all(isinstance(i, int) for i in sent)


def test_queued_entries_old_float_and_new_int_forms_decode(tmp_path, monkeypatch):
    """Pending entries written by older versions carry 0.1-1.0 floats; new ones
    carry ints. Both must reach remember_batch as the same int 1-10."""
    import secrets

    from totalreclaw.agent.lifecycle import drain_pending_stores
    from totalreclaw.agent.pending_store import PendingStoreQueue

    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    seen = {}

    class _Client:
        resolved_chain_id = 100
        eoa_address = "0x000000000000000000000000000000000000cafe"

        def __init__(self):
            self._q = PendingStoreQueue.for_owner(self.eoa_address, secrets.token_bytes(32))

        @property
        def pending_store(self):
            return self._q

        async def fetch_recent_facts(self, limit=10):
            return []

        async def recall(self, *a, **k):
            return []

        async def remember_batch(self, facts, source=None):
            for f in facts:
                seen[f["text"]] = f["importance"]
            return [f"id-{i}" for i in range(len(facts))]

    client = _Client()
    client.pending_store.add(
        [
            {"text": "old format ten about trains", "importance": 1.0},
            {"text": "old format six about boats", "importance": 0.6},
            {"text": "new format ten about planes", "importance": 10},
            {"text": "new format six about cars", "importance": 6},
        ],
        reason="write_quota",
    )
    with patch("totalreclaw.embedding.get_embedding", return_value=None), \
         patch("totalreclaw.agent.lifecycle.record_write"):
        drain_pending_stores(MagicMock(), client)

    assert seen == {
        "old format ten about trains": 10,
        "old format six about boats": 6,
        "new format ten about planes": 10,
        "new format six about cars": 6,
    }
