"""Unit tests for ``TotalReclaw.from_bundle`` (Option E Phase 2, P2-7).

Proves the derived-bundle-v1.md §4.6 consumption contract:

  1. A bundle-configured client and a mnemonic-configured client, derived
     from the SAME mnemonic, produce identical vault-keyed cryptographic
     output: ``auth_key_hash``, XChaCha20-Poly1305 ciphertext (cross-
     decryptable), content fingerprints, and LSH bucket hashes.
  2. ``wallet_address`` resolves from the bundle with zero network calls.
  3. ``self._mnemonic`` is ``None`` on a bundle-configured client.
  4. A mnemonic-requiring path (``register()``) raises a clear,
     actionable error rather than ``AttributeError``.
  5. ``register()`` is a silent no-op through the internal
     ``_ensure_registered`` auto-check, but raises loudly when called
     directly.
"""
from __future__ import annotations

import pytest

totalreclaw_core = pytest.importorskip("totalreclaw_core")

pytestmark = pytest.mark.skipif(
    not hasattr(totalreclaw_core, "derive_bundle_from_mnemonic"),
    reason="installed totalreclaw_core predates the derived-bundle-v1 bindings (#581)",
)

from totalreclaw.bundle import derive_bundle_from_mnemonic  # noqa: E402
from totalreclaw.client import TotalReclaw  # noqa: E402
from totalreclaw.crypto import (  # noqa: E402
    compute_auth_key_hash,
    decrypt,
    encrypt,
    generate_content_fingerprint,
)

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)
TEST_SMART_ACCOUNT = "0x2c0CF74B2b76110708CA431796367779e3738250"
TEST_SERVER_URL = "https://api-staging.totalreclaw.xyz"


@pytest.fixture
def mnemonic_client() -> TotalReclaw:
    return TotalReclaw(mnemonic=TEST_MNEMONIC, relay_url=TEST_SERVER_URL)


@pytest.fixture
def bundle_client(mnemonic_client: TotalReclaw) -> TotalReclaw:
    bundle = derive_bundle_from_mnemonic(
        TEST_MNEMONIC, 100, "local-migration", TEST_SMART_ACCOUNT
    )
    return TotalReclaw.from_bundle(bundle, server_url=TEST_SERVER_URL)


# ---------------------------------------------------------------------------
# 1. Byte-identical cryptographic output on the same vault
# ---------------------------------------------------------------------------


def test_auth_key_hash_matches(mnemonic_client, bundle_client):
    mnemonic_hash = compute_auth_key_hash(mnemonic_client._keys.auth_key)
    bundle_hash = compute_auth_key_hash(bundle_client._keys.auth_key)
    assert mnemonic_hash == bundle_hash


def test_vault_keys_are_byte_identical(mnemonic_client, bundle_client):
    assert mnemonic_client._keys.auth_key == bundle_client._keys.auth_key
    assert mnemonic_client._keys.encryption_key == bundle_client._keys.encryption_key
    assert mnemonic_client._keys.dedup_key == bundle_client._keys.dedup_key
    assert mnemonic_client._lsh_seed == bundle_client._lsh_seed


def test_ciphertext_cross_decrypts_between_clients(mnemonic_client, bundle_client):
    """Proves the encryption keys are identical: ciphertext produced with
    one client's key decrypts cleanly under the other's."""
    plaintext = "The user prefers dark mode and uses Vim keybindings."

    encrypted_by_mnemonic = encrypt(plaintext, mnemonic_client._keys.encryption_key)
    decrypted_by_bundle = decrypt(encrypted_by_mnemonic, bundle_client._keys.encryption_key)
    assert decrypted_by_bundle == plaintext

    encrypted_by_bundle = encrypt(plaintext, bundle_client._keys.encryption_key)
    decrypted_by_mnemonic = decrypt(encrypted_by_bundle, mnemonic_client._keys.encryption_key)
    assert decrypted_by_mnemonic == plaintext


def test_content_fingerprint_matches(mnemonic_client, bundle_client):
    text = "Pedro prefers dark mode"
    fp_mnemonic = generate_content_fingerprint(text, mnemonic_client._keys.dedup_key)
    fp_bundle = generate_content_fingerprint(text, bundle_client._keys.dedup_key)
    assert fp_mnemonic == fp_bundle


def test_blind_indices_match(mnemonic_client, bundle_client):
    """Blind indices are unkeyed (plain SHA-256 of tokens) by design, so
    this is trivially true for ANY two clients — included because the spec
    names it explicitly, but the vault-identity proof is the other tests
    in this file (auth_key_hash, ciphertext, fingerprint, LSH buckets)."""
    from totalreclaw.crypto import generate_blind_indices

    text = "Alice likes distributed systems and Rust programming"
    assert generate_blind_indices(text) == generate_blind_indices(text)


