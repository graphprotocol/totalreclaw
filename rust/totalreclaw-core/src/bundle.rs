//! Derived-material bundle v1 — the cross-client Phase 2 credential contract.
//!
//! Contract: `docs/specs/totalreclaw/client-consistency.md` (public, "Credential
//! Material" section) — the full design rationale lives in the internal repo's
//! `docs/specs/cred/derived-bundle-v1.md`.
//!
//! A `derived-bundle-v1` is what an agent host holds *instead of* the BIP-39
//! recovery phrase: the four HKDF-derived vault-global working keys plus a
//! signing key, but never the phrase or the 64-byte seed that reproduces it.
//!
//! **No new cryptography.** `derive_bundle_from_mnemonic` is a *composition* of
//! [`crypto::derive_keys_from_mnemonic`], [`crypto::derive_lsh_seed`], and
//! [`wallet::derive_eoa`] — the exact same derivation every client already
//! performs. [`test_bundle_vault_matches_legacy_derivation`] proves this
//! byte-for-byte on the canonical test vector; if you find yourself writing a
//! new HKDF call with a new info string here, stop — that silently orphans
//! every existing vault.
//!
//! **The bundle never carries the mnemonic or the 64-byte seed.**
//! [`DerivedBundleV1`] has no such field, and
//! [`test_bundle_json_contains_no_root_material`] asserts the serialised form
//! contains neither for the canonical vector.
//!
//! **The HKDF salt is deliberately not carried either** — once the four vault
//! keys exist there is nothing left to derive from it. See derived-bundle-v1.md
//! §4.5 for the full "why", including a retracted-and-recorded earlier argument.
//!
//! # Smart Account address — a deliberate deviation from the design doc
//!
//! The design doc's public-surface sketch shows
//! `derive_bundle_from_mnemonic(mnemonic, chain_id, provisioned_by)` with no
//! `smart_account` parameter, on the assumption core might already have a
//! CREATE2 helper. It does not: the CREATE2 Smart Account address is derived
//! today via an `eth_call` RPC round-trip to `SimpleAccountFactory.getAddress`
//! (see `python/src/totalreclaw/client.py::_derive_smart_account_address`),
//! which is network I/O and therefore cannot live in this crate (pure
//! computation only). Per the design doc's own explicit fallback ("if core has
//! no CREATE2 helper today, `smart_account` becomes a required argument..."),
//! this module takes `smart_account: &str` as a required parameter rather than
//! inventing a second CREATE2 implementation.

use serde::{Deserialize, Serialize};
use tiny_keccak::{Hasher, Keccak};

use crate::{crypto, wallet, Error, Result};

// ---------------------------------------------------------------------------
// Hex(32) — the wire format for every 32-byte secret in the bundle.
//
// Lowercase, no `0x` prefix, exactly 64 chars. This is the ONLY parser for
// bundle key material; both `serde(with = "hex32")` fields below and manual
// validation route through `parse_hex32` so there is one rejection path.
// ---------------------------------------------------------------------------

fn parse_hex32(s: &str) -> Result<[u8; 32]> {
    if s.len() != 64 {
        return Err(Error::Parse(format!(
            "expected exactly 64 lowercase hex chars, got {} chars",
            s.len()
        )));
    }
    if !s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        return Err(Error::Parse(
            "key material must be lowercase hex with no 0x prefix".into(),
        ));
    }
    let decoded = hex::decode(s).map_err(|e| Error::Parse(format!("invalid hex: {e}")))?;
    let mut out = [0u8; 32];
    out.copy_from_slice(&decoded);
    Ok(out)
}

mod hex32 {
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(bytes: &[u8; 32], s: S) -> std::result::Result<S::Ok, S::Error> {
        s.serialize_str(&hex::encode(bytes))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<[u8; 32], D::Error> {
        let s = String::deserialize(d)?;
        super::parse_hex32(&s).map_err(serde::de::Error::custom)
    }
}

// ---------------------------------------------------------------------------
// EIP-55 checksum casing.
//
// `signing.address` and `account.smart_account` keep their `0x` prefix and
// EIP-55 checksum casing (derived-bundle-v1.md §4.1) — this matches what
// `eth_account.Account.from_mnemonic(...).address` (Python) and viem's
// `mnemonicToAccount` (TS) already produce, but `wallet::derive_eoa` here
// returns a plain lowercase address (`hex::encode` never uppercases). This is
// a standard, deterministic, well-specified *display* transform of an address
// that has already been derived — not a new cryptographic derivation, so it
// does not run afoul of the "no new cryptography" rule.
//
// Known-answer checked against `eth_account`'s output for the canonical test
// vector in `test_eip55_checksum_known_answer`.
// ---------------------------------------------------------------------------

fn to_eip55_checksum(address: &str) -> Result<String> {
    let stripped = address.strip_prefix("0x").unwrap_or(address);
    if stripped.len() != 40 || !stripped.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(Error::InvalidInput(format!(
            "not a valid 20-byte hex address: {address}"
        )));
    }
    let lower = stripped.to_ascii_lowercase();

    let mut keccak = Keccak::v256();
    let mut hash = [0u8; 32];
    keccak.update(lower.as_bytes());
    keccak.finalize(&mut hash);
    let hash_hex = hex::encode(hash);

