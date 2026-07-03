"""Task 2 (#370): query entity-trapdoors flow into search_facts's trapdoor set.

The read side must compute a trapdoor per query-extracted entity (via the SAME
``compute_entity_trapdoor`` the write side uses -- DRY, guarantees byte-match)
and append them to the search trapdoor set, otherwise stored entity-trapdoors
are never matched at recall time.

This test isolates the entity signal as the SOLE differentiator:

* ``generate_blind_indices`` is patched to ``[]`` (no word trapdoors).
* No ``query_embedding`` / ``lsh_hasher`` is passed (no LSH trapdoors).
* ``relay.query_subgraph`` is faked to record the ``trapdoors`` value of every
  call into a list and return empty results, so decryption/rerank are trivially
  skipped.

With the entity block absent (RED), ``all_trapdoors`` is empty -> ``search_facts``
hits its early-return guard before ever calling ``relay.query_subgraph`` ->
the recorded trapdoor set is empty -> the assertion fails. With the entity block
present (GREEN), the entity trapdoor for ``"honda civic"`` lands in one of the
chunks and is captured.
"""

from __future__ import annotations

from unittest import mock

import pytest

from totalreclaw import operations
from totalreclaw.claims_helper import compute_entity_trapdoor


@pytest.mark.asyncio
async def test_search_facts_includes_query_entity_trapdoor() -> None:
    query = "my Honda Civic service history"

    # Silence word trapdoors so the entity trapdoor is the ONLY differentiator.
    with mock.patch.object(
        operations, "generate_blind_indices", return_value=[]
    ):
        # Fake relay: capture every trapdoors chunk, return empty results so
        # there are no candidates to decrypt/rerank (trivially empty output).
        recorded_trapdoors: list[list[str]] = []

        class _FakeRelay:
            async def query_subgraph(self, gql: str, variables: dict) -> dict:
                td = variables.get("trapdoors") or []
                if td:
                    recorded_trapdoors.append(list(td))
                # Return a well-formed empty result for any query shape.
                return {"data": {"blindIndexes": [], "facts": []}}

        # Build a DerivedKeys-equivalent stub: search_facts only touches
        # ``keys.encryption_key`` inside the decrypt loop, which is never
        # reached because all candidate sets are empty.
        keys = mock.Mock()
        keys.encryption_key = b"\x00" * 32

        result = await operations.search_facts(
            query=query,
            keys=keys,
            owner="0x" + "ab" * 20,
            relay=_FakeRelay(),  # type: ignore[arg-type]
            # No query_embedding / lsh_hasher -> LSH contributes nothing.
            max_candidates=10,
            top_k=8,
        )

    # Sanity: search completed without error and yielded nothing (empty store).
    assert result == []

    # The whole point: the entity trapdoor for the extracted "honda civic"
    # must appear in at least one trapdoor chunk sent to the relay.
    union: set[str] = set()
    for chunk in recorded_trapdoors:
        union.update(chunk)

    assert recorded_trapdoors, (
        "relay.query_subgraph was never called -- search_facts hit the "
        "early-return guard, so entity trapdoors never reached the wire"
    )
    assert compute_entity_trapdoor("honda civic") in union, (
        "entity trapdoor for 'honda civic' missing from queried trapdoors; "
        f"got {sorted(union)}"
    )
