"""Mechanism test: write-side entity trapdoors collide with read-side query
entity trapdoors when surface forms align (#370).

Pins the byte-match invariant for the ALIGNED case: a canonical entity named
verbatim in a query must produce a colliding trapdoor across write/read. This
is what makes stored entity-trapdoors matchable at recall time.

Non-aligned cases are NOT asserted here -- they are measured offline (Task 0,
``asymmetry-gate-370.json``: 30.0% asymmetric collision = 76% of symmetric
39.3%) and gated by the Task 6 ship measurement. One such miss (internal
punctuation) is documented below as a known limitation, not a regression.
"""

import hashlib

from totalreclaw.claims_helper import compute_entity_trapdoor
from totalreclaw.entity_extract import extract_query_entities


def _read_trapdoors(query: str) -> set[str]:
    """The trapdoor set the read path (search_facts) generates from a query."""
    return {compute_entity_trapdoor(name) for name in extract_query_entities(query)}


class TestEntityTrapdoorRoundtrip:
    def test_canonical_place_collides(self):
        # Write stored entity "San Francisco"; query names it verbatim.
        assert compute_entity_trapdoor("San Francisco") in _read_trapdoors(
            "my San Francisco trip notes"
        )

    def test_multi_word_product_collides(self):
        assert compute_entity_trapdoor("Honda Civic") in _read_trapdoors(
            "Honda Civic service history"
        )

    def test_lowercase_domain_seed_collides(self):
        # Write stored "Postgres"; query uses the lowercase domain-seed form.
        assert compute_entity_trapdoor("Postgres") in _read_trapdoors(
            "rotate the postgres credentials"
        )

    def test_recipe_is_unkeyed_sha256(self):
        # Pin the production recipe: unkeyed sha256("entity:" + normalize(name)).
        # Consistent with word trapdoors (rust/totalreclaw-core/src/blind.rs).
        assert compute_entity_trapdoor("Acme") == hashlib.sha256(
            b"entity:acme"
        ).hexdigest()

    def test_internal_punctuation_is_a_known_miss(self):
        # Documented limitation, NOT a regression. The heuristic extractor
        # full-strips non-alphanumerics (matching the spike), so "Node.js" ->
        # "nodejs"; the write side normalizes to "node.js" -> a DIFFERENT
        # trapdoor, so they do not collide. The canonicalizer lever
        # (TODO #370) would bridge this; banked as a Task 6 fallback.
        write_td = compute_entity_trapdoor("Node.js")
        assert write_td not in _read_trapdoors("using Node.js today")
