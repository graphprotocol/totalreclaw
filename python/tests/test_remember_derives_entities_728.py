"""#728 items 1-2: explicit remembers store derived entities, pin keeps them,
and the contradiction counter keys mean what they say."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw import entity_extract
from totalreclaw.agent import contradiction
from totalreclaw.claims_helper import entity_refs_from_blob
from totalreclaw.entity_extract import derive_entities_for_write
from totalreclaw.hermes.state import PluginState
from totalreclaw.hermes.tools import remember


def _state():
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    client = MagicMock()
    client.remember = AsyncMock(return_value="fact-1")
    state.get_client = MagicMock(return_value=client)
    return state, client


TEXT = "Pedro's home city is Lisbon and he works with Kubernetes"


@pytest.mark.asyncio
async def test_explicit_remember_passes_derived_entities():
    state, client = _state()
    out = json.loads(await remember({"text": TEXT, "force": True}, state))
    assert out["stored"] is True
    ents = client.remember.await_args.kwargs["entities"]
    assert ents, "derived entities expected"
    names = {e["name"] for e in ents}
    assert "Lisbon" in names
    for e in ents:
        assert set(e) == {"name", "type"}
        assert e["type"] in {"person", "project", "tool", "company", "concept", "place"}


@pytest.mark.asyncio
async def test_derivation_failure_still_stores_without_entities():
    state, client = _state()
    with patch.object(entity_extract, "extract_query_entities", side_effect=RuntimeError("boom")):
        out = json.loads(await remember({"text": TEXT, "force": True}, state))
    assert out["stored"] is True
    assert not client.remember.await_args.kwargs.get("entities")


@pytest.mark.parametrize("text,banned", [
    ("I may move to Lisbon in May", {"may"}),
    ("Meeting with the team moved to Thursday", {"meeting"}),
    ("The car needs new tires before the trip", {"car", "trip"}),
])
def test_derivation_skips_domain_seed_junk(text, banned):
    names = {e["name"].lower() for e in derive_entities_for_write(text)}
    assert not (names & banned)


def test_derivation_drops_names_contained_in_longer_name():
    names = [e["name"] for e in derive_entities_for_write("Acme Corp renews")]
    assert names == ["Acme Corp"]


def test_derivation_restores_source_casing():
    names = [e["name"] for e in derive_entities_for_write("I moved to Lisbon")]
    assert names == ["Lisbon"]


def test_read_side_extraction_unchanged():
    assert "may" in entity_extract.extract_query_entities("I may move to Lisbon in May")


def test_derived_entities_land_in_v1_claim_and_resolve_as_refs():
    from totalreclaw.claims_helper import build_canonical_claim_v1

    fact = {
        "text": TEXT, "type": "claim", "source": "user", "scope": "unspecified",
        "confidence": 1.0, "entities": derive_entities_for_write(TEXT),
    }
    blob = build_canonical_claim_v1(fact, importance=8)
    refs = entity_refs_from_blob(blob)
    assert any(r["n"].lower() == "lisbon" for r in refs)


@pytest.mark.asyncio
@patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
async def test_pin_preserves_entities_of_v1_claim(mock_send):
    from totalreclaw import operations
    from totalreclaw.claims_helper import build_canonical_claim_v1
    from totalreclaw.crypto import derive_keys_from_mnemonic
    from tests import test_pin_unpin as tpu  # reuse the relay mock helpers

    keys = derive_keys_from_mnemonic(tpu.TEST_MNEMONIC)
    mock_send.return_value = "0xabc"
    entities = derive_entities_for_write(TEXT)
    source_blob = build_canonical_claim_v1(
        {"text": TEXT, "type": "claim", "source": "user", "scope": "unspecified",
         "confidence": 1.0, "entities": entities},
        importance=8,
    )
    relay = tpu._build_relay_mock(keys, fact_id="old", text="x")
    relay.query_subgraph.side_effect = None
    relay.query_subgraph = AsyncMock(return_value={"data": {"fact": {
        "id": "old", "owner": tpu.OWNER.lower(),
        "encryptedBlob": tpu._encrypted_hex(source_blob, keys.encryption_key),
        "encryptedEmbedding": None, "decayScore": "0.7", "timestamp": "1760000000",
        "createdAt": "1760000000", "isActive": True, "contentFp": "abc", "indexScheme": 1,
    }}})
    captured = {}
    real = operations.build_canonical_claim_v1

    def spy(fact, **kw):
        out = real(fact, **kw)
        captured["blob"] = out
        return out

    with patch.object(operations, "build_canonical_claim_v1", side_effect=spy):
        result = await operations.pin_fact(
            fact_id="old", keys=keys, owner=tpu.OWNER, relay=relay,
            eoa_private_key=tpu.EOA_PRIVATE_KEY, eoa_address=tpu.EOA_ADDRESS,
            sender=tpu.OWNER,
        )
    assert result["success"] is True
    refs = entity_refs_from_blob(captured["blob"])
    assert {r["n"] for r in refs} == {e["name"] for e in entities}


def _cand(cid, names, emb):
    return {"id": cid, "embedding": emb,
            "claim": {"st": "p", "e": [{"n": n, "tp": "place"} for n in names]}}


def test_counter_keys_distinguish_in_band_from_no_overlap():
    new_emb = [1.0, 0.0]
    in_band = [0.6, 0.8]  # cosine 0.6, inside the band
    cands = [_cand("a", ["lisbon"], in_band), _cand("b", ["porto"], in_band)]
    pinned_n, in_band_not_skipped, oob = contradiction._pinned_candidate_stats(
        new_emb, cands, []
    )
    assert (pinned_n, in_band_not_skipped, oob) == (2, 2, 0)
    # Legacy count includes the candidate that shares an entity ...
    assert "whatever its entity state" in contradiction._pinned_candidate_stats.__doc__
    # ... the correctly named count does not.
    n = contradiction._no_name_overlap_count(
        new_emb, cands, [], {"lisbon"}, lambda s: s.lower()
    )
    assert n == 1
