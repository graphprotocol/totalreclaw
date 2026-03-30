//! Key derivation and AES-256-GCM encryption.
//!
//! Matches the TypeScript implementation in `mcp/src/subgraph/crypto.ts` byte-for-byte.
//!
//! Key derivation chain (BIP-39 path):
//!   mnemonic -> PBKDF2-HMAC-SHA512(mnemonic, "mnemonic", 2048) -> 64-byte seed
//!   salt = seed[0..32]
//!   HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1", 32)       -> authKey
//!   HKDF-SHA256(seed, salt, "totalreclaw-encryption-key-v1", 32) -> encryptionKey
//!   HKDF-SHA256(seed, salt, "openmemory-dedup-v1", 32)           -> dedupKey
//!   HKDF-SHA256(seed, salt, "openmemory-lsh-seed-v1", 32)        -> lshSeed
//!
//! AES-256-GCM wire format: iv(12) || tag(16) || ciphertext -> base64

use aes_gcm::{
    aead::{Aead, KeyInit, Payload},
    Aes256Gcm, Key, Nonce,
};
use hkdf::Hkdf;
use sha2::{Digest, Sha256, Sha512};

use crate::{Error, Result};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const AUTH_KEY_INFO: &[u8] = b"totalreclaw-auth-key-v1";
const ENCRYPTION_KEY_INFO: &[u8] = b"totalreclaw-encryption-key-v1";
const DEDUP_KEY_INFO: &[u8] = b"openmemory-dedup-v1";
const LSH_SEED_INFO: &[u8] = b"openmemory-lsh-seed-v1";

const IV_LENGTH: usize = 12;
const TAG_LENGTH: usize = 16;

// ---------------------------------------------------------------------------
// Key material
// ---------------------------------------------------------------------------

/// Derived key material from a BIP-39 mnemonic.
#[derive(Clone)]
pub struct DerivedKeys {
    pub auth_key: [u8; 32],
    pub encryption_key: [u8; 32],
    pub dedup_key: [u8; 32],
    pub salt: [u8; 32],
}

/// Derive the 64-byte BIP-39 seed from a mnemonic phrase.
///
/// Uses PBKDF2-HMAC-SHA512 with passphrase="mnemonic" and 2048 iterations,
/// matching the BIP-39 spec and `@scure/bip39`'s `mnemonicToSeedSync`.
fn mnemonic_to_seed(mnemonic: &str) -> Result<[u8; 64]> {
    // Validate words are in BIP-39 English wordlist
    let trimmed = mnemonic.trim();

    // Try strict parsing first (validates checksum)
    match bip39::Mnemonic::parse(trimmed) {
        Ok(_) => {}
        Err(_) => {
            // Lenient: check each word is in the wordlist
            let words: Vec<&str> = trimmed.split_whitespace().collect();
            if words.len() != 12 && words.len() != 24 {
                return Err(Error::InvalidMnemonic(format!(
                    "expected 12 or 24 words, got {}",
                    words.len()
                )));
            }
            let wordlist = bip39::Language::English.word_list();
            for word in &words {
                if !wordlist.contains(word) {
                    return Err(Error::InvalidMnemonic(format!(
                        "word '{}' not in BIP-39 English wordlist",
                        word
                    )));
                }
            }
        }
    }

    // PBKDF2-HMAC-SHA512: password = mnemonic (NFKD normalized), salt = "mnemonic", 2048 rounds
    // This matches @scure/bip39's mnemonicToSeedSync exactly.
    let mnemonic_nfkd = trimmed; // BIP-39 English words are ASCII, NFKD is identity
    let salt = b"mnemonic";

    let mut seed = [0u8; 64];
    pbkdf2::pbkdf2_hmac::<Sha512>(mnemonic_nfkd.as_bytes(), salt, 2048, &mut seed);

    Ok(seed)
}

/// Derive encryption keys from a BIP-39 mnemonic.
///
/// Matches `deriveKeysFromMnemonic()` in `mcp/src/subgraph/crypto.ts`.
pub fn derive_keys_from_mnemonic(mnemonic: &str) -> Result<DerivedKeys> {
    let seed = mnemonic_to_seed(mnemonic)?;

    // salt = seed[0..32]
    let mut salt = [0u8; 32];
    salt.copy_from_slice(&seed[..32]);

    // HKDF-SHA256 for each sub-key
    let auth_key = hkdf_sha256(&seed, &salt, AUTH_KEY_INFO)?;
    let encryption_key = hkdf_sha256(&seed, &salt, ENCRYPTION_KEY_INFO)?;
    let dedup_key = hkdf_sha256(&seed, &salt, DEDUP_KEY_INFO)?;

    Ok(DerivedKeys {
        auth_key,
        encryption_key,
        dedup_key,
        salt,
    })
}

/// Derive the 32-byte LSH seed from a BIP-39 mnemonic.
///
/// Matches `deriveLshSeed()` in `mcp/src/subgraph/crypto.ts` (BIP-39 path).
pub fn derive_lsh_seed(mnemonic: &str, salt: &[u8; 32]) -> Result<[u8; 32]> {
    let seed = mnemonic_to_seed(mnemonic)?;
    hkdf_sha256(&seed, salt, LSH_SEED_INFO)
}

/// Compute SHA-256(authKey) as a hex string.
///
/// Matches `computeAuthKeyHash()` in `mcp/src/subgraph/crypto.ts`.
pub fn compute_auth_key_hash(auth_key: &[u8; 32]) -> String {
    let hash = Sha256::digest(auth_key);
    hex::encode(hash)
}

