//! TotalReclaw Memory Backend
//!
//! Rust implementation of the TotalReclaw E2EE memory pipeline.
//! Produces byte-for-byte identical crypto output to the TypeScript MCP server.
//!
//! # Modules
//!
//! - [`crypto`] — Key derivation (BIP-39 + HKDF-SHA256), XChaCha20-Poly1305 encrypt/decrypt
//! - [`blind`] — Blind index generation (SHA-256 token hashing + Porter stemming)
//! - [`fingerprint`] — Content fingerprint (HMAC-SHA256 with NFC normalization)
//! - [`lsh`] — Locality-sensitive hashing (random hyperplane LSH)
//! - [`embedding`] — Embedding pipeline (Local ONNX, Ollama, ZeroClaw, LLM provider)
//! - [`reranker`] — BM25 + Cosine + RRF fusion reranker
//! - [`relay`] — HTTP client for TotalReclaw relay server
//! - [`protobuf`] — Minimal protobuf encoder for fact payloads
//! - [`store`] — Encrypt → index → encode → submit pipeline (with Phase 2 KG contradiction check)
//! - [`search`] — Subgraph query → decrypt → rerank pipeline
//! - [`backend`] — ZeroClaw Memory trait implementation
//! - [`setup`] — First-use setup wizard (credentials + embedding config)

pub mod backend;
pub mod billing;
pub mod blind;
pub mod crypto;
pub mod debrief;
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

// Re-export core Phase 2 KG types for downstream consumers.
pub use totalreclaw_core::claims::{
    Claim, ClaimCategory, ClaimStatus, EntityRef, EntityType, ResolutionAction, SkipReason,
    TIE_ZONE_SCORE_TOLERANCE, is_pinned_claim, is_pinned_json, respect_pin_in_resolution,
};
// Memory Taxonomy v1 types. As of totalreclaw-memory 2.0 these are the
// canonical taxonomy; v0 categories remain for pre-2.0 vault back-compat.
pub use totalreclaw_core::claims::{
    MemoryClaimV1, MemoryEntityV1, MemorySource, MemoryScope, MemoryTypeV1,
    MemoryVolatility, MEMORY_CLAIM_V1_SCHEMA_VERSION,
};
pub use totalreclaw_core::contradiction::{
    Contradiction, ResolutionOutcome, ResolutionWeights, ScoreComponents,
    resolve_with_candidates, resolve_pair, detect_contradictions, default_weights,
    DEFAULT_LOWER_THRESHOLD, DEFAULT_UPPER_THRESHOLD,
};
pub use totalreclaw_core::decision_log::{
    DecisionLogEntry, DECISION_LOG_MAX_LINES, CONTRADICTION_CANDIDATE_CAP,
    find_loser_claim_in_decision_log, find_decision_for_pin,
    build_feedback_from_decision, append_decision_entry,
};
pub use store::ContradictionStoreResult;

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

/// Bridge totalreclaw_core errors into this crate's Error type.
impl From<totalreclaw_core::Error> for Error {
    fn from(e: totalreclaw_core::Error) -> Self {
        match e {
            totalreclaw_core::Error::Crypto(msg) => Error::Crypto(msg),
            totalreclaw_core::Error::InvalidMnemonic(msg) => Error::InvalidMnemonic(msg),
            totalreclaw_core::Error::Lsh(msg) => Error::Lsh(msg),
            totalreclaw_core::Error::Reranker(msg) => Error::Reranker(msg),
        }
    }
}

pub type Result<T> = std::result::Result<T, Error>;
