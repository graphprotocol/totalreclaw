"""
Parity tests: totalreclaw_core (Rust/PyO3) vs totalreclaw (Python).

Runs both implementations against the same inputs and compares outputs
byte-for-byte. Requires both packages installed in the same environment.

Usage:
    cd rust/totalreclaw-core
    PYTHONPATH=../../python/src python tests/python_parity_test.py
"""
import json
import math
import os
import sys

# Rust implementation (PyO3)
import totalreclaw_core

# Python implementation
from totalreclaw.crypto import (
    DerivedKeys,
    compute_auth_key_hash as py_compute_auth_key_hash,
    decrypt as py_decrypt,
    derive_keys_from_mnemonic as py_derive_keys,
    derive_lsh_seed as py_derive_lsh_seed,
    encrypt as py_encrypt,
    generate_blind_indices as py_blind_indices,
    generate_content_fingerprint as py_fingerprint,
)
from totalreclaw.lsh import LSHHasher as PyLSHHasher
from totalreclaw.hermes.debrief import (
    parse_debrief_response as py_parse_debrief,
)

# ---------------------------------------------------------------------------
# Load test vectors
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(FIXTURES_DIR, "crypto_vectors.json")) as f:
    VECTORS = json.load(f)

MNEMONIC = VECTORS["key_derivation"]["mnemonic"]


# ---------------------------------------------------------------------------
# Key derivation parity
# ---------------------------------------------------------------------------


def test_key_derivation_parity():
    """Same mnemonic produces identical keys from both implementations."""
    rust_keys = totalreclaw_core.derive_keys_from_mnemonic(MNEMONIC)
    py_keys = py_derive_keys(MNEMONIC)

    assert rust_keys["salt"] == py_keys.salt, "salt mismatch"
    assert rust_keys["auth_key"] == py_keys.auth_key, "auth_key mismatch"
    assert rust_keys["encryption_key"] == py_keys.encryption_key, "encryption_key mismatch"
    assert rust_keys["dedup_key"] == py_keys.dedup_key, "dedup_key mismatch"


def test_auth_key_hash_parity():
    """compute_auth_key_hash matches between Rust and Python."""
    rust_keys = totalreclaw_core.derive_keys_from_mnemonic(MNEMONIC)
    py_keys = py_derive_keys(MNEMONIC)

    rust_hash = totalreclaw_core.compute_auth_key_hash(rust_keys["auth_key"])
    py_hash = py_compute_auth_key_hash(py_keys.auth_key)
    assert rust_hash == py_hash, f"auth_key_hash mismatch: {rust_hash} != {py_hash}"


def test_lsh_seed_parity():
    """derive_lsh_seed matches between Rust and Python."""
    rust_keys = totalreclaw_core.derive_keys_from_mnemonic(MNEMONIC)
    py_keys = py_derive_keys(MNEMONIC)

    rust_lsh_seed = totalreclaw_core.derive_lsh_seed(MNEMONIC, rust_keys["salt"])
    py_lsh_seed = py_derive_lsh_seed(MNEMONIC, py_keys.salt)
    assert rust_lsh_seed == py_lsh_seed, "LSH seed mismatch"


# ---------------------------------------------------------------------------
# Cross-implementation encryption parity
# ---------------------------------------------------------------------------


def test_encrypt_rust_decrypt_python():
    """Encrypt with Rust, decrypt with Python."""
    rust_keys = totalreclaw_core.derive_keys_from_mnemonic(MNEMONIC)
    plaintext = "Cross-implementation test: Rust -> Python"
    encrypted = totalreclaw_core.encrypt(plaintext, rust_keys["encryption_key"])
    decrypted = py_decrypt(encrypted, rust_keys["encryption_key"])
    assert decrypted == plaintext, f"Rust->Python decrypt failed: {decrypted}"


def test_encrypt_python_decrypt_rust():
    """Encrypt with Python, decrypt with Rust."""
    py_keys = py_derive_keys(MNEMONIC)
    plaintext = "Cross-implementation test: Python -> Rust"
    encrypted = py_encrypt(plaintext, py_keys.encryption_key)
    decrypted = totalreclaw_core.decrypt(encrypted, py_keys.encryption_key)
    assert decrypted == plaintext, f"Python->Rust decrypt failed: {decrypted}"


