"""Generate test vectors from Python crypto for cross-language validation."""
import json
import sys
import hashlib
import math
import struct

from totalreclaw.crypto import (
    derive_keys_from_mnemonic,
    compute_auth_key_hash,
    derive_lsh_seed,
    encrypt,
    generate_blind_indices,
    generate_content_fingerprint,
)
from totalreclaw.lsh import LSHHasher

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def make_embedding(seed: int, dims: int) -> list[float]:
    """Deterministic embedding generator matching the TypeScript fixture generator."""
    vec = [0.0] * dims
    h = hashlib.sha256(f"embedding_{seed}".encode("utf-8")).digest()
    offset = 0

    for i in range(dims):
        if offset + 4 > len(h):
            h = hashlib.sha256(h).digest()
            offset = 0
        val = struct.unpack_from("<I", h, offset)[0]
        vec[i] = (val / 0xFFFFFFFF) * 2 - 1
        offset += 4

    norm = math.sqrt(sum(x * x for x in vec))
    for i in range(dims):
        vec[i] /= norm

    return vec


def main() -> None:
    keys = derive_keys_from_mnemonic(TEST_MNEMONIC)
    lsh_seed = derive_lsh_seed(TEST_MNEMONIC, keys.salt)

    test_text = "The quick brown fox jumps over the lazy dog"
    encrypted = encrypt(test_text, keys.encryption_key)

    embedding = make_embedding(42, 1024)
    hasher = LSHHasher(lsh_seed, 1024, 20, 32)
    buckets = hasher.hash(embedding)

    vectors = {
        "mnemonic": TEST_MNEMONIC,
        "source": "python",
        "derived": {
            "salt_hex": keys.salt.hex(),
            "auth_key_hex": keys.auth_key.hex(),
            "encryption_key_hex": keys.encryption_key.hex(),
            "dedup_key_hex": keys.dedup_key.hex(),
            "auth_key_hash_hex": compute_auth_key_hash(keys.auth_key),
            "lsh_seed_hex": lsh_seed.hex(),
        },
        "encryption": {
            "plaintext": test_text,
            "encrypted_base64": encrypted,
        },
        "blind_indices": {
            "input_text": test_text,
            "expected": generate_blind_indices(test_text),
        },
        "content_fingerprint": {
            "input_text": test_text,
            "expected_hex": generate_content_fingerprint(test_text, keys.dedup_key),
        },
        "lsh": {
            "embedding_seed": 42,
            "embedding_dims": 1024,
            "n_tables": 20,
            "n_bits": 32,
            "expected_buckets": buckets,
        },
    }
    json.dump(vectors, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
