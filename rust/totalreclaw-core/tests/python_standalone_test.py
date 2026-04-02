"""
Standalone tests for totalreclaw_core PyO3 module.

Tests the Rust module in isolation (no dependency on the Python totalreclaw package).
Uses the same test vectors from crypto_vectors.json for deterministic validation.
"""
import json
import os
import sys

import totalreclaw_core


# ---------------------------------------------------------------------------
# Load test vectors
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(FIXTURES_DIR, "crypto_vectors.json")) as f:
    VECTORS = json.load(f)


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_derive_keys_from_mnemonic():
    kd = VECTORS["key_derivation"]
    mnemonic = kd["mnemonic"]
    keys = totalreclaw_core.derive_keys_from_mnemonic(mnemonic)

    assert isinstance(keys, dict)
    assert isinstance(keys["salt"], bytes)
    assert isinstance(keys["auth_key"], bytes)
    assert isinstance(keys["encryption_key"], bytes)
    assert isinstance(keys["dedup_key"], bytes)

    assert keys["salt"].hex() == kd["salt_hex"]
    assert keys["auth_key"].hex() == kd["auth_key_hex"]
    assert keys["encryption_key"].hex() == kd["encryption_key_hex"]
    assert keys["dedup_key"].hex() == kd["dedup_key_hex"]


def test_derive_keys_from_mnemonic_lenient():
    """Lenient mode should produce same keys for a valid mnemonic."""
    kd = VECTORS["key_derivation"]
    mnemonic = kd["mnemonic"]
    keys = totalreclaw_core.derive_keys_from_mnemonic_lenient(mnemonic)
    assert keys["salt"].hex() == kd["salt_hex"]
    assert keys["auth_key"].hex() == kd["auth_key_hex"]


def test_lenient_accepts_bad_checksum():
    """Lenient should accept valid words with invalid checksum."""
    bad_checksum = "abandon " * 12  # all same word = bad checksum
    keys = totalreclaw_core.derive_keys_from_mnemonic_lenient(bad_checksum.strip())
    assert isinstance(keys["salt"], bytes)
    assert len(keys["salt"]) == 32


def test_strict_rejects_bad_checksum():
    """Strict mode should reject invalid checksum."""
    bad_checksum = "abandon " * 12
    try:
        totalreclaw_core.derive_keys_from_mnemonic(bad_checksum.strip())
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "mnemonic" in str(e).lower()


def test_lenient_rejects_invalid_words():
    """Lenient should reject words not in BIP-39 wordlist."""
    bad_words = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon xyzzy"
    try:
        totalreclaw_core.derive_keys_from_mnemonic_lenient(bad_words)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "xyzzy" in str(e)


def test_compute_auth_key_hash():
    kd = VECTORS["key_derivation"]
    auth_key = bytes.fromhex(kd["auth_key_hex"])
    result = totalreclaw_core.compute_auth_key_hash(auth_key)
    assert result == kd["auth_key_hash"]


def test_derive_lsh_seed():
    kd = VECTORS["key_derivation"]
    mnemonic = kd["mnemonic"]
    salt = bytes.fromhex(kd["salt_hex"])
    lsh_seed = totalreclaw_core.derive_lsh_seed(mnemonic, salt)
    assert isinstance(lsh_seed, bytes)
    assert len(lsh_seed) == 32
    assert lsh_seed.hex() == VECTORS["lsh"]["lsh_seed_hex"]


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


def test_aes_gcm_fixed_iv():
    aes = VECTORS["aes_gcm"]
    enc_key = bytes.fromhex(aes["encryption_key_hex"])

    # We can't set the IV from Python, but we can verify round-trip
    plaintext = aes["plaintext"]
    encrypted = totalreclaw_core.encrypt(plaintext, enc_key)
    decrypted = totalreclaw_core.decrypt(encrypted, enc_key)
    assert decrypted == plaintext