    let mut out = String::with_capacity(42);
    out.push_str("0x");
    for (ch, hash_nibble) in lower.chars().zip(hash_hex.chars()) {
        if ch.is_ascii_digit() {
            out.push(ch);
        } else {
            // hash_nibble is itself a hex digit 0-9a-f; compare its numeric value.
            let nibble_value = hash_nibble.to_digit(16).unwrap_or(0);
            if nibble_value >= 8 {
                out.push(ch.to_ascii_uppercase());
            } else {
                out.push(ch);
            }
        }
    }
    Ok(out)
}

/// Case-insensitive address equality (EIP-55 casing is a display convention;
/// the underlying 20-byte value is what matters for identity checks).
fn addresses_equal_ignoring_case(a: &str, b: &str) -> bool {
    a.eq_ignore_ascii_case(b)
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/// The four HKDF-derived vault-global working keys.
///
/// These must be byte-identical on every host sharing a vault — they decrypt
/// and index one immutable on-chain corpus and cannot be scoped, rotated, or
/// made per-host.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VaultKeys {
    #[serde(with = "hex32")]
    pub encryption_key: [u8; 32],
    #[serde(with = "hex32")]
    pub dedup_key: [u8; 32],
    #[serde(with = "hex32")]
    pub auth_key: [u8; 32],
    #[serde(with = "hex32")]
    pub lsh_seed: [u8; 32],
}

/// The signing half — a discriminated union on `kind`. Only this half is
/// scopable (Phase 3); the vault half above cannot be.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
pub enum SigningMaterial {
    /// Phase 2's shipping configuration. `private_key` is the BIP-44
    /// `m/44'/60'/0'/0/0` key derived from the root — the Smart Account
    /// **owner**. Not scoped, not revocable. Treat with the same care as the
    /// recovery phrase.
    #[serde(rename = "owner-eoa")]
    OwnerEoa {
        #[serde(with = "hex32")]
        private_key: [u8; 32],
        /// `0x`-prefixed, EIP-55 checksummed.
        address: String,
    },
    /// Phase 3. `private_key` is 32 fresh CSPRNG bytes, authorised by a
    /// master-wallet-signed `SessionKeyPermissionGrant`. Scoped, revocable.
    /// Phase 2 parses and validates this variant but does not ship it — see
    /// the module-level scope boundary in the implementation spec.
    #[serde(rename = "session-key")]
    SessionKey {
        #[serde(with = "hex32")]
        private_key: [u8; 32],
        /// `0x`-prefixed, EIP-55 checksummed.
        address: String,
        /// `SessionKeyPermissionGrant` v1, verbatim — opaque to this crate.
        grant: serde_json::Value,
    },
}

impl SigningMaterial {
    pub fn kind(&self) -> &'static str {
        match self {
            SigningMaterial::OwnerEoa { .. } => "owner-eoa",
            SigningMaterial::SessionKey { .. } => "session-key",
        }
    }

    pub fn address(&self) -> &str {
        match self {
            SigningMaterial::OwnerEoa { address, .. } => address,
            SigningMaterial::SessionKey { address, .. } => address,
        }
    }

    pub fn private_key(&self) -> &[u8; 32] {
        match self {
            SigningMaterial::OwnerEoa { private_key, .. } => private_key,
            SigningMaterial::SessionKey { private_key, .. } => private_key,
        }
    }
}

/// The on-chain identity this bundle authenticates writes/reads for.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccountRef {
    /// CREATE2 Smart Account address — facts are indexed by this.
    /// `0x`-prefixed, EIP-55 checksummed.
    pub smart_account: String,
    pub chain_id: u64,
}

/// A `derived-bundle-v1` credential bundle.
///
/// Deliberately **has no seed field and no mnemonic field** — see the module
/// doc and [`test_bundle_json_contains_no_root_material`]. `version` and
/// `schema` are not struct fields: they are wire-protocol constants injected
/// by [`bundle_to_json`] and checked by [`parse_bundle_v1`], not business
/// data a caller can vary.
///
/// **[`parse_bundle_v1`] is the only validated constructor.** This type
/// derives `Deserialize`, so `serde_json::from_str::<DerivedBundleV1>(..)`
/// compiles and will happily accept a syntactically well-formed but
/// semantically invalid bundle (mismatched address/private_key, an
/// `owner-eoa` carrying a stray `grant`, …) — it runs none of
/// [`validate_bundle_v1`]'s checks. Every binding in this crate (PyO3, WASM)
/// is confirmed to route through [`parse_bundle_v1`], never a bare
/// deserialize; a future binding or internal caller MUST do the same.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DerivedBundleV1 {
    pub vault: VaultKeys,
    pub signing: SigningMaterial,
    pub account: AccountRef,
    /// RFC 3339 UTC. Informational only — no code path depends on it.
    pub provisioned_at: String,
    /// `"spa"` | `"local-migration"` | `"external"`. Informational only —
    /// not validated against a closed enum here, so a future provisioning
    /// origin string round-trips without a core release. It IS bounded to
    /// `^[a-z0-9-]{1,32}$` by [`validate_bundle_v1`] (a short lowercase
    /// slug shape) — free-form-but-unbounded would make this field an
    /// undocumented carrier for smuggled secret material.
    pub provisioned_by: String,
}

const BUNDLE_VERSION: u32 = 2;
const BUNDLE_SCHEMA: &str = "derived-bundle-v1";