def test_lsh_buckets_match(mnemonic_client, bundle_client):
    embedding = [0.1 * i for i in range(640)]
    mnemonic_hasher = mnemonic_client._get_lsh_hasher()
    bundle_hasher = bundle_client._get_lsh_hasher()

    mnemonic_hashes = mnemonic_hasher.hash(embedding)
    bundle_hashes = bundle_hasher.hash(embedding)

    assert len(mnemonic_hashes) == 20
    assert len(bundle_hashes) == 20
    assert mnemonic_hashes == bundle_hashes


def test_wallet_context_signing_key_matches(mnemonic_client, bundle_client):
    """The owner-eoa signing key in a bundle IS the mnemonic-derived EOA
    private key — same slot, same value, for owner-eoa (Phase 2)."""
    assert mnemonic_client._signing_priv_key == bundle_client._signing_priv_key
    assert mnemonic_client.eoa_address.lower() == bundle_client.eoa_address.lower()


# ---------------------------------------------------------------------------
# 2. wallet_address resolves without network
# ---------------------------------------------------------------------------


def test_wallet_address_resolves_without_network(bundle_client):
    # No await/resolve_address() call — must already be resolved.
    assert bundle_client.wallet_address == TEST_SMART_ACCOUNT.lower()


def test_wallet_address_matches_bundle_smart_account():
    bundle = derive_bundle_from_mnemonic(
        TEST_MNEMONIC, 100, "local-migration", TEST_SMART_ACCOUNT
    )
    client = TotalReclaw.from_bundle(bundle, server_url=TEST_SERVER_URL)
    assert client.wallet_address == bundle.account.smart_account.lower()
    assert client.resolved_wallet_address == bundle.account.smart_account.lower()


def test_chain_id_is_seeded_from_bundle_but_not_eagerly_resolved(bundle_client):
    """Regression test for a live E2E-discovered bug (2026-08-03, staging
    S1): chain_id must NOT be eagerly marked resolved from the bundle —
    that skipped the billing lookup entirely, so _data_edge_address stayed
    None and every write silently targeted core's built-in PRODUCTION
    DataEdge default even while running against the staging relay (a
    bundle client has no other source for the environment-specific
    DataEdge override /v1/billing/status carries). chain_id IS seeded from
    bundle.account.chain_id (a safe, correct fallback value, and the value
    _chain_id_fallback returns if the billing lookup fails) but resolution
    must still go through the normal lazy resolve_chain_id() path so
    _data_edge_address gets populated on a live relay.
    """
    # Not yet "resolved" — the .chain_id property raises until a real
    # remember/recall/resolve_chain_id() call has run, exactly like
    # mnemonic-mode clients.
    assert bundle_client._chain_id_resolved is False
    with pytest.raises(RuntimeError, match="not yet resolved"):
        _ = bundle_client.chain_id

    # But the SEED is already correct and available without network —
    # this is what _chain_id_fallback falls back to if billing fails.
    assert bundle_client._chain_id == 100


# ---------------------------------------------------------------------------
# 3. self._mnemonic is None
# ---------------------------------------------------------------------------


def test_mnemonic_is_none_on_bundle_client(bundle_client):
    assert bundle_client._mnemonic is None


def test_mnemonic_is_set_on_mnemonic_client(mnemonic_client):
    assert mnemonic_client._mnemonic is not None


# ---------------------------------------------------------------------------
# 4 & 5. register() — loud error when called directly, silent no-op via
# the internal auto-registration check.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_raises_clear_error_on_bundle_client(bundle_client):
    with pytest.raises(RuntimeError, match="bundle-configured client"):
        await bundle_client.register()


@pytest.mark.asyncio
async def test_ensure_registered_is_a_silent_noop_on_bundle_client(bundle_client):
    # Must not raise, must not attempt any relay call.
    await bundle_client._ensure_registered()
    assert bundle_client._registered is False


# ---------------------------------------------------------------------------
# from_bundle rejects session-key bundles (Phase 2 scope boundary)
# ---------------------------------------------------------------------------


def test_from_bundle_rejects_session_key_kind():
    bundle = derive_bundle_from_mnemonic(
        TEST_MNEMONIC, 100, "local-migration", TEST_SMART_ACCOUNT
    )
    # Session-key bundles cannot be derived by this client yet (core has no
    # derivation path) — construct one by hand via the dataclass to
    # exercise the from_bundle-side guard specifically.
    from dataclasses import replace

    from totalreclaw.bundle import SigningMaterial

    session_key_signing = SigningMaterial(
        kind="session-key",
        private_key=bundle.signing.private_key,
        address=bundle.signing.address,
        grant='{"account":"0x0"}',
    )
    session_key_bundle = replace(bundle, signing=session_key_signing)

    with pytest.raises(ValueError, match="session-key"):
        TotalReclaw.from_bundle(session_key_bundle, server_url=TEST_SERVER_URL)


