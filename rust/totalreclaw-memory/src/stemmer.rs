//! Porter Stemmer (Porter 1 algorithm).
//!
//! Delegates to `totalreclaw_core::stemmer` — the canonical implementation.
//! Re-exports all public items for backward compatibility.

pub use totalreclaw_core::stemmer::*;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_stemmer_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let stemmer_tests = fixture["porter_stemmer"].as_array().unwrap();
        for test in stemmer_tests {
            let word = test["word"].as_str().unwrap();
            let expected = test["stem"].as_str().unwrap();
            let result = stem(word);
            assert_eq!(
                result, expected,
                "Porter stem('{}') = '{}', expected '{}'",
                word, result, expected
            );
        }
    }

    #[test]
    fn test_basic_stems() {
        assert_eq!(stem("caresses"), "caress");
        assert_eq!(stem("ponies"), "poni");
        assert_eq!(stem("cats"), "cat");
        assert_eq!(stem("agreed"), "agre");
        assert_eq!(stem("disabled"), "disabl");
        assert_eq!(stem("matting"), "mat");
        assert_eq!(stem("mating"), "mate");
        assert_eq!(stem("meeting"), "meet");
        assert_eq!(stem("milling"), "mill");
        assert_eq!(stem("messing"), "mess");
        assert_eq!(stem("meetings"), "meet");
    }
}
