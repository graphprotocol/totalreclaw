//! Ethereum wallet derivation from BIP-39 mnemonic.
//!
//! Derives the EOA (externally-owned account) address and private key
//! via BIP-44 path m/44'/60'/0'/0/0, matching viem's mnemonicToAccount().

use coins_bip32::prelude::*;
use k256::ecdsa::SigningKey;
use tiny_keccak::{Hasher, Keccak};

use crate::{Error, Result};

/// Derived Ethereum wallet (EOA + signing key).
pub struct EthWallet {
    /// Private key bytes (32 bytes).
    pub private_key: [u8; 32],
    /// EOA address (0x-prefixed, lowercase hex).
    pub address: String,
}

/// Derive an Ethereum EOA from a BIP-39 mnemonic via BIP-44.
///
/// Path: m/44'/60'/0'/0/0 (standard Ethereum derivation path).
/// Matches viem's `mnemonicToAccount(mnemonic)`.
pub fn derive_eoa(mnemonic: &str) -> Result<EthWallet> {
    // 1. BIP-39 seed (same as crypto.rs -- PBKDF2-HMAC-SHA512)
    let seed = crate::crypto::mnemonic_to_seed_bytes(mnemonic)?;

    // 2. BIP-32 master key from seed
    let master = XPriv::root_from_seed(&seed, None)
        .map_err(|e| Error::Crypto(format!("BIP-32 master key failed: {}", e)))?;

    // 3. Derive m/44'/60'/0'/0/0
    let path = "m/44'/60'/0'/0/0";
    let derived = master
        .derive_path(path)
        .map_err(|e| Error::Crypto(format!("BIP-44 derivation failed: {}", e)))?;

    // 4. Extract 32-byte private key (via AsRef<SigningKey>)
    let derived_signing_key: &k256::ecdsa::SigningKey = derived.as_ref();
    let mut private_key = [0u8; 32];
    private_key.copy_from_slice(&derived_signing_key.to_bytes());

    // 5. Derive public key -> keccak256 -> last 20 bytes = address
    let signing_key = SigningKey::from_bytes((&private_key).into())
        .map_err(|e| Error::Crypto(format!("Invalid private key: {}", e)))?;
    let verifying_key = signing_key.verifying_key();
    let public_key_bytes = verifying_key.to_encoded_point(false);
    // Uncompressed public key: 0x04 || x (32 bytes) || y (32 bytes)
    // Keccak256 the 64 bytes (skip the 0x04 prefix)
    let pubkey_raw = &public_key_bytes.as_bytes()[1..]; // skip 0x04

    let mut keccak = Keccak::v256();
    let mut hash = [0u8; 32];
    keccak.update(pubkey_raw);
    keccak.finalize(&mut hash);

    // Address = last 20 bytes of keccak256(pubkey)
    let address = format!("0x{}", hex::encode(&hash[12..]));

    Ok(EthWallet {
        private_key,
        address,
    })
}

/// Convenience: derive just the address string.
pub fn derive_eoa_address(mnemonic: &str) -> Result<String> {
    Ok(derive_eoa(mnemonic)?.address)
}

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
