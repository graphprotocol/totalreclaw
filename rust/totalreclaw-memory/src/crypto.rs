//! Key derivation and AES-256-GCM encryption.
//!
//! Delegates to `totalreclaw_core::crypto` — the canonical implementation.
//! Re-exports all public items for backward compatibility.

pub use totalreclaw_core::crypto::*;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_key_derivation_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let kd = &fixture["key_derivation"];
        let mnemonic = kd["mnemonic"].as_str().unwrap();

        let keys = derive_keys_from_mnemonic(mnemonic).unwrap();

        assert_eq!(hex::encode(keys.salt), kd["salt_hex"].as_str().unwrap());
        assert_eq!(hex::encode(keys.auth_key), kd["auth_key_hex"].as_str().unwrap());
        assert_eq!(
            hex::encode(keys.encryption_key),
            kd["encryption_key_hex"].as_str().unwrap()
        );
        assert_eq!(hex::encode(keys.dedup_key), kd["dedup_key_hex"].as_str().unwrap());

        // Auth key hash
        let hash = compute_auth_key_hash(&keys.auth_key);
        assert_eq!(hash, kd["auth_key_hash"].as_str().unwrap());
    }

    #[test]
    fn test_bip39_seed_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let mnemonic = fixture["key_derivation"]["mnemonic"].as_str().unwrap();
        let expected_seed_hex = fixture["key_derivation"]["bip39_seed_hex"].as_str().unwrap();

        let seed = mnemonic_to_seed_bytes(mnemonic).unwrap();
        assert_eq!(hex::encode(seed), expected_seed_hex);
    }

    #[test]
    fn test_lsh_seed_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let mnemonic = fixture["key_derivation"]["mnemonic"].as_str().unwrap();
        let keys = derive_keys_from_mnemonic(mnemonic).unwrap();
        let lsh_seed = derive_lsh_seed(mnemonic, &keys.salt).unwrap();

        assert_eq!(
            hex::encode(lsh_seed),
            fixture["lsh"]["lsh_seed_hex"].as_str().unwrap()
        );
    }

    #[test]
    fn test_aes_gcm_fixed_iv_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let aes = &fixture["aes_gcm"];
        let key_hex = aes["encryption_key_hex"].as_str().unwrap();
        let key_bytes = hex::decode(key_hex).unwrap();
        let mut key = [0u8; 32];
        key.copy_from_slice(&key_bytes);

        let iv = [0u8; 12]; // fixed zero IV
        let plaintext = aes["plaintext"].as_str().unwrap();
        let expected_b64 = aes["fixed_iv_encrypted_base64"].as_str().unwrap();

        let encrypted = encrypt_with_iv(plaintext, &key, &iv).unwrap();
        assert_eq!(encrypted, expected_b64);

        // Round-trip
        let decrypted = decrypt(&encrypted, &key).unwrap();
        assert_eq!(decrypted, plaintext);
    }

    #[test]
    fn test_aes_gcm_round_trip() {
        let keys = derive_keys_from_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        .unwrap();

        let plaintext = "Hello, TotalReclaw!";
        let encrypted = encrypt(plaintext, &keys.encryption_key).unwrap();
        let decrypted = decrypt(&encrypted, &keys.encryption_key).unwrap();
        assert_eq!(decrypted, plaintext);
    }
}