/// The wire envelope — adds `version`/`schema` and pins field order to the
/// derived-bundle-v1.md §4.1 listing. Never constructed by a caller directly;
/// only [`bundle_to_json`] and [`parse_bundle_v1`] touch this type.
///
/// `deny_unknown_fields` is load-bearing here (and on [`VaultKeys`] /
/// [`AccountRef`] / both [`SigningMaterial`] variants): without it, serde
/// silently *drops* any unrecognised top-level or nested field instead of
/// erroring, so `{...valid bundle..., "mnemonic": "<phrase>", "seed":
/// "<hex>"}` would parse as `Ok` with the injected fields discarded — a
/// consumer that persists the raw JSON it received (rather than only
/// `bundle_to_json`'s own re-serialisation) would silently write root
/// material to disk under a "validated bundle" label. See
/// `test_reject_injected_mnemonic_field_top_level` and the nested variants.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct BundleWire {
    version: u32,
    schema: String,
    vault: VaultKeys,
    signing: SigningMaterial,
    account: AccountRef,
    provisioned_at: String,
    provisioned_by: String,
}

// ---------------------------------------------------------------------------
// Derivation
// ---------------------------------------------------------------------------

/// Derive a `derived-bundle-v1` bundle from a BIP-39 mnemonic.
///
/// A **composition**, not a reimplementation: [`crypto::derive_keys_from_mnemonic`]
/// for `encryption_key`/`dedup_key`/`auth_key` (and the salt it returns, used
/// only as an input to the next call, never stored — see derived-bundle-v1.md
/// §4.5), [`crypto::derive_lsh_seed`] for `lsh_seed`, [`wallet::derive_eoa`]
/// for the signing key and address.
///
/// `smart_account` must be supplied by the caller (see the module doc for
/// why — core has no CREATE2 helper today). It is normalised to EIP-55
/// checksum casing regardless of the casing passed in.
///
/// `provisioned_by` is stored verbatim (informational only, not validated —
/// see [`DerivedBundleV1::provisioned_by`]).
///
/// Always derives `signing.kind = "owner-eoa"` — Phase 2's shipping
/// configuration. There is no `session-key` derivation path in core yet;
/// that lands with Phase 3, which will mint the CSPRNG session key on the
/// consuming host rather than deriving it here.
pub fn derive_bundle_from_mnemonic(
    mnemonic: &str,
    chain_id: u64,
    provisioned_by: &str,
    smart_account: &str,
) -> Result<DerivedBundleV1> {
    // Fail fast on a malformed provisioned_by rather than returning a bundle
    // that would later fail validate_bundle_v1 on its first round-trip
    // through parse_bundle_v1 (bundle_to_json -> parse_bundle_v1).
    require_bounded_provisioned_by(provisioned_by)?;

    let keys = crypto::derive_keys_from_mnemonic(mnemonic)?;
    let lsh_seed = crypto::derive_lsh_seed(mnemonic, &keys.salt)?;
    let eth_wallet = wallet::derive_eoa(mnemonic)?;

    let vault = VaultKeys {
        encryption_key: keys.encryption_key,
        dedup_key: keys.dedup_key,
        auth_key: keys.auth_key,
        lsh_seed,
    };

    let signing = SigningMaterial::OwnerEoa {
        private_key: eth_wallet.private_key,
        address: to_eip55_checksum(&eth_wallet.address)?,
    };

    let account = AccountRef {
        smart_account: to_eip55_checksum(smart_account)?,
        chain_id,
    };

    Ok(DerivedBundleV1 {
        vault,
        signing,
        account,
        provisioned_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
        provisioned_by: provisioned_by.to_string(),
    })
}

// ---------------------------------------------------------------------------
// Serialisation
// ---------------------------------------------------------------------------

/// Serialise a bundle to its canonical JSON wire form.
///
/// Field order is pinned to derived-bundle-v1.md §4.1
/// (`version, schema, vault{...}, signing{...}, account{...}, provisioned_at,
/// provisioned_by`) via [`BundleWire`]'s declaration order — `serde_json`
/// preserves struct field order, so this is deterministic and cross-language
/// byte-equality-safe as long as every implementation serialises a
/// fixed-order struct rather than a map (derived-bundle-v1.md P2-4).
pub fn bundle_to_json(bundle: &DerivedBundleV1) -> Result<String> {
    let wire = BundleWire {
        version: BUNDLE_VERSION,
        schema: BUNDLE_SCHEMA.to_string(),
        vault: bundle.vault.clone(),
        signing: bundle.signing.clone(),
        account: bundle.account.clone(),
        provisioned_at: bundle.provisioned_at.clone(),
        provisioned_by: bundle.provisioned_by.clone(),
    };
    serde_json::to_string(&wire).map_err(|e| Error::Parse(format!("bundle serialisation failed: {e}")))
}

