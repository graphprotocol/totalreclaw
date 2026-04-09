//! Key derivation and XChaCha20-Poly1305 encryption.
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
//! XChaCha20-Poly1305 wire format: nonce(24) || tag(16) || ciphertext -> base64

use chacha20poly1305::{
    aead::{Aead, KeyInit, Payload},
    XChaCha20Poly1305, Key, XNonce,
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

const NONCE_LENGTH: usize = 24;
const TAG_LENGTH: usize = 16;

// ---------------------------------------------------------------------------
// Key material
// ---------------------------------------------------------------------------

/// Derived key material from a BIP-39 mnemonic.
#[derive(Clone, Debug)]
pub struct DerivedKeys {
    pub auth_key: [u8; 32],
    pub encryption_key: [u8; 32],
    pub dedup_key: [u8; 32],
    pub salt: [u8; 32],
}

/// Derive the 64-byte BIP-39 seed from a mnemonic phrase (strict mode).
///
/// Uses PBKDF2-HMAC-SHA512 with passphrase="mnemonic" and 2048 iterations,
/// matching the BIP-39 spec and `@scure/bip39`'s `mnemonicToSeedSync`.
fn mnemonic_to_seed(mnemonic: &str) -> Result<[u8; 64]> {
    // Validate BIP-39 mnemonic (strict: checksum must be valid).
    let trimmed = mnemonic.trim();
    bip39::Mnemonic::parse(trimmed).map_err(|e| {
        Error::InvalidMnemonic(format!("invalid BIP-39 mnemonic: {}", e))
    })?;

    pbkdf2_seed(trimmed)
}

/// Derive the 64-byte BIP-39 seed from a mnemonic phrase (lenient mode).
///
/// Validates that all words are in the BIP-39 English wordlist but does NOT
/// check the checksum. This allows LLM-generated mnemonics where the words
/// are valid but the checksum is wrong.
fn mnemonic_to_seed_lenient(mnemonic: &str) -> Result<[u8; 64]> {
    let trimmed = mnemonic.trim();
    let words: Vec<&str> = trimmed.split_whitespace().collect();

    // Must be 12 or 24 words
    if words.len() != 12 && words.len() != 24 {
        return Err(Error::InvalidMnemonic(format!(
            "expected 12 or 24 words, got {}",
            words.len()
        )));
    }

    // Validate each word is in the BIP-39 English wordlist
    let lang = bip39::Language::English;
    for word in &words {
        if lang.find_word(word).is_none() {
            return Err(Error::InvalidMnemonic(format!(
                "word '{}' not in BIP-39 English wordlist",
                word
            )));
        }
    }

    // Derive seed (skip checksum validation)
    pbkdf2_seed(trimmed)
}

/// PBKDF2-HMAC-SHA512 seed derivation (shared between strict and lenient).
fn pbkdf2_seed(mnemonic: &str) -> Result<[u8; 64]> {
    let salt = b"mnemonic";
    let mut seed = [0u8; 64];
    pbkdf2::pbkdf2_hmac::<Sha512>(mnemonic.as_bytes(), salt, 2048, &mut seed);
    Ok(seed)
}

/// Derive encryption keys from a BIP-39 mnemonic (strict checksum validation).
///
/// Matches `deriveKeysFromMnemonic()` in `mcp/src/subgraph/crypto.ts`.
pub fn derive_keys_from_mnemonic(mnemonic: &str) -> Result<DerivedKeys> {
    let seed = mnemonic_to_seed(mnemonic)?;
    derive_keys_from_seed(&seed)
}

/// Derive encryption keys from a BIP-39 mnemonic (lenient — skips checksum).
///
/// Validates words are in the BIP-39 wordlist but accepts invalid checksums.
/// Use this for LLM-generated mnemonics.
pub fn derive_keys_from_mnemonic_lenient(mnemonic: &str) -> Result<DerivedKeys> {
    let seed = mnemonic_to_seed_lenient(mnemonic)?;
    derive_keys_from_seed(&seed)
}

/// Internal: derive keys from a 64-byte seed.
fn derive_keys_from_seed(seed: &[u8; 64]) -> Result<DerivedKeys> {
    let mut salt = [0u8; 32];
    salt.copy_from_slice(&seed[..32]);

    let auth_key = hkdf_sha256(seed, &salt, AUTH_KEY_INFO)?;
    let encryption_key = hkdf_sha256(seed, &salt, ENCRYPTION_KEY_INFO)?;
    let dedup_key = hkdf_sha256(seed, &salt, DEDUP_KEY_INFO)?;

    Ok(DerivedKeys {
        auth_key,
        encryption_key,
        dedup_key,
        salt,
    })
}

/// Public access to the raw BIP-39 seed bytes (64 bytes).
/// Used by wallet derivation (BIP-32/BIP-44).
pub fn mnemonic_to_seed_bytes(mnemonic: &str) -> Result<[u8; 64]> {
    mnemonic_to_seed(mnemonic)
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
// XChaCha20-Poly1305
// ---------------------------------------------------------------------------

/// Encrypt a UTF-8 plaintext string with XChaCha20-Poly1305.
///
/// Wire format (base64-encoded): nonce(24) || tag(16) || ciphertext
///
/// Uses a random 24-byte nonce.
pub fn encrypt(plaintext: &str, encryption_key: &[u8; 32]) -> Result<String> {
    let nonce_bytes: [u8; NONCE_LENGTH] = rand::random();
    encrypt_with_nonce(plaintext, encryption_key, &nonce_bytes)
}

/// Encrypt with a specific nonce (for deterministic testing).
pub fn encrypt_with_nonce(
    plaintext: &str,
    encryption_key: &[u8; 32],
    nonce: &[u8; NONCE_LENGTH],
) -> Result<String> {
    let key = Key::from_slice(encryption_key);
    let cipher = XChaCha20Poly1305::new(key);
    let xnonce = XNonce::from_slice(nonce);

    let ciphertext_with_tag = cipher
        .encrypt(xnonce, Payload { msg: plaintext.as_bytes(), aad: b"" })
        .map_err(|e| Error::Crypto(format!("XChaCha20-Poly1305 encrypt failed: {}", e)))?;

    // chacha20poly1305 appends the tag at the end: ciphertext || tag
    // We need wire format: nonce || tag || ciphertext
    let ct_len = ciphertext_with_tag.len() - TAG_LENGTH;
    let ciphertext = &ciphertext_with_tag[..ct_len];
    let tag = &ciphertext_with_tag[ct_len..];

    let mut combined = Vec::with_capacity(NONCE_LENGTH + TAG_LENGTH + ct_len);
    combined.extend_from_slice(nonce);
    combined.extend_from_slice(tag);
    combined.extend_from_slice(ciphertext);

    use base64::Engine;
    Ok(base64::engine::general_purpose::STANDARD.encode(&combined))
}

/// Decrypt a base64-encoded XChaCha20-Poly1305 blob back to a UTF-8 string.
///
/// Expects wire format: nonce(24) || tag(16) || ciphertext
pub fn decrypt(encrypted_base64: &str, encryption_key: &[u8; 32]) -> Result<String> {
    use base64::Engine;
    let combined = base64::engine::general_purpose::STANDARD
        .decode(encrypted_base64)
        .map_err(|e| Error::Crypto(format!("base64 decode failed: {}", e)))?;

    if combined.len() < NONCE_LENGTH + TAG_LENGTH {
        return Err(Error::Crypto("Encrypted data too short".into()));
    }

    let nonce = &combined[..NONCE_LENGTH];
    let tag = &combined[NONCE_LENGTH..NONCE_LENGTH + TAG_LENGTH];
    let ciphertext = &combined[NONCE_LENGTH + TAG_LENGTH..];

    // chacha20poly1305 expects: ciphertext || tag
    let mut ct_with_tag = Vec::with_capacity(ciphertext.len() + TAG_LENGTH);
    ct_with_tag.extend_from_slice(ciphertext);
    ct_with_tag.extend_from_slice(tag);

    let key = Key::from_slice(encryption_key);
    let cipher = XChaCha20Poly1305::new(key);
    let xnonce = XNonce::from_slice(nonce);

    let plaintext_bytes = cipher
        .decrypt(xnonce, Payload { msg: &ct_with_tag, aad: b"" })
        .map_err(|e| Error::Crypto(format!("XChaCha20-Poly1305 decrypt failed: {}", e)))?;

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
    fn test_xchacha_fixed_nonce_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let xc = &fixture["xchacha20"];
        let key_hex = xc["encryption_key_hex"].as_str().unwrap();
        let key_bytes = hex::decode(key_hex).unwrap();
        let mut key = [0u8; 32];
        key.copy_from_slice(&key_bytes);

        let nonce_hex = xc["fixed_nonce_hex"].as_str().unwrap();
        let nonce_bytes = hex::decode(nonce_hex).unwrap();
        let mut nonce = [0u8; 24];
        nonce.copy_from_slice(&nonce_bytes);

        let plaintext = xc["plaintext"].as_str().unwrap();
        let expected_b64 = xc["fixed_nonce_encrypted_base64"].as_str().unwrap();

        let encrypted = encrypt_with_nonce(plaintext, &key, &nonce).unwrap();
        assert_eq!(encrypted, expected_b64);

        let decrypted = decrypt(&encrypted, &key).unwrap();
        assert_eq!(decrypted, plaintext);
    }

    #[test]
    fn test_xchacha_round_trip() {
        let keys = derive_keys_from_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        )
        .unwrap();

        let plaintext = "Hello, TotalReclaw!";
        let encrypted = encrypt(plaintext, &keys.encryption_key).unwrap();
        let decrypted = decrypt(&encrypted, &keys.encryption_key).unwrap();
        assert_eq!(decrypted, plaintext);
    }

    #[test]
    fn test_lenient_accepts_valid_mnemonic() {
        // A valid BIP-39 mnemonic should work in lenient mode too
        let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
        let strict = derive_keys_from_mnemonic(mnemonic).unwrap();
        let lenient = derive_keys_from_mnemonic_lenient(mnemonic).unwrap();

        assert_eq!(strict.auth_key, lenient.auth_key);
        assert_eq!(strict.encryption_key, lenient.encryption_key);
        assert_eq!(strict.dedup_key, lenient.dedup_key);
        assert_eq!(strict.salt, lenient.salt);
    }

    #[test]
    fn test_lenient_rejects_invalid_words() {
        let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon xyzzy";
        let result = derive_keys_from_mnemonic_lenient(mnemonic);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("xyzzy"));
    }

    #[test]
    fn test_lenient_rejects_wrong_word_count() {
        let mnemonic = "abandon abandon abandon"; // only 3 words
        let result = derive_keys_from_mnemonic_lenient(mnemonic);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("expected 12 or 24"));
    }

    #[test]
    fn test_strict_rejects_bad_checksum() {
        // All valid BIP-39 words but wrong checksum
        let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon";
        let result = derive_keys_from_mnemonic(mnemonic);
        assert!(result.is_err());
    }

    #[test]
    fn test_lenient_accepts_bad_checksum() {
        // All valid BIP-39 words but wrong checksum — lenient should accept
        let mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon";
        let result = derive_keys_from_mnemonic_lenient(mnemonic);
        assert!(result.is_ok());
    }

}
