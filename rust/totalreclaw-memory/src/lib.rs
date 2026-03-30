//! TotalReclaw Memory Backend
//!
//! Rust implementation of the TotalReclaw E2EE memory pipeline.
//! Produces byte-for-byte identical crypto output to the TypeScript MCP server.
//!
//! # Modules
//!
//! - [`crypto`] — Key derivation (BIP-39 + HKDF-SHA256), AES-256-GCM encrypt/decrypt
//! - [`blind`] — Blind index generation (SHA-256 token hashing + Porter stemming)
//! - [`fingerprint`] — Content fingerprint (HMAC-SHA256 with NFC normalization)
//! - [`lsh`] — Locality-sensitive hashing (random hyperplane LSH)
//! - [`embedding`] — Embedding pipeline (Local ONNX, Ollama, ZeroClaw, LLM provider)
//! - [`reranker`] — BM25 + Cosine + RRF fusion reranker

pub mod blind;
pub mod crypto;
pub mod embedding;
pub mod fingerprint;
pub mod lsh;
pub mod reranker;
pub mod stemmer;

/// Crate-level error type.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("crypto error: {0}")]
    Crypto(String),

    #[error("invalid mnemonic: {0}")]
    InvalidMnemonic(String),

    #[error("embedding error: {0}")]
    Embedding(String),

    #[error("reranker error: {0}")]
    Reranker(String),

    #[error("LSH error: {0}")]
    Lsh(String),

    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("HTTP error: {0}")]
    Http(String),
}

pub type Result<T> = std::result::Result<T, Error>;
