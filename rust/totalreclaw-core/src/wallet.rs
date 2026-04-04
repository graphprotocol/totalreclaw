//! Ethereum wallet derivation from BIP-39 mnemonic.
//!
//! Derives the EOA (externally-owned account) address and private key
//! via BIP-44 path m/44'/60'/0'/0/0, matching viem's mnemonicToAccount().
//!
//! This module contains only pure crypto -- no network I/O, no filesystem.

use coins_bip32::prelude::*;
use k256::ecdsa::SigningKey;
use serde::{Deserialize, Serialize, Serializer};
use tiny_keccak::{Hasher, Keccak};

use crate::{Error, Result};

/// Derived Ethereum wallet (EOA + signing key).
///
/// Private key is serialized as a hex string for WASM/PyO3 interop.
#[derive(Debug, Clone, Deserialize)]
pub struct EthWallet {
    /// Private key bytes (32 bytes).
    #[serde(deserialize_with = "deserialize_privkey_hex")]
    pub private_key: [u8; 32],
    /// EOA address (0x-prefixed, lowercase hex).
    pub address: String,
}

impl Serialize for EthWallet {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        use serde::ser::SerializeStruct;
        let mut s = serializer.serialize_struct("EthWallet", 2)?;
        s.serialize_field("private_key", &hex::encode(self.private_key))?;
        s.serialize_field("address", &self.address)?;
        s.end()
    }
}

fn deserialize_privkey_hex<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<[u8; 32], D::Error> {
    let s = String::deserialize(deserializer)?;
    let bytes = hex::decode(&s).map_err(serde::de::Error::custom)?;
    if bytes.len() != 32 {
        return Err(serde::de::Error::custom(format!(
            "expected 32 bytes, got {}",
            bytes.len()
        )));
    }
    let mut arr = [0u8; 32];
    arr.copy_from_slice(&bytes);
    Ok(arr)
}

/// Derive an Ethereum EOA from a BIP-39 mnemonic via BIP-44.
///
/// Path: m/44'/60'/0'/0/0 (standard Ethereum derivation path).
/// Matches viem's `mnemonicToAccount(mnemonic)`.
pub fn derive_eoa(mnemonic: &str) -> Result<EthWallet> {
    // 1. BIP-39 seed (PBKDF2-HMAC-SHA512)
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

#[cfg(test)]
mod tests {
    use super::*;

    /// The 12-word "abandon...about" mnemonic's EOA at BIP-44 m/44'/60'/0'/0/0.
    /// Verified against Python eth_account.Account.from_mnemonic() and iancoleman.io/bip39.
    #[test]
    fn test_eoa_derivation_parity() {
        let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
        let w = derive_eoa(mnemonic).unwrap();
        assert_eq!(
            hex::encode(&w.private_key),
            "1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727",
            "Private key must match reference derivation"
        );
        assert_eq!(
            w.address.to_lowercase(),
            "0x9858effd232b4033e47d90003d41ec34ecaeda94",
            "EOA must match eth_account.from_mnemonic for the 12-word test mnemonic"
        );
    }

    #[test]
    fn test_derive_eoa_address() {
        let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
        let address = derive_eoa_address(mnemonic).unwrap();
        assert_eq!(
            address.to_lowercase(),
            "0x9858effd232b4033e47d90003d41ec34ecaeda94"
        );
    }

    #[test]
    fn test_ethwallet_serialization() {
        let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
        let w = derive_eoa(mnemonic).unwrap();
        let json = serde_json::to_string(&w).unwrap();
        assert!(json.contains("1ab42cc412b618bdea3a599e3c9bae199ebf030895b039e9db1e30dafb12b727"));
        assert!(json.contains("0x9858effd232b4033e47d90003d41ec34ecaeda94"));

        // Round-trip deserialization
        let w2: EthWallet = serde_json::from_str(&json).unwrap();
        assert_eq!(w.private_key, w2.private_key);
        assert_eq!(w.address, w2.address);
    }
}
