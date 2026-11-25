"""Keyed blind indices (index scheme 1, internal#573) -- PyO3 leg.

Reads ``fixtures/keyed-blind-indices-v1.json`` (written by the independent
stdlib-Python generator) and asserts the PyO3 core reproduces every tag,
fingerprint and wire byte from the raw inputs. Siblings:
``rust/totalreclaw-core/tests/keyed_blind_indices.rs`` (Rust) and
``tests/parity/keyed-blind-indices-parity.test.ts`` (WASM).

Run (CI ``python-tests`` job, local core wheel installed)::

    cd python && python -m pytest ../tests/parity/test_keyed_blind_indices_parity.py -v
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import totalreclaw_core as core

FX = json.loads((Path(__file__).parent / "fixtures" / "keyed-blind-indices-v1.json").read_text())
ENC = bytes.fromhex(FX["meta"]["encryption_key_hex"])
DEDUP = bytes.fromhex(FX["meta"]["dedup_key_hex"])
SEED = bytes.fromhex(FX["meta"]["lsh_seed_hex"])


def test_stem_tags():
    ix = core.BlindIndexer(ENC)
    for v in FX["stem_vectors"]:
        assert ix.stem_tags(v["text"]) == v["tags"], v["name"]


def test_entities_marker_separation():
    ix = core.BlindIndexer(ENC)
    for v in FX["entity_vectors"]:
        assert ix.entity_tag(v["normalized"]) == v["tag"]
    assert ix.entity_tag("Café") == ix.entity_tag("Café")
    assert ix.digest_marker_tag() == FX["marker"]["tag"]
    sep = FX["separation"]
    assert ix.stem_tags("digest")[0] == sep["stem"]
    assert ix.entity_tag("digest") == sep["entity"]
    assert len({sep["stem"], sep["entity"], sep["type"]}) == 3
    other = core.BlindIndexer(bytes.fromhex(FX["meta"]["second_encryption_key_hex"]))
    assert not set(ix.stem_tags("prefers dark mode")) & set(other.stem_tags("prefers dark mode"))
    assert core.sort_dedup_tags(["b", "a", "b"]) == ["a", "b"]


def test_lsh_keyed():
    lsh = FX["lsh"]
    hasher = core.LshHasher(SEED, lsh["dims"], lsh["n_tables"], lsh["n_bits"])
    assert hasher.hash_keyed(lsh["embedding"]) == lsh["keyed_tags"]
    assert hasher.hash(lsh["embedding"]) == lsh["legacy_sha256_tags"]


def test_content_fingerprint_v2():
    for v in FX["fp_v2"]:
        assert core.generate_content_fingerprint_v2(v["text"], DEDUP) == v["fp"], v["text"]


def test_wire_bytes():
    fact = FX["wire"]["fact"]
    assert core.encode_fact_protobuf_keyed(json.dumps(fact["input"])).hex() == fact["expected_hex"]
    assert core.encode_fact_protobuf(json.dumps(fact["input"])).hex() != fact["expected_hex"]
    t = FX["wire"]["tombstone"]
    out = core.encode_tombstone_protobuf_keyed(t["fact_id"], t["owner"], t["version"]).hex()
    assert out.startswith(t["prefix_hex"]) and out.endswith(t["suffix_hex"])
    assert out[len(t["prefix_hex"]):].startswith("12") and out.endswith("7001")


def test_no_keyed_tag_equals_a_legacy_sha256_value():
    legacy = FX["legacy_sha256"]
    banned = set(legacy["tokens"] + legacy["stems"] + legacy["entities"] + legacy["lsh"] + [legacy["marker"]])
    assert hashlib.sha256(b"type:digest").hexdigest() == legacy["marker"]
    ix = core.BlindIndexer(ENC)
    produced = [t for v in FX["stem_vectors"] for t in ix.stem_tags(v["text"])]
    produced += [ix.entity_tag(v["normalized"]) for v in FX["entity_vectors"]]
    produced.append(ix.digest_marker_tag())
    lsh = FX["lsh"]
    produced += core.LshHasher(SEED, lsh["dims"], lsh["n_tables"], lsh["n_bits"]).hash_keyed(lsh["embedding"])
    assert produced and all(len(t) == 64 and t not in banned for t in produced)
