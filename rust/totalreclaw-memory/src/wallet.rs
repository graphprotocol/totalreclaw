//! Ethereum wallet derivation and Smart Account resolution.
//!
//! Pure crypto (BIP-44 derivation) is re-exported from `totalreclaw_core::wallet`.
//! This module adds `resolve_smart_account_address()` which requires network I/O.

pub use totalreclaw_core::wallet::{derive_eoa, derive_eoa_address, EthWallet};

use crate::{Error, Result};

/// SimpleAccountFactory address (v0.7, same on all EVM chains).
const SIMPLE_ACCOUNT_FACTORY: &str = "0x91E60e0613810449d098b0b5Ec8b51A0FE8c8985";

/// Resolve the CREATE2 Smart Account address by calling the factory.
///
/// Calls SimpleAccountFactory.getAddress(owner, 0) via eth_call.
/// The factory returns the deterministic CREATE2 address (same on all chains).
pub async fn resolve_smart_account_address(
    eoa_address: &str,
    rpc_url: &str,
) -> Result<String> {
    // ABI-encode: getAddress(address,uint256)
    // keccak256("getAddress(address,uint256)")[:4] = 0x8cb84e18
    let selector = "8cb84e18";
    let owner = eoa_address.trim_start_matches("0x").to_lowercase();
    let owner_padded = format!("{:0>64}", owner);
    let salt_padded = "0".repeat(64);
    let calldata = format!("0x{}{}{}", selector, owner_padded, salt_padded);

    let client = reqwest::Client::new();
    let resp = client
        .post(rpc_url)
        .json(&serde_json::json!({
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": SIMPLE_ACCOUNT_FACTORY, "data": calldata}, "latest"],
            "id": 1
        }))
        .send()
        .await
        .map_err(|e| Error::Http(e.to_string()))?;

    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| Error::Http(e.to_string()))?;

    let result = body["result"].as_str().unwrap_or("");
    if result.len() < 42 {
        return Err(Error::Http(format!(
            "Factory returned invalid result: {}",
            result
        )));
    }

    Ok(format!("0x{}", &result[result.len() - 40..]).to_lowercase())
}
