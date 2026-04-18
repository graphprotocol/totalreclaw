//! BM25 + Cosine + RRF fusion reranker.
//!
//! Matches the TypeScript reranker in the MCP server.
//!
//! Parameters:
//! - BM25: k1=1.2, b=0.75
//! - RRF: k=60
//! - Intent-weighted fusion:
//!   - `intent_score = cosine(query_embedding, fact_embedding)`
//!   - `bm25_weight = 0.3 + 0.3 * (1 - intent_score)`
//!   - `cosine_weight = 0.3 + 0.3 * intent_score`
//!   - `final_score = bm25_weight * rrf_bm25 + cosine_weight * rrf_cosine`
//!
//! # Retrieval v2 Tier 1: source-weighted final score
//!
//! When [`RerankerConfig::apply_source_weights`] is true the final fused score
//! is multiplied by a provenance weight derived from [`MemorySource`] (or by
//! [`LEGACY_CLAIM_FALLBACK_WEIGHT`] when the candidate has no source field).
//!
//! See `docs/specs/totalreclaw/retrieval-v2.md` §Tier 1.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::claims::MemorySource;
use crate::Result;

/// BM25 parameters.
const BM25_K1: f64 = 1.2;
const BM25_B: f64 = 0.75;

/// RRF fusion parameter.
const RRF_K: f64 = 60.0;

/// Source-weight multipliers applied to the final fused score when
/// [`RerankerConfig::apply_source_weights`] is enabled.
///
/// Values sourced from `docs/specs/totalreclaw/retrieval-v2.md` §Tier 1. The
/// array is sorted highest-to-lowest trust; values MUST NOT be edited without
/// updating the spec + recalibrating via the E13 retrieval benchmark.
pub const SOURCE_WEIGHTS: &[(MemorySource, f64)] = &[
    (MemorySource::User, 1.00),
    (MemorySource::UserInferred, 0.90),
    (MemorySource::Derived, 0.70),
    (MemorySource::External, 0.70),
    (MemorySource::Assistant, 0.55),
];

/// Fallback weight applied to candidates that have no `source` field.
///
/// Used for legacy v0 claims written before Memory Taxonomy v1 introduced
/// the `source` axis. Value (0.85) sits between `user-inferred` (0.90) and
/// `derived` / `external` (0.70) — mild penalty without erasing legacy data.
pub const LEGACY_CLAIM_FALLBACK_WEIGHT: f64 = 0.85;

/// Return the source-weight multiplier for a known [`MemorySource`].
///
/// Unknown sources (should not happen once `from_str_lossy` has run) fall back
/// to [`LEGACY_CLAIM_FALLBACK_WEIGHT`].
pub fn source_weight(source: MemorySource) -> f64 {
    SOURCE_WEIGHTS
        .iter()
        .find(|(s, _)| *s == source)
        .map(|(_, w)| *w)
        .unwrap_or(LEGACY_CLAIM_FALLBACK_WEIGHT)
}

/// Reranker runtime configuration.
///
/// v0 callers can stay with the legacy [`rerank`] API (no source awareness).
/// v1+ callers use [`rerank_with_config`] with `apply_source_weights = true`
/// so the final RRF score respects provenance per Retrieval v2 Tier 1.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct RerankerConfig {
    /// When true, multiply the final fused score by the source-weight for each
    /// candidate. When false, behaviour is identical to the v0 [`rerank`] fn.
    pub apply_source_weights: bool,
}

impl Default for RerankerConfig {
    /// Defaults to v0-compatible behaviour (`apply_source_weights = false`) so
    /// pre-v1 callers can bump the core version without ranking drift.
    fn default() -> Self {
        RerankerConfig {
            apply_source_weights: false,
        }
    }
}

