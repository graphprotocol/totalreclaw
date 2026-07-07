//! TotalReclaw Memory Backend
//!
//! Rust implementation of the TotalReclaw E2EE memory pipeline.
//! Produces byte-for-byte identical crypto output to the TypeScript MCP server.
//!
//! # Modules
//!
//! This crate is a thin ZeroClaw adapter over `totalreclaw-core`. Pure
//! computation (crypto, indexing, ranking) lives in core; the backend-specific
//! wiring (relay HTTP, ZeroClaw `Memory` trait, setup, caches) lives here.
//!
//! ## Owned by this crate (backend adapter)
//!
//! - [`embedding`] — Embedding pipeline (Local ONNX, Ollama, ZeroClaw, LLM provider)
//! - [`relay`] — HTTP client for TotalReclaw relay server
//! - [`store`] — Encrypt → index → encode → submit pipeline (with Phase 2 KG contradiction check)
//! - [`search`] — Subgraph query → decrypt → rerank pipeline
//! - [`backend`] — ZeroClaw Memory trait implementation
//! - [`setup`] — First-use setup wizard (credentials + embedding config)
//! - [`billing`] — Billing cache (2h TTL) + relay feature-flag parsing
//! - [`hotcache`] — In-memory hot cache wrapper over the generic core cache
//! - [`userop`] — ERC-4337 UserOp construction and signing
//! - [`wallet`] — Wallet / key material handling
//!
//! ## Re-exported from `totalreclaw-core` (thin `pub use` shims)
//!
//! These modules own no logic here; they re-export the canonical core
//! implementation so downstream `totalreclaw_memory::<name>` paths keep working.
//!
//! - [`crypto`] — Key derivation (BIP-39 + HKDF-SHA256), XChaCha20-Poly1305 encrypt/decrypt
//! - [`blind`] — Blind index generation (SHA-256 token hashing + Porter stemming)
//! - [`fingerprint`] — Content fingerprint (HMAC-SHA256 with NFC normalization)
//! - [`lsh`] — Locality-sensitive hashing (random hyperplane LSH)
//! - [`protobuf`] — Minimal protobuf encoder for fact payloads
//! - [`stemmer`] — Porter stemmer (Porter 1 algorithm)
//! - [`debrief`] — Session debrief extraction + parsing
//! - [`reranker`] — BM25 + Cosine + RRF fusion reranker (source-weighted Tier 1)

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

pub use backend::{
    BatchStoreItem, MemoryCategory, MemoryEntry, TotalReclawConfig, TotalReclawMemory,
};

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

    /// A caller passed a malformed or out-of-range argument. Mirrors
    /// [`totalreclaw_core::Error::InvalidInput`]; distinct from [`Error::Crypto`].
    #[error("invalid input: {0}")]
    InvalidInput(String),

    /// A feature exists in the trait surface but has no native ZeroClaw
    /// implementation yet (pin / retype / set_scope route through the MCP
    /// server instead). See CLAUDE.md Known Gaps.
    #[error("not implemented: {0}")]
    NotImplemented(String),

    /// (De)serialization of a local blob (credentials file, v1 envelope) failed.
    #[error("serialization error: {0}")]
    Serialization(String),

    /// A serialized blob could not be parsed. Mirrors
    /// [`totalreclaw_core::Error::Parse`]; surfaced verbatim (no prefix) so
    /// binding-visible strings stay stable.
    #[error("{0}")]
    Parse(String),
}

/// Bridge totalreclaw_core errors into this crate's Error type.
impl From<totalreclaw_core::Error> for Error {
    fn from(e: totalreclaw_core::Error) -> Self {
        match e {
            totalreclaw_core::Error::Crypto(msg) => Error::Crypto(msg),
            totalreclaw_core::Error::InvalidMnemonic(msg) => Error::InvalidMnemonic(msg),
            totalreclaw_core::Error::Lsh(msg) => Error::Lsh(msg),
            totalreclaw_core::Error::Reranker(msg) => Error::Reranker(msg),
            totalreclaw_core::Error::InvalidInput(msg) => Error::InvalidInput(msg),
            totalreclaw_core::Error::Parse(msg) => Error::Parse(msg),
        }
    }
}

pub type Result<T> = std::result::Result<T, Error>;
