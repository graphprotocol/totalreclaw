"""Unit tests for ``totalreclaw.bundle`` (Option E Phase 2, P2-5).

Thin-wrapper tests: round-trip through the PyO3 FFI, the §4.7 rejection
cases (mirroring bundle.rs's negative test table), and the redaction
guarantee — no 64-hex secret ever appears in ``repr()``/``str()``.

Cross-language byte-equality against the Rust/WASM implementations lives in
``python/tests/test_bundle_parity.py``; this file only tests the Python
wrapper's own behaviour (dataclass shape, redaction, error propagation).
"""
from __future__ import annotations

import pytest

totalreclaw_core = pytest.importorskip("totalreclaw_core")

pytestmark = pytest.mark.skipif(
    not hasattr(totalreclaw_core, "derive_bundle_from_mnemonic"),
    reason="installed totalreclaw_core predates the derived-bundle-v1 bindings (#581)",
)

from totalreclaw.bundle import (  # noqa: E402
    AccountRef,
    DerivedBundle,
    SigningMaterial,
    VaultKeys,
    derive_bundle_from_mnemonic,
    parse_bundle_v1,
    validate_bundle_v1,
)

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)
TEST_SMART_ACCOUNT = "0x2c0CF74B2b76110708CA431796367779e3738250"


@pytest.fixture
def bundle() -> DerivedBundle:
    return derive_bundle_from_mnemonic(TEST_MNEMONIC, 100, "local-migration", TEST_SMART_ACCOUNT)


# ---------------------------------------------------------------------------
# Shape + round-trip
# ---------------------------------------------------------------------------


def test_derive_bundle_from_mnemonic_returns_derived_bundle(bundle: DerivedBundle):
    assert isinstance(bundle, DerivedBundle)
    assert isinstance(bundle.vault, VaultKeys)
    assert isinstance(bundle.signing, SigningMaterial)
    assert isinstance(bundle.account, AccountRef)


def test_derive_bundle_vault_keys_are_32_byte_values(bundle: DerivedBundle):
    assert len(bundle.vault.encryption_key) == 32
    assert len(bundle.vault.dedup_key) == 32
    assert len(bundle.vault.auth_key) == 32
    assert len(bundle.vault.lsh_seed) == 32


def test_derive_bundle_signing_is_owner_eoa(bundle: DerivedBundle):
    assert bundle.signing.kind == "owner-eoa"
    assert len(bundle.signing.private_key) == 32
    assert bundle.signing.address.startswith("0x")
    assert bundle.signing.grant is None


def test_derive_bundle_account_matches_input(bundle: DerivedBundle):
    assert bundle.account.smart_account.lower() == TEST_SMART_ACCOUNT.lower()
    assert bundle.account.chain_id == 100


def test_derive_bundle_provisioned_by_matches_input(bundle: DerivedBundle):
    assert bundle.provisioned_by == "local-migration"


def test_parse_bundle_v1_reproduces_derived_bundle_fields(bundle: DerivedBundle):
    """Round-trip via totalreclaw_core.derive_bundle_from_mnemonic's raw JSON,
    proving parse_bundle_v1 and derive_bundle_from_mnemonic agree."""
    raw_json = totalreclaw_core.derive_bundle_from_mnemonic(
        TEST_MNEMONIC, 100, "local-migration", TEST_SMART_ACCOUNT
    )
    reparsed = parse_bundle_v1(raw_json)
    assert reparsed.vault == bundle.vault
    assert reparsed.signing == bundle.signing
    assert reparsed.account == bundle.account


def test_validate_bundle_v1_accepts_a_freshly_derived_bundle():
    raw_json = totalreclaw_core.derive_bundle_from_mnemonic(
        TEST_MNEMONIC, 100, "local-migration", TEST_SMART_ACCOUNT
    )
    validate_bundle_v1(raw_json)  # must not raise


# ---------------------------------------------------------------------------
# §4.7 rejection cases (through the FFI) — mirrors bundle.rs's negative table
# ---------------------------------------------------------------------------


def _raw_bundle_json() -> str:
    return totalreclaw_core.derive_bundle_from_mnemonic(
        TEST_MNEMONIC, 100, "local-migration", TEST_SMART_ACCOUNT
    )


def test_parse_bundle_v1_rejects_wrong_version():
    bad = _raw_bundle_json().replace('"version":2', '"version":3', 1)
    with pytest.raises(ValueError):
        parse_bundle_v1(bad)


def test_parse_bundle_v1_rejects_wrong_schema():
    bad = _raw_bundle_json().replace("derived-bundle-v1", "derived-bundle-v2", 1)
    with pytest.raises(ValueError):
        parse_bundle_v1(bad)


def test_parse_bundle_v1_rejects_unknown_signing_kind():
    bad = _raw_bundle_json().replace('"kind":"owner-eoa"', '"kind":"root-eoa"', 1)
    with pytest.raises(ValueError):
        parse_bundle_v1(bad)


def test_parse_bundle_v1_rejects_malformed_json():
    with pytest.raises(ValueError):
        parse_bundle_v1("not json")


def test_parse_bundle_v1_rejects_address_private_key_mismatch(bundle: DerivedBundle):
    bad = _raw_bundle_json().replace(
        bundle.signing.address, "0x000000000000000000000000000000000000dd", 1
    )
    with pytest.raises(ValueError):
        parse_bundle_v1(bad)


def test_validate_bundle_v1_rejects_wrong_version():
    bad = _raw_bundle_json().replace('"version":2', '"version":3', 1)
    with pytest.raises(ValueError):
        validate_bundle_v1(bad)


# ---------------------------------------------------------------------------
# Redaction — no 64-hex secret ever appears in repr()/str()
# ---------------------------------------------------------------------------


def test_repr_redacts_secrets(bundle: DerivedBundle):
    for form in (repr(bundle), str(bundle)):
        assert bundle.vault.encryption_key.hex() not in form
        assert bundle.vault.dedup_key.hex() not in form
        assert bundle.vault.auth_key.hex() not in form
        assert bundle.vault.lsh_seed.hex() not in form
        assert bundle.signing.private_key.hex() not in form
        # No bare 64-hex-char run at all (catches any secret leaking under
        # a field name this test doesn't explicitly enumerate).
        import re

        assert not re.search(r"\b[0-9a-f]{64}\b", form)


def test_vault_keys_repr_redacts_secrets(bundle: DerivedBundle):
    for form in (repr(bundle.vault), str(bundle.vault)):
        assert bundle.vault.encryption_key.hex() not in form
        assert "redacted" in form


def test_signing_material_repr_redacts_private_key_but_shows_address(bundle: DerivedBundle):
    form = repr(bundle.signing)
    assert bundle.signing.private_key.hex() not in form
    assert bundle.signing.address in form  # address is not secret
    assert bundle.signing.kind in form