/// A candidate fact for reranking.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candidate {
    /// Unique identifier for the fact.
    pub id: String,
    /// Decrypted plaintext of the fact.
    pub text: String,
    /// Embedding vector of the fact.
    pub embedding: Vec<f32>,
    /// Timestamp (passed through to results).
    pub timestamp: String,
    /// Optional Memory Taxonomy v1 provenance source.
    ///
    /// If present AND [`RerankerConfig::apply_source_weights`] is true, the
    /// candidate's final score is multiplied by [`source_weight`]. Absent
    /// source yields [`LEGACY_CLAIM_FALLBACK_WEIGHT`].
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<MemorySource>,
}

/// A reranked result with scores.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RankedResult {
    /// Unique identifier.
    pub id: String,
    /// Decrypted plaintext.
    pub text: String,
    /// Final fused score (post source-weight multiplication if enabled).
    pub score: f64,
    /// BM25 component score.
    pub bm25_score: f64,
    /// Cosine similarity score.
    pub cosine_score: f64,
    /// Timestamp (passed through from candidate).
    pub timestamp: String,
    /// Source-weight multiplier applied to `score` (1.0 when disabled or
    /// no source field). Useful for diagnostics / parity tests.
    #[serde(default, skip_serializing_if = "is_one_f64")]
    pub source_weight: f64,
}

fn is_one_f64(v: &f64) -> bool {
    (*v - 1.0).abs() < f64::EPSILON
}

/// Rerank candidates using BM25 + Cosine + RRF fusion (v0-compatible).
///
/// This function does NOT apply source weights — call [`rerank_with_config`]
/// with `apply_source_weights = true` to enable Retrieval v2 Tier 1.
///
/// # Arguments
/// - `query` — The search query text
/// - `query_embedding` — The query's embedding vector
/// - `candidates` — Candidate facts to rerank
/// - `top_k` — Number of top results to return
///
/// # Returns
/// Top-K results sorted by descending fused score.
pub fn rerank(
    query: &str,
    query_embedding: &[f32],
    candidates: &[Candidate],
    top_k: usize,
) -> Result<Vec<RankedResult>> {
    rerank_with_config(
        query,
        query_embedding,
        candidates,
        top_k,
        RerankerConfig::default(),
    )
}

