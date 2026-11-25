"""Generate keyed-blind-indices-v1.json (internal#573, index scheme 1).

INDEPENDENT of the Rust core: standard-library Python only (hmac, hashlib,
unicodedata, struct, math). It re-implements the scheme-1 spec, then the Rust,
WASM/TS and PyO3 legs must reproduce every value from the raw inputs.

What is independent here: the three HKDF derivations, the tag composition
(domain || value under HMAC-SHA256), the normalisation (lowercase then NFC) and
tokenizer, the LSH hyperplane generation (HKDF + Box-Muller, checked against the
legacy SHA-256 vectors in crypto_vectors.json), contentFp v2, and the protobuf
wire layout. What is NOT: the Porter stems. They are hand-written below (the
expected stem of each token), and every leg asserts its stemmer reproduces
them.

Inputs: the public test mnemonic's keys, read from
rust/totalreclaw-core/tests/fixtures/crypto_vectors.json. No real secret.

Run from the repo root:  python3 tests/parity/fixtures/generate-keyed-blind-indices-v1.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import struct
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CRYPTO = json.loads((ROOT / "rust/totalreclaw-core/tests/fixtures/crypto_vectors.json").read_text())
OUT = Path(__file__).with_name("keyed-blind-indices-v1.json")

MNEMONIC = CRYPTO["key_derivation"]["mnemonic"]
ENCRYPTION_KEY = bytes.fromhex(CRYPTO["key_derivation"]["encryption_key_hex"])
DEDUP_KEY = bytes.fromhex(CRYPTO["key_derivation"]["dedup_key_hex"])
LSH_SEED = bytes.fromhex(CRYPTO["lsh"]["lsh_seed_hex"])

LABELS = {
    "index_key": "totalreclaw-blind-index-v1",
    "lsh_bucket_key": "totalreclaw-lsh-bucket-key-v1",
    "fp_key_v2": "totalreclaw-content-fp-v2",
}


def hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac_sha256(salt if salt else b"\x00" * 32, ikm)
    okm, t, i = b"", b"", 1
    while len(okm) < length:
        t = hmac_sha256(prk, t + info + bytes([i]))
        okm += t
        i += 1
    return okm[:length]


INDEX_KEY = hkdf(ENCRYPTION_KEY, b"", LABELS["index_key"].encode(), 32)
LSH_BUCKET_KEY = hkdf(LSH_SEED, b"", LABELS["lsh_bucket_key"].encode(), 32)
FP_KEY_V2 = hkdf(DEDUP_KEY, b"", LABELS["fp_key_v2"].encode(), 32)

# RFC 5869 test case 3 (empty salt, empty info): first 32 bytes of the 42-byte OKM.
RFC3_IKM = bytes.fromhex("0b" * 22)
RFC3_OKM = "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8"
assert hkdf(RFC3_IKM, b"", b"", 42).hex() == RFC3_OKM, "HKDF reference broken"


def tag(domain: str, value: str) -> str:
    return hmac_sha256(INDEX_KEY, (domain + value).encode()).hex()


def lower_nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text.lower())


# Rust's `char::is_alphanumeric` is Unicode Alphabetic or category N*. Python has no
# `Alphabetic` property: str.isalnum() misses the combining marks that are
# Other_Alphabetic. These are the ones the vectors exercise (Devanagari vowel signs,
# Arabic fatha, Hebrew vowel points and shin dot, Thai vowel above); the Rust
# tables, pinned by rust-toolchain.toml, are the reference for everything else.
OTHER_ALPHABETIC_USED = {0x093F, 0x0902, 0x0940, 0x064E, 0x05B4, 0x05B8, 0x05B9, 0x05C1, 0x0E31, 0x0E35}


def is_word_char(c: str) -> bool:
    return c.isalnum() or ord(c) in OTHER_ALPHABETIC_USED


def tokenize(text: str) -> list[str]:
    cleaned = "".join(c if (is_word_char(c) or c.isspace()) else " " for c in lower_nfc(text))
    return [t for t in cleaned.split() if len(t.encode()) >= 2]


def normalize_entity(name: str) -> str:
    collapsed = " ".join(name.split())
    return unicodedata.normalize("NFC", "".join(c.lower() for c in collapsed))


# (name, text, expected tokens, expected tag inputs = stem, or the token when the stem is < 2 bytes)
# The stems are written by hand (Porter 1) and asserted by every leg.
STEM_CASES = [
    ("plain", "prefers dark mode", ["prefers", "dark", "mode"], ["prefer", "dark", "mode"]),
    ("plural", "preferences", ["preferences"], ["prefer"]),
    ("base_form", "prefer", ["prefer"], ["prefer"]),
    ("ties", "ties", ["ties"], ["ti"]),
    ("ies_short_stem_fallback", "ies", ["ies"], ["ies"]),
    ("two_char_token", "is", ["is"], ["is"]),
    ("number", "2024", ["2024"], ["2024"]),
    ("phone", "555 123 4567", ["555", "123", "4567"], ["555", "123", "4567"]),
    ("nfc_cafe", "café", ["café"], ["café"]),
    ("nfd_cafe_same_as_nfc", "café", ["café"], ["café"]),
    ("dotted_capital_i", "İstanbul", ["stanbul"], ["stanbul"]),
    ("cjk", "東京タワー", ["東京タワー"], ["東京タワー"]),
    ("emoji_dropped", "I ❤️ tea", ["tea"], ["tea"]),
    ("byte_filter", "a é", ["é"], ["é"]),
    ("empty", "", [], []),
    ("final_sigma", "ΟΔΥΣΣΕΥΣ", ["οδυσσευς"], ["οδυσσευς"]),
    ("hindi_vowel_signs_kept", "\u0939\u093f\u0902\u0926\u0940", ["\u0939\u093f\u0902\u0926\u0940"], ["\u0939\u093f\u0902\u0926\u0940"]),
    ("hindi_virama_splits", "\u0939\u093f\u0928\u094d\u0926\u0940", ["\u0939\u093f\u0928", "\u0926\u0940"], ["\u0939\u093f\u0928", "\u0926\u0940"]),
    ("thai_tone_mark_splits", "\u0e17\u0e35\u0e48\u0e19\u0e35\u0e48", ["\u0e17\u0e35", "\u0e19\u0e35"], ["\u0e17\u0e35", "\u0e19\u0e35"]),
    ("arabic_vowel_mark_kept", "\u0643\u064e\u062a\u064e\u0628\u064e", ["\u0643\u064e\u062a\u064e\u0628\u064e"], ["\u0643\u064e\u062a\u064e\u0628\u064e"]),
    ("hebrew_points_kept", "\u05e9\u05b8\u05c1\u05dc\u05b9\u05dd \u05e9\u05b4\u05c1", ["\u05e9\u05b8\u05c1\u05dc\u05b9\u05dd", "\u05e9\u05b4\u05c1"], ["\u05e9\u05b8\u05c1\u05dc\u05b9\u05dd", "\u05e9\u05b4\u05c1"]),
    ("numeric_symbols", "\u00bd \u2167", ["\u00bd", "\u2177"], ["\u00bd", "\u2177"]),
    ("dedup", "Running quickly, running!", ["running", "quickly", "running"], ["run", "quickli"]),
]

ENTITY_NAMES = ["PostgreSQL", "  postgre   sql ", "Café", "Café", "\u039f\u0394\u03a5\u03a3\u03a3\u0395\u03a5\u03a3", "\u0130stanbul"]


def stem_vectors():
    out = []
    for name, text, tokens, stems in STEM_CASES:
        assert tokenize(text) == tokens, (name, tokenize(text))
        tags = sorted({tag("stem:", s) for s in stems})
        out.append({"name": name, "text": text, "tokens": tokens, "stem_inputs": sorted(set(stems)), "tags": tags})
    return out


def entity_vectors():
    return [{"name": n, "normalized": normalize_entity(n), "tag": tag("entity:", normalize_entity(n))} for n in ENTITY_NAMES]


# ---- LSH: independent hyperplane generation (HKDF + Box-Muller) -------------
def random_bytes(seed: bytes, base_info: str, length: int) -> bytes:
    out, block = b"", 0
    while len(out) < length:
        chunk = min(length - len(out), 255 * 32)
        out += hkdf(seed, b"", f"{base_info}_block_{block}".encode(), chunk)
        block += 1
    return out


def bucket_ids(seed: bytes, dims: int, n_tables: int, n_bits: int, emb: list[float]) -> list[str]:
    ids = []
    for t in range(n_tables):
        raw = random_bytes(seed, f"lsh_table_{t}", dims * n_bits * 8)
        sig = ""
        for b in range(n_bits):
            dot = 0.0
            for d in range(dims):
                off = (b * dims + d) * 8
                u1r, u2r = struct.unpack_from("<II", raw, off)
                u1 = (u1r + 1.0) / (0xFFFFFFFF + 2.0)
                u2 = (u2r + 1.0) / (0xFFFFFFFF + 2.0)
                g = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
                dot += g * emb[d]
            sig += "1" if dot >= 0.0 else "0"
        ids.append(f"lsh_t{t}_{sig}")
    return ids


def lsh_vector():
    # Self-check against the legacy (SHA-256) vectors already in crypto_vectors.json.
    s = CRYPTO["lsh"]["small"]
    ids = bucket_ids(LSH_SEED, s["dims"], s["n_tables"], s["n_bits"], s["embedding"])
    assert [hashlib.sha256(i.encode()).hexdigest() for i in ids] == s["hashes"], "LSH reference broken"
    dims, n_tables, n_bits = 8, 20, 32
    emb = [0.5, -0.25, 0.125, 0.75, -0.5, 0.375, -0.875, 0.0625]
    ids = bucket_ids(LSH_SEED, dims, n_tables, n_bits, emb)
    return {
        "dims": dims, "n_tables": n_tables, "n_bits": n_bits, "embedding": emb,
        "bucket_ids": ids,
        "keyed_tags": [hmac_sha256(LSH_BUCKET_KEY, i.encode()).hex() for i in ids],
        "keyed_tags_sorted": sorted(hmac_sha256(LSH_BUCKET_KEY, i.encode()).hex() for i in ids),
        "legacy_sha256_tags": [hashlib.sha256(i.encode()).hexdigest() for i in ids],
    }


def normalize_fp(text: str) -> str:
    return " ".join(lower_nfc(text).split())


FP_TEXTS = [
    "User prefers dark mode",
    "user  prefers DARK mode ",
    "İstanbul trip",
    "Café au lait",
    "Café au lait",
    "",
]


def fp_vectors():
    return [{"text": t, "normalized": normalize_fp(t), "fp": hmac_sha256(FP_KEY_V2, normalize_fp(t).encode()).hex()} for t in FP_TEXTS]


# ---- protobuf wire (independent writer) ------------------------------------
def varint(n: int) -> bytes:
    out = b""
    while n > 0x7F:
        out += bytes([(n & 0x7F) | 0x80])
        n >>= 7
    return out + bytes([n])


def ld(field: int, data: bytes) -> bytes:
    return varint((field << 3) | 2) + varint(len(data)) + data


def vf(field: int, v: int) -> bytes:
    return varint(field << 3) + varint(v)


def wire_fact(f: dict) -> bytes:
    b = ld(1, f["id"].encode()) + ld(2, f["timestamp"].encode()) + ld(3, f["owner"].encode())
    b += ld(4, bytes.fromhex(f["encrypted_blob_hex"]))
    for t in sorted(set(f["blind_indices"])):
        b += ld(5, t.encode())
    b += varint((6 << 3) | 1) + struct.pack("<d", f["decay_score"])
    b += vf(7, 1) + vf(8, f["version"]) + ld(10, f["content_fp"].encode())
    if f.get("encrypted_embedding"):
        b += ld(13, f["encrypted_embedding"].encode())
    return b + vf(14, 1)


def wire_tombstone(fact_id: str, owner: str, version: int, ts: str) -> tuple[bytes, bytes, bytes]:
    head = ld(1, fact_id.encode())
    ts_field = ld(2, ts.encode())
    tail = ld(3, owner.encode()) + ld(4, b"") + varint((6 << 3) | 1) + struct.pack("<d", 0.0) + vf(7, 0) + vf(8, version) + vf(14, 1)
    return head, ts_field, tail


def wire_vectors():
    t = [tag("stem:", s) for s in ("prefer", "dark", "mode")]
    fact = {
        "id": "7f3c2a10-6b1e-4c1f-9d2e-0000000000b1",
        "timestamp": "2026-11-25T12:00:00.000Z",
        "owner": "0x2c0cf74b2b76110708ca431796367779e3738250",
        "encrypted_blob_hex": "00ff11ee22dd33cc44bb55aa",
        # Deliberately unsorted, with a duplicate: the keyed encoder must sort and dedup.
        "blind_indices": [t[2], t[0], t[1], t[0], tag("entity:", "postgresql"), tag("type:", "digest")],
        "decay_score": 0.8, "source": "mcp_remember", "content_fp": hmac_sha256(FP_KEY_V2, b"x").hex(),
        "agent_id": "mcp-server", "encrypted_embedding": "AAAAdGVzdA==", "version": 4,
    }
    ts = "2026-11-25T12:00:00+00:00"
    head, ts_field, tail = wire_tombstone(fact["id"], fact["owner"], 4, ts)
    return {
        "fact": {"input": fact, "expected_hex": wire_fact(fact).hex()},
        "tombstone": {
            "fact_id": fact["id"], "owner": fact["owner"], "timestamp": ts,
            "expected_hex": (head + ts_field + tail).hex(),
            "prefix_hex": head.hex(), "suffix_hex": tail.hex(),
        },
    }


def main():
    stems = stem_vectors()
    ents = entity_vectors()
    lsh = lsh_vector()
    doc = {
        "meta": {
            "scheme": 1,
            "mnemonic": MNEMONIC,
            "note": "Public BIP-39 test mnemonic (abandon x11 about). Keys below derive from it; no real secret.",
            "encryption_key_hex": ENCRYPTION_KEY.hex(),
            "dedup_key_hex": DEDUP_KEY.hex(),
            "lsh_seed_hex": LSH_SEED.hex(),
            "labels": LABELS,
            "second_encryption_key_hex": bytes(range(32)).hex(),
        },
        "keys": {"index_key_hex": INDEX_KEY.hex(), "lsh_bucket_key_hex": LSH_BUCKET_KEY.hex(), "fp_key_v2_hex": FP_KEY_V2.hex()},
        "hkdf_rfc5869_case3": {"ikm_hex": RFC3_IKM.hex(), "okm_first32_hex": RFC3_OKM[:64]},
        "stem_vectors": stems,
        "entity_vectors": ents,
        "marker": {"type": "digest", "tag": tag("type:", "digest")},
        "separation": {"value": "digest", "stem": tag("stem:", "digest"), "entity": tag("entity:", "digest"), "type": tag("type:", "digest")},
        "lsh": lsh,
        "fp_v2": fp_vectors(),
        "wire": wire_vectors(),
        "legacy_sha256": {
            "tokens": sorted({hashlib.sha256(tok.encode()).hexdigest() for c in STEM_CASES for tok in c[2]}),
            "stems": sorted({hashlib.sha256(("stem:" + s).encode()).hexdigest() for c in STEM_CASES for s in c[3]}),
            "entities": sorted({hashlib.sha256(("entity:" + normalize_entity(n)).encode()).hexdigest() for n in ENTITY_NAMES}),
            "marker": hashlib.sha256(b"type:digest").hexdigest(),
            "lsh": lsh["legacy_sha256_tags"],
        },
    }
    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=True) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
