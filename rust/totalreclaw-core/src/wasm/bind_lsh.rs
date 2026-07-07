//! `bind_lsh` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// LSH Hasher
// ---------------------------------------------------------------------------

/// Random Hyperplane LSH hasher (WASM wrapper).
///
/// Construct with `new WasmLshHasher(seedHex, dims)`.
/// Call `hash(embeddingFloat64Array)` to get bucket IDs.
#[wasm_bindgen]
pub struct WasmLshHasher {
    pub(crate) inner: lsh::LshHasher,
}

#[wasm_bindgen]
impl WasmLshHasher {
    /// Create a new LSH hasher with default parameters (20 tables, 32 bits).
    ///
    /// `seed_hex`: hex-encoded seed (>= 32 chars = 16 bytes).
    /// `dims`: embedding dimensionality (e.g. 640).
    #[wasm_bindgen(constructor)]
    pub fn new(seed_hex: &str, dims: usize) -> Result<WasmLshHasher, JsError> {
        let seed =
            hex::decode(seed_hex).map_err(|e| JsError::new(&format!("invalid seed hex: {}", e)))?;
        let inner = lsh::LshHasher::new(&seed, dims).map_err(to_js_error)?;
        Ok(WasmLshHasher { inner })
    }

    /// Create a new LSH hasher with custom parameters.
    ///
    /// `seed_hex`: hex-encoded seed.
    /// `dims`: embedding dimensionality.
    /// `n_tables`: number of hash tables.
    /// `n_bits`: bits per table.
    #[wasm_bindgen(js_name = "withParams")]
    pub fn with_params(
        seed_hex: &str,
        dims: usize,
        n_tables: usize,
        n_bits: usize,
    ) -> Result<WasmLshHasher, JsError> {
        let seed =
            hex::decode(seed_hex).map_err(|e| JsError::new(&format!("invalid seed hex: {}", e)))?;
        let inner =
            lsh::LshHasher::with_params(&seed, dims, n_tables, n_bits).map_err(to_js_error)?;
        Ok(WasmLshHasher { inner })
    }

    /// Hash an embedding vector to blind-hashed bucket IDs.
    ///
    /// `embedding`: Float64Array of length `dims`.
    /// Returns a JSON array of hex strings (one per table).
    pub fn hash(&self, embedding: &[f64]) -> Result<JsValue, JsError> {
        let hashes = self.inner.hash(embedding).map_err(to_js_error)?;
        serde_wasm_bindgen::to_value(&hashes).map_err(|e| JsError::new(&e.to_string()))
    }

    /// Number of hash tables.
    #[wasm_bindgen(getter)]
    pub fn tables(&self) -> usize {
        self.inner.tables()
    }

    /// Bits per table.
    #[wasm_bindgen(getter)]
    pub fn bits(&self) -> usize {
        self.inner.bits()
    }

    /// Embedding dimensionality.
    #[wasm_bindgen(getter)]
    pub fn dimensions(&self) -> usize {
        self.inner.dimensions()
    }
}