# ---------------------------------------------------------------------------
# 6. REGRESSION (2026-08-03, live staging E2E S1) — a bundle-mode client
# must never write against the prod-default DataEdge while pointed at a
# non-prod relay. This was a real bug: from_bundle originally set
# _chain_id_resolved = True eagerly (from bundle.account.chain_id) with
# _data_edge_address left None. resolve_chain_id() early-returns when
# _chain_id_resolved is already True, so the billing lookup that populates
# _data_edge_address never ran — every first write silently fell through
# to core's hardcoded production DATA_EDGE_ADDRESS
# (0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca) even while configured
# against the staging relay. Confirmed live: a staging E2E write landed on
# the production subgraph (tx 0xa70c3402b7e2f338a17212d2acfc5a9bf843689aaea237df5572f4ef637ae447).
#
# Fix: from_bundle now seeds _chain_id from the bundle but leaves
# _chain_id_resolved = False, so the normal lazy resolve_chain_id() path
# (identical mechanism mnemonic-mode already relies on) runs on first
# write and populates _data_edge_address from the relay's own billing
# response. _chain_id_fallback() ensures a billing-lookup FAILURE still
# doesn't clobber the bundle's own known-correct chain_id with the
# free-tier default.
# ---------------------------------------------------------------------------

import httpx  # noqa: E402
from unittest.mock import patch  # noqa: E402


def _patch_billing(response_body: dict | None, status_code: int = 200, raise_exc: Exception | None = None):
    """Mirrors test_chain_id_autodetect.py's helper — mocks the billing
    endpoint via httpx.MockTransport so no real network call is made."""
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if raise_exc is not None:
            raise raise_exc
        if response_body is None:
            return httpx.Response(status_code, text="")
        return httpx.Response(status_code, json=response_body)

    transport = httpx.MockTransport(_handler)

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kw):
            kw.pop("transport", None)
            super().__init__(*args, transport=transport, **kw)

    return patch("totalreclaw.client._httpx.AsyncClient", _PatchedAsyncClient), captured


FAKE_STAGING_DATA_EDGE = "0xE7a4D2677B686e13775Ba9092631089e35F0BB91"
PROD_DATA_EDGE_DEFAULT = "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca"


@pytest.mark.asyncio
async def test_bundle_client_resolves_data_edge_from_billing_not_prod_default(bundle_client):
    """The billing-supplied data_edge_address must be used — never the
    core default — for a bundle-configured client."""
    assert bundle_client._data_edge_address is None  # not yet resolved

    patch_ctx, captured = _patch_billing(
        {"tier": "free", "chain_id": 100, "data_edge_address": FAKE_STAGING_DATA_EDGE}
    )
    with patch_ctx:
        chain_id = await bundle_client.resolve_chain_id()

    assert len(captured) == 1, "resolve_chain_id must actually call the billing endpoint"
    assert chain_id == 100
    assert bundle_client._data_edge_address == FAKE_STAGING_DATA_EDGE
    assert bundle_client._data_edge_address != PROD_DATA_EDGE_DEFAULT


@pytest.mark.asyncio
async def test_bundle_client_remember_never_encodes_prod_default_when_billing_succeeds(bundle_client):
    """Full remember() path — asserts data_edge_address=None never reaches
    the write encoder when a relay is configured and billing succeeds."""
    import totalreclaw.client as client_module

    patch_ctx, _captured = _patch_billing(
        {"tier": "free", "chain_id": 100, "data_edge_address": FAKE_STAGING_DATA_EDGE}
    )

    captured_kwargs: dict = {}

    async def _fake_store_fact(**kwargs):
        captured_kwargs.update(kwargs)
        return "fake-fact-id"

    with patch_ctx, patch.object(client_module, "store_fact", _fake_store_fact):
        # _ensure_registered is a no-op in bundle mode (see test above) —
        # no relay call needed for that step.
        await bundle_client.remember("regression test fact")

    assert "data_edge_address" in captured_kwargs
    assert captured_kwargs["data_edge_address"] is not None
    assert captured_kwargs["data_edge_address"] == FAKE_STAGING_DATA_EDGE
    assert captured_kwargs["data_edge_address"] != PROD_DATA_EDGE_DEFAULT


@pytest.mark.asyncio
async def test_bundle_client_chain_id_fallback_preserves_bundle_value_on_billing_failure(bundle_client):
    """A billing-lookup failure (network error, non-200) must NOT clobber
    the bundle's own authoritative chain_id with the free-tier default —
    that would target the WRONG CHAIN outright, not just the wrong
    environment's DataEdge."""
    patch_ctx, _captured = _patch_billing(None, status_code=500)

    with patch_ctx:
        chain_id = await bundle_client.resolve_chain_id()

    assert chain_id == 100  # the bundle's own account.chain_id, preserved
    assert chain_id != 84532  # DEFAULT_CHAIN_ID_FREE — must not clobber