/// Parse and fully validate a `derived-bundle-v1` JSON string.
///
/// Rejects loudly — never a silent downgrade — on: wrong `version`, wrong
/// `schema`, malformed/short/uppercase/prefixed hex in any key field, an
/// unknown `signing.kind`, an `owner-eoa` bundle carrying a `grant`, a
/// `session-key` bundle missing or inconsistent with its `grant`, or a
/// `signing.address` that does not match `address(signing.private_key)`.
/// Runs every invariant in derived-bundle-v1.md §4.7 via [`validate_bundle_v1`].
pub fn parse_bundle_v1(json: &str) -> Result<DerivedBundleV1> {
    let wire: BundleWire =
        serde_json::from_str(json).map_err(|e| Error::Parse(format!("malformed bundle JSON: {e}")))?;

    if wire.version != BUNDLE_VERSION {
        return Err(Error::Parse(format!(
            "unsupported bundle version {} (expected {BUNDLE_VERSION}) — refusing to guess, never a silent downgrade",
            wire.version
        )));
    }
    if wire.schema != BUNDLE_SCHEMA {
        return Err(Error::Parse(format!(
            "unsupported bundle schema {:?} (expected {BUNDLE_SCHEMA:?})",
            wire.schema
        )));
    }

    let bundle = DerivedBundleV1 {
        vault: wire.vault,
        signing: wire.signing,
        account: wire.account,
        provisioned_at: wire.provisioned_at,
        provisioned_by: wire.provisioned_by,
    };

    validate_bundle_v1(&bundle)?;
    Ok(bundle)
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

/// Validate every invariant in derived-bundle-v1.md §4.7 against an
/// already-constructed bundle.
///
/// Most structural invariants (hex length/casing, closed `signing.kind`
/// enum, `owner-eoa` rejecting an unexpected `grant`) are already enforced by
/// [`DerivedBundleV1`]'s type shape and [`parse_bundle_v1`]'s deserialisation
/// step — a bundle that exists as a Rust value already satisfies those. What
/// remains here are the cross-field consistency checks that no amount of
/// typing alone can catch: does `signing.address` actually belong to
/// `signing.private_key`, and (for `session-key`) is the grant internally
/// consistent with the rest of the bundle.
///
/// `account.chain_id` is deliberately **not** enforced to `== 100` here —
/// per §4.7, a client MAY warn rather than fail on other values to keep
/// self-hosted forks working; that policy choice belongs at the client layer,
/// not in core.
pub fn validate_bundle_v1(bundle: &DerivedBundleV1) -> Result<()> {
    // signing.address must match address(signing.private_key).
    let derived_address = address_from_private_key(bundle.signing.private_key())?;
    if !addresses_equal_ignoring_case(&derived_address, bundle.signing.address()) {
        return Err(Error::InvalidInput(format!(
            "signing.address {} does not match the address derived from signing.private_key ({derived_address}) — corrupt or tampered provisioning payload",
            bundle.signing.address()
        )));
    }

    // account.smart_account and signing.address must be well-formed 20-byte
    // hex addresses (defensive — catches a hand-built bundle bypassing
    // to_eip55_checksum's own format check).
    require_valid_address(&bundle.account.smart_account, "account.smart_account")?;
    require_valid_address(bundle.signing.address(), "signing.address")?;

    // Both addresses must carry EIP-55 checksum casing — not just be
    // case-insensitively valid. Without this, a correct-but-lowercase
    // address parses and round-trips lowercase, silently breaking the
    // cross-client byte-equality parity checksum casing exists for (see
    // to_eip55_checksum's doc comment and the parity fixture). Runs AFTER
    // the address/private-key mismatch check above so a mismatched address
    // still reports as a mismatch, not a casing error.
    require_eip55_checksum(&bundle.account.smart_account, "account.smart_account")?;
    require_eip55_checksum(bundle.signing.address(), "signing.address")?;

    // provisioned_by is informational (not a closed enum — see
    // DerivedBundleV1::provisioned_by), but it is still an unauthenticated
    // free-form string a malicious or buggy provisioner controls. Bound its
    // shape so it cannot smuggle root material (a mnemonic, a seed, or any
    // other secret-shaped payload) under a field nobody expects to hold one.
    require_bounded_provisioned_by(&bundle.provisioned_by)?;

    if let SigningMaterial::SessionKey { grant, address, .. } = &bundle.signing {
        let grant_account = grant.get("account").and_then(|v| v.as_str()).ok_or_else(|| {
            Error::InvalidInput("session-key grant is missing required field \"account\"".into())
        })?;
        if !addresses_equal_ignoring_case(grant_account, &bundle.account.smart_account) {
            return Err(Error::InvalidInput(format!(
                "session-key grant.account ({grant_account}) does not match account.smart_account ({})",
                bundle.account.smart_account
            )));
        }

        let grant_signer = grant.get("signer").and_then(|v| v.as_str()).ok_or_else(|| {
            Error::InvalidInput("session-key grant is missing required field \"signer\"".into())
        })?;
        if !addresses_equal_ignoring_case(grant_signer, address) {
            return Err(Error::InvalidInput(format!(
                "session-key grant.signer ({grant_signer}) does not match signing.address ({address})"
            )));
        }

        let grant_chain_id = grant
            .get("domain")
            .and_then(|d| d.get("chainId"))
            .and_then(|v| v.as_u64())
            .ok_or_else(|| {
                Error::InvalidInput(
                    "session-key grant is missing required field \"domain.chainId\"".into(),
                )
            })?;
        if grant_chain_id != bundle.account.chain_id {
            return Err(Error::InvalidInput(format!(
                "session-key grant.domain.chainId ({grant_chain_id}) does not match account.chain_id ({})",
                bundle.account.chain_id
            )));
        }
    }

    Ok(())
}

fn require_valid_address(address: &str, field: &str) -> Result<()> {
    let stripped = address.strip_prefix("0x").ok_or_else(|| {
        Error::InvalidInput(format!("{field} must be 0x-prefixed, got {address:?}"))
    })?;
    if stripped.len() != 40 || !stripped.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(Error::InvalidInput(format!(
            "{field} is not a valid 20-byte hex address: {address:?}"
        )));
    }
    Ok(())
}