/// Rerank candidates using BM25 + Cosine + RRF fusion, honouring the supplied
/// [`RerankerConfig`].
///
/// When `config.apply_source_weights` is true the final RRF score is
/// multiplied by the per-candidate [`source_weight`] AFTER fusion and BEFORE
/// top-k truncation. Candidates with no `source` field receive
/// [`LEGACY_CLAIM_FALLBACK_WEIGHT`] so v0 vaults still rank sensibly during
/// the v0→v1 migration window.
///
/// All weights are deterministic — per `retrieval-v2.md` §cross-client, the
/// same inputs MUST produce the same top-k across TS/Python/Rust bindings.
pub fn rerank_with_config(
    query: &str,
    query_embedding: &[f32],
    candidates: &[Candidate],
    top_k: usize,
    config: RerankerConfig,
) -> Result<Vec<RankedResult>> {
    if candidates.is_empty() {
        return Ok(Vec::new());
    }

    // Tokenize query
    let query_tokens = tokenize(query);

    // Build document frequency map
    let mut df: HashMap<String, usize> = HashMap::new();
    let mut doc_tokens: Vec<Vec<String>> = Vec::with_capacity(candidates.len());
    let mut total_doc_len: usize = 0;

    for candidate in candidates {
        let tokens = tokenize(&candidate.text);
        total_doc_len += tokens.len();
        for token in &tokens {
            *df.entry(token.clone()).or_insert(0) += 1;
        }
        doc_tokens.push(tokens);
    }

    let avg_doc_len = total_doc_len as f64 / candidates.len() as f64;
    let n_docs = candidates.len() as f64;

    // Compute BM25 scores
    let mut bm25_scores: Vec<f64> = Vec::with_capacity(candidates.len());
    for tokens in &doc_tokens {
        let score = bm25_score(&query_tokens, tokens, &df, n_docs, avg_doc_len);
        bm25_scores.push(score);
    }

    // Compute cosine similarities
    let mut cosine_scores: Vec<f64> = Vec::with_capacity(candidates.len());
    for candidate in candidates {
        let sim = cosine_similarity_f32(query_embedding, &candidate.embedding);
        cosine_scores.push(sim);
    }

    // Compute RRF ranks
    let bm25_ranks = compute_ranks(&bm25_scores);
    let cosine_ranks = compute_ranks(&cosine_scores);

    // Intent-weighted fusion
    let mut results: Vec<RankedResult> = Vec::with_capacity(candidates.len());
    for (i, candidate) in candidates.iter().enumerate() {
        let intent_score = cosine_scores[i].clamp(0.0, 1.0);
        let bm25_weight = 0.3 + 0.3 * (1.0 - intent_score);
        let cosine_weight = 0.3 + 0.3 * intent_score;

        let rrf_bm25 = 1.0 / (RRF_K + bm25_ranks[i] as f64);
        let rrf_cosine = 1.0 / (RRF_K + cosine_ranks[i] as f64);

        let fused = bm25_weight * rrf_bm25 + cosine_weight * rrf_cosine;

        // Tier 1 source weighting (post-fusion, pre-truncation).
        let src_weight = if config.apply_source_weights {
            match candidate.source {
                Some(src) => source_weight(src),
                None => LEGACY_CLAIM_FALLBACK_WEIGHT,
            }
        } else {
            1.0
        };

        let final_score = fused * src_weight;

        results.push(RankedResult {
            id: candidate.id.clone(),
            text: candidate.text.clone(),
            score: final_score,
            bm25_score: bm25_scores[i],
            cosine_score: cosine_scores[i],
            timestamp: candidate.timestamp.clone(),
            source_weight: src_weight,
        });
    }

    // Sort by descending score, breaking ties deterministically on id so the
    // cross-client parity guarantee holds even when two candidates collide.
    results.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.id.cmp(&b.id))
    });

    // Take top K
    results.truncate(top_k);

    Ok(results)
}

/// Compute BM25 score for a single document.
fn bm25_score(
    query_tokens: &[String],
    doc_tokens: &[String],
    df: &HashMap<String, usize>,
    n_docs: f64,
    avg_doc_len: f64,
) -> f64 {
    let doc_len = doc_tokens.len() as f64;

    // Count term frequencies in document
    let mut tf: HashMap<&str, usize> = HashMap::new();
    for token in doc_tokens {
        *tf.entry(token.as_str()).or_insert(0) += 1;
    }

    let mut score = 0.0;
    for qt in query_tokens {
        let term_freq = *tf.get(qt.as_str()).unwrap_or(&0) as f64;
        if term_freq == 0.0 {
            continue;
        }

        let doc_freq = *df.get(qt.as_str()).unwrap_or(&0) as f64;
        // IDF: log((N - df + 0.5) / (df + 0.5) + 1)
        let idf = ((n_docs - doc_freq + 0.5) / (doc_freq + 0.5) + 1.0).ln();

        // BM25 TF component
        let tf_component = (term_freq * (BM25_K1 + 1.0))
            / (term_freq + BM25_K1 * (1.0 - BM25_B + BM25_B * doc_len / avg_doc_len));

        score += idf * tf_component;
    }

    score
}

/// Simple tokenization for BM25 (lowercase, split on non-alphanumeric).
fn tokenize(text: &str) -> Vec<String> {
    text.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|s| s.len() >= 2)
        .map(|s| s.to_string())
        .collect()
}

/// Cosine similarity between two f32 vectors.
pub fn cosine_similarity_f32(a: &[f32], b: &[f32]) -> f64 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }

    let mut dot: f64 = 0.0;
    let mut norm_a: f64 = 0.0;
    let mut norm_b: f64 = 0.0;

    for (x, y) in a.iter().zip(b.iter()) {
        let x = *x as f64;
        let y = *y as f64;
        dot += x * y;
        norm_a += x * x;
        norm_b += y * y;
    }

    let denom = norm_a.sqrt() * norm_b.sqrt();
    if denom == 0.0 {
        0.0
    } else {
        dot / denom
    }
}

