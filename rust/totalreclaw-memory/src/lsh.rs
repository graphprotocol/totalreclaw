//! Locality-Sensitive Hashing (Random Hyperplane LSH).
//!
//! Delegates to `totalreclaw_core::lsh` — the canonical implementation.
//! Re-exports all public items for backward compatibility.

pub use totalreclaw_core::lsh::*;

#[cfg(test)]
mod tests {
    use super::*;

    // Note: test_lsh_small_hyperplanes_parity uses #[cfg(test)]-only get_hyperplanes()
    // which is not accessible from downstream crates. That test lives in totalreclaw-core.

    #[test]
    fn test_lsh_small_hashes_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let lsh_seed_hex = fixture["lsh"]["lsh_seed_hex"].as_str().unwrap();
        let seed = hex::decode(lsh_seed_hex).unwrap();

        let small = &fixture["lsh"]["small"];
        let dims = small["dims"].as_u64().unwrap() as usize;
        let n_tables = small["n_tables"].as_u64().unwrap() as usize;
        let n_bits = small["n_bits"].as_u64().unwrap() as usize;

        let embedding: Vec<f64> = small["embedding"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap())
            .collect();

        let expected_hashes: Vec<String> = small["hashes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();

        let hasher = LshHasher::with_params(&seed, dims, n_tables, n_bits).unwrap();
        let hashes = hasher.hash(&embedding).unwrap();

        assert_eq!(hashes, expected_hashes, "LSH small hashes mismatch");
    }

    #[test]
    fn test_lsh_real_hashes_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let lsh_seed_hex = fixture["lsh"]["lsh_seed_hex"].as_str().unwrap();
        let seed = hex::decode(lsh_seed_hex).unwrap();

        let real = &fixture["lsh"]["real"];
        let dims = real["dims"].as_u64().unwrap() as usize;
        let n_tables = real["n_tables"].as_u64().unwrap() as usize;
        let n_bits = real["n_bits"].as_u64().unwrap() as usize;

        // Reconstruct embedding: sin(i * 0.1) * 0.5 for i in 0..1024
        let embedding: Vec<f64> = (0..dims).map(|i| (i as f64 * 0.1).sin() * 0.5).collect();

        // Verify first 10 match fixture
        let expected_first_10: Vec<f64> = real["embedding_first_10"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap())
            .collect();
        for (i, (a, e)) in embedding.iter().zip(expected_first_10.iter()).enumerate() {
            assert!(
                (a - e).abs() < 1e-14,
                "Embedding[{}] mismatch: got {}, expected {}",
                i,
                a,
                e
            );
        }

        let expected_hashes: Vec<String> = real["hashes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();

        let hasher = LshHasher::with_params(&seed, dims, n_tables, n_bits).unwrap();
        let hashes = hasher.hash(&embedding).unwrap();

        assert_eq!(hashes, expected_hashes, "LSH real (1024d) hashes mismatch");
    }
}