/// Require `address` to already carry correct EIP-55 checksum casing.
/// Caller must have already run [`require_valid_address`] — this assumes
/// well-formed 20-byte hex.
fn require_eip55_checksum(address: &str, field: &str) -> Result<()> {
    let checksummed = to_eip55_checksum(address)?;
    if checksummed != address {
        return Err(Error::InvalidInput(format!(
            "{field} is not EIP-55 checksummed (expected {checksummed}, got {address}) — \
             load-bearing for cross-client byte-equality parity, see derived-bundle-v1.md §4.1"
        )));
    }
    Ok(())
}

/// `provisioned_by` is informational only (not a closed enum — new
/// provisioning origins round-trip without a core release), but it is still
/// attacker/provisioner-controlled input. Bound it to a short lowercase
/// slug shape so it cannot carry a smuggled mnemonic, seed, or other
/// secret-shaped payload: `^[a-z0-9-]{1,32}$`.
fn require_bounded_provisioned_by(value: &str) -> Result<()> {
    let len_ok = !value.is_empty() && value.len() <= 32;
    let chars_ok = value
        .bytes()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
    if !len_ok || !chars_ok {
        return Err(Error::InvalidInput(format!(
            "provisioned_by must match ^[a-z0-9-]{{1,32}}$ (a short lowercase slug — \
             \"spa\" | \"local-migration\" | \"external\" today, others may be added later), \
             got {value:?}"
        )));
    }
    Ok(())
}

