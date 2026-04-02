//! Content fingerprint (HMAC-SHA256 with NFC normalization).
//!
//! Matches `generateContentFingerprint()` in `mcp/src/subgraph/crypto.ts`.
//!
//! Normalization:
//!   1. Unicode NFC normalization
//!   2. Lowercase
//!   3. Collapse whitespace (spaces/tabs/newlines to single space)
//!   4. Trim leading/trailing whitespace

use hmac::{Hmac, Mac};
use sha2::Sha256;
use unicode_normalization::UnicodeNormalization;

type HmacSha256 = Hmac<Sha256>;

/// Normalize text for deterministic fingerprinting.
///
/// Steps:
///   1. Unicode NFC normalization
///   2. Lowercase
///   3. Collapse whitespace to single space
///   4. Trim
pub fn normalize_text(text: &str) -> String {
    let nfc: String = text.nfc().collect();
    let lowered = nfc.to_lowercase();

    // Collapse whitespace
    let mut result = String::with_capacity(lowered.len());
    let mut prev_ws = false;
    for c in lowered.chars() {
        if c.is_whitespace() {
            if !prev_ws {
                result.push(' ');
            }
            prev_ws = true;
        } else {
            result.push(c);
            prev_ws = false;
        }
    }

    result.trim().to_string()
}

/// Compute an HMAC-SHA256 content fingerprint.
///
/// Returns a 64-character hex string.
pub fn generate_content_fingerprint(plaintext: &str, dedup_key: &[u8; 32]) -> String {
    let normalized = normalize_text(plaintext);
    let mut mac = HmacSha256::new_from_slice(dedup_key).expect("HMAC key length is always valid");
    mac.update(normalized.as_bytes());
    let result = mac.finalize().into_bytes();
    hex::encode(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fingerprint_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let dedup_key_hex = fixture["content_fingerprint"]["dedup_key_hex"]
            .as_str()
            .unwrap();
        let dedup_key_bytes = hex::decode(dedup_key_hex).unwrap();
        let mut dedup_key = [0u8; 32];
        dedup_key.copy_from_slice(&dedup_key_bytes);

        let test_cases = fixture["content_fingerprint"]["test_cases"]
            .as_array()
            .unwrap();
        for tc in test_cases {
            let text = tc["text"].as_str().unwrap();
            let expected = tc["fingerprint"].as_str().unwrap();

            let result = generate_content_fingerprint(text, &dedup_key);
            assert_eq!(
                result, expected,
                "Fingerprint mismatch for text: {:?}",
                text
            );
        }
    }

    #[test]
    fn test_normalization_collapses_whitespace() {
        // Two texts that normalize to the same thing should produce the same fingerprint
        let key = [0u8; 32];
        let fp1 = generate_content_fingerprint("hello  world", &key);
        let fp2 = generate_content_fingerprint("  hello   world  ", &key);
        assert_eq!(fp1, fp2);
    }
}