def test_cross_encryption_unicode():
    """Cross-implementation encryption with Unicode text."""
    keys = totalreclaw_core.derive_keys_from_mnemonic(MNEMONIC)
    texts = [
        "Caf\u00e9 latte",
        "\u4f60\u597d\u4e16\u754c",
        "\U0001f680 Rocket science \U0001f30d",
        "\u00e9\u00e8\u00ea\u00eb \u00fc\u00f6\u00e4",
    ]
    for text in texts:
        # Rust -> Python
        enc_rust = totalreclaw_core.encrypt(text, keys["encryption_key"])
        dec_py = py_decrypt(enc_rust, keys["encryption_key"])
        assert dec_py == text, f"Rust->Python failed for: {text}"

        # Python -> Rust
        enc_py = py_encrypt(text, keys["encryption_key"])
        dec_rust = totalreclaw_core.decrypt(enc_py, keys["encryption_key"])
        assert dec_rust == text, f"Python->Rust failed for: {text}"


# ---------------------------------------------------------------------------
# Blind indices parity
# ---------------------------------------------------------------------------


def test_blind_indices_parity():
    """Same text produces identical blind indices from both implementations."""
    for tc in VECTORS["blind_indices"]["test_cases"]:
        text = tc["text"]
        rust_indices = totalreclaw_core.generate_blind_indices(text)
        py_indices = py_blind_indices(text)
        assert rust_indices == py_indices, (
            f"Blind indices mismatch for: {text}\n"
            f"  Rust: {rust_indices}\n"
            f"  Python: {py_indices}"
        )


def test_blind_indices_parity_extra():
    """Additional blind indices parity tests with various inputs.

    NOTE: Rust uses Porter 1 stemmer (matching TypeScript canonical),
    Python uses Snowball (Porter 2). Some words stem differently, e.g.
    Porter 1: monday -> mondai, Porter 2: monday -> monday.

    For words that stem identically under both algorithms, the indices
    must be byte-for-byte identical. For the known divergent cases,
    we verify that the Python set is a subset of the Rust set (Rust
    may produce extra stem hashes that Snowball doesn't).
    """
    # These texts have no Porter1 vs Snowball divergence
    exact_match_texts = [
        "The user prefers VSCode over Vim",
        "API endpoint: /v1/users/create",
        "UPPERCASE TEXT WITH NUMBERS 123",
    ]
    for text in exact_match_texts:
        rust_indices = totalreclaw_core.generate_blind_indices(text)
        py_indices = py_blind_indices(text)
        assert rust_indices == py_indices, f"Blind indices mismatch for: {text}"

    # These texts may have Porter1 vs Snowball stem divergence.
    # Python indices must be a subset of Rust indices (Rust=canonical).
    subset_texts = [
        "Meeting at 3pm on Monday",
        "caf\u00e9 \u00e9clair",
    ]
    for text in subset_texts:
        rust_indices = totalreclaw_core.generate_blind_indices(text)
        py_indices = py_blind_indices(text)
        rust_set = set(rust_indices)
        py_set = set(py_indices)
        assert py_set.issubset(rust_set), (
            f"Python indices not subset of Rust for: {text}\n"
            f"  Extra in Python: {py_set - rust_set}"
        )


# ---------------------------------------------------------------------------
# Content fingerprint parity
# ---------------------------------------------------------------------------


def test_content_fingerprint_parity():
    """Same text and key produce identical fingerprints from both implementations."""
    fp = VECTORS["content_fingerprint"]
    dedup_key = bytes.fromhex(fp["dedup_key_hex"])
    for tc in fp["test_cases"]:
        text = tc["text"]
        rust_fp = totalreclaw_core.generate_content_fingerprint(text, dedup_key)
        py_fp = py_fingerprint(text, dedup_key)
        assert rust_fp == py_fp, f"Fingerprint mismatch for: {text}\n  Rust: {rust_fp}\n  Python: {py_fp}"


