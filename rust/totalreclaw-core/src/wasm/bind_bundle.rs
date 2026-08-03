//! `bind_bundle` wasm-bindgen bindings for the `derived-bundle-v1` credential
//! bundle (Option E Phase 2). Shared imports + helpers (`to_js_error`) come
//! from the parent module via `use super::*;`. Each `#[wasm_bindgen]` export
//! keeps its exact `js_name`, so the JS-visible surface is unchanged.
//!
//! Must be present in **both** the bundler build and the `--target web`
//! build (the `./web` subpath, core 2.5.6+) — the SPA provisioning path
//! (`provisioned_by: "spa"`) runs in a browser and needs `deriveBundleFromMnemonic`
//! there. `#[wasm_bindgen]` exports are unconditionally included in whichever
//! target `wasm-pack build` is invoked for; there is no separate feature gate
//! per target in this crate, so no extra wiring is needed beyond this file.
//!
//! Deviation from the design doc's sketched core signature:
//! `derive_bundle_from_mnemonic` takes `smart_account` as a **required**
//! parameter — see `bundle.rs`'s module doc for why (no CREATE2 helper in
//! this crate; the address comes from an `eth_call` RPC round-trip today).

use super::*;
use crate::bundle;

// ---------------------------------------------------------------------------
// Derivation
// ---------------------------------------------------------------------------

/// Derive a `derived-bundle-v1` bundle from a BIP-39 mnemonic.
///
/// Returns the canonical JSON **string** (not a JS object) — this is a
/// deliberate deviation from most other bindings in this file, which return
/// `JsValue` objects for ergonomic JS access. The canonical string is what
/// `tests/parity/fixtures/generate-derived-bundle-v1.ts` writes verbatim to
/// the parity fixture, and it is the same `bundle::bundle_to_json` output the
/// PyO3 binding returns — one serialisation implementation, byte-identical
/// across both bindings by construction.
///
/// `smart_account`: the CREATE2 Smart Account address (`0x`-prefixed hex,
/// any casing — normalised to EIP-55 checksum casing in the output).
#[wasm_bindgen(js_name = "deriveBundleFromMnemonic")]
pub fn wasm_derive_bundle_from_mnemonic(
    mnemonic: &str,
    chain_id: u64,
    provisioned_by: &str,
    smart_account: &str,
) -> Result<String, JsError> {
    let derived =
        bundle::derive_bundle_from_mnemonic(mnemonic, chain_id, provisioned_by, smart_account)
            .map_err(to_js_error)?;
    bundle::bundle_to_json(&derived).map_err(to_js_error)
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

/// Parse and fully validate a `derived-bundle-v1` JSON string.
///
/// Returns a plain JS object mirroring `DerivedBundleV1`'s shape (`vault`,
/// `signing`, `account`, `provisioned_at`, `provisioned_by`), with every
/// 32-byte key field as a lowercase hex string (this crate's WASM convention
/// — "all byte arrays cross the boundary as hex strings", per this module's
/// top-level doc comment).
///
/// Raises a `JsError` on any violation of derived-bundle-v1.md §4.7.
#[wasm_bindgen(js_name = "parseBundleV1")]
pub fn wasm_parse_bundle_v1(json: &str) -> Result<JsValue, JsError> {
    let parsed = bundle::parse_bundle_v1(json).map_err(to_js_error)?;
    serde_wasm_bindgen::to_value(&parsed).map_err(|e| JsError::new(&e.to_string()))
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

/// Validate a `derived-bundle-v1` JSON string against every invariant in
/// derived-bundle-v1.md §4.7. Throws on any violation; resolves to nothing
/// on success.
#[wasm_bindgen(js_name = "validateBundleV1")]
pub fn wasm_validate_bundle_v1(json: &str) -> Result<(), JsError> {
    bundle::parse_bundle_v1(json).map_err(to_js_error)?;
    Ok(())
}
