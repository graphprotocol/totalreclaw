//! Blind index generation (SHA-256 token hashing + Porter stemming).
//!
//! Delegates to `totalreclaw_core::blind` — the canonical implementation.
//! Re-exports all public items for backward compatibility.

pub use totalreclaw_core::blind::*;

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
}