def test_content_fingerprint_parity_extra():
    """Additional fingerprint parity tests."""
    keys = totalreclaw_core.derive_keys_from_mnemonic(MNEMONIC)
    texts = [
        "Hello, World!",
        "  extra   whitespace   ",
        "caf\u00e9 br\u00fbl\u00e9e",
        "a\t\nb\n\nc",
    ]
    for text in texts:
        rust_fp = totalreclaw_core.generate_content_fingerprint(text, keys["dedup_key"])
        py_fp = py_fingerprint(text, keys["dedup_key"])
        assert rust_fp == py_fp, f"Fingerprint mismatch for: {repr(text)}"


# ---------------------------------------------------------------------------
# LSH parity
# ---------------------------------------------------------------------------


def test_lsh_small_parity():
    """Small-dimension LSH hashes match between Rust and Python."""
    small = VECTORS["lsh"]["small"]
    seed = bytes.fromhex(VECTORS["lsh"]["lsh_seed_hex"])

    rust_hasher = totalreclaw_core.LshHasher(
        seed, small["dims"], small["n_tables"], small["n_bits"]
    )
    py_hasher = PyLSHHasher(
        seed, small["dims"], small["n_tables"], small["n_bits"]
    )

    embedding = small["embedding"]
    rust_hashes = rust_hasher.hash(embedding)
    py_hashes = py_hasher.hash(embedding)
    assert rust_hashes == py_hashes, "LSH small hashes mismatch"


def test_lsh_real_1024d_parity():
    """Real-size 1024-dimension LSH hashes match between Rust and Python."""
    real = VECTORS["lsh"]["real"]
    seed = bytes.fromhex(VECTORS["lsh"]["lsh_seed_hex"])
    dims = real["dims"]

    embedding = [math.sin(i * 0.1) * 0.5 for i in range(dims)]

    rust_hasher = totalreclaw_core.LshHasher(
        seed, dims, real["n_tables"], real["n_bits"]
    )
    py_hasher = PyLSHHasher(seed, dims, real["n_tables"], real["n_bits"])

    rust_hashes = rust_hasher.hash(embedding)
    py_hashes = py_hasher.hash(embedding)
    assert rust_hashes == py_hashes, "LSH 1024d hashes mismatch"


# ---------------------------------------------------------------------------
# Debrief parity
# ---------------------------------------------------------------------------


def test_debrief_parse_parity():
    """parse_debrief_response matches between Rust and Python."""
    test_cases = [
        # Valid JSON
        json.dumps([
            {"text": "Session was about refactoring the auth module", "type": "summary", "importance": 8},
            {"text": "Migration to new API is still pending", "type": "context", "importance": 7},
        ]),
        # Empty array
        "[]",
        # Invalid JSON
        "not json",
        # With code fences
        '```json\n[{"text": "Summary of session work done today", "type": "summary", "importance": 8}]\n```',
        # Low importance filtered
        json.dumps([
            {"text": "Important item passes filter threshold", "type": "summary", "importance": 8},
            {"text": "Low importance item gets filtered out", "type": "context", "importance": 3},
        ]),
        # Default importance
        json.dumps([{"text": "Item without importance score value", "type": "summary"}]),
        # Invalid type defaults to context
        json.dumps([{"text": "Item with invalid type value here", "type": "fact", "importance": 7}]),
    ]

    for response in test_cases:
        rust_items = totalreclaw_core.parse_debrief_response(response)
        py_items = py_parse_debrief(response)

        assert len(rust_items) == len(py_items), (
            f"Debrief count mismatch for: {response}\n"
            f"  Rust: {len(rust_items)}, Python: {len(py_items)}"
        )

        for i, (r, p) in enumerate(zip(rust_items, py_items)):
            assert r["text"] == p.text, f"Debrief[{i}] text mismatch"
            assert r["type"] == p.type, f"Debrief[{i}] type mismatch"
            assert r["importance"] == p.importance, f"Debrief[{i}] importance mismatch"


def test_debrief_prompt_parity():
    """Debrief system prompt is identical between Rust and Python."""
    from totalreclaw.hermes.debrief import DEBRIEF_SYSTEM_PROMPT as PY_PROMPT
    rust_prompt = totalreclaw_core.get_debrief_system_prompt()
    assert rust_prompt == PY_PROMPT, "Debrief system prompt mismatch"


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for fn in test_funcs:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")

    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    sys.exit(1 if failed else 0)
