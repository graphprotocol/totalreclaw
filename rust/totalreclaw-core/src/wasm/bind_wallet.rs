//! `bind_wallet` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// Wallet derivation
// ---------------------------------------------------------------------------

/// Derive an Ethereum EOA wallet from a BIP-39 mnemonic via BIP-44.
///
/// Path: m/44'/60'/0'/0/0 (standard Ethereum derivation path).
/// Returns a JS object: `{ private_key: "hex...", address: "0x..." }`.
#[wasm_bindgen(js_name = "deriveEoa")]
pub fn wasm_derive_eoa(mnemonic: &str) -> Result<JsValue, JsError> {
    let w = wallet::derive_eoa(mnemonic).map_err(to_js_error)?;
    serde_wasm_bindgen::to_value(&w).map_err(|e| JsError::new(&e.to_string()))
}

/// Derive just the Ethereum EOA address from a BIP-39 mnemonic.
///
/// Returns: `"0x..."` (lowercase hex).
#[wasm_bindgen(js_name = "deriveEoaAddress")]
pub fn wasm_derive_eoa_address(mnemonic: &str) -> Result<String, JsError> {
    wallet::derive_eoa_address(mnemonic).map_err(to_js_error)
}

