"""PRD-04 DEP-5 -- short-key entity refs handed to core's contradiction resolver."""
from __future__ import annotations

import json

import totalreclaw_core

from totalreclaw.agent.extraction import VALID_ENTITY_TYPES, ExtractedEntity
from totalreclaw.claims_helper import CORE_ENTITY_TYPES, entity_refs_from_blob, to_entity_refs


def test_core_entity_types_match_extractor_vocabulary() -> None:
    assert set(CORE_ENTITY_TYPES) == set(VALID_ENTITY_TYPES)


def test_to_entity_refs_accepts_objects_and_both_dict_shapes() -> None:
    refs = to_entity_refs([
        ExtractedEntity(name="Pedro", type="person", role="subject"),
        {"name": "Lisbon", "type": "Place"},
        {"n": "TotalReclaw", "tp": "project", "r": "employer"},
    ])
    assert refs == [
        {"n": "Pedro", "tp": "person", "r": "subject"},
        {"n": "Lisbon", "tp": "place"},
        {"n": "TotalReclaw", "tp": "project", "r": "employer"},
    ]


def test_to_entity_refs_drops_unknown_types_and_empty_names() -> None:
    assert to_entity_refs([
        {"name": "Rex", "type": "animal"},
        {"name": "   ", "type": "person"},
        {"name": "Ana"},
    ]) == []


def test_to_entity_refs_non_list_is_empty() -> None:
    assert to_entity_refs(None) == []
    assert to_entity_refs("Pedro") == []


def test_entity_refs_from_v1_and_v0_blobs() -> None:
    v1 = json.dumps({
        "id": "x", "text": "t", "type": "claim", "source": "user",
        "created_at": "2026-09-01T00:00:00Z", "schema_version": "1.0",
        "entities": [{"name": "Pedro", "type": "person"}],
    })
    v0 = json.dumps({"t": "t", "c": "fact", "cf": 0.9, "i": 8, "sa": "x", "e": [{"n": "Pedro", "tp": "person"}]})
    assert entity_refs_from_blob(v1) == [{"n": "Pedro", "tp": "person"}]
    assert entity_refs_from_blob(v0) == [{"n": "Pedro", "tp": "person"}]
    assert entity_refs_from_blob("not json") == []
    assert entity_refs_from_blob(json.dumps({"text": "legacy"})) == []


def test_entity_refs_are_accepted_by_core_claim_parser() -> None:
    refs = to_entity_refs([{"name": "Pedro", "type": "person", "role": "subject"}])
    claim = {"t": "Pedro lives in Lisbon", "c": "fact", "cf": 0.9, "i": 8, "sa": "x", "e": refs}
    # Raises ValueError if core rejects the short-key claim.
    totalreclaw_core.canonicalize_claim(json.dumps(claim))
