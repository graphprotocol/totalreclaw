//! Content fingerprint (HMAC-SHA256 with NFC normalization).
//!
//! Delegates to `totalreclaw_core::fingerprint` — the canonical implementation.
//! Re-exports all public items for backward compatibility.

pub use totalreclaw_core::fingerprint::*;

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
        let key = [0u8; 32];
        let fp1 = generate_content_fingerprint("hello  world", &key);
        let fp2 = generate_content_fingerprint("  hello   world  ", &key);
        assert_eq!(fp1, fp2);
    }
}
