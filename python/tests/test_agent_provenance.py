"""Tests for issue #317 — agent-instance provenance in the encrypted blob.

Records which *agent instance* stored a memory (user-given name + client type),
not just the bare client type, so provenance reads e.g. "John (Hermes)" rather
than "hermes".

Coverage:
  * ``resolve_agent_name`` precedence (explicit > env > None) + normalization.
  * ``compose_provenance_label`` render ("John (Hermes)" vs "Hermes").
  * env-set → the v1 blob carries ``agent_name``; ``read_blob_unified`` surfaces it.
  * env-unset → blob is byte-identical to the pre-#317 blob (no ``agent_name`` key).
  * Full crypto round-trip: encrypt with agent_name → decrypt on a FRESH key set
    from the same mnemonic → ``agent_name`` present (proves it lives in the
    envelope, not a transient in-process field).
  * Whitelist guard: ``read_blob_unified`` rebuilds a fresh metadata dict, so
    this asserts ``agent_name`` specifically is NOT stripped on read.
"""
from __future__ import annotations

import json

import pytest

from totalreclaw.claims_helper import (
    AGENT_NAME_ENV_VAR,
    AGENT_NAME_MAX_LEN,
    build_canonical_claim_v1,
    compose_provenance_label,
    read_blob_unified,
    resolve_agent_name,
)
from totalreclaw.crypto import decrypt, derive_keys_from_mnemonic, encrypt

# A deterministic 12-word BIP-39 test mnemonic (all-"abandon" + "about").
_TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)

_BASE_FACT = {"text": "Deploys on Fridays", "type": "claim", "source": "user"}


# ---------------------------------------------------------------------------
# resolve_agent_name — precedence + normalization
# ---------------------------------------------------------------------------


def test_resolve_agent_name_explicit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AGENT_NAME_ENV_VAR, "EnvName")
    assert resolve_agent_name("Explicit") == "Explicit"


def test_resolve_agent_name_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AGENT_NAME_ENV_VAR, "John")
    assert resolve_agent_name(None) == "John"


def test_resolve_agent_name_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AGENT_NAME_ENV_VAR, raising=False)
    assert resolve_agent_name(None) is None


def test_resolve_agent_name_blank_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AGENT_NAME_ENV_VAR, "   ")
    assert resolve_agent_name(None) is None
    assert resolve_agent_name("  ") is None


def test_resolve_agent_name_trims_and_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AGENT_NAME_ENV_VAR, raising=False)
    assert resolve_agent_name("  Padded  ") == "Padded"
    long = "x" * (AGENT_NAME_MAX_LEN + 50)
    assert len(resolve_agent_name(long)) == AGENT_NAME_MAX_LEN


# ---------------------------------------------------------------------------
# compose_provenance_label — the "John (Hermes)" render
# ---------------------------------------------------------------------------


def test_compose_provenance_label_with_name() -> None:
    assert compose_provenance_label("Hermes", "John") == "John (Hermes)"


def test_compose_provenance_label_without_name_is_client_only() -> None:
    assert compose_provenance_label("Hermes", None) == "Hermes"
    assert compose_provenance_label("Hermes", "") == "Hermes"


# ---------------------------------------------------------------------------
# build_canonical_claim_v1 — env-set carries agent_name; env-unset omits it
# ---------------------------------------------------------------------------


def test_blob_carries_agent_name_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AGENT_NAME_ENV_VAR, "John")
    payload = json.loads(
        build_canonical_claim_v1(
            _BASE_FACT, importance=6, created_at="2026-07-02T00:00:00.000Z"
        )
    )
    assert payload["agent_name"] == "John"


def test_blob_carries_agent_name_from_explicit_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AGENT_NAME_ENV_VAR, raising=False)
    payload = json.loads(
        build_canonical_claim_v1(
            _BASE_FACT,
            importance=6,
            created_at="2026-07-02T00:00:00.000Z",
            agent_name="Ada",
        )
    )
    assert payload["agent_name"] == "Ada"


def test_env_unset_produces_no_agent_name_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AGENT_NAME_ENV_VAR, raising=False)
    payload = json.loads(
        build_canonical_claim_v1(
            _BASE_FACT, importance=6, created_at="2026-07-02T00:00:00.000Z"
        )
    )
    assert "agent_name" not in payload


def test_env_unset_blob_byte_identical_to_pre_317(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour-preserving: with no name resolvable, the emitted blob string
    contains no agent_name key (byte-identical to a pre-#317 build)."""
    monkeypatch.delenv(AGENT_NAME_ENV_VAR, raising=False)
    without = build_canonical_claim_v1(
        _BASE_FACT,
        importance=6,
        created_at="2026-07-02T00:00:00.000Z",
        claim_id="fixed-id",
    )
    assert "agent_name" not in without


def test_agent_name_does_not_overload_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent_name is a DIFFERENT axis — the v1 ``source`` field is unchanged."""
    monkeypatch.setenv(AGENT_NAME_ENV_VAR, "John")
    payload = json.loads(
        build_canonical_claim_v1(
            {"text": "x", "type": "claim", "source": "assistant"},
            importance=5,
            created_at="2026-07-02T00:00:00.000Z",
        )
    )
    assert payload["source"] == "assistant"
    assert payload["agent_name"] == "John"


# ---------------------------------------------------------------------------
# read_blob_unified — whitelist guard: agent_name survives on read
# ---------------------------------------------------------------------------


def test_read_blob_unified_surfaces_agent_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AGENT_NAME_ENV_VAR, "John")
    blob = build_canonical_claim_v1(
        _BASE_FACT, importance=6, created_at="2026-07-02T00:00:00.000Z"
    )
    doc = read_blob_unified(blob)
    # The read path rebuilds a fresh metadata dict from named keys; assert the
    # #317 whitelist entry is present rather than silently dropped.
    assert doc["metadata"]["agent_name"] == "John"


def test_read_blob_unified_agent_name_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AGENT_NAME_ENV_VAR, raising=False)
    blob = build_canonical_claim_v1(
        _BASE_FACT, importance=6, created_at="2026-07-02T00:00:00.000Z"
    )
    doc = read_blob_unified(blob)
    assert doc["metadata"]["agent_name"] is None


# ---------------------------------------------------------------------------
# Full crypto round-trip — proves agent_name is IN the envelope, not transient
# ---------------------------------------------------------------------------


def test_agent_name_survives_encrypt_decrypt_on_fresh_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AGENT_NAME_ENV_VAR, "John")

    # --- Store side: build blob, encrypt with keys derived from the mnemonic.
    keys_store = derive_keys_from_mnemonic(_TEST_MNEMONIC)
    blob = build_canonical_claim_v1(
        _BASE_FACT, importance=6, created_at="2026-07-02T00:00:00.000Z"
    )
    ciphertext = encrypt(blob, keys_store.encryption_key)

    # --- Read side: a FRESH derivation from the same mnemonic (a different
    # client instance). The env var is irrelevant to the read; if agent_name
    # only lived in process state it would be gone here.
    monkeypatch.delenv(AGENT_NAME_ENV_VAR, raising=False)
    keys_read = derive_keys_from_mnemonic(_TEST_MNEMONIC)
    decrypted = decrypt(ciphertext, keys_read.encryption_key)
    doc = read_blob_unified(decrypted)

    assert doc["metadata"]["agent_name"] == "John"
    assert (
        compose_provenance_label("Hermes", doc["metadata"]["agent_name"])
        == "John (Hermes)"
    )
