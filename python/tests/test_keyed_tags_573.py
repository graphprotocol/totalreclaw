"""Keyed blind-index parity against the frozen vectors (internal#573, spec 8)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import totalreclaw_core

from totalreclaw import blind_tags
from totalreclaw.crypto import derive_keys_from_mnemonic, derive_lsh_seed
from totalreclaw.lsh import LSHHasher

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests" / "parity" / "fixtures" / "keyed-blind-indices-v1.json"
)
FX = json.loads(FIXTURE.read_text())
META = FX["meta"]
LSH = FX["lsh"]
LEGACY = set()
for _k in ("tokens", "stems", "entities", "lsh"):
    LEGACY.update(FX["legacy_sha256"][_k])
LEGACY.add(FX["legacy_sha256"]["marker"])


@pytest.fixture(scope="module")
def keys():
    return derive_keys_from_mnemonic(META["mnemonic"])


@pytest.fixture(scope="module")
def lsh_seed(keys):
    return derive_lsh_seed(META["mnemonic"], keys.salt)


def test_key_derivation_matches_fixture(keys, lsh_seed):
    assert keys.encryption_key.hex() == META["encryption_key_hex"]
    assert keys.dedup_key.hex() == META["dedup_key_hex"]
    assert lsh_seed.hex() == META["lsh_seed_hex"]


@pytest.mark.parametrize("v", FX["stem_vectors"], ids=lambda v: v["name"])
def test_stem_vectors(keys, v):
    assert blind_tags.fact_tags(keys, v["text"]) == v["tags"]


@pytest.mark.parametrize("v", FX["entity_vectors"], ids=lambda v: v["name"])
def test_entity_vectors(keys, v):
    assert blind_tags.indexer(keys).entity_tag(v["name"]) == v["tag"]


def test_keyed_lsh(lsh_seed, keys):
    h = LSHHasher(lsh_seed, LSH["dims"], LSH["n_tables"], LSH["n_bits"])
    assert not hasattr(h, "hash")
    assert h.hash_keyed(LSH["embedding"]) == LSH["keyed_tags"]
    tags = blind_tags.fact_tags(keys, "prefers dark mode", embedding=LSH["embedding"], lsh_hasher=h)
    assert set(LSH["keyed_tags_sorted"]) <= set(tags)
    assert tags == sorted(set(tags))


@pytest.mark.parametrize("v", FX["fp_v2"], ids=lambda v: v["text"][:20])
def test_fp_v2(keys, v):
    assert blind_tags.content_fp(keys, v["text"]) == v["fp"]


def test_entity_names_helper():
    ents = [{"name": "Acme"}, {"name": "Acme"}, {"name": ""}, {"name": 5}, {"x": 1}]

    class O:
        name = "Beta"

    assert blind_tags.entity_names(ents + [O()]) == ["Acme", "Beta"]
    assert blind_tags.entity_names(None) == []


def test_query_trapdoors_with_embedding(keys, lsh_seed):
    h = LSHHasher(lsh_seed, LSH["dims"], LSH["n_tables"], LSH["n_bits"])
    names = ["PostgreSQL", "Café"]
    got = blind_tags.query_trapdoors(
        keys, "prefers dark mode", query_embedding=LSH["embedding"], lsh_hasher=h, entity_names=names
    )
    ix = blind_tags.indexer(keys)
    want = sorted(
        set(ix.stem_tags("prefers dark mode")) | {ix.entity_tag(n) for n in names} | set(LSH["keyed_tags"])
    )
    assert got == want
    assert not (set(got) & LEGACY)


def test_query_trapdoors_without_embedding(keys):
    ix = blind_tags.indexer(keys)
    got = blind_tags.query_trapdoors(keys, "prefers dark mode", entity_names=["Acme"])
    assert got == sorted(set(ix.stem_tags("prefers dark mode") + [ix.entity_tag("Acme")]))


def test_bundle_path_identical(keys, lsh_seed):
    """A bundle host (``from_bundle``) yields the same tags as the phrase path."""
    from totalreclaw.bundle import derive_bundle_from_mnemonic
    from totalreclaw.client import TotalReclaw

    sa = "0x2c0CF74B2b76110708CA431796367779e3738250"
    bundle = derive_bundle_from_mnemonic(META["mnemonic"], 100, "local-migration", sa)
    bc = TotalReclaw.from_bundle(bundle, server_url="https://example.invalid")
    pc = TotalReclaw(mnemonic=META["mnemonic"], relay_url="https://example.invalid")
    text = "Alice likes distributed systems and Rust programming"
    got = []
    for c in (pc, bc):
        got.append(blind_tags.fact_tags(
            c._keys, text, embedding=[0.1 * i for i in range(640)],
            lsh_hasher=c._get_lsh_hasher(), entity_names=["Alice"],
        ))
        assert blind_tags.content_fp(c._keys, text) == blind_tags.content_fp(keys, text)
    assert got[0] == got[1]
    assert not (set(got[0]) & LEGACY)


def test_nothing_equals_legacy(keys):
    for v in FX["stem_vectors"]:
        assert not (set(blind_tags.fact_tags(keys, v["text"])) & LEGACY)
