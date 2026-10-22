"""Constants and helpers shared by the enclave tests (import as ``tests.support``)."""

from __future__ import annotations

from pathlib import Path

T0 = 1_790_000_000  # 2026-09-21T13:33:20Z
VAULT_A = "0x" + "a1" * 20
VAULT_B = "0x" + "b2" * 20

# Synthetic secrets for the no-plaintext tests. FAKE_PHRASE is the public
# BIP-39 test vector already used by the repo's fixtures; FAKE_TOKEN and
# MEMORY_TEXT are invented for these tests and match no real credential.
FAKE_PHRASE = " ".join(["abandon"] * 11 + ["about"])
FAKE_TOKEN = "trat_Zx9Qk2Lm7Np4Rs8Tv1Wy3Ab5Cd6Ef0GhJk"
MEMORY_TEXT = "I am allergic to penicillin and my locker combination is kept at home"
# Substrings that must never appear in any log line or audit row.
SENTINELS = ("abandon", "Zx9Qk2Lm7Np4", "penicillin", "locker")


def staging_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "ENCLAVE_ENV": "staging",
        "ENCLAVE_PUBLIC_URL": "https://enclave-staging.totalreclaw.xyz",
        "ENCLAVE_DB_PATH": str(tmp_path / "staging.sqlite3"),
        "ENCLAVE_NEAR_CONTRACT": "enclave-staging.totalreclaw.testnet",
        "ENCLAVE_MEASUREMENT_ID": "m-2026-09-27",
        "ENCLAVE_COMPOSE_HASH": "c" * 64,
        "ENCLAVE_OS_IMAGE_HASH": "d" * 64,
        "ENCLAVE_INFERENCE_POLICY_VERSION": "p-1",
    }
    env.update(overrides)
    return env


def prod_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = staging_env(
        tmp_path,
        ENCLAVE_ENV="prod",
        ENCLAVE_PUBLIC_URL="https://enclave.totalreclaw.xyz",
        ENCLAVE_NEAR_CONTRACT="enclave.totalreclaw.near",
    )
    env.update(overrides)
    return env
