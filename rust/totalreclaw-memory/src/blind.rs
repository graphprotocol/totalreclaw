//! Blind index generation (SHA-256 token hashing + Porter stemming).
//!
//! Matches `generateBlindIndices()` in `mcp/src/subgraph/crypto.ts`.
//!
//! Tokenization:
//!   1. Lowercase
//!   2. Replace non-alphanumeric/whitespace with space (Unicode-aware)
//!   3. Split on whitespace
//!   4. Filter tokens shorter than 2 characters
//!   5. SHA-256 each token -> hex
//!   6. Porter stem each token; if stem != token and len >= 2, SHA-256("stem:" + stem) -> hex
//!   7. Deduplicate (preserving insertion order)

use sha2::{Digest, Sha256};
use std::collections::HashSet;

use crate::stemmer;

/// Generate blind indices for a text string.
///
/// Returns deduplicated hex-encoded SHA-256 hashes of tokens and their stems.
pub fn generate_blind_indices(text: &str) -> Vec<String> {
    let tokens = tokenize(text);

    let mut seen = HashSet::new();
    let mut indices = Vec::new();

    for token in &tokens {
        // Exact word hash
        let hash = sha256_hex(token.as_bytes());
        if seen.insert(hash.clone()) {
            indices.push(hash);
        }

        // Stemmed word hash
        let stem = stemmer::stem(token);
        if stem.len() >= 2 && stem != *token {
            let stem_input = format!("stem:{}", stem);
            let stem_hash = sha256_hex(stem_input.as_bytes());
            if seen.insert(stem_hash.clone()) {
                indices.push(stem_hash);
            }
        }
    }

    indices
}

/// Tokenize text for blind indexing.
///
/// 1. Lowercase
/// 2. Replace non-alphanumeric/non-whitespace chars with space (Unicode \p{L}\p{N}\s)
/// 3. Split on whitespace
/// 4. Filter tokens shorter than 2 chars
fn tokenize(text: &str) -> Vec<String> {
    let lowered = text.to_lowercase();

    // Replace non-(letter/number/whitespace) with space
    // This matches the TypeScript regex: /[^\p{L}\p{N}\s]/gu
    let cleaned: String = lowered
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c.is_whitespace() {
                c
            } else {
                ' '
            }
        })
        .collect();

    cleaned
        .split_whitespace()
        .filter(|t| t.len() >= 2)
        .map(|t| t.to_string())
        .collect()
}

fn sha256_hex(data: &[u8]) -> String {
    let hash = Sha256::digest(data);
    hex::encode(hash)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_blind_indices_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let test_cases = fixture["blind_indices"]["test_cases"].as_array().unwrap();
        for tc in test_cases {
            let text = tc["text"].as_str().unwrap();
            let expected: Vec<String> = tc["indices"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect();

            let result = generate_blind_indices(text);
            assert_eq!(
                result, expected,
                "Blind indices mismatch for text: {:?}\n  got:      {:?}\n  expected: {:?}",
                text, result, expected
            );
        }
    }

    #[test]
    fn test_token_hash_mappings() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let mappings = fixture["blind_indices"]["token_hash_mappings"]
            .as_object()
            .unwrap();
        for (token, expected_hash) in mappings {
            let hash = sha256_hex(token.as_bytes());
            assert_eq!(
                hash,
                expected_hash.as_str().unwrap(),
                "Token hash mismatch for '{}'",
                token
            );
        }
    }
}
