"""PRD-04 F1 / DEP-5 -- search_facts surfaces pin state + entity refs and
passes core's pin boost to the reranker."""
from __future__ import annotations

import base64
import json
from unittest import mock

from totalreclaw import operations
from totalreclaw.crypto import encrypt

KEY = bytes(range(32))


def _blob_hex(obj: dict) -> str:
    return "0x" + base64.b64decode(encrypt(json.dumps(obj), KEY)).hex()


def _fact(fact_id: str, text: str, pinned: bool, entities: list[dict]) -> dict:
    blob = {
        "id": fact_id, "text": text, "type": "claim", "source": "user",
        "created_at": "2026-09-01T00:00:00Z", "schema_version": "1.0", "importance": 8,
        "entities": entities,
    }
    if pinned:
        blob["pin_status"] = "pinned"
    return {
        "id": fact_id, "encryptedBlob": _blob_hex(blob), "encryptedEmbedding": None,
        "decayScore": "0.9", "timestamp": "", "isActive": True,
    }


class _FakeRelay:
    def __init__(self, facts: list[dict]) -> None:
        self._facts = facts

    async def query_subgraph(self, gql: str, variables: dict) -> dict:
        return {
            "data": {
                "blindIndex": [{"id": f"bi-{f['id']}", "fact": f} for f in self._facts],
                "facts": list(self._facts),
            }
        }


def _keys():
    keys = mock.Mock()
    keys.encryption_key = KEY
    return keys


async def test_search_facts_marks_pinned_and_carries_entity_refs() -> None:
    relay = _FakeRelay([
        _fact("f-pinned", "Pedro lives in Lisbon", True, [{"name": "Pedro", "type": "person"}]),
        # "animal" is outside core's EntityType enum -> dropped from the refs.
        _fact("f-plain", "Pedro likes coffee", False,
              [{"name": "Pedro", "type": "person"}, {"name": "Rex", "type": "animal"}]),
    ])
    with mock.patch.object(operations, "generate_blind_indices", return_value=["td"]):
        results = await operations.search_facts(
            query="where does Pedro live", keys=_keys(), owner="0x" + "ab" * 20,
            relay=relay, max_candidates=10, top_k=8,
        )
    by_id = {r.id: r for r in results}
    assert by_id["f-pinned"].pinned is True
    assert by_id["f-plain"].pinned is False
    assert by_id["f-pinned"].entities == [{"n": "Pedro", "tp": "person"}]
    assert by_id["f-plain"].entities == [{"n": "Pedro", "tp": "person"}]


async def test_search_facts_passes_core_pin_boost_to_rerank() -> None:
    relay = _FakeRelay([_fact("f-pinned", "Pedro lives in Lisbon", True, [])])
    with mock.patch.object(operations, "generate_blind_indices", return_value=["td"]), \
         mock.patch.object(operations, "rerank", wraps=operations.rerank) as spy:
        await operations.search_facts(
            query="where does Pedro live", keys=_keys(), owner="0x" + "ab" * 20,
            relay=relay, max_candidates=10, top_k=8,
        )
    assert operations.PIN_BOOST_DEFAULT == 1.5
    assert spy.call_args.kwargs["pin_boost"] == operations.PIN_BOOST_DEFAULT
