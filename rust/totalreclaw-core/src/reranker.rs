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

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::Result;

/// BM25 parameters.
const BM25_K1: f64 = 1.2;
const BM25_B: f64 = 0.75;

/// RRF fusion parameter.
const RRF_K: f64 = 60.0;

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
}

/// A reranked result with scores.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RankedResult {
    /// Unique identifier.
    pub id: String,
    /// Decrypted plaintext.
    pub text: String,
    /// Final fused score.
    pub score: f64,
    /// BM25 component score.
    pub bm25_score: f64,
    /// Cosine similarity score.
    pub cosine_score: f64,
    /// Timestamp (passed through from candidate).
    pub timestamp: String,
}

/// Rerank candidates using BM25 + Cosine + RRF fusion.
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
        let intent_score = cosine_scores[i].max(0.0).min(1.0);
        let bm25_weight = 0.3 + 0.3 * (1.0 - intent_score);
        let cosine_weight = 0.3 + 0.3 * intent_score;

        let rrf_bm25 = 1.0 / (RRF_K + bm25_ranks[i] as f64);
        let rrf_cosine = 1.0 / (RRF_K + cosine_ranks[i] as f64);

        let final_score = bm25_weight * rrf_bm25 + cosine_weight * rrf_cosine;

        results.push(RankedResult {
            id: candidate.id.clone(),
            text: candidate.text.clone(),
            score: final_score,
            bm25_score: bm25_scores[i],
            cosine_score: cosine_scores[i],
            timestamp: candidate.timestamp.clone(),
        });
    }

    // Sort by descending score
    results.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));

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
        let tf_component =
            (term_freq * (BM25_K1 + 1.0)) / (term_freq + BM25_K1 * (1.0 - BM25_B + BM25_B * doc_len / avg_doc_len));

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
fn compute_ranks(scores: &[f64]) -> Vec<usize> {
    let mut indexed: Vec<(usize, f64)> = scores.iter().copied().enumerate().collect();
    indexed.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    let mut ranks = vec![0usize; scores.len()];
    for (rank, (idx, _)) in indexed.iter().enumerate() {
        ranks[*idx] = rank + 1;
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

        let score = bm25_score(&query_tokens, &doc_tokens, &df, 1.0, doc_tokens.len() as f64);
        assert!(score > 0.0, "BM25 score should be positive for matching terms");
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
}
