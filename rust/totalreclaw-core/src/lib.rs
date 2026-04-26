//! TotalReclaw Core — Canonical crypto primitives and shared logic.
//!
//! This crate is the single source of truth for all TotalReclaw cryptographic
//! operations. It provides WASM and PyO3 bindings (feature-gated) so that
//! TypeScript, Python, and Rust consumers share byte-for-byte identical output.
//!
//! # Modules
//!
//! - [`crypto`] — Key derivation (BIP-39 + HKDF-SHA256), XChaCha20-Poly1305 encrypt/decrypt
//! - [`lsh`] — Locality-sensitive hashing (random hyperplane LSH)
//! - [`blind`] — Blind index generation (SHA-256 token hashing + Porter stemming)
//! - [`stemmer`] — Porter 1 stemmer (hand-rolled, NOT Snowball/Porter 2)
//! - [`fingerprint`] — Content fingerprint (HMAC-SHA256 with NFC normalization)
//! - [`protobuf`] — Minimal protobuf encoder for fact payloads
//! - [`reranker`] — BM25 + Cosine + RRF fusion reranker
//! - [`debrief`] — Session debrief response parser
//! - [`store`] — Store pipeline (pure computation: encrypt, index, encode)
//! - [`search`] — Search pipeline (pure computation: trapdoors, parse, decrypt+rerank; feature: `managed`)
//! - [`wallet`] — Ethereum wallet derivation (BIP-44 + Keccak256)
//! - [`userop`] — ERC-4337 v0.7 UserOp building + signing (feature: `managed`)
//! - [`hotcache`] — Generic in-memory hot cache for semantic query dedup (no WASM binding)
//! - [`consolidation`] — Store-time near-duplicate detection + supersede logic
//! - [`smart_import`] — Smart import profiling (prompt construction + response parsing)
//! - [`memory_types`] — Memory Taxonomy v1 string-level constants + runtime guard
//! - [`prompts`] — Canonical extraction + compaction system prompts (2.2.0)
//! - [`confirm`] — Read-after-write primitive for on-chain mutation tools (2.2.x)

pub mod blind;
pub mod claims;
pub mod confirm;
pub mod consolidation;
pub mod contradiction;
pub mod decision_log;
pub mod feedback_log;
pub mod digest;
pub mod crypto;
pub mod debrief;
pub mod fingerprint;
pub mod hotcache;
pub mod lsh;
pub mod memory_types;
pub mod prompts;
pub mod protobuf;
pub mod reranker;
pub mod smart_import;
pub mod stemmer;
pub mod store;
pub mod wallet;

#[cfg(feature = "managed")]
pub mod search;
#[cfg(feature = "managed")]
pub mod userop;

#[cfg(feature = "wasm")]
pub mod wasm;

#[cfg(feature = "python")]
pub mod python;

/// Crate-level error type.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("crypto error: {0}")]
    Crypto(String),

    #[error("invalid mnemonic: {0}")]
    InvalidMnemonic(String),

    #[error("LSH error: {0}")]
    Lsh(String),

    #[error("reranker error: {0}")]
    Reranker(String),
}

pub type Result<T> = std::result::Result<T, Error>;