def test_decrypt_known_vector():
    """Decrypt a known test vector (fixed IV from Rust tests)."""
    aes = VECTORS["aes_gcm"]
    enc_key = bytes.fromhex(aes["encryption_key_hex"])
    expected_b64 = aes["fixed_iv_encrypted_base64"]
    decrypted = totalreclaw_core.decrypt(expected_b64, enc_key)
    assert decrypted == aes["plaintext"]


def test_encrypt_decrypt_round_trip():
    keys = totalreclaw_core.derive_keys_from_mnemonic(VECTORS["key_derivation"]["mnemonic"])
    plaintext = "Hello from PyO3 bindings!"
    encrypted = totalreclaw_core.encrypt(plaintext, keys["encryption_key"])
    decrypted = totalreclaw_core.decrypt(encrypted, keys["encryption_key"])
    assert decrypted == plaintext


def test_encrypt_decrypt_unicode():
    keys = totalreclaw_core.derive_keys_from_mnemonic(VECTORS["key_derivation"]["mnemonic"])
    plaintext = "Cafe\u0301 latte\u2014cre\u0300me bru\u0302le\u0301e \U0001f370"
    encrypted = totalreclaw_core.encrypt(plaintext, keys["encryption_key"])
    decrypted = totalreclaw_core.decrypt(encrypted, keys["encryption_key"])
    assert decrypted == plaintext


# ---------------------------------------------------------------------------
# Blind indices
# ---------------------------------------------------------------------------


def test_blind_indices():
    for tc in VECTORS["blind_indices"]["test_cases"]:
        result = totalreclaw_core.generate_blind_indices(tc["text"])
        assert result == tc["indices"], f"Blind indices mismatch for: {tc['text']}"


def test_blind_indices_returns_list_of_strings():
    result = totalreclaw_core.generate_blind_indices("hello world test")
    assert isinstance(result, list)
    assert all(isinstance(h, str) for h in result)
    assert all(len(h) == 64 for h in result)  # SHA-256 hex = 64 chars


# ---------------------------------------------------------------------------
# Content fingerprint
# ---------------------------------------------------------------------------


def test_content_fingerprint():
    fp = VECTORS["content_fingerprint"]
    dedup_key = bytes.fromhex(fp["dedup_key_hex"])
    for tc in fp["test_cases"]:
        result = totalreclaw_core.generate_content_fingerprint(tc["text"], dedup_key)
        assert result == tc["fingerprint"], f"Fingerprint mismatch for: {tc['text']}"


def test_normalize_text():
    assert totalreclaw_core.normalize_text("  Hello   World  ") == "hello world"
    assert totalreclaw_core.normalize_text("UPPERCASE") == "uppercase"
    # Whitespace collapse
    assert totalreclaw_core.normalize_text("a\t\nb") == "a b"


def test_fingerprint_whitespace_invariance():
    dedup_key = b"\x00" * 32
    fp1 = totalreclaw_core.generate_content_fingerprint("hello  world", dedup_key)
    fp2 = totalreclaw_core.generate_content_fingerprint("  hello   world  ", dedup_key)
    assert fp1 == fp2


# ---------------------------------------------------------------------------
# LSH Hasher
# ---------------------------------------------------------------------------


def test_lsh_small_hashes():
    small = VECTORS["lsh"]["small"]
    seed = bytes.fromhex(VECTORS["lsh"]["lsh_seed_hex"])
    hasher = totalreclaw_core.LshHasher(
        seed, small["dims"], small["n_tables"], small["n_bits"]
    )
    hashes = hasher.hash(small["embedding"])
    assert hashes == small["hashes"]


def test_lsh_real_hashes():
    real = VECTORS["lsh"]["real"]
    seed = bytes.fromhex(VECTORS["lsh"]["lsh_seed_hex"])
    dims = real["dims"]

    # Reconstruct embedding: sin(i * 0.1) * 0.5 for i in 0..1024
    import math
    embedding = [math.sin(i * 0.1) * 0.5 for i in range(dims)]

    hasher = totalreclaw_core.LshHasher(
        seed, dims, real["n_tables"], real["n_bits"]
    )
    hashes = hasher.hash(embedding)
    assert hashes == real["hashes"]


