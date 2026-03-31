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
//! - [`relay`] — HTTP client for TotalReclaw relay server
//! - [`protobuf`] — Minimal protobuf encoder for fact payloads
//! - [`store`] — Encrypt → index → encode → submit pipeline
//! - [`search`] — Subgraph query → decrypt → rerank pipeline
//! - [`backend`] — ZeroClaw Memory trait implementation
//! - [`setup`] — First-use setup wizard (credentials + embedding config)

pub mod backend;
pub mod billing;
pub mod blind;
pub mod crypto;
pub mod embedding;
pub mod fingerprint;
pub mod hotcache;
pub mod lsh;
pub mod protobuf;
pub mod relay;
pub mod reranker;
pub mod search;
pub mod setup;
pub mod stemmer;
pub mod store;
pub mod userop;
pub mod wallet;

pub use backend::{MemoryCategory, MemoryEntry, TotalReclawConfig, TotalReclawMemory};

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

    #[error("quota exceeded: {0}")]
    QuotaExceeded(String),
}

pub type Result<T> = std::result::Result<T, Error>;