/// Single HKDF-SHA256 expand producing 32 bytes.
fn hkdf_sha256(ikm: &[u8], salt: &[u8], info: &[u8]) -> Result<[u8; 32]> {
    let hk = Hkdf::<Sha256>::new(Some(salt), ikm);
    let mut okm = [0u8; 32];
    hk.expand(info, &mut okm)
        .map_err(|e| Error::Crypto(format!("HKDF expand failed: {}", e)))?;
    Ok(okm)
}

// ---------------------------------------------------------------------------
// AES-256-GCM
// ---------------------------------------------------------------------------

/// Encrypt a UTF-8 plaintext string with AES-256-GCM.
///
/// Wire format (base64-encoded): iv(12) || tag(16) || ciphertext
///
/// Uses a random 12-byte IV.
pub fn encrypt(plaintext: &str, encryption_key: &[u8; 32]) -> Result<String> {
    let iv_bytes: [u8; IV_LENGTH] = rand::random();
    encrypt_with_iv(plaintext, encryption_key, &iv_bytes)
}

/// Encrypt with a specific IV (for deterministic testing).
pub fn encrypt_with_iv(
    plaintext: &str,
    encryption_key: &[u8; 32],
    iv: &[u8; IV_LENGTH],
) -> Result<String> {
    let key = Key::<Aes256Gcm>::from_slice(encryption_key);
    let cipher = Aes256Gcm::new(key);
    let nonce = Nonce::from_slice(iv);

    let ciphertext_with_tag = cipher
        .encrypt(nonce, Payload { msg: plaintext.as_bytes(), aad: b"" })
        .map_err(|e| Error::Crypto(format!("AES-GCM encrypt failed: {}", e)))?;

    // aes-gcm appends the tag at the end: ciphertext || tag
    // We need wire format: iv || tag || ciphertext
    let ct_len = ciphertext_with_tag.len() - TAG_LENGTH;
    let ciphertext = &ciphertext_with_tag[..ct_len];
    let tag = &ciphertext_with_tag[ct_len..];

    let mut combined = Vec::with_capacity(IV_LENGTH + TAG_LENGTH + ct_len);
    combined.extend_from_slice(iv);
    combined.extend_from_slice(tag);
    combined.extend_from_slice(ciphertext);

    use base64::Engine;
    Ok(base64::engine::general_purpose::STANDARD.encode(&combined))
}

/// Decrypt a base64-encoded AES-256-GCM blob back to a UTF-8 string.
///
/// Expects wire format: iv(12) || tag(16) || ciphertext
pub fn decrypt(encrypted_base64: &str, encryption_key: &[u8; 32]) -> Result<String> {
    use base64::Engine;
    let combined = base64::engine::general_purpose::STANDARD
        .decode(encrypted_base64)
        .map_err(|e| Error::Crypto(format!("base64 decode failed: {}", e)))?;

    if combined.len() < IV_LENGTH + TAG_LENGTH {
        return Err(Error::Crypto("Encrypted data too short".into()));
    }

    let iv = &combined[..IV_LENGTH];
    let tag = &combined[IV_LENGTH..IV_LENGTH + TAG_LENGTH];
    let ciphertext = &combined[IV_LENGTH + TAG_LENGTH..];

    // aes-gcm expects: ciphertext || tag
    let mut ct_with_tag = Vec::with_capacity(ciphertext.len() + TAG_LENGTH);
    ct_with_tag.extend_from_slice(ciphertext);
    ct_with_tag.extend_from_slice(tag);

    let key = Key::<Aes256Gcm>::from_slice(encryption_key);
    let cipher = Aes256Gcm::new(key);
    let nonce = Nonce::from_slice(iv);

    let plaintext_bytes = cipher
        .decrypt(nonce, Payload { msg: &ct_with_tag, aad: b"" })
        .map_err(|e| Error::Crypto(format!("AES-GCM decrypt failed: {}", e)))?;

    String::from_utf8(plaintext_bytes)
        .map_err(|e| Error::Crypto(format!("UTF-8 decode failed: {}", e)))
}

// ---------------------------------------------------------------------------
// HKDF utilities for LSH (chunked output)
// ---------------------------------------------------------------------------

/// Derive `length` pseudo-random bytes using chunked HKDF-SHA256.
///
/// A single HKDF-SHA256 call can output at most 255 * 32 = 8160 bytes.
/// For larger outputs we iterate over sub-block indices in the info string.
///
/// Matches `deriveRandomBytes()` in `mcp/src/subgraph/lsh.ts`.
pub fn derive_random_bytes(seed: &[u8], base_info: &str, length: usize) -> Result<Vec<u8>> {
    const MAX_HKDF_OUTPUT: usize = 255 * 32;
    let mut result = vec![0u8; length];
    let mut offset = 0;
    let mut block_index = 0;

    while offset < length {
        let remaining = length - offset;
        let chunk_len = remaining.min(MAX_HKDF_OUTPUT);
        let info = format!("{}_block_{}", base_info, block_index);

        // Empty salt (matches TypeScript: `new Uint8Array(0)`)
        let hk = Hkdf::<Sha256>::new(Some(&[]), seed);
        hk.expand(info.as_bytes(), &mut result[offset..offset + chunk_len])
            .map_err(|e| Error::Crypto(format!("HKDF expand failed: {}", e)))?;

        offset += chunk_len;
        block_index += 1;
    }

    Ok(result)
}

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

        let seed = mnemonic_to_seed(mnemonic).unwrap();
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