/// Compute 1-based ranks from scores (highest score = rank 1).
///
/// Ties use **competition ranking** (aka "1224" ranking): candidates with
/// equal scores share the lowest rank in their tied group. This is critical
/// for the source-weighted reranker — without it, two identical candidates
/// receive different RRF positions purely because of input-order, which
/// breaks cross-client parity and the "uniform multiplier preserves order"
/// invariant.
fn compute_ranks(scores: &[f64]) -> Vec<usize> {
    let mut indexed: Vec<(usize, f64)> = scores.iter().copied().enumerate().collect();
    indexed.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    let mut ranks = vec![0usize; scores.len()];
    let mut current_rank = 1usize;
    for (i, (idx, score)) in indexed.iter().enumerate() {
        if i > 0 {
            let prev_score = indexed[i - 1].1;
            // Only advance the rank if the score strictly dropped from the
            // previous position. Equal scores share a rank.
            if (score - prev_score).abs() > 0.0 {
                current_rank = i + 1;
            }
        }
        ranks[*idx] = current_rank;
    }
    ranks
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_bm25_basic() {
        let query_tokens = tokenize("dark mode preference");
        let doc_tokens = tokenize("The user prefers dark mode in all applications");

        let mut df: HashMap<String, usize> = HashMap::new();
        for t in &doc_tokens {
            *df.entry(t.clone()).or_insert(0) += 1;
        }

        let score = bm25_score(
            &query_tokens,
            &doc_tokens,
            &df,
            1.0,
            doc_tokens.len() as f64,
        );
        assert!(
            score > 0.0,
            "BM25 score should be positive for matching terms"
        );
    }

    #[test]
    fn test_cosine_similarity() {
        let a = vec![1.0f32, 0.0, 0.0];
        let b = vec![1.0f32, 0.0, 0.0];
        assert!((cosine_similarity_f32(&a, &b) - 1.0).abs() < 1e-10);

        let c = vec![0.0f32, 1.0, 0.0];
        assert!(cosine_similarity_f32(&a, &c).abs() < 1e-10);
    }

    #[test]
    fn test_rerank_returns_top_k() {
        let candidates: Vec<Candidate> = (0..10)
            .map(|i| Candidate {
                id: format!("fact_{}", i),
                text: format!("fact number {} about dark mode preferences", i),
                embedding: vec![i as f32 / 10.0; 4],
                timestamp: String::new(),
                source: None,
            })
            .collect();

        let query_embedding = vec![0.5f32; 4];
        let results = rerank("dark mode", &query_embedding, &candidates, 3).unwrap();

        assert_eq!(results.len(), 3);
        // Scores should be in descending order
        for i in 0..results.len() - 1 {
            assert!(results[i].score >= results[i + 1].score);
        }
    }

    #[test]
    fn test_rerank_empty() {
        let results = rerank("query", &[0.5f32; 4], &[], 3).unwrap();
        assert!(results.is_empty());
    }

    #[test]
    fn test_intent_weighting() {
        // High cosine similarity -> higher cosine weight
        let intent_score = 0.9;
        let bm25_weight = 0.3 + 0.3 * (1.0 - intent_score);
        let cosine_weight = 0.3 + 0.3 * intent_score;
        assert!(cosine_weight > bm25_weight);
        // bm25_weight + cosine_weight = 0.3 + 0.3*(1-s) + 0.3 + 0.3*s = 0.9
        assert!(((bm25_weight + cosine_weight) - 0.9_f64).abs() < 1e-10);

        // Low cosine similarity -> higher bm25 weight
        let intent_score = 0.1;
        let bm25_weight = 0.3 + 0.3 * (1.0 - intent_score);
        let cosine_weight = 0.3 + 0.3 * intent_score;
        assert!(bm25_weight > cosine_weight);
    }

    // === Retrieval v2 Tier 1: source-weighted reranking ===

    fn cand(id: &str, text: &str, embedding: Vec<f32>, source: Option<MemorySource>) -> Candidate {
        Candidate {
            id: id.to_string(),
            text: text.to_string(),
            embedding,
            timestamp: String::new(),
            source,
        }
    }

    #[test]
    fn test_source_weight_table_matches_spec() {
        assert_eq!(source_weight(MemorySource::User), 1.00);
        assert_eq!(source_weight(MemorySource::UserInferred), 0.90);
        assert_eq!(source_weight(MemorySource::Derived), 0.70);
        assert_eq!(source_weight(MemorySource::External), 0.70);
        assert_eq!(source_weight(MemorySource::Assistant), 0.55);
    }

    #[test]
    fn test_reranker_config_default_is_v0_compat() {
        assert!(!RerankerConfig::default().apply_source_weights);
    }

    #[test]
    fn test_rerank_source_weight_flag_off_matches_default() {
        // Two candidates with different sources; flag OFF must ignore the source.
        let candidates = vec![
            cand(
                "u",
                "dark mode preference",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::User),
            ),
            cand(
                "a",
                "dark mode preference",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::Assistant),
            ),
        ];
        let query_embedding = vec![0.9f32, 0.1, 0.0, 0.0];

        let off = rerank_with_config(
            "dark mode",
            &query_embedding,
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: false,
            },
        )
        .unwrap();
        let default = rerank("dark mode", &query_embedding, &candidates, 10).unwrap();

        // Scores must match one-for-one when flag is OFF.
        assert_eq!(off.len(), default.len());
        for (a, b) in off.iter().zip(default.iter()) {
            assert!(
                (a.score - b.score).abs() < 1e-12,
                "flag off should equal v0 behaviour"
            );
            assert!((a.source_weight - 1.0).abs() < 1e-12, "no weight applied");
        }
    }

    #[test]
    fn test_rerank_source_weight_promotes_user_over_assistant_on_tie() {
        // Two candidates with IDENTICAL base scores (same text, same embedding).
        // With flag ON the user-authored fact must outrank the assistant-authored one.
        let candidates = vec![
            cand(
                "a",
                "dark mode preference",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::Assistant),
            ),
            cand(
                "u",
                "dark mode preference",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::User),
            ),
        ];
        let query_embedding = vec![0.9f32, 0.1, 0.0, 0.0];

        let ranked = rerank_with_config(
            "dark mode",
            &query_embedding,
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: true,
            },
        )
        .unwrap();

        assert_eq!(ranked.len(), 2);
        assert_eq!(
            ranked[0].id, "u",
            "user source must outrank assistant on base-score tie"
        );
        assert_eq!(ranked[1].id, "a");
        // Sanity-check the per-result source_weight field.
        assert!((ranked[0].source_weight - 1.00).abs() < 1e-12);
        assert!((ranked[1].source_weight - 0.55).abs() < 1e-12);
        // Assistant score must be ~55% of user score on tie.
        let ratio = ranked[1].score / ranked[0].score;
        assert!(
            (ratio - 0.55).abs() < 1e-6,
            "assistant/user ratio should equal 0.55, got {}",
            ratio
        );
    }

    #[test]
    fn test_rerank_source_weight_assistant_score_never_zero() {
        // Spec §Tier 1: "Never drop to zero — all facts remain eligible for
        // top-k." Assistant source must still retain a positive score.
        let candidates = vec![cand(
            "a",
            "dark mode preference",
            vec![0.9f32, 0.1, 0.0, 0.0],
            Some(MemorySource::Assistant),
        )];
        let query_embedding = vec![0.9f32, 0.1, 0.0, 0.0];
        let ranked = rerank_with_config(
            "dark mode",
            &query_embedding,
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: true,
            },
        )
        .unwrap();
        assert_eq!(ranked.len(), 1);
        assert!(
            ranked[0].score > 0.0,
            "assistant score must not drop to zero"
        );
        assert!((ranked[0].source_weight - 0.55).abs() < 1e-12);
    }

    #[test]
    fn test_rerank_source_weight_preserves_base_score_multiplier() {
        // Invariant: with flag ON, score = fused_score * source_weight.
        // Easy way to verify: run with flag OFF to get fused_score, then
        // compare against ON * source_weight.
        let candidates = vec![
            cand(
                "asst",
                "dark mode preference is set",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::Assistant),
            ),
            cand(
                "user",
                "dark mode preference is set",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::User),
            ),
            cand(
                "derived",
                "dark mode preference is set",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::Derived),
            ),
            cand(
                "ext",
                "dark mode preference is set",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::External),
            ),
            cand(
                "inferred",
                "dark mode preference is set",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::UserInferred),
            ),
        ];
        let query_embedding = vec![0.9f32, 0.1, 0.0, 0.0];

        let off = rerank_with_config(
            "dark mode preference",
            &query_embedding,
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: false,
            },
        )
        .unwrap();
        let on = rerank_with_config(
            "dark mode preference",
            &query_embedding,
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: true,
            },
        )
        .unwrap();

        // Each candidate's ON score == OFF score * source_weight.
        let off_map: std::collections::HashMap<_, _> =
            off.iter().map(|r| (r.id.clone(), r.score)).collect();
        for r in &on {
            let expected = off_map[&r.id] * r.source_weight;
            assert!(
                (r.score - expected).abs() < 1e-12,
                "id={}: expected score {} * {} = {}, got {}",
                r.id,
                off_map[&r.id],
                r.source_weight,
                expected,
                r.score
            );
        }

        // And the canonical ordering: user (1.00) > inferred (0.90) > ext/derived
        // (0.70) > assistant (0.55). All base scores are equal, so source
        // weight is the only discriminator.
        let ids: Vec<_> = on.iter().map(|r| r.id.as_str()).collect();
        assert_eq!(ids[0], "user");
        assert_eq!(ids[1], "inferred");
        // derived and ext both at 0.70 — deterministic tie-break on id.
        assert_eq!(ids[2], "derived");
        assert_eq!(ids[3], "ext");
        assert_eq!(ids[4], "asst");
    }

    #[test]
    fn test_rerank_legacy_claim_without_source_uses_fallback_weight() {
        let candidates = vec![
            cand(
                "legacy",
                "dark mode preference",
                vec![0.9f32, 0.1, 0.0, 0.0],
                None,
            ),
            cand(
                "asst",
                "dark mode preference",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::Assistant),
            ),
            cand(
                "user",
                "dark mode preference",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::User),
            ),
        ];
        let query_embedding = vec![0.9f32, 0.1, 0.0, 0.0];

        let ranked = rerank_with_config(
            "dark mode",
            &query_embedding,
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: true,
            },
        )
        .unwrap();

        // On a three-way tie the legacy fallback (0.85) sits between assistant (0.55)
        // and user (1.00) — so the ordering MUST be user > legacy > assistant.
        assert_eq!(ranked[0].id, "user");
        assert_eq!(ranked[1].id, "legacy");
        assert_eq!(ranked[2].id, "asst");
        assert!((ranked[1].source_weight - LEGACY_CLAIM_FALLBACK_WEIGHT).abs() < 1e-12);
    }

    #[test]
    fn test_rerank_source_weight_stable_on_all_assistant_candidates() {
        // If every candidate is assistant-source the ordering must still reflect
        // the base-score differences (uniform multiplier, no instability).
        let candidates = vec![
            cand(
                "low",
                "weak signal",
                vec![0.0f32, 0.0, 1.0, 0.0],
                Some(MemorySource::Assistant),
            ),
            cand(
                "mid",
                "medium signal dark mode",
                vec![0.5f32, 0.5, 0.0, 0.0],
                Some(MemorySource::Assistant),
            ),
            cand(
                "hi",
                "very strong dark mode signal",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::Assistant),
            ),
        ];
        let query_embedding = vec![0.9f32, 0.1, 0.0, 0.0];

        let off = rerank_with_config(
            "dark mode",
            &query_embedding,
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: false,
            },
        )
        .unwrap();
        let on = rerank_with_config(
            "dark mode",
            &query_embedding,
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: true,
            },
        )
        .unwrap();

        // The ORDERING must be identical between flag on & off — uniform multiplier.
        let ids_off: Vec<_> = off.iter().map(|r| r.id.clone()).collect();
        let ids_on: Vec<_> = on.iter().map(|r| r.id.clone()).collect();
        assert_eq!(
            ids_off, ids_on,
            "uniform source must not change relative ordering"
        );

        // And every score in the weighted run must equal the unweighted score times 0.55.
        for (w, u) in on.iter().zip(off.iter()) {
            assert!((w.score - u.score * 0.55).abs() < 1e-12);
            assert!((w.source_weight - 0.55).abs() < 1e-12);
        }
    }

    #[test]
    fn test_rerank_deterministic_id_tiebreak() {
        // When two candidates produce identical final scores the tiebreak MUST be
        // deterministic (ascending id) so cross-client parity holds.
        let candidates = vec![
            cand(
                "zzz",
                "dark mode preference",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::User),
            ),
            cand(
                "aaa",
                "dark mode preference",
                vec![0.9f32, 0.1, 0.0, 0.0],
                Some(MemorySource::User),
            ),
        ];
        let query_embedding = vec![0.9f32, 0.1, 0.0, 0.0];

        let ranked = rerank_with_config(
            "dark mode",
            &query_embedding,
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: true,
            },
        )
        .unwrap();

        // Tied scores — ascending id wins.
        assert_eq!(ranked[0].id, "aaa");
        assert_eq!(ranked[1].id, "zzz");
    }

    #[test]
    fn test_candidate_source_field_serde_roundtrip() {
        let candidates = vec![
            Candidate {
                id: "1".into(),
                text: "hi".into(),
                embedding: vec![0.1f32, 0.2],
                timestamp: "2026-04-17T00:00:00Z".into(),
                source: Some(MemorySource::User),
            },
            Candidate {
                id: "2".into(),
                text: "legacy".into(),
                embedding: vec![0.1f32, 0.2],
                timestamp: String::new(),
                source: None,
            },
        ];
        let json = serde_json::to_string(&candidates).unwrap();
        assert!(json.contains("\"source\":\"user\""));
        // Legacy candidate should not serialize a null source field (skip_serializing_if).
        assert!(!json.contains("\"source\":null"));
        let back: Vec<Candidate> = serde_json::from_str(&json).unwrap();
        assert_eq!(back.len(), 2);
        assert_eq!(back[0].source, Some(MemorySource::User));
        assert_eq!(back[1].source, None);
    }

    #[test]
    fn test_rerank_empty_with_flag_on_returns_empty() {
        let results = rerank_with_config(
            "query",
            &[0.5f32; 4],
            &[],
            3,
            RerankerConfig {
                apply_source_weights: true,
            },
        )
        .unwrap();
        assert!(results.is_empty());
    }

    #[test]
    fn test_ranked_result_preserves_source_weight_field() {
        let candidates = vec![cand(
            "u",
            "hello world",
            vec![0.5f32, 0.5],
            Some(MemorySource::User),
        )];
        let ranked = rerank_with_config(
            "hello",
            &[0.5f32, 0.5],
            &candidates,
            10,
            RerankerConfig {
                apply_source_weights: true,
            },
        )
        .unwrap();
        assert_eq!(ranked.len(), 1);
        assert!((ranked[0].source_weight - 1.0).abs() < 1e-12);
    }
}
