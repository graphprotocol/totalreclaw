//! `bind_lsh` PyO3 bindings (split out of the former monolithic python.rs).
//!
//! Shared imports + helpers (`to_pyerr`, `bytes_to_array32`) come from the
//! parent module via `use super::*;`. Registered in `super`'s `#[pymodule]`.

use super::*;

// ---------------------------------------------------------------------------
// LSH Hasher
// ---------------------------------------------------------------------------

/// Locality-Sensitive Hashing (Random Hyperplane LSH).
///
/// Construct with a seed (bytes) and embedding dimensionality.
/// Call ``hash()`` with an embedding vector to get blind-hashed bucket IDs.
#[pyclass(name = "LshHasher")]
pub(crate) struct PyLshHasher {
    pub(crate) inner: lsh::LshHasher,
}

#[pymethods]
impl PyLshHasher {
    /// Create a new LSH hasher.
    ///
    /// Args:
    ///     seed: 32-byte seed (from derive_lsh_seed).
    ///     dims: Embedding dimensionality (e.g. 640).
    ///     n_tables: Number of hash tables (default 20).
    ///     n_bits: Number of bits per table (default 32).
    #[new]
    #[pyo3(signature = (seed, dims, n_tables=20, n_bits=32))]
    fn new(seed: &[u8], dims: usize, n_tables: usize, n_bits: usize) -> PyResult<Self> {
        let inner = lsh::LshHasher::with_params(seed, dims, n_tables, n_bits).map_err(to_pyerr)?;
        Ok(Self { inner })
    }

    /// Hash an embedding vector to blind-hashed bucket IDs.
    ///
    /// Args:
    ///     embedding: List of floats (must have ``dims`` elements).
    ///
    /// Returns:
    ///     List of hex strings (one blind hash per table).
    fn hash(&self, embedding: Vec<f64>) -> PyResult<Vec<String>> {
        self.inner.hash(&embedding).map_err(to_pyerr)
    }

    /// Number of hash tables.
    #[getter]
    fn tables(&self) -> usize {
        self.inner.tables()
    }

    /// Number of bits per table.
    #[getter]
    fn bits(&self) -> usize {
        self.inner.bits()
    }

    /// Embedding dimensionality.
    #[getter]
    fn dimensions(&self) -> usize {
        self.inner.dimensions()
    }
}

