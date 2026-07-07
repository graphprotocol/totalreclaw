//! BM25 + cosine + RRF reranking (intent-weighted, source-weighted Tier 1).
//!
//! Delegates to `totalreclaw_core::reranker` — the canonical implementation.
//! Re-exports all public items for backward compatibility.

pub use totalreclaw_core::reranker::*;
