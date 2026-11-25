//! Blind index generation.
//!
//! Two schemes live here. [`generate_blind_indices`] is the legacy UNKEYED one
//! (plain SHA-256 of words and stems; removed in core 3.0.0). [`BlindIndexer`]
//! is index scheme 1: HMAC-SHA256 under a per-vault key, stems only.
//!
//! Legacy scheme:
//!
//! Matches `generateBlindIndices()` in `mcp/src/subgraph/crypto.ts`.
//!
//! Tokenization:
//!   1. Lowercase
//!   2. Replace non-alphanumeric/whitespace with space (Unicode-aware)
//!   3. Split on whitespace
//!   4. Filter tokens shorter than 2 characters
//!   5. SHA-256 each token -> hex
//!   6. Porter stem each token; if len >= 2, SHA-256("stem:" + stem) -> hex (always, even if stem == token)
//!   7. Deduplicate (preserving insertion order)

use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashSet};

use crate::crypto::{derive_blind_index_key, SecretKey};
use crate::fingerprint::lowercase_nfc;
use crate::{stemmer, Result};

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

        // Stemmed word hash — ALWAYS generated (even when stem == token).
        // This ensures "prefer" (stem=prefer) and "preferences" (stem=prefer)
        // both produce SHA256("stem:prefer") and match each other.
        let stem = stemmer::stem(token);
        if stem.len() >= 2 {
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
/// 2. Replace non-(letter/number/whitespace) with space (Unicode \p{L}\p{N}\s)
/// 3. Split on whitespace
/// 4. Filter tokens shorter than 2 chars
fn tokenize(text: &str) -> Vec<String> {
    split_tokens(&text.to_lowercase())
}

/// Steps 2-4 of [`tokenize`], on already-lowercased text.
fn split_tokens(lowered: &str) -> Vec<String> {
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

// ---------------------------------------------------------------------------
// Keyed blind indices (index scheme 1, internal#573)
// ---------------------------------------------------------------------------

/// Domain prefixes of scheme 1. Prefix-free: no domain is a prefix of another,
/// so `domain || value` decodes in exactly one way.
const DOMAIN_STEM: &str = "stem:";
const DOMAIN_ENTITY: &str = "entity:";
const DOMAIN_TYPE: &str = "type:";

/// Keyed blind-tag generator: `tag(domain, value) =
/// hex(HMAC-SHA256(indexKey, domain || value))`, `indexKey =
/// HKDF(encryptionKey, "", "totalreclaw-blind-index-v1")`.
///
/// Holds the key privately. No `Debug`, no `Clone`, no `Serialize`, no key
/// getter. Every method returns tags sorted and deduplicated where it returns
/// a list. Nothing here calls the unkeyed [`generate_blind_indices`].
pub struct BlindIndexer {
    key: SecretKey,
}

impl BlindIndexer {
    /// Build an indexer from the vault's `encryptionKey`.
    pub fn new(encryption_key: &[u8; 32]) -> Result<Self> {
        Ok(Self { key: derive_blind_index_key(encryption_key)? })
    }

    fn tag(&self, domain: &str, value: &str) -> String {
        let mut message = Vec::with_capacity(domain.len() + value.len());
        message.extend_from_slice(domain.as_bytes());
        message.extend_from_slice(value.as_bytes());
        self.key.hmac_hex(&message)
    }

    /// Stem tags of a text, sorted and deduplicated. Used for both store and
    /// query, so a stored fact and a query for the same words always meet.
    ///
    /// Tokenizer (frozen by scheme 1): lowercase, NFC, replace every character
    /// that is neither alphanumeric nor whitespace with a space, split on
    /// whitespace, drop tokens under 2 BYTES, Porter-stem (ASCII only); a stem
    /// under 2 bytes falls back to the token. Words themselves are not tagged.
    ///
    /// Character classes: alphanumeric = Unicode `Alphabetic` or General_Category
    /// `N*` (Rust `char::is_alphanumeric`); whitespace = `White_Space`
    /// (`char::is_whitespace`); Unicode version = that of the pinned release
    /// toolchain (`rust-toolchain.toml`). Combining marks that are `Alphabetic`
    /// (Devanagari vowel signs, Arabic and Hebrew vowel points, some Thai vowels)
    /// stay in the word; the others (virama, Thai tone marks) split it.
    pub fn stem_tags(&self, text: &str) -> Vec<String> {
        let tags: BTreeSet<String> =
            stem_inputs(text).iter().map(|input| self.tag(DOMAIN_STEM, input)).collect();
        tags.into_iter().collect()
    }

    /// Tag of an entity name: `tag("entity:", normalize_entity_name(name))`.
    pub fn entity_tag(&self, name: &str) -> String {
        self.tag(DOMAIN_ENTITY, &crate::claims::normalize_entity_name(name))
    }

    /// Tag of the digest-fact marker: `tag("type:", "digest")`.
    pub fn digest_marker_tag(&self) -> String {
        self.tag(DOMAIN_TYPE, "digest")
    }
}

/// The strings that get a stem tag: the Porter stem of each token, or the token
/// itself when the stem is under 2 bytes (`"ies"` stems to `"i"`).
fn stem_inputs(text: &str) -> Vec<String> {
    split_tokens(&lowercase_nfc(text))
        .into_iter()
        .map(|token| {
            let stem = stemmer::stem(&token);
            if stem.len() >= 2 { stem } else { token }
        })
        .collect()
}

/// Sort ascending (byte order of the lowercase hex) and deduplicate.
/// Apply to the combined stem + entity + marker + LSH list before it is
/// written or, for a query, before it is split into batches.
pub fn sort_dedup_tags(tags: Vec<String>) -> Vec<String> {
    tags.into_iter().collect::<BTreeSet<_>>().into_iter().collect()
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
