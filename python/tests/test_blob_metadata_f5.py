"""F5 read-side — the v1 blob reader dropped the blob's own metadata dict.

``build_canonical_claim_v1(..., extra_metadata={...})`` writes the import
provenance (``import_source``, ``session_id``, Crystal fields) into the blob's
``metadata`` object, but ``read_blob_unified``'s v1 branch REBUILT ``metadata``
from typed fields only, dropping ``import_source`` / ``session_id`` entirely.
The fix merges the blob's ``metadata`` dict into the returned metadata, with
the typed/rebuilt fields winning on key collisions.
"""
from __future__ import annotations

from totalreclaw.claims_helper import (
    build_canonical_claim_v1,
    read_claim_from_blob,
    read_blob_unified,
)


def _fact(**over):
    base = {
        "text": "User relocated to Berlin for a new job",
        "type": "fact",
        "source": "external",
        "scope": "personal",
    }
    base.update(over)
    return base


def test_v1_blob_roundtrips_import_metadata():
    blob = build_canonical_claim_v1(
        _fact(),
        importance=8,
        extra_metadata={"import_source": "chatgpt", "session_id": "sess-x"},
    )
    doc = read_claim_from_blob(blob)
    meta = doc["metadata"]

    # The blob's own metadata survives the read.
    assert meta.get("import_source") == "chatgpt"
    assert meta.get("session_id") == "sess-x"

    # ...AND the typed/rebuilt fields are still present. ("fact" normalizes to
    # the v1 canonical type "claim" on write.)
    assert meta.get("type") == "claim"
    assert meta.get("source") == "external"
    assert meta.get("scope") == "personal"


def test_typed_fields_win_on_key_collision():
    # A blob whose embedded metadata tries to shadow a typed field must not
    # override the canonical typed value.
    blob = build_canonical_claim_v1(
        _fact(source="external"),
        importance=6,
        extra_metadata={"source": "user-inferred", "import_source": "gemini"},
    )
    meta = read_claim_from_blob(blob)["metadata"]
    assert meta["source"] == "external"          # typed value wins
    assert meta["import_source"] == "gemini"     # extra field carried through


def test_v0_and_legacy_branches_unaffected():
    # v0 short-key blob — no embedded metadata dict to merge; must still read.
    v0 = '{"t":"some text","c":"fact","i":7,"sa":"auto-extraction"}'
    doc = read_blob_unified(v0)
    assert doc["text"] == "some text"
    assert doc["metadata"]["source"] == "auto-extraction"

    # Legacy {text, metadata} doc — metadata already flows through unchanged.
    legacy = '{"text":"hi","metadata":{"type":"preference","importance":0.9}}'
    doc = read_blob_unified(legacy)
    assert doc["text"] == "hi"
    assert doc["metadata"]["type"] == "preference"
