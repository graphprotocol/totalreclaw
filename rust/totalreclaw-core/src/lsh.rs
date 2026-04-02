//! Locality-Sensitive Hashing (Random Hyperplane LSH).
//!
//! Matches `LSHHasher` in `mcp/src/subgraph/lsh.ts` byte-for-byte.
//!
//! Architecture:
//!   1. Seed (32 bytes) -> HKDF per table -> random bytes
//!   2. Random bytes -> Box-Muller transform -> Gaussian hyperplanes
//!   3. Embedding dot hyperplane -> sign bit -> N-bit signature per table
//!   4. Signature -> "lsh_t{table}_{signature}" -> SHA-256 -> blind hash hex

use sha2::{Digest, Sha256};

use crate::crypto::derive_random_bytes;
use crate::{Error, Result};

/// Default number of independent hash tables.
const DEFAULT_N_TABLES: usize = 20;

/// Default number of bits (hyperplanes) per table.
const DEFAULT_N_BITS: usize = 32;

/// Bytes per Gaussian float via Box-Muller (2 x uint32 = 8 bytes).
const BYTES_PER_FLOAT: usize = 8;

/// Random Hyperplane LSH hasher.
///
/// All state is deterministic from the seed. Construct once per session;
/// call `hash()` for every store/search operation.
pub struct LshHasher {
    /// Hyperplane matrices: `hyperplanes[t]` has length `dims * n_bits`.
    hyperplanes: Vec<Vec<f64>>,
    dims: usize,
    n_tables: usize,
    n_bits: usize,
}

impl LshHasher {
    /// Create a new LSH hasher with default parameters (20 tables, 32 bits).
    pub fn new(seed: &[u8], dims: usize) -> Result<Self> {
        Self::with_params(seed, dims, DEFAULT_N_TABLES, DEFAULT_N_BITS)
    }

    /// Create a new LSH hasher with custom parameters.
    pub fn with_params(
        seed: &[u8],
        dims: usize,
        n_tables: usize,
        n_bits: usize,
    ) -> Result<Self> {
        if seed.len() < 16 {
            return Err(Error::Lsh(format!(
                "seed too short: expected >= 16 bytes, got {}",
                seed.len()
            )));
        }
        if dims < 1 {
            return Err(Error::Lsh(format!("dims must be positive, got {}", dims)));
        }
        if n_tables < 1 {
            return Err(Error::Lsh(format!(
                "n_tables must be positive, got {}",
                n_tables
            )));
        }
        if n_bits < 1 {
            return Err(Error::Lsh(format!(
                "n_bits must be positive, got {}",
                n_bits
            )));
        }

        let mut hyperplanes = Vec::with_capacity(n_tables);
        for t in 0..n_tables {
            hyperplanes.push(generate_table_hyperplanes(seed, t, dims, n_bits)?);
        }

        Ok(Self {
            hyperplanes,
            dims,
            n_tables,
            n_bits,
        })
    }

    /// Hash an embedding vector to blind-hashed bucket IDs.
    ///
    /// Returns `n_tables` hex strings (one SHA-256 blind hash per table).
    pub fn hash(&self, embedding: &[f64]) -> Result<Vec<String>> {
        if embedding.len() != self.dims {
            return Err(Error::Lsh(format!(
                "embedding dimension mismatch: expected {}, got {}",
                self.dims,
                embedding.len()
            )));
        }

        let mut results = Vec::with_capacity(self.n_tables);

        for t in 0..self.n_tables {
            let matrix = &self.hyperplanes[t];

            // Build binary signature
            let mut signature = String::with_capacity(self.n_bits);
            for b in 0..self.n_bits {
                let base_offset = b * self.dims;
                let mut dot: f64 = 0.0;
                for d in 0..self.dims {
                    dot += matrix[base_offset + d] * embedding[d];
                }
                signature.push(if dot >= 0.0 { '1' } else { '0' });
            }

            let bucket_id = format!("lsh_t{}_{}", t, signature);

            // SHA-256 blind hash
            let hash = Sha256::digest(bucket_id.as_bytes());
            results.push(hex::encode(hash));
        }

        Ok(results)
    }

    /// Number of hash tables.
    pub fn tables(&self) -> usize {
        self.n_tables
    }

    /// Number of bits per table.
    pub fn bits(&self) -> usize {
        self.n_bits
    }

    /// Embedding dimensionality.
    pub fn dimensions(&self) -> usize {
        self.dims
    }

    /// Get the hyperplane values for a specific table (for testing).
    #[cfg(test)]
    pub fn get_hyperplanes(&self, table: usize) -> &[f64] {
        &self.hyperplanes[table]
    }
}

/// Generate hyperplane matrix for a single table using HKDF + Box-Muller.
fn generate_table_hyperplanes(
    seed: &[u8],
    table_index: usize,
    dims: usize,
    n_bits: usize,
) -> Result<Vec<f64>> {
    let total_floats = dims * n_bits;
    let total_bytes = total_floats * BYTES_PER_FLOAT;

    let base_info = format!("lsh_table_{}", table_index);
    let random_bytes = derive_random_bytes(seed, &base_info, total_bytes)?;

    let mut hyperplanes = Vec::with_capacity(total_floats);

    for i in 0..total_floats {
        let offset = i * BYTES_PER_FLOAT;

        // Read two uint32 little-endian
        let u1_raw = u32::from_le_bytes([
            random_bytes[offset],
            random_bytes[offset + 1],
            random_bytes[offset + 2],
            random_bytes[offset + 3],
        ]);
        let u2_raw = u32::from_le_bytes([
            random_bytes[offset + 4],
            random_bytes[offset + 5],
            random_bytes[offset + 6],
            random_bytes[offset + 7],
        ]);

        // Map to (0, 1]: (uint32 + 1) / (0xFFFFFFFF + 2)
        // In f64: 0xFFFFFFFF = 4294967295, + 2 = 4294967297
        let u1 = (u1_raw as f64 + 1.0) / (0xFFFF_FFFFu64 as f64 + 2.0);
        let u2 = (u2_raw as f64 + 1.0) / (0xFFFF_FFFFu64 as f64 + 2.0);

        // Box-Muller transform
        let gaussian = (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos();
        hyperplanes.push(gaussian);
    }

    Ok(hyperplanes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lsh_small_hyperplanes_parity() {
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

        let hasher = LshHasher::with_params(&seed, dims, n_tables, n_bits).unwrap();

        let expected_hp: Vec<f64> = small["first_hyperplanes_table0"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap())
            .collect();

        let actual_hp = hasher.get_hyperplanes(0);
        for (i, (a, e)) in actual_hp.iter().zip(expected_hp.iter()).enumerate() {
            assert!(
                (a - e).abs() < 1e-10,
                "Hyperplane[0][{}] mismatch: got {}, expected {}",
                i,
                a,
                e
            );
        }
    }

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