/// Recover the EIP-55 checksummed Ethereum address for a secp256k1 private key.
fn address_from_private_key(private_key: &[u8; 32]) -> Result<String> {
    use k256::ecdsa::SigningKey;

    let signing_key = SigningKey::from_bytes(private_key.into())
        .map_err(|e| Error::Crypto(format!("invalid private key: {e}")))?;
    let verifying_key = signing_key.verifying_key();
    let public_key_bytes = verifying_key.to_encoded_point(false);
    let pubkey_raw = &public_key_bytes.as_bytes()[1..]; // skip the 0x04 prefix

    let mut keccak = Keccak::v256();
    let mut hash = [0u8; 32];
    keccak.update(pubkey_raw);
    keccak.finalize(&mut hash);

    let address = format!("0x{}", hex::encode(&hash[12..]));
    to_eip55_checksum(&address)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_MNEMONIC: &str =
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
    // Deterministic CREATE2 Smart Account fixture used across the test suite —
    // this crate has no CREATE2 helper (see the module doc), so tests supply a
    // fixed placeholder rather than a real on-chain-queried address. Not a
    // real Smart Account; just 20 arbitrary bytes in valid address shape.
    const TEST_SMART_ACCOUNT: &str = "0x1234567890AbcdEF1234567890aBcdef12345678";

    fn derive_test_bundle() -> DerivedBundleV1 {
        derive_bundle_from_mnemonic(TEST_MNEMONIC, 100, "local-migration", TEST_SMART_ACCOUNT)
            .expect("canonical vector must derive cleanly")
    }

    // -- EIP-55 -------------------------------------------------------------

    #[test]
    fn test_eip55_checksum_known_answer() {
        // Ground truth: eth_account.Account.from_mnemonic(TEST_MNEMONIC).address
        // for the canonical vector — see python/tests/test_userop.py.
        let checksummed = to_eip55_checksum("0x9858effd232b4033e47d90003d41ec34ecaeda94").unwrap();
        assert_eq!(checksummed, "0x9858EfFD232B4033E47d90003D41EC34EcaEda94");
    }

    #[test]
    fn test_eip55_checksum_idempotent_on_already_checksummed_input() {
        let once = to_eip55_checksum("0x9858effd232b4033e47d90003d41ec34ecaeda94").unwrap();
        let twice = to_eip55_checksum(&once).unwrap();
        assert_eq!(once, twice);
    }

    // -- P2-1 required test table --------------------------------------------

    /// Load-bearing: proves Phase 2 changes no cryptography. Each `vault`
    /// field must equal the corresponding output of the pre-existing
    /// `derive_keys_from_mnemonic` / `derive_lsh_seed` functions.
    #[test]
    fn test_bundle_vault_matches_legacy_derivation() {
        let bundle = derive_test_bundle();
        let legacy_keys = crypto::derive_keys_from_mnemonic(TEST_MNEMONIC).unwrap();
        let legacy_lsh_seed = crypto::derive_lsh_seed(TEST_MNEMONIC, &legacy_keys.salt).unwrap();

        assert_eq!(bundle.vault.encryption_key, legacy_keys.encryption_key);
        assert_eq!(bundle.vault.dedup_key, legacy_keys.dedup_key);
        assert_eq!(bundle.vault.auth_key, legacy_keys.auth_key);
        assert_eq!(bundle.vault.lsh_seed, legacy_lsh_seed);
    }

    #[test]
    fn test_bundle_json_contains_no_root_material() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();

        assert!(
            !json.contains("abandon"),
            "serialised bundle must never contain the mnemonic"
        );

        // The 64-byte BIP-39 seed for the canonical vector, hex-encoded —
        // must not appear anywhere in the bundle (nor any 32-byte half of it,
        // which would still leak root-equivalent material).
        let seed = crypto::mnemonic_to_seed_bytes(TEST_MNEMONIC).unwrap();
        let seed_hex = hex::encode(seed);
        assert!(!json.contains(&seed_hex), "must not contain the full seed hex");
        assert!(
            !json.contains(&seed_hex[..64]),
            "must not contain the seed's first half (== the derivation salt)"
        );
        assert!(
            !json.contains(&seed_hex[64..]),
            "must not contain the seed's second half"
        );
    }

    #[test]
    fn test_bundle_roundtrip() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let parsed = parse_bundle_v1(&json).unwrap();
        assert_eq!(bundle, parsed);
    }

    #[test]
    fn test_signing_address_matches_private_key() {
        let bundle = derive_test_bundle();
        let expected = address_from_private_key(bundle.signing.private_key()).unwrap();
        assert_eq!(bundle.signing.address(), expected);
    }

    #[test]
    fn test_reject_wrong_version() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let bad = json.replacen("\"version\":2", "\"version\":3", 1);
        assert_ne!(bad, json, "sanity: replacement must have taken effect");
        let result = parse_bundle_v1(&bad);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("version"));
    }

    #[test]
    fn test_reject_wrong_schema() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let bad = json.replacen("derived-bundle-v1", "derived-bundle-v2", 1);
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("schema"));
    }

    #[test]
    fn test_reject_unknown_signing_kind() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let bad = json.replacen("\"kind\":\"owner-eoa\"", "\"kind\":\"root-eoa\"", 1);
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(result.is_err(), "unknown signing.kind must never fall back to owner-eoa");
    }

    #[test]
    fn test_reject_short_key() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let full_key_hex = hex::encode(bundle.vault.encryption_key);
        let short_key_hex = &full_key_hex[..62]; // 31 bytes
        let bad = json.replacen(&full_key_hex, short_key_hex, 1);
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(result.is_err());
    }

    #[test]
    fn test_reject_uppercase_or_prefixed_hex() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let full_key_hex = hex::encode(bundle.vault.encryption_key);

        let uppercased = json.replacen(&full_key_hex, &full_key_hex.to_uppercase(), 1);
        assert!(parse_bundle_v1(&uppercased).is_err(), "uppercase key hex must be rejected");

        let prefixed = json.replacen(&full_key_hex, &format!("0x{full_key_hex}"), 1);
        assert!(parse_bundle_v1(&prefixed).is_err(), "0x-prefixed key hex must be rejected");
    }

    #[test]
    fn test_reject_address_mismatch() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        // Swap in a syntactically valid but wrong address.
        let bad = json.replacen(
            bundle.signing.address(),
            "0x000000000000000000000000000000000000dd",
            1,
        );
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("does not match"));
    }

    #[test]
    fn test_reject_owner_eoa_with_grant() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let bad = json.replacen(
            &format!("\"address\":\"{}\"", bundle.signing.address()),
            &format!(
                "\"address\":\"{}\",\"grant\":{{\"account\":\"0x0\"}}",
                bundle.signing.address()
            ),
            1,
        );
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(
            result.is_err(),
            "an owner-eoa bundle carrying a grant field must be rejected, not silently ignored"
        );
    }

    #[test]
    fn test_session_key_requires_grant_consistency() {
        let bundle = derive_test_bundle();
        let smart_account = bundle.account.smart_account.clone();
        let signer_address = bundle.signing.address().to_string();
        let private_key_hex = hex::encode(bundle.signing.private_key());

        // (a) missing grant entirely.
        let missing_grant = format!(
            r#"{{"version":2,"schema":"derived-bundle-v1","vault":{{"encryption_key":"{}","dedup_key":"{}","auth_key":"{}","lsh_seed":"{}"}},"signing":{{"kind":"session-key","private_key":"{}","address":"{}"}},"account":{{"smart_account":"{}","chain_id":100}},"provisioned_at":"2026-08-02T00:00:00Z","provisioned_by":"local-migration"}}"#,
            hex::encode(bundle.vault.encryption_key),
            hex::encode(bundle.vault.dedup_key),
            hex::encode(bundle.vault.auth_key),
            hex::encode(bundle.vault.lsh_seed),
            private_key_hex,
            signer_address,
            smart_account,
        );
        assert!(parse_bundle_v1(&missing_grant).is_err(), "missing grant must be rejected");

        // (b) grant.signer != signing.address.
        let wrong_signer = build_session_key_json(&bundle, |g| {
            g["signer"] = serde_json::json!("0x000000000000000000000000000000000000dd");
        });
        assert!(
            parse_bundle_v1(&wrong_signer).is_err(),
            "grant.signer mismatching signing.address must be rejected"
        );

        // (c) grant.domain.chainId != account.chain_id.
        let wrong_chain = build_session_key_json(&bundle, |g| {
            g["domain"]["chainId"] = serde_json::json!(1);
        });
        assert!(
            parse_bundle_v1(&wrong_chain).is_err(),
            "grant.domain.chainId mismatching account.chain_id must be rejected"
        );

        // (d) sanity: an internally-consistent session-key bundle DOES parse —
        // Phase 2 core must still be able to parse/validate the schema even
        // though no client ships it yet (scope boundary is client-side).
        let consistent = build_session_key_json(&bundle, |_| {});
        assert!(
            parse_bundle_v1(&consistent).is_ok(),
            "an internally-consistent session-key bundle must parse in core"
        );
    }

    /// Build a `session-key` bundle JSON string sharing the canonical
    /// bundle's vault/account/signing-key-material, with an internally
    /// consistent grant that the caller can mutate before parsing.
    fn build_session_key_json(
        bundle: &DerivedBundleV1,
        mutate_grant: impl FnOnce(&mut serde_json::Value),
    ) -> String {
        let mut grant = serde_json::json!({
            "account": bundle.account.smart_account,
            "signer": bundle.signing.address(),
            "domain": { "chainId": bundle.account.chain_id },
        });
        mutate_grant(&mut grant);

        serde_json::json!({
            "version": 2,
            "schema": "derived-bundle-v1",
            "vault": {
                "encryption_key": hex::encode(bundle.vault.encryption_key),
                "dedup_key": hex::encode(bundle.vault.dedup_key),
                "auth_key": hex::encode(bundle.vault.auth_key),
                "lsh_seed": hex::encode(bundle.vault.lsh_seed),
            },
            "signing": {
                "kind": "session-key",
                "private_key": hex::encode(bundle.signing.private_key()),
                "address": bundle.signing.address(),
                "grant": grant,
            },
            "account": {
                "smart_account": bundle.account.smart_account,
                "chain_id": bundle.account.chain_id,
            },
            "provisioned_at": "2026-08-02T00:00:00Z",
            "provisioned_by": "local-migration",
        })
        .to_string()
    }

    // -- Additional coverage --------------------------------------------------

    #[test]
    fn test_derive_bundle_smart_account_normalised_to_checksum() {
        let lowercase_input = TEST_SMART_ACCOUNT.to_ascii_lowercase();
        let bundle =
            derive_bundle_from_mnemonic(TEST_MNEMONIC, 100, "local-migration", &lowercase_input)
                .unwrap();
        assert_eq!(bundle.account.smart_account, TEST_SMART_ACCOUNT);
    }

    #[test]
    fn test_derive_bundle_rejects_malformed_smart_account() {
        let result = derive_bundle_from_mnemonic(TEST_MNEMONIC, 100, "local-migration", "not-an-address");
        assert!(result.is_err());
    }

    #[test]
    fn test_validate_bundle_v1_accepts_freshly_derived_bundle() {
        let bundle = derive_test_bundle();
        assert!(validate_bundle_v1(&bundle).is_ok());
    }

    #[test]
    fn test_parse_bundle_v1_rejects_malformed_json() {
        assert!(parse_bundle_v1("not json").is_err());
        assert!(parse_bundle_v1("{}").is_err());
    }

    #[test]
    fn test_bundle_to_json_field_order_matches_spec() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let version_pos = json.find("\"version\"").unwrap();
        let schema_pos = json.find("\"schema\"").unwrap();
        let vault_pos = json.find("\"vault\"").unwrap();
        let signing_pos = json.find("\"signing\"").unwrap();
        let account_pos = json.find("\"account\"").unwrap();
        let provisioned_at_pos = json.find("\"provisioned_at\"").unwrap();
        let provisioned_by_pos = json.find("\"provisioned_by\"").unwrap();

        assert!(version_pos < schema_pos);
        assert!(schema_pos < vault_pos);
        assert!(vault_pos < signing_pos);
        assert!(signing_pos < account_pos);
        assert!(account_pos < provisioned_at_pos);
        assert!(provisioned_at_pos < provisioned_by_pos);
    }

    #[test]
    fn test_bundle_to_json_vault_field_order_matches_spec() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let enc_pos = json.find("\"encryption_key\"").unwrap();
        let dedup_pos = json.find("\"dedup_key\"").unwrap();
        let auth_pos = json.find("\"auth_key\"").unwrap();
        let lsh_pos = json.find("\"lsh_seed\"").unwrap();
        assert!(enc_pos < dedup_pos);
        assert!(dedup_pos < auth_pos);
        assert!(auth_pos < lsh_pos);
    }

    // -- Adversarial review findings (PR #587 REQUEST-CHANGES) ---------------
    //
    // MAJOR: without `deny_unknown_fields` on BundleWire/VaultKeys/AccountRef,
    // serde silently DROPS unrecognised fields instead of erroring, so a
    // bundle carrying an injected "mnemonic" or "seed" field alongside
    // otherwise-valid data would parse as Ok() with the injected field
    // discarded. A consumer that persists the RAW JSON it received (rather
    // than only ever re-serialising through bundle_to_json) would then
    // silently write root material to disk under a "validated bundle" label.
    // These tests assert the injection is rejected outright, not silently
    // stripped.

    #[test]
    fn test_reject_injected_mnemonic_field_top_level() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let bad = json.replacen(
            "\"provisioned_by\":\"local-migration\"",
            &format!(
                "\"provisioned_by\":\"local-migration\",\"mnemonic\":\"{TEST_MNEMONIC}\""
            ),
            1,
        );
        assert_ne!(bad, json, "sanity: replacement must have taken effect");
        let result = parse_bundle_v1(&bad);
        assert!(
            result.is_err(),
            "a bundle with an injected top-level mnemonic field must be rejected outright, \
             never silently accepted with the field dropped"
        );
    }

    #[test]
    fn test_reject_injected_seed_field_nested_in_vault() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let seed_hex = hex::encode(crypto::mnemonic_to_seed_bytes(TEST_MNEMONIC).unwrap());
        let lsh_field = format!("\"lsh_seed\":\"{}\"", hex::encode(bundle.vault.lsh_seed));
        let bad = json.replacen(
            &lsh_field,
            &format!("{lsh_field},\"seed\":\"{seed_hex}\""),
            1,
        );
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(
            result.is_err(),
            "an injected seed field nested inside vault must be rejected"
        );
    }

    #[test]
    fn test_reject_injected_mnemonic_field_nested_in_account() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let chain_id_field = format!("\"chain_id\":{}", bundle.account.chain_id);
        let bad = json.replacen(
            &chain_id_field,
            &format!("{chain_id_field},\"mnemonic\":\"{TEST_MNEMONIC}\""),
            1,
        );
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(
            result.is_err(),
            "an injected mnemonic field nested inside account must be rejected"
        );
    }

    #[test]
    fn test_reject_injected_field_nested_in_owner_eoa_signing() {
        // SigningMaterial already had deny_unknown_fields pre-review — this
        // is a regression guard, not a new gap, but it belongs alongside the
        // other injection tests for a complete adversarial table.
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let address_field = format!("\"address\":\"{}\"", bundle.signing.address());
        let bad = json.replacen(
            &address_field,
            &format!("{address_field},\"seed\":\"{}\"", "aa".repeat(64)),
            1,
        );
        assert_ne!(bad, json);
        assert!(parse_bundle_v1(&bad).is_err());
    }

    // MINOR: provisioned_by is unauthenticated, provisioner-controlled input.
    // Bound its shape so it cannot smuggle a phrase or other secret-shaped
    // payload under a field nobody expects to hold one.

    #[test]
    fn test_reject_provisioned_by_smuggling_a_phrase() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let bad = json.replacen(
            "\"provisioned_by\":\"local-migration\"",
            &format!("\"provisioned_by\":\"{TEST_MNEMONIC}\""),
            1,
        );
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(
            result.is_err(),
            "provisioned_by must not accept a smuggled phrase-shaped value (spaces, length > 32)"
        );
    }

    #[test]
    fn test_reject_provisioned_by_uppercase() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let bad = json.replacen(
            "\"provisioned_by\":\"local-migration\"",
            "\"provisioned_by\":\"Local-Migration\"",
            1,
        );
        assert!(parse_bundle_v1(&bad).is_err());
    }

    #[test]
    fn test_reject_provisioned_by_empty() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let bad = json.replacen("\"provisioned_by\":\"local-migration\"", "\"provisioned_by\":\"\"", 1);
        assert!(parse_bundle_v1(&bad).is_err());
    }

    #[test]
    fn test_reject_provisioned_by_too_long() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let too_long = "a".repeat(33);
        let bad = json.replacen(
            "\"provisioned_by\":\"local-migration\"",
            &format!("\"provisioned_by\":\"{too_long}\""),
            1,
        );
        assert!(parse_bundle_v1(&bad).is_err());
    }

    #[test]
    fn test_accept_provisioned_by_documented_values() {
        for value in ["spa", "local-migration", "external"] {
            let result =
                derive_bundle_from_mnemonic(TEST_MNEMONIC, 100, value, TEST_SMART_ACCOUNT);
            assert!(result.is_ok(), "provisioned_by={value:?} must be accepted");
        }
    }

    #[test]
    fn test_derive_bundle_rejects_malformed_provisioned_by() {
        let result =
            derive_bundle_from_mnemonic(TEST_MNEMONIC, 100, "not a valid slug!", TEST_SMART_ACCOUNT);
        assert!(
            result.is_err(),
            "derive_bundle_from_mnemonic must fail fast on a malformed provisioned_by rather \
             than returning a bundle that fails to round-trip through parse_bundle_v1 later"
        );
    }

    // MINOR: correct-but-lowercase addresses must not silently validate —
    // EIP-55 casing is load-bearing for cross-client byte-equality parity
    // (derived-bundle-v1.md §4.1), so a bundle carrying a non-checksummed
    // (but otherwise valid) address must be rejected, not accepted-as-is.

    #[test]
    fn test_reject_lowercase_signing_address() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let lowercase_address = bundle.signing.address().to_ascii_lowercase();
        assert_ne!(
            lowercase_address,
            bundle.signing.address(),
            "sanity: the canonical address must contain at least one letter EIP-55 would case"
        );
        // Case-insensitive equality means this still passes the
        // address-matches-private-key check, so it isolates the NEW
        // checksum-casing check.
        let bad = json.replacen(bundle.signing.address(), &lowercase_address, 1);
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(
            result.is_err(),
            "a correct-but-lowercase signing.address must be rejected, not silently accepted"
        );
        assert!(result.unwrap_err().to_string().contains("EIP-55"));
    }

    #[test]
    fn test_reject_lowercase_smart_account() {
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let lowercase_smart_account = bundle.account.smart_account.to_ascii_lowercase();
        assert_ne!(lowercase_smart_account, bundle.account.smart_account);
        let bad = json.replacen(&bundle.account.smart_account, &lowercase_smart_account, 1);
        assert_ne!(bad, json);
        let result = parse_bundle_v1(&bad);
        assert!(
            result.is_err(),
            "a correct-but-lowercase account.smart_account must be rejected"
        );
        assert!(result.unwrap_err().to_string().contains("EIP-55"));
    }

    #[test]
    fn test_validate_bundle_v1_still_reports_mismatch_before_casing_for_wrong_address() {
        // Regression guard for check ORDER: an address that is BOTH the
        // wrong address AND not checksummed must still surface as a
        // "does not match" error (checked first), not an EIP-55 error —
        // otherwise test_reject_address_mismatch's assertion on the error
        // text would be accidentally coupled to casing.
        let bundle = derive_test_bundle();
        let json = bundle_to_json(&bundle).unwrap();
        let bad = json.replacen(
            bundle.signing.address(),
            "0x000000000000000000000000000000000000dd",
            1,
        );
        let result = parse_bundle_v1(&bad);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("does not match"));
    }
}