def test_lsh_default_params():
    seed = bytes.fromhex(VECTORS["lsh"]["lsh_seed_hex"])
    hasher = totalreclaw_core.LshHasher(seed, 1024)
    assert hasher.tables == 20
    assert hasher.bits == 32
    assert hasher.dimensions == 1024


def test_lsh_dimension_mismatch():
    seed = bytes.fromhex(VECTORS["lsh"]["lsh_seed_hex"])
    hasher = totalreclaw_core.LshHasher(seed, 4, 2, 2)
    try:
        hasher.hash([1.0, 2.0])  # wrong dimension
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "mismatch" in str(e).lower()


# ---------------------------------------------------------------------------
# Protobuf
# ---------------------------------------------------------------------------


def test_encode_fact_protobuf():
    payload = json.dumps({
        "id": "test-id",
        "timestamp": "2026-01-01T00:00:00Z",
        "owner": "0xABCD",
        "encrypted_blob_hex": "deadbeef",
        "blind_indices": ["hash1", "hash2"],
        "decay_score": 0.8,
        "source": "zeroclaw_fact",
        "content_fp": "fp123",
        "agent_id": "zeroclaw",
    })
    result = totalreclaw_core.encode_fact_protobuf(payload)
    assert isinstance(result, bytes)
    assert len(result) > 0
    assert b"test-id" in result


def test_encode_tombstone_protobuf():
    result = totalreclaw_core.encode_tombstone_protobuf("fact-123", "0xOwner")
    assert isinstance(result, bytes)
    assert len(result) > 0
    assert b"fact-123" in result
    assert b"0xOwner" in result


# ---------------------------------------------------------------------------
# Debrief
# ---------------------------------------------------------------------------


def test_parse_debrief_response_valid():
    response = json.dumps([
        {"text": "Session was about refactoring the auth module", "type": "summary", "importance": 8},
        {"text": "Migration to new API is still pending", "type": "context", "importance": 7},
    ])
    result = totalreclaw_core.parse_debrief_response(response)
    assert len(result) == 2
    assert result[0]["type"] == "summary"
    assert result[0]["importance"] == 8
    assert result[1]["type"] == "context"


def test_parse_debrief_response_empty():
    result = totalreclaw_core.parse_debrief_response("[]")
    assert result == []


def test_parse_debrief_response_invalid():
    result = totalreclaw_core.parse_debrief_response("not json at all")
    assert result == []


def test_parse_debrief_filters_low_importance():
    response = json.dumps([
        {"text": "Important item that passes the threshold", "type": "summary", "importance": 8},
        {"text": "Low importance item that gets filtered out", "type": "context", "importance": 3},
    ])
    result = totalreclaw_core.parse_debrief_response(response)
    assert len(result) == 1
    assert result[0]["importance"] == 8


def test_parse_debrief_caps_at_5():
    items = [{"text": f"Debrief item number {i} with enough text", "type": "summary", "importance": 7} for i in range(8)]
    result = totalreclaw_core.parse_debrief_response(json.dumps(items))
    assert len(result) == 5


def test_parse_debrief_strips_code_fences():
    response = '```json\n[{"text": "Session summary here with enough text", "type": "summary", "importance": 8}]\n```'
    result = totalreclaw_core.parse_debrief_response(response)
    assert len(result) == 1


def test_parse_debrief_defaults_importance():
    response = json.dumps([{"text": "Item without importance score set", "type": "summary"}])
    result = totalreclaw_core.parse_debrief_response(response)
    assert len(result) == 1
    assert result[0]["importance"] == 7


def test_parse_debrief_defaults_type_to_context():
    response = json.dumps([{"text": "Item with invalid type value here", "type": "fact", "importance": 7}])
    result = totalreclaw_core.parse_debrief_response(response)
    assert len(result) == 1
    assert result[0]["type"] == "context"


def test_get_debrief_system_prompt():
    prompt = totalreclaw_core.get_debrief_system_prompt()
    assert isinstance(prompt, str)
    assert "already_stored_facts" in prompt
    assert "Broader context" in prompt
    assert "Maximum 5 items" in prompt


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
