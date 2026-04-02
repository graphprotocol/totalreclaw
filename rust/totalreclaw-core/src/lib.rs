//! TotalReclaw Core — Canonical crypto primitives and shared logic.
//!
//! This crate is the single source of truth for all TotalReclaw cryptographic
//! operations. It provides WASM and PyO3 bindings (feature-gated) so that
//! TypeScript, Python, and Rust consumers share byte-for-byte identical output.
//!
//! # Modules
//!
//! - [`crypto`] — Key derivation (BIP-39 + HKDF-SHA256), AES-256-GCM encrypt/decrypt
//! - [`lsh`] — Locality-sensitive hashing (random hyperplane LSH)
//! - [`blind`] — Blind index generation (SHA-256 token hashing + Porter stemming)
//! - [`stemmer`] — Porter 1 stemmer (hand-rolled, NOT Snowball/Porter 2)
//! - [`fingerprint`] — Content fingerprint (HMAC-SHA256 with NFC normalization)
//! - [`protobuf`] — Minimal protobuf encoder for fact payloads
//! - [`debrief`] — Session debrief response parser

pub mod blind;
pub mod crypto;
pub mod debrief;
pub mod fingerprint;
pub mod lsh;
pub mod protobuf;
pub mod stemmer;

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
}

pub type Result<T> = std::result::Result<T, Error>;
