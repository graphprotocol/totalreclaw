//! Contradiction detection and resolution primitives (Phase 2 Slice 2a).

use crate::claims::{deterministic_entity_id, Claim};
use crate::reranker::cosine_similarity_f32;
use chrono::DateTime;
use serde::{Deserialize, Serialize};

/// Weights for the resolution formula. Defaults come from P2-3.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResolutionWeights {
    pub confidence: f64,
    pub corroboration: f64,
    pub recency: f64,
    pub validation: f64,
}

/// A detected contradiction between two claims that share an entity.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Contradiction {
    pub claim_a_id: String,
    pub claim_b_id: String,
    pub entity_id: String,
    pub similarity: f64,
}

/// Per-component breakdown of a claim's score.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScoreComponents {
    pub confidence: f64,
    pub corroboration: f64,
    pub recency: f64,
    pub validation: f64,
    pub weighted_total: f64,
}

/// Output of running the resolution formula on a contradiction.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResolutionOutcome {
    pub winner_id: String,
    pub loser_id: String,
    pub winner_score: f64,
    pub loser_score: f64,
    pub score_delta: f64,
    pub winner_components: ScoreComponents,
    pub loser_components: ScoreComponents,
}

/// How the user resolved a contradiction relative to the formula's choice.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UserPinned {
    /// User pinned the loser (formula chose the wrong claim).
    Loser,
    /// User pinned both (they are not actually a contradiction).
    Both,
}

/// A user-override event used by the feedback-tuning loop.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Counterexample {
    pub formula_winner: ScoreComponents,
    pub formula_loser: ScoreComponents,
    pub user_pinned: UserPinned,
}

/// Default contradiction detection band lower bound (below = unrelated).
pub const DEFAULT_LOWER_THRESHOLD: f64 = 0.3;

/// Default contradiction detection band upper bound (at/above = duplicate).
pub const DEFAULT_UPPER_THRESHOLD: f64 = 0.85;

/// Gradient step size for weight feedback adjustment.
pub const FEEDBACK_STEP_SIZE: f64 = 0.02;

/// Per-weight lower clamp.
pub const WEIGHT_MIN: f64 = 0.05;

/// Per-weight upper clamp.
pub const WEIGHT_MAX: f64 = 0.60;

/// Weight sum lower bound (after clamping + rescaling).
pub const WEIGHT_SUM_MIN: f64 = 0.9;

/// Weight sum upper bound (after clamping + rescaling).
pub const WEIGHT_SUM_MAX: f64 = 1.1;

/// Structurally defensible default weights from P2-3 of the Phase 2 design doc.
pub fn default_weights() -> ResolutionWeights {
    ResolutionWeights {
        confidence: 0.25,
        corroboration: 0.15,
        recency: 0.40,
        validation: 0.20,
    }
}

fn parse_iso_to_unix(s: &str) -> Option<i64> {
    DateTime::parse_from_rfc3339(s).ok().map(|dt| dt.timestamp())
}

fn days_since(extracted_at: Option<&str>, now_unix: i64) -> f64 {
    match extracted_at.and_then(parse_iso_to_unix) {
        Some(ts) => {
            let delta = (now_unix - ts) as f64;
            (delta / 86400.0).max(0.0)
        }
        None => 10000.0,
    }
}

fn recency_weight(extracted_at: Option<&str>, now_unix: i64) -> f64 {
    let days = days_since(extracted_at, now_unix);
    1.0 / (1.0 + days / 30.0)
}

fn validation_component(source_agent: &str) -> f64 {
    if source_agent == "totalreclaw_remember" {
        1.0
    } else if source_agent.starts_with("openclaw-wiki-compile") {
        0.95
    } else {
        0.7
    }
}

fn corroboration_component(corroboration_count: u32) -> f64 {
    let n = corroboration_count.max(1) as f64;
    n.sqrt().min(3.0)
}

/// Compute a claim's score components for contradiction resolution.
pub fn compute_score_components(
    claim: &Claim,
    now_unix_seconds: i64,
    weights: &ResolutionWeights,
) -> ScoreComponents {
    let confidence = claim.confidence.clamp(0.0, 1.0);
    let corroboration = corroboration_component(claim.corroboration_count);
    let recency = recency_weight(claim.extracted_at.as_deref(), now_unix_seconds);
    let validation = validation_component(&claim.source_agent);
    let weighted_total = confidence * weights.confidence
        + corroboration * weights.corroboration
        + recency * weights.recency
        + validation * weights.validation;
    ScoreComponents {
        confidence,
        corroboration,
        recency,
        validation,
        weighted_total,
    }
}

/// Run the resolution formula on two contradicting claims. Returns winner and loser.
/// Ties (equal weighted totals) favour `claim_a` deterministically.
pub fn resolve_pair(
    claim_a: &Claim,
    claim_a_id: &str,
    claim_b: &Claim,
    claim_b_id: &str,
    now_unix_seconds: i64,
    weights: &ResolutionWeights,
) -> ResolutionOutcome {
    let a = compute_score_components(claim_a, now_unix_seconds, weights);
    let b = compute_score_components(claim_b, now_unix_seconds, weights);
    if a.weighted_total >= b.weighted_total {
        let score_delta = a.weighted_total - b.weighted_total;
        ResolutionOutcome {
            winner_id: claim_a_id.to_string(),
            loser_id: claim_b_id.to_string(),
            winner_score: a.weighted_total,
            loser_score: b.weighted_total,
            score_delta,
            winner_components: a,
            loser_components: b,
        }
    } else {
        let score_delta = b.weighted_total - a.weighted_total;
        ResolutionOutcome {
            winner_id: claim_b_id.to_string(),
            loser_id: claim_a_id.to_string(),
            winner_score: b.weighted_total,
            loser_score: a.weighted_total,
            score_delta,
            winner_components: b,
            loser_components: a,
        }
    }
}

/// Detect contradictions between a new claim and existing claims that share at least one entity.
///
/// Returns contradictions where cosine similarity is in `[lower_threshold, upper_threshold)`.
/// Pairs with multiple shared entities are reported once, using the first shared entity
/// (by insertion order of the new claim's entity list) as the representative.
///
/// Existing claims with an empty embedding vector are skipped (cannot be evaluated).
/// If the new claim has no entities, returns an empty vec.
pub fn detect_contradictions(
    new_claim: &Claim,
    new_claim_id: &str,
    new_embedding: &[f32],
    existing: &[(Claim, String, Vec<f32>)],
    lower_threshold: f64,
    upper_threshold: f64,
) -> Vec<Contradiction> {
    if new_claim.entities.is_empty() {
        return Vec::new();
    }

    let new_entity_ids: Vec<String> = new_claim
        .entities
        .iter()
        .map(|e| deterministic_entity_id(&e.name))
        .collect();

    let mut out: Vec<Contradiction> = Vec::new();

    for (existing_claim, existing_id, existing_emb) in existing.iter() {
        if existing_emb.is_empty() {
            continue;
        }
        if existing_id == new_claim_id {
            continue;
        }
        let existing_entity_ids: Vec<String> = existing_claim
            .entities
            .iter()
            .map(|e| deterministic_entity_id(&e.name))
            .collect();

        let shared_entity = new_entity_ids
            .iter()
            .find(|id| existing_entity_ids.iter().any(|eid| eid == *id));

        let Some(entity_id) = shared_entity else {
            continue;
        };

        let sim = cosine_similarity_f32(new_embedding, existing_emb);
        if sim >= lower_threshold && sim < upper_threshold {
            out.push(Contradiction {
                claim_a_id: new_claim_id.to_string(),
                claim_b_id: existing_id.clone(),
                entity_id: entity_id.clone(),
                similarity: sim,
            });
        }
    }

    out
}

// ---------------------------------------------------------------------------
// Step D: Contradiction Detection Orchestration
// ---------------------------------------------------------------------------

/// Core orchestration loop for contradiction resolution.
///
/// Given a new claim and a set of candidates (existing claims with embeddings),
/// detect contradictions and resolve each one by checking pin status, running the
/// resolution formula, and applying the tie-zone guard.
///
/// All I/O (subgraph queries, decryption, file reads) stays in client adapters.
/// This function operates on pre-fetched, pre-decrypted data only.
///
/// Returns an empty vec when:
/// - candidates is empty
/// - new_embedding is empty
/// - no contradictions are detected
pub fn resolve_with_candidates(
    new_claim: &Claim,
    new_claim_id: &str,
    new_embedding: &[f32],
    candidates: &[(Claim, String, Vec<f32>)],
    weights: &ResolutionWeights,
    threshold_lower: f64,
    threshold_upper: f64,
    now_unix_seconds: i64,
    tie_zone_tolerance: f64,
) -> Vec<crate::claims::ResolutionAction> {
    use crate::claims::{is_pinned_claim, ResolutionAction, SkipReason};

    if candidates.is_empty() || new_embedding.is_empty() {
        return Vec::new();
    }

    let contradictions = detect_contradictions(
        new_claim,
        new_claim_id,
        new_embedding,
        candidates,
        threshold_lower,
        threshold_upper,
    );

    if contradictions.is_empty() {
        return Vec::new();
    }

    // Index candidates by id for fast lookup.
    let by_id: std::collections::HashMap<&str, &(Claim, String, Vec<f32>)> = candidates
        .iter()
        .map(|c| (c.1.as_str(), c))
        .collect();

    let mut actions: Vec<ResolutionAction> = Vec::new();

    for contradiction in &contradictions {
        let Some(existing_tuple) = by_id.get(contradiction.claim_b_id.as_str()) else {
            continue;
        };
        let existing_claim = &existing_tuple.0;
        let existing_id = &existing_tuple.1;

        // Pinned existing claims are untouchable.
        if is_pinned_claim(existing_claim) {
            actions.push(ResolutionAction::SkipNew {
                reason: SkipReason::ExistingPinned,
                existing_id: existing_id.clone(),
                new_id: new_claim_id.to_string(),
                entity_id: Some(contradiction.entity_id.clone()),
                similarity: Some(contradiction.similarity),
                winner_score: None,
                loser_score: None,
                winner_components: None,
                loser_components: None,
            });
            continue;
        }

        // Run the resolution formula.
        let outcome = resolve_pair(
            new_claim,
            new_claim_id,
            existing_claim,
            existing_id,
            now_unix_seconds,
            weights,
        );

        if outcome.winner_id == new_claim_id {
            // New claim wins — check tie zone.
            if outcome.score_delta.abs() < tie_zone_tolerance {
                actions.push(ResolutionAction::TieLeaveBoth {
                    existing_id: existing_id.clone(),
                    new_id: new_claim_id.to_string(),
                    similarity: contradiction.similarity,
                    score_gap: outcome.score_delta,
                    entity_id: Some(contradiction.entity_id.clone()),
                    winner_score: Some(outcome.winner_score),
                    loser_score: Some(outcome.loser_score),
                    winner_components: Some(outcome.winner_components),
                    loser_components: Some(outcome.loser_components),
                });
            } else {
                actions.push(ResolutionAction::SupersedeExisting {
                    existing_id: existing_id.clone(),
                    new_id: new_claim_id.to_string(),
                    similarity: contradiction.similarity,
                    score_gap: outcome.score_delta,
                    entity_id: Some(contradiction.entity_id.clone()),
                    winner_score: Some(outcome.winner_score),
                    loser_score: Some(outcome.loser_score),
                    winner_components: Some(outcome.winner_components),
                    loser_components: Some(outcome.loser_components),
                });
            }
        } else {
            // Existing claim wins.
            actions.push(ResolutionAction::SkipNew {
                reason: SkipReason::ExistingWins,
                existing_id: existing_id.clone(),
                new_id: new_claim_id.to_string(),
                entity_id: Some(contradiction.entity_id.clone()),
                similarity: Some(contradiction.similarity),
                winner_score: Some(outcome.winner_score),
                loser_score: Some(outcome.loser_score),
                winner_components: Some(outcome.winner_components),
                loser_components: Some(outcome.loser_components),
            });
        }
    }

    actions
}

/// Convert resolution actions + metadata into decision log entries.
///
/// For each action, builds a `DecisionLogEntry` with scores, entity, and mode.
/// For `SupersedeExisting`, populates `loser_claim_json` from the provided map
/// (enables pin-on-tombstone recovery).
pub fn build_decision_log_entries(
    actions: &[crate::claims::ResolutionAction],
    _new_claim_json: &str,
    existing_claims_json: &std::collections::HashMap<String, String>,
    mode: &str,
    now_unix: i64,
) -> Vec<crate::decision_log::DecisionLogEntry> {
    use crate::claims::ResolutionAction;
    use crate::decision_log::DecisionLogEntry;

    let mut entries = Vec::new();

    for action in actions {
        match action {
            ResolutionAction::SupersedeExisting {
                existing_id,
                new_id,
                similarity,
                entity_id,
                winner_score,
                loser_score,
                winner_components,
                loser_components,
                ..
            } => {
                let loser_json = existing_claims_json.get(existing_id).cloned();
                entries.push(DecisionLogEntry {
                    ts: now_unix,
                    entity_id: entity_id.clone().unwrap_or_default(),
                    new_claim_id: new_id.clone(),
                    existing_claim_id: existing_id.clone(),
                    similarity: *similarity,
                    action: if mode == "shadow" {
                        "shadow".to_string()
                    } else {
                        "supersede_existing".to_string()
                    },
                    reason: Some("new_wins".to_string()),
                    winner_score: *winner_score,
                    loser_score: *loser_score,
                    winner_components: winner_components.clone(),
                    loser_components: loser_components.clone(),
                    loser_claim_json: loser_json,
                    mode: mode.to_string(),
                });
            }
            ResolutionAction::SkipNew {
                reason,
                existing_id,
                new_id,
                entity_id,
                similarity,
                winner_score,
                loser_score,
                winner_components,
                loser_components,
            } => {
                entries.push(DecisionLogEntry {
                    ts: now_unix,
                    entity_id: entity_id.clone().unwrap_or_default(),
                    new_claim_id: new_id.clone(),
                    existing_claim_id: existing_id.clone(),
                    similarity: similarity.unwrap_or(0.0),
                    action: if mode == "shadow" {
                        "shadow".to_string()
                    } else {
                        "skip_new".to_string()
                    },
                    reason: Some(serde_json::to_value(reason)
                        .ok()
                        .and_then(|v| v.as_str().map(|s| s.to_string()))
                        .unwrap_or_else(|| format!("{:?}", reason).to_lowercase())),
                    winner_score: *winner_score,
                    loser_score: *loser_score,
                    winner_components: winner_components.clone(),
                    loser_components: loser_components.clone(),
                    loser_claim_json: None,
                    mode: mode.to_string(),
                });
            }
            ResolutionAction::TieLeaveBoth {
                existing_id,
                new_id,
                similarity,
                entity_id,
                winner_score,
                loser_score,
                winner_components,
                loser_components,
                ..
            } => {
                entries.push(DecisionLogEntry {
                    ts: now_unix,
                    entity_id: entity_id.clone().unwrap_or_default(),
                    new_claim_id: new_id.clone(),
                    existing_claim_id: existing_id.clone(),
                    similarity: *similarity,
                    action: "tie_leave_both".to_string(),
                    reason: Some("tie_below_tolerance".to_string()),
                    winner_score: *winner_score,
                    loser_score: *loser_score,
                    winner_components: winner_components.clone(),
                    loser_components: loser_components.clone(),
                    loser_claim_json: None,
                    mode: mode.to_string(),
                });
            }
            ResolutionAction::NoContradiction => {
                // No log entry for no-op actions.
            }
        }
    }

    entries
}

/// Filter resolution actions based on the auto-resolve mode.
///
/// - `"active"`: return actions as-is (but filter out ties, which are informational only)
/// - `"shadow"`: return empty vec (log only, no side effects)
/// - `"off"` or anything else: return empty vec
pub fn filter_shadow_mode(
    actions: Vec<crate::claims::ResolutionAction>,
    mode: &str,
) -> Vec<crate::claims::ResolutionAction> {
    use crate::claims::ResolutionAction;
    match mode {
        "active" => actions
            .into_iter()
            .filter(|a| !matches!(a, ResolutionAction::TieLeaveBoth { .. }))
            .collect(),
        _ => Vec::new(),
    }
}

/// Apply a single counterexample to the weights via a small gradient step.
/// See `FEEDBACK_STEP_SIZE`, `WEIGHT_MIN`, `WEIGHT_MAX`, and `WEIGHT_SUM_MIN`/`MAX`.
///
/// For `UserPinned::Both`, the detection (not the weights) was wrong, so weights are unchanged.
pub fn apply_feedback(
    weights: &ResolutionWeights,
    counterexample: &Counterexample,
) -> ResolutionWeights {
    if matches!(counterexample.user_pinned, UserPinned::Both) {
        return weights.clone();
    }

    let winner = &counterexample.formula_winner;
    let loser = &counterexample.formula_loser;

    // Per-component deltas, clamped to [-1, 1] so each step stays bounded by ±step_size.
    let d_conf = (loser.confidence - winner.confidence).clamp(-1.0, 1.0);
    let d_corr = (loser.corroboration - winner.corroboration).clamp(-1.0, 1.0);
    let d_rec = (loser.recency - winner.recency).clamp(-1.0, 1.0);
    let d_val = (loser.validation - winner.validation).clamp(-1.0, 1.0);

    let mut new = ResolutionWeights {
        confidence: weights.confidence + FEEDBACK_STEP_SIZE * d_conf,
        corroboration: weights.corroboration + FEEDBACK_STEP_SIZE * d_corr,
        recency: weights.recency + FEEDBACK_STEP_SIZE * d_rec,
        validation: weights.validation + FEEDBACK_STEP_SIZE * d_val,
    };

    // Clamp each weight individually.
    new.confidence = new.confidence.clamp(WEIGHT_MIN, WEIGHT_MAX);
    new.corroboration = new.corroboration.clamp(WEIGHT_MIN, WEIGHT_MAX);
    new.recency = new.recency.clamp(WEIGHT_MIN, WEIGHT_MAX);
    new.validation = new.validation.clamp(WEIGHT_MIN, WEIGHT_MAX);

    // If the sum drifted outside [0.9, 1.1], rescale proportionally toward 1.0.
    let sum = new.confidence + new.corroboration + new.recency + new.validation;
    if sum < WEIGHT_SUM_MIN || sum > WEIGHT_SUM_MAX {
        let scale = 1.0 / sum;
        new.confidence = (new.confidence * scale).clamp(WEIGHT_MIN, WEIGHT_MAX);
        new.corroboration = (new.corroboration * scale).clamp(WEIGHT_MIN, WEIGHT_MAX);
        new.recency = (new.recency * scale).clamp(WEIGHT_MIN, WEIGHT_MAX);
        new.validation = (new.validation * scale).clamp(WEIGHT_MIN, WEIGHT_MAX);
    }

    new
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claims::{ClaimCategory, ClaimStatus, EntityRef, EntityType};

    fn make_claim(
        text: &str,
        confidence: f64,
        corroboration: u32,
        source: &str,
        extracted_at: Option<&str>,
        entities: Vec<&str>,
    ) -> Claim {
        Claim {
            text: text.to_string(),
            category: ClaimCategory::Fact,
            confidence,
            importance: 5,
            corroboration_count: corroboration,
            source_agent: source.to_string(),
            source_conversation: None,
            extracted_at: extracted_at.map(|s| s.to_string()),
            entities: entities
                .iter()
                .map(|n| EntityRef {
                    name: n.to_string(),
                    entity_type: EntityType::Tool,
                    role: None,
                })
                .collect(),
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        }
    }

    /// 2026-04-12T00:00:00Z — fixed "now" for deterministic recency tests.
    const NOW: i64 = 1776211200;

    fn iso_days_ago(days: i64) -> String {
        let ts = NOW - days * 86400;
        chrono::DateTime::<chrono::Utc>::from_timestamp(ts, 0)
            .unwrap()
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
    }

    // ----- default_weights -----

    #[test]
    fn test_default_weights_values() {
        let w = default_weights();
        assert_eq!(w.confidence, 0.25);
        assert_eq!(w.corroboration, 0.15);
        assert_eq!(w.recency, 0.40);
        assert_eq!(w.validation, 0.20);
    }

    #[test]
    fn test_default_weights_sum_to_one() {
        let w = default_weights();
        let sum = w.confidence + w.corroboration + w.recency + w.validation;
        assert!((sum - 1.0).abs() < 1e-12);
    }

    // ----- compute_score_components: validation -----

    #[test]
    fn test_validation_explicit_remember() {
        let c = make_claim("x", 0.8, 1, "totalreclaw_remember", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert_eq!(s.validation, 1.0);
    }

    #[test]
    fn test_validation_wiki_compile_exact() {
        let c = make_claim("x", 0.8, 1, "openclaw-wiki-compile", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert_eq!(s.validation, 0.95);
    }

    #[test]
    fn test_validation_wiki_compile_prefix() {
        let c = make_claim("x", 0.8, 1, "openclaw-wiki-compile-v2", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert_eq!(s.validation, 0.95);
    }

    #[test]
    fn test_validation_other_source() {
        let c = make_claim("x", 0.8, 1, "openclaw-plugin", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert_eq!(s.validation, 0.7);
    }

    #[test]
    fn test_validation_unknown_source() {
        let c = make_claim("x", 0.8, 1, "unknown", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert_eq!(s.validation, 0.7);
    }

    // ----- compute_score_components: corroboration -----

    #[test]
    fn test_corroboration_one() {
        let c = make_claim("x", 0.8, 1, "oc", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert!((s.corroboration - 1.0).abs() < 1e-12);
    }

    #[test]
    fn test_corroboration_nine() {
        let c = make_claim("x", 0.8, 9, "oc", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert!((s.corroboration - 3.0).abs() < 1e-12);
    }

    #[test]
    fn test_corroboration_capped_at_three() {
        let c = make_claim("x", 0.8, 100, "oc", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert!((s.corroboration - 3.0).abs() < 1e-12);
    }

    #[test]
    fn test_corroboration_four() {
        let c = make_claim("x", 0.8, 4, "oc", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert!((s.corroboration - 2.0).abs() < 1e-12);
    }

    #[test]
    fn test_corroboration_zero_treated_as_one() {
        // `corroboration_count` is u32 and defaults to 1, but defensively handle 0.
        let c = make_claim("x", 0.8, 0, "oc", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert!((s.corroboration - 1.0).abs() < 1e-12);
    }

    // ----- compute_score_components: recency -----

    #[test]
    fn test_recency_two_days_ago() {
        let c = make_claim("x", 0.8, 1, "oc", Some(&iso_days_ago(2)), vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        // 1 / (1 + 2/30) = 30/32 = 0.9375
        assert!((s.recency - 0.9375).abs() < 1e-9);
    }

    #[test]
    fn test_recency_thirty_days_ago() {
        let c = make_claim("x", 0.8, 1, "oc", Some(&iso_days_ago(30)), vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        // 1 / (1 + 30/30) = 0.5
        assert!((s.recency - 0.5).abs() < 1e-9);
    }

    #[test]
    fn test_recency_missing_timestamp() {
        let c = make_claim("x", 0.8, 1, "oc", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        // days = 10000 -> 1 / (1 + 10000/30) ~= 0.002994
        assert!((s.recency - 0.002994).abs() < 1e-5);
    }

    #[test]
    fn test_recency_today() {
        let c = make_claim("x", 0.8, 1, "oc", Some(&iso_days_ago(0)), vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert!((s.recency - 1.0).abs() < 1e-9);
    }

    #[test]
    fn test_recency_unparseable_string_treated_as_missing() {
        let c = make_claim("x", 0.8, 1, "oc", Some("not-a-date"), vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert!((s.recency - 0.002994).abs() < 1e-5);
    }

    #[test]
    fn test_recency_future_timestamp_clamped_to_zero_days() {
        let c = make_claim("x", 0.8, 1, "oc", Some(&iso_days_ago(-10)), vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        // Future -> days = 0 -> recency = 1.0
        assert!((s.recency - 1.0).abs() < 1e-9);
    }

    // ----- compute_score_components: confidence clamping -----

    #[test]
    fn test_confidence_clamped_high() {
        let c = make_claim("x", 1.5, 1, "oc", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert_eq!(s.confidence, 1.0);
    }

    #[test]
    fn test_confidence_clamped_low() {
        let c = make_claim("x", -0.3, 1, "oc", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert_eq!(s.confidence, 0.0);
    }

    #[test]
    fn test_confidence_passthrough() {
        let c = make_claim("x", 0.82, 1, "oc", None, vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        assert!((s.confidence - 0.82).abs() < 1e-12);
    }

    // ----- compute_score_components: weighted total -----

    #[test]
    fn test_weighted_total_formula() {
        let c = make_claim("x", 0.9, 1, "totalreclaw_remember", Some(&iso_days_ago(0)), vec![]);
        let s = compute_score_components(&c, NOW, &default_weights());
        // 0.9*0.25 + 1*0.15 + 1*0.40 + 1*0.20 = 0.225 + 0.15 + 0.40 + 0.20 = 0.975
        assert!((s.weighted_total - 0.975).abs() < 1e-9);
    }

    #[test]
    fn test_weighted_total_custom_weights() {
        let c = make_claim("x", 0.5, 1, "oc", Some(&iso_days_ago(0)), vec![]);
        let w = ResolutionWeights {
            confidence: 0.1,
            corroboration: 0.1,
            recency: 0.5,
            validation: 0.3,
        };
        let s = compute_score_components(&c, NOW, &w);
        // 0.5*0.1 + 1*0.1 + 1*0.5 + 0.7*0.3 = 0.05 + 0.1 + 0.5 + 0.21 = 0.86
        assert!((s.weighted_total - 0.86).abs() < 1e-9);
    }

    // ----- resolve_pair -----

    #[test]
    fn test_resolve_pair_vim_vs_vscode_defaults() {
        // Pedro's known-answer scenario from the plan.
        // Vim: confidence 0.8, 60 days old, corroboration 3, auto-extracted
        // VS Code: confidence 0.9, 7 days old, corroboration 1, auto-extracted
        // With default weights, VS Code wins because recency dominates.
        let vim = make_claim("uses Vim", 0.8, 3, "oc", Some(&iso_days_ago(60)), vec!["editor"]);
        let vscode = make_claim(
            "uses VS Code",
            0.9,
            1,
            "oc",
            Some(&iso_days_ago(7)),
            vec!["editor"],
        );
        let outcome = resolve_pair(&vim, "vim_id", &vscode, "vscode_id", NOW, &default_weights());
        assert_eq!(outcome.winner_id, "vscode_id");
        assert_eq!(outcome.loser_id, "vim_id");
        assert!(outcome.winner_score > outcome.loser_score);
        assert!(outcome.score_delta > 0.0);
    }

    #[test]
    fn test_resolve_pair_components_populated() {
        let a = make_claim("a", 0.9, 1, "oc", Some(&iso_days_ago(1)), vec![]);
        let b = make_claim("b", 0.5, 1, "oc", Some(&iso_days_ago(100)), vec![]);
        let outcome = resolve_pair(&a, "a", &b, "b", NOW, &default_weights());
        assert!(outcome.winner_components.weighted_total > outcome.loser_components.weighted_total);
        assert!(outcome.winner_components.weighted_total == outcome.winner_score);
        assert!(outcome.loser_components.weighted_total == outcome.loser_score);
    }

    #[test]
    fn test_resolve_pair_flipped_by_different_weights() {
        // With default (recency-heavy) weights, a fresh auto-extracted claim beats
        // an older explicit user-remembered claim.
        // With validation-heavy weights, the explicit user-remembered claim wins.
        let explicit_old = make_claim(
            "old",
            0.95,
            1,
            "totalreclaw_remember",
            Some(&iso_days_ago(60)),
            vec![],
        );
        let auto_new = make_claim("new", 0.7, 1, "oc", Some(&iso_days_ago(7)), vec![]);

        let defaults = default_weights();
        let outcome_default =
            resolve_pair(&explicit_old, "old", &auto_new, "new", NOW, &defaults);
        assert_eq!(outcome_default.winner_id, "new");

        let validation_heavy = ResolutionWeights {
            confidence: 0.10,
            corroboration: 0.10,
            recency: 0.20,
            validation: 0.60,
        };
        let outcome_val =
            resolve_pair(&explicit_old, "old", &auto_new, "new", NOW, &validation_heavy);
        assert_eq!(outcome_val.winner_id, "old");
    }

    #[test]
    fn test_resolve_pair_tie_favours_a() {
        // Identical claims except IDs -> weighted totals equal -> a wins by tie-break.
        let a = make_claim("same", 0.8, 1, "oc", Some(&iso_days_ago(5)), vec![]);
        let b = make_claim("same", 0.8, 1, "oc", Some(&iso_days_ago(5)), vec![]);
        let outcome = resolve_pair(&a, "id_a", &b, "id_b", NOW, &default_weights());
        assert_eq!(outcome.winner_id, "id_a");
        assert_eq!(outcome.loser_id, "id_b");
        assert!(outcome.score_delta.abs() < 1e-12);
    }

    #[test]
    fn test_resolve_pair_ids_correct() {
        let a = make_claim("a", 0.9, 1, "oc", Some(&iso_days_ago(1)), vec![]);
        let b = make_claim("b", 0.5, 1, "oc", Some(&iso_days_ago(100)), vec![]);
        let outcome = resolve_pair(&a, "alpha", &b, "beta", NOW, &default_weights());
        assert_eq!(outcome.winner_id, "alpha");
        assert_eq!(outcome.loser_id, "beta");
    }

    #[test]
    fn test_resolve_pair_score_delta_nonnegative() {
        let a = make_claim("a", 0.1, 1, "oc", Some(&iso_days_ago(365)), vec![]);
        let b = make_claim("b", 0.9, 1, "totalreclaw_remember", Some(&iso_days_ago(1)), vec![]);
        let outcome = resolve_pair(&a, "a", &b, "b", NOW, &default_weights());
        assert!(outcome.score_delta >= 0.0);
        assert_eq!(outcome.score_delta, outcome.winner_score - outcome.loser_score);
    }

    // ----- detect_contradictions -----

    fn emb_along_axis(axis: usize, dim: usize) -> Vec<f32> {
        let mut v = vec![0.0f32; dim];
        v[axis] = 1.0;
        v
    }

    /// Build an embedding at a controlled angle from a reference axis-aligned vector.
    /// Returns a unit vector whose cosine similarity with `emb_along_axis(axis, dim)` equals `cos_target`.
    fn emb_at_cosine(axis: usize, other_axis: usize, dim: usize, cos_target: f64) -> Vec<f32> {
        let mut v = vec![0.0f32; dim];
        let sin = (1.0 - cos_target * cos_target).sqrt();
        v[axis] = cos_target as f32;
        v[other_axis] = sin as f32;
        v
    }

    #[test]
    fn test_detect_empty_existing() {
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);
        let out = detect_contradictions(&new_claim, "new_id", &emb, &[], 0.3, 0.85);
        assert!(out.is_empty());
    }

    #[test]
    fn test_detect_new_claim_no_entities() {
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec![]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("y", 0.8, 1, "oc", None, vec!["editor"]);
        let existing_emb = emb_at_cosine(0, 1, 8, 0.5);
        let out = detect_contradictions(
            &new_claim,
            "new_id",
            &emb,
            &[(existing_claim, "exist".to_string(), existing_emb)],
            0.3,
            0.85,
        );
        assert!(out.is_empty());
    }

    #[test]
    fn test_detect_single_contradiction_in_band() {
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("y", 0.8, 1, "oc", None, vec!["editor"]);
        let existing_emb = emb_at_cosine(0, 1, 8, 0.5);
        let out = detect_contradictions(
            &new_claim,
            "new_id",
            &emb,
            &[(existing_claim, "exist".to_string(), existing_emb)],
            0.3,
            0.85,
        );
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].claim_a_id, "new_id");
        assert_eq!(out[0].claim_b_id, "exist");
        assert!((out[0].similarity - 0.5).abs() < 1e-6);
        assert_eq!(out[0].entity_id, deterministic_entity_id("editor"));
    }

    #[test]
    fn test_detect_above_upper_threshold_is_duplicate() {
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("y", 0.8, 1, "oc", None, vec!["editor"]);
        let existing_emb = emb_at_cosine(0, 1, 8, 0.9);
        let out = detect_contradictions(
            &new_claim,
            "new_id",
            &emb,
            &[(existing_claim, "exist".to_string(), existing_emb)],
            0.3,
            0.85,
        );
        assert!(out.is_empty());
    }

    #[test]
    fn test_detect_exactly_at_upper_threshold_is_duplicate() {
        // Upper threshold is exclusive: sim == 0.85 should NOT be a contradiction.
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("y", 0.8, 1, "oc", None, vec!["editor"]);
        let existing_emb = emb_at_cosine(0, 1, 8, 0.85);
        let out = detect_contradictions(
            &new_claim,
            "new_id",
            &emb,
            &[(existing_claim, "exist".to_string(), existing_emb)],
            0.3,
            0.85,
        );
        assert!(out.is_empty());
    }

    #[test]
    fn test_detect_exactly_at_lower_threshold_is_contradiction() {
        // Lower threshold is inclusive: sim == 0.3 IS a contradiction.
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("y", 0.8, 1, "oc", None, vec!["editor"]);
        let existing_emb = emb_at_cosine(0, 1, 8, 0.3);
        let out = detect_contradictions(
            &new_claim,
            "new_id",
            &emb,
            &[(existing_claim, "exist".to_string(), existing_emb)],
            0.3,
            0.85,
        );
        assert_eq!(out.len(), 1);
    }

    #[test]
    fn test_detect_below_lower_threshold_unrelated() {
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("y", 0.8, 1, "oc", None, vec!["editor"]);
        let existing_emb = emb_at_cosine(0, 1, 8, 0.2);
        let out = detect_contradictions(
            &new_claim,
            "new_id",
            &emb,
            &[(existing_claim, "exist".to_string(), existing_emb)],
            0.3,
            0.85,
        );
        assert!(out.is_empty());
    }

    #[test]
    fn test_detect_different_entities_no_contradiction() {
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("y", 0.8, 1, "oc", None, vec!["database"]);
        let existing_emb = emb_at_cosine(0, 1, 8, 0.5);
        let out = detect_contradictions(
            &new_claim,
            "new_id",
            &emb,
            &[(existing_claim, "exist".to_string(), existing_emb)],
            0.3,
            0.85,
        );
        assert!(out.is_empty());
    }

    #[test]
    fn test_detect_skips_empty_embedding() {
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("y", 0.8, 1, "oc", None, vec!["editor"]);
        let out = detect_contradictions(
            &new_claim,
            "new_id",
            &emb,
            &[(existing_claim, "exist".to_string(), Vec::new())],
            0.3,
            0.85,
        );
        assert!(out.is_empty());
    }

    #[test]
    fn test_detect_skips_self_by_id() {
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let existing_emb = emb_at_cosine(0, 1, 8, 0.5);
        let out = detect_contradictions(
            &new_claim,
            "same_id",
            &emb,
            &[(existing_claim, "same_id".to_string(), existing_emb)],
            0.3,
            0.85,
        );
        assert!(out.is_empty());
    }

    #[test]
    fn test_detect_multiple_candidates_mixed() {
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor"]);
        let emb = emb_along_axis(0, 8);

        let c_in_band = make_claim("a", 0.8, 1, "oc", None, vec!["editor"]);
        let e_in_band = emb_at_cosine(0, 1, 8, 0.5);

        let c_duplicate = make_claim("b", 0.8, 1, "oc", None, vec!["editor"]);
        let e_duplicate = emb_at_cosine(0, 2, 8, 0.9);

        let c_unrelated_entity = make_claim("c", 0.8, 1, "oc", None, vec!["database"]);
        let e_unrelated_entity = emb_at_cosine(0, 3, 8, 0.5);

        let c_unrelated_low = make_claim("d", 0.8, 1, "oc", None, vec!["editor"]);
        let e_unrelated_low = emb_at_cosine(0, 4, 8, 0.1);

        let c_in_band2 = make_claim("e", 0.8, 1, "oc", None, vec!["editor"]);
        let e_in_band2 = emb_at_cosine(0, 5, 8, 0.7);

        let existing = vec![
            (c_in_band, "a".to_string(), e_in_band),
            (c_duplicate, "b".to_string(), e_duplicate),
            (c_unrelated_entity, "c".to_string(), e_unrelated_entity),
            (c_unrelated_low, "d".to_string(), e_unrelated_low),
            (c_in_band2, "e".to_string(), e_in_band2),
        ];

        let out = detect_contradictions(&new_claim, "new_id", &emb, &existing, 0.3, 0.85);
        let hit_ids: Vec<String> = out.iter().map(|c| c.claim_b_id.clone()).collect();
        assert_eq!(hit_ids, vec!["a".to_string(), "e".to_string()]);
    }

    #[test]
    fn test_detect_multi_shared_entity_reports_first() {
        // New claim has entities [editor, language]. Existing shares both.
        // First shared entity is "editor" (new claim's order).
        let new_claim = make_claim("x", 0.8, 1, "oc", None, vec!["editor", "language"]);
        let emb = emb_along_axis(0, 8);
        let existing_claim = make_claim("y", 0.8, 1, "oc", None, vec!["language", "editor"]);
        let existing_emb = emb_at_cosine(0, 1, 8, 0.5);
        let out = detect_contradictions(
            &new_claim,
            "new_id",
            &emb,
            &[(existing_claim, "exist".to_string(), existing_emb)],
            0.3,
            0.85,
        );
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].entity_id, deterministic_entity_id("editor"));
    }

    // ----- apply_feedback -----

    fn components(
        confidence: f64,
        corroboration: f64,
        recency: f64,
        validation: f64,
    ) -> ScoreComponents {
        ScoreComponents {
            confidence,
            corroboration,
            recency,
            validation,
            weighted_total: 0.0,
        }
    }

    #[test]
    fn test_feedback_both_pinned_unchanged() {
        let w = default_weights();
        let ce = Counterexample {
            formula_winner: components(0.9, 3.0, 1.0, 1.0),
            formula_loser: components(0.5, 1.0, 0.1, 0.7),
            user_pinned: UserPinned::Both,
        };
        let new_w = apply_feedback(&w, &ce);
        assert_eq!(new_w, w);
    }

    #[test]
    fn test_feedback_identity_equal_components() {
        // If winner == loser components, no shift.
        let w = default_weights();
        let c = components(0.8, 2.0, 0.5, 0.7);
        let ce = Counterexample {
            formula_winner: c.clone(),
            formula_loser: c,
            user_pinned: UserPinned::Loser,
        };
        let new_w = apply_feedback(&w, &ce);
        assert!((new_w.confidence - w.confidence).abs() < 1e-12);
        assert!((new_w.corroboration - w.corroboration).abs() < 1e-12);
        assert!((new_w.recency - w.recency).abs() < 1e-12);
        assert!((new_w.validation - w.validation).abs() < 1e-12);
    }

    #[test]
    fn test_feedback_recency_increases_when_loser_had_more() {
        // Formula picked the low-recency winner, user pinned the high-recency loser.
        // -> recency weight should increase.
        let w = default_weights();
        let winner = components(0.9, 1.0, 0.2, 0.7); // winner had high confidence
        let loser = components(0.7, 1.0, 0.9, 0.7); // loser had high recency
        let ce = Counterexample {
            formula_winner: winner,
            formula_loser: loser,
            user_pinned: UserPinned::Loser,
        };
        let new_w = apply_feedback(&w, &ce);
        assert!(new_w.recency > w.recency, "recency should increase");
        assert!(new_w.confidence < w.confidence, "confidence should decrease");
    }

    #[test]
    fn test_feedback_clamped_to_range_after_many_steps() {
        // Apply an extreme counterexample repeatedly and ensure weights stay in [0.05, 0.60].
        let mut w = default_weights();
        let ce = Counterexample {
            formula_winner: components(1.0, 3.0, 1.0, 1.0),
            formula_loser: components(0.0, 0.0, 0.0, 0.0),
            user_pinned: UserPinned::Loser,
        };
        for _ in 0..500 {
            w = apply_feedback(&w, &ce);
            assert!(w.confidence >= WEIGHT_MIN - 1e-12 && w.confidence <= WEIGHT_MAX + 1e-12);
            assert!(
                w.corroboration >= WEIGHT_MIN - 1e-12 && w.corroboration <= WEIGHT_MAX + 1e-12
            );
            assert!(w.recency >= WEIGHT_MIN - 1e-12 && w.recency <= WEIGHT_MAX + 1e-12);
            assert!(w.validation >= WEIGHT_MIN - 1e-12 && w.validation <= WEIGHT_MAX + 1e-12);
            let sum = w.confidence + w.corroboration + w.recency + w.validation;
            assert!(
                sum >= WEIGHT_SUM_MIN - 1e-9 && sum <= WEIGHT_SUM_MAX + 1e-9,
                "sum drifted to {}",
                sum
            );
        }
    }

    #[test]
    fn test_feedback_sum_stays_in_band_typical_steps() {
        let mut w = default_weights();
        let ce = Counterexample {
            formula_winner: components(0.9, 2.0, 0.5, 0.7),
            formula_loser: components(0.6, 1.0, 0.9, 0.95),
            user_pinned: UserPinned::Loser,
        };
        for _ in 0..50 {
            w = apply_feedback(&w, &ce);
            let sum = w.confidence + w.corroboration + w.recency + w.validation;
            assert!(sum >= WEIGHT_SUM_MIN - 1e-9 && sum <= WEIGHT_SUM_MAX + 1e-9);
        }
    }

    #[test]
    fn test_feedback_single_step_magnitude_bounded() {
        // A single step shouldn't move any weight by more than step_size.
        let w = default_weights();
        let ce = Counterexample {
            formula_winner: components(1.0, 3.0, 1.0, 1.0),
            formula_loser: components(0.0, 0.0, 0.0, 0.0),
            user_pinned: UserPinned::Loser,
        };
        let new_w = apply_feedback(&w, &ce);
        assert!((new_w.confidence - w.confidence).abs() <= FEEDBACK_STEP_SIZE + 1e-12);
        assert!((new_w.corroboration - w.corroboration).abs() <= FEEDBACK_STEP_SIZE + 1e-12);
        assert!((new_w.recency - w.recency).abs() <= FEEDBACK_STEP_SIZE + 1e-12);
        assert!((new_w.validation - w.validation).abs() <= FEEDBACK_STEP_SIZE + 1e-12);
    }

    // ----- cosine_similarity (reusing reranker::cosine_similarity_f32) -----

    #[test]
    fn test_cosine_identical() {
        let a = vec![1.0f32, 2.0, 3.0];
        let b = vec![1.0f32, 2.0, 3.0];
        assert!((cosine_similarity_f32(&a, &b) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn test_cosine_orthogonal() {
        let a = vec![1.0f32, 0.0];
        let b = vec![0.0f32, 1.0];
        assert!(cosine_similarity_f32(&a, &b).abs() < 1e-9);
    }

    #[test]
    fn test_cosine_opposite() {
        let a = vec![1.0f32, 0.0];
        let b = vec![-1.0f32, 0.0];
        assert!((cosine_similarity_f32(&a, &b) + 1.0).abs() < 1e-9);
    }

    #[test]
    fn test_cosine_zero_vector_returns_zero_not_nan() {
        let a = vec![0.0f32, 0.0, 0.0];
        let b = vec![1.0f32, 2.0, 3.0];
        let sim = cosine_similarity_f32(&a, &b);
        assert!(!sim.is_nan());
        assert_eq!(sim, 0.0);
    }

    #[test]
    fn test_cosine_mismatched_lengths_returns_zero() {
        // Reranker's cosine_similarity_f32 returns 0.0 for mismatched lengths (by contract).
        let a = vec![1.0f32, 2.0];
        let b = vec![1.0f32, 2.0, 3.0];
        assert_eq!(cosine_similarity_f32(&a, &b), 0.0);
    }

    // ----- Serde round-trip for new types -----

    #[test]
    fn test_weights_serde_round_trip() {
        let w = default_weights();
        let j = serde_json::to_string(&w).unwrap();
        let back: ResolutionWeights = serde_json::from_str(&j).unwrap();
        assert_eq!(w, back);
    }

    #[test]
    fn test_outcome_serde_round_trip() {
        // f64 fields may drift by ~1 ulp through JSON; compare with tight tolerance.
        let a = make_claim("a", 0.9, 1, "oc", Some(&iso_days_ago(1)), vec![]);
        let b = make_claim("b", 0.5, 1, "oc", Some(&iso_days_ago(100)), vec![]);
        let outcome = resolve_pair(&a, "alpha", &b, "beta", NOW, &default_weights());
        let j = serde_json::to_string(&outcome).unwrap();
        let back: ResolutionOutcome = serde_json::from_str(&j).unwrap();
        assert_eq!(back.winner_id, outcome.winner_id);
        assert_eq!(back.loser_id, outcome.loser_id);
        assert!((back.winner_score - outcome.winner_score).abs() < 1e-12);
        assert!((back.loser_score - outcome.loser_score).abs() < 1e-12);
        assert!((back.score_delta - outcome.score_delta).abs() < 1e-12);
        assert!(
            (back.winner_components.weighted_total - outcome.winner_components.weighted_total)
                .abs()
                < 1e-12
        );
    }

    #[test]
    fn test_contradiction_serde_round_trip() {
        let c = Contradiction {
            claim_a_id: "a".to_string(),
            claim_b_id: "b".to_string(),
            entity_id: "deadbeef".to_string(),
            similarity: 0.5,
        };
        let j = serde_json::to_string(&c).unwrap();
        let back: Contradiction = serde_json::from_str(&j).unwrap();
        assert_eq!(c, back);
    }

    #[test]
    fn test_counterexample_serde_round_trip() {
        let ce = Counterexample {
            formula_winner: components(0.9, 2.0, 0.5, 1.0),
            formula_loser: components(0.5, 1.0, 0.9, 0.7),
            user_pinned: UserPinned::Loser,
        };
        let j = serde_json::to_string(&ce).unwrap();
        let back: Counterexample = serde_json::from_str(&j).unwrap();
        assert_eq!(ce, back);
    }

    #[test]
    fn test_counterexample_both_variant_serde() {
        let ce = Counterexample {
            formula_winner: components(0.9, 2.0, 0.5, 1.0),
            formula_loser: components(0.5, 1.0, 0.9, 0.7),
            user_pinned: UserPinned::Both,
        };
        let j = serde_json::to_string(&ce).unwrap();
        let back: Counterexample = serde_json::from_str(&j).unwrap();
        assert_eq!(ce, back);
    }

    // =========================================================================
    // Step D: resolve_with_candidates
    // =========================================================================

    /// Helper: create a normalized embedding vector of the given dimension.
    fn make_embedding(seed: f32, dim: usize) -> Vec<f32> {
        let raw: Vec<f32> = (0..dim).map(|i| seed + i as f32 * 0.1).collect();
        let norm: f32 = raw.iter().map(|x| x * x).sum::<f32>().sqrt();
        raw.iter().map(|x| x / norm).collect()
    }

    /// Similar embedding (slightly perturbed).
    fn perturb_embedding(base: &[f32], delta: f32) -> Vec<f32> {
        let raw: Vec<f32> = base.iter().enumerate().map(|(i, &x)| {
            if i == 0 { x + delta } else { x }
        }).collect();
        let norm: f32 = raw.iter().map(|x| x * x).sum::<f32>().sqrt();
        raw.iter().map(|x| x / norm).collect()
    }

    #[test]
    fn test_resolve_with_candidates_no_contradictions() {
        // New claim and existing share no entities → no contradictions.
        let new = make_claim("prefers Vim", 0.9, 1, "oc", Some(&iso_days_ago(1)), vec!["editor"]);
        let existing = make_claim("likes Rust", 0.8, 1, "oc", Some(&iso_days_ago(5)), vec!["programming"]);
        let emb = make_embedding(1.0, 10);
        let candidates = vec![(existing, "exist_id".to_string(), emb.clone())];

        let actions = resolve_with_candidates(
            &new, "new_id", &emb, &candidates, &default_weights(),
            DEFAULT_LOWER_THRESHOLD, DEFAULT_UPPER_THRESHOLD, NOW, 0.01,
        );
        assert!(actions.is_empty());
    }

    #[test]
    fn test_resolve_with_candidates_empty_candidates() {
        let new = make_claim("prefers Vim", 0.9, 1, "oc", Some(&iso_days_ago(1)), vec!["editor"]);
        let emb = make_embedding(1.0, 10);
        let candidates: Vec<(Claim, String, Vec<f32>)> = vec![];

        let actions = resolve_with_candidates(
            &new, "new_id", &emb, &candidates, &default_weights(),
            DEFAULT_LOWER_THRESHOLD, DEFAULT_UPPER_THRESHOLD, NOW, 0.01,
        );
        assert!(actions.is_empty());
    }

    #[test]
    fn test_resolve_with_candidates_empty_embedding() {
        let new = make_claim("prefers Vim", 0.9, 1, "oc", Some(&iso_days_ago(1)), vec!["editor"]);
        let existing = make_claim("uses VS Code", 0.8, 1, "oc", Some(&iso_days_ago(30)), vec!["editor"]);
        let emb = make_embedding(1.0, 10);
        let candidates = vec![(existing, "exist_id".to_string(), emb)];

        let actions = resolve_with_candidates(
            &new, "new_id", &[], &candidates, &default_weights(),
            DEFAULT_LOWER_THRESHOLD, DEFAULT_UPPER_THRESHOLD, NOW, 0.01,
        );
        assert!(actions.is_empty());
    }

    #[test]
    fn test_resolve_with_candidates_new_wins_supersede() {
        // New claim: recent, high confidence. Existing: old, lower confidence.
        // Both share entity "editor". Embeddings are similar but in the contradiction band.
        let new = make_claim("uses VS Code", 0.95, 1, "totalreclaw_remember", Some(&iso_days_ago(1)), vec!["editor"]);
        let existing = make_claim("prefers Vim", 0.6, 1, "oc", Some(&iso_days_ago(60)), vec!["editor"]);

        let new_emb = make_embedding(1.0, 10);
        let existing_emb = perturb_embedding(&new_emb, 0.3);
        let candidates = vec![(existing, "exist_id".to_string(), existing_emb)];

        let actions = resolve_with_candidates(
            &new, "new_id", &new_emb, &candidates, &default_weights(),
            0.0, 1.0, NOW, 0.01,
        );

        assert_eq!(actions.len(), 1);
        match &actions[0] {
            crate::claims::ResolutionAction::SupersedeExisting {
                existing_id, new_id, winner_score, loser_score, entity_id, ..
            } => {
                assert_eq!(existing_id, "exist_id");
                assert_eq!(new_id, "new_id");
                assert!(winner_score.unwrap() > loser_score.unwrap());
                assert!(entity_id.is_some());
            }
            other => panic!("expected SupersedeExisting, got {:?}", other),
        }
    }

    #[test]
    fn test_resolve_with_candidates_existing_wins_skip() {
        // Existing claim: recent, explicit remember, high confidence.
        // New claim: old, auto-extracted, lower confidence.
        let new = make_claim("prefers Vim", 0.5, 1, "oc", Some(&iso_days_ago(60)), vec!["editor"]);
        let existing = make_claim("uses VS Code", 0.95, 1, "totalreclaw_remember", Some(&iso_days_ago(1)), vec!["editor"]);

        let new_emb = make_embedding(1.0, 10);
        let existing_emb = perturb_embedding(&new_emb, 0.3);
        let candidates = vec![(existing, "exist_id".to_string(), existing_emb)];

        let actions = resolve_with_candidates(
            &new, "new_id", &new_emb, &candidates, &default_weights(),
            0.0, 1.0, NOW, 0.01,
        );

        assert_eq!(actions.len(), 1);
        match &actions[0] {
            crate::claims::ResolutionAction::SkipNew {
                reason, existing_id, winner_score, loser_score, ..
            } => {
                assert_eq!(*reason, crate::claims::SkipReason::ExistingWins);
                assert_eq!(existing_id, "exist_id");
                assert!(winner_score.is_some());
                assert!(loser_score.is_some());
            }
            other => panic!("expected SkipNew, got {:?}", other),
        }
    }

    #[test]
    fn test_resolve_with_candidates_pinned_existing_skip() {
        let new = make_claim("uses VS Code", 0.95, 1, "totalreclaw_remember", Some(&iso_days_ago(1)), vec!["editor"]);
        let mut existing = make_claim("prefers Vim", 0.6, 1, "oc", Some(&iso_days_ago(60)), vec!["editor"]);
        existing.status = ClaimStatus::Pinned;

        let new_emb = make_embedding(1.0, 10);
        let existing_emb = perturb_embedding(&new_emb, 0.3);
        let candidates = vec![(existing, "exist_id".to_string(), existing_emb)];

        let actions = resolve_with_candidates(
            &new, "new_id", &new_emb, &candidates, &default_weights(),
            0.0, 1.0, NOW, 0.01,
        );

        assert_eq!(actions.len(), 1);
        match &actions[0] {
            crate::claims::ResolutionAction::SkipNew {
                reason, existing_id, ..
            } => {
                assert_eq!(*reason, crate::claims::SkipReason::ExistingPinned);
                assert_eq!(existing_id, "exist_id");
            }
            other => panic!("expected SkipNew ExistingPinned, got {:?}", other),
        }
    }

    #[test]
    fn test_resolve_with_candidates_tie_zone() {
        // Two claims with near-identical scores → tie zone.
        // Both have same confidence, corroboration, source, and very close recency.
        let new = make_claim("prefers Postgres for OLTP", 0.85, 1, "oc", Some(&iso_days_ago(2)), vec!["database"]);
        let existing = make_claim("prefers DuckDB for OLAP", 0.85, 1, "oc", Some(&iso_days_ago(2)), vec!["database"]);

        let new_emb = make_embedding(1.0, 10);
        let existing_emb = perturb_embedding(&new_emb, 0.3);
        let candidates = vec![(existing, "exist_id".to_string(), existing_emb)];

        // Use a very large tie_zone_tolerance to force a tie.
        let actions = resolve_with_candidates(
            &new, "new_id", &new_emb, &candidates, &default_weights(),
            0.0, 1.0, NOW, 10.0, // huge tolerance → everything is a tie
        );

        assert_eq!(actions.len(), 1);
        match &actions[0] {
            crate::claims::ResolutionAction::TieLeaveBoth {
                existing_id, new_id, entity_id, ..
            } => {
                assert_eq!(existing_id, "exist_id");
                assert_eq!(new_id, "new_id");
                assert!(entity_id.is_some());
            }
            other => panic!("expected TieLeaveBoth, got {:?}", other),
        }
    }

    // =========================================================================
    // Step D: build_decision_log_entries
    // =========================================================================

    #[test]
    fn test_build_decision_log_entries_supersede_populates_loser_json() {
        use crate::claims::ResolutionAction;
        let actions = vec![ResolutionAction::SupersedeExisting {
            existing_id: "0xold".to_string(),
            new_id: "0xnew".to_string(),
            similarity: 0.72,
            score_gap: 0.15,
            entity_id: Some("ent123".to_string()),
            winner_score: Some(0.8),
            loser_score: Some(0.65),
            winner_components: None,
            loser_components: None,
        }];
        let mut existing_map = std::collections::HashMap::new();
        existing_map.insert("0xold".to_string(), r#"{"t":"old claim"}"#.to_string());

        let entries = build_decision_log_entries(&actions, "{}", &existing_map, "active", 1_776_384_000);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].action, "supersede_existing");
        assert_eq!(entries[0].entity_id, "ent123");
        assert_eq!(entries[0].loser_claim_json.as_deref(), Some(r#"{"t":"old claim"}"#));
        assert_eq!(entries[0].mode, "active");
        assert_eq!(entries[0].reason.as_deref(), Some("new_wins"));
    }

    #[test]
    fn test_build_decision_log_entries_skip_no_loser_json() {
        use crate::claims::{ResolutionAction, SkipReason};
        let actions = vec![ResolutionAction::SkipNew {
            reason: SkipReason::ExistingWins,
            existing_id: "0xold".to_string(),
            new_id: "0xnew".to_string(),
            entity_id: Some("ent123".to_string()),
            similarity: Some(0.72),
            winner_score: Some(0.8),
            loser_score: Some(0.65),
            winner_components: None,
            loser_components: None,
        }];
        let existing_map = std::collections::HashMap::new();

        let entries = build_decision_log_entries(&actions, "{}", &existing_map, "active", 1_776_384_000);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].action, "skip_new");
        assert!(entries[0].loser_claim_json.is_none());
        assert_eq!(entries[0].reason.as_deref(), Some("existing_wins"));
    }

    #[test]
    fn test_build_decision_log_entries_tie() {
        use crate::claims::ResolutionAction;
        let actions = vec![ResolutionAction::TieLeaveBoth {
            existing_id: "0xold".to_string(),
            new_id: "0xnew".to_string(),
            similarity: 0.72,
            score_gap: 0.005,
            entity_id: Some("ent123".to_string()),
            winner_score: Some(0.7),
            loser_score: Some(0.695),
            winner_components: None,
            loser_components: None,
        }];
        let existing_map = std::collections::HashMap::new();

        let entries = build_decision_log_entries(&actions, "{}", &existing_map, "active", 1_776_384_000);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].action, "tie_leave_both");
        assert_eq!(entries[0].reason.as_deref(), Some("tie_below_tolerance"));
    }

    #[test]
    fn test_build_decision_log_entries_shadow_mode_overrides_action() {
        use crate::claims::ResolutionAction;
        let actions = vec![ResolutionAction::SupersedeExisting {
            existing_id: "0xold".to_string(),
            new_id: "0xnew".to_string(),
            similarity: 0.72,
            score_gap: 0.15,
            entity_id: Some("ent123".to_string()),
            winner_score: Some(0.8),
            loser_score: Some(0.65),
            winner_components: None,
            loser_components: None,
        }];
        let existing_map = std::collections::HashMap::new();

        let entries = build_decision_log_entries(&actions, "{}", &existing_map, "shadow", 1_776_384_000);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].action, "shadow");
        assert_eq!(entries[0].mode, "shadow");
    }

    // =========================================================================
    // Step D: filter_shadow_mode
    // =========================================================================

    #[test]
    fn test_filter_shadow_mode_active_passes_through() {
        use crate::claims::ResolutionAction;
        let actions = vec![
            ResolutionAction::SupersedeExisting {
                existing_id: "a".to_string(),
                new_id: "b".to_string(),
                similarity: 0.7,
                score_gap: 0.2,
                entity_id: None,
                winner_score: None,
                loser_score: None,
                winner_components: None,
                loser_components: None,
            },
        ];
        let filtered = filter_shadow_mode(actions, "active");
        assert_eq!(filtered.len(), 1);
    }

    #[test]
    fn test_filter_shadow_mode_active_removes_ties() {
        use crate::claims::ResolutionAction;
        let actions = vec![
            ResolutionAction::TieLeaveBoth {
                existing_id: "a".to_string(),
                new_id: "b".to_string(),
                similarity: 0.7,
                score_gap: 0.005,
                entity_id: None,
                winner_score: None,
                loser_score: None,
                winner_components: None,
                loser_components: None,
            },
            ResolutionAction::SupersedeExisting {
                existing_id: "c".to_string(),
                new_id: "d".to_string(),
                similarity: 0.7,
                score_gap: 0.2,
                entity_id: None,
                winner_score: None,
                loser_score: None,
                winner_components: None,
                loser_components: None,
            },
        ];
        let filtered = filter_shadow_mode(actions, "active");
        assert_eq!(filtered.len(), 1);
        match &filtered[0] {
            ResolutionAction::SupersedeExisting { existing_id, .. } => {
                assert_eq!(existing_id, "c");
            }
            other => panic!("expected SupersedeExisting, got {:?}", other),
        }
    }

    #[test]
    fn test_filter_shadow_mode_shadow_returns_empty() {
        use crate::claims::ResolutionAction;
        let actions = vec![ResolutionAction::SupersedeExisting {
            existing_id: "a".to_string(),
            new_id: "b".to_string(),
            similarity: 0.7,
            score_gap: 0.2,
            entity_id: None,
            winner_score: None,
            loser_score: None,
            winner_components: None,
            loser_components: None,
        }];
        let filtered = filter_shadow_mode(actions, "shadow");
        assert!(filtered.is_empty());
    }

    #[test]
    fn test_filter_shadow_mode_off_returns_empty() {
        use crate::claims::ResolutionAction;
        let actions = vec![ResolutionAction::SupersedeExisting {
            existing_id: "a".to_string(),
            new_id: "b".to_string(),
            similarity: 0.7,
            score_gap: 0.2,
            entity_id: None,
            winner_score: None,
            loser_score: None,
            winner_components: None,
            loser_components: None,
        }];
        let filtered = filter_shadow_mode(actions, "off");
        assert!(filtered.is_empty());
    }

    // =========================================================================
    // Step D: Integration test — full pipeline
    // =========================================================================

    #[test]
    fn test_full_pipeline_resolve_to_decision_log() {
        // Full integration: resolve_with_candidates → build_decision_log_entries → filter
        let new = make_claim("uses VS Code", 0.95, 1, "totalreclaw_remember", Some(&iso_days_ago(1)), vec!["editor"]);
        let existing = make_claim("prefers Vim", 0.6, 1, "oc", Some(&iso_days_ago(60)), vec!["editor"]);

        let new_emb = make_embedding(1.0, 10);
        let existing_emb = perturb_embedding(&new_emb, 0.3);
        let existing_json = serde_json::to_string(&existing).unwrap();
        let candidates = vec![(existing, "0xold".to_string(), existing_emb)];

        // Step 1: Resolve
        let actions = resolve_with_candidates(
            &new, "0xnew", &new_emb, &candidates, &default_weights(),
            0.0, 1.0, NOW, 0.01,
        );
        assert!(!actions.is_empty());

        // Step 2: Build decision log entries
        let mut existing_map = std::collections::HashMap::new();
        existing_map.insert("0xold".to_string(), existing_json.clone());
        let entries = build_decision_log_entries(&actions, "{}", &existing_map, "active", NOW);
        assert_eq!(entries.len(), actions.len());
        // Verify the entry has the right shape.
        let entry = &entries[0];
        assert!(entry.ts == NOW);
        assert!(!entry.entity_id.is_empty());
        assert_eq!(entry.new_claim_id, "0xnew");
        assert_eq!(entry.existing_claim_id, "0xold");

        // Step 3: Filter for active mode
        let filtered = filter_shadow_mode(actions.clone(), "active");
        // Should have at least 1 actionable result (supersede or skip, no ties)
        for a in &filtered {
            assert!(!matches!(a, crate::claims::ResolutionAction::TieLeaveBoth { .. }));
        }

        // Step 4: Shadow mode returns empty
        let shadow_filtered = filter_shadow_mode(actions, "shadow");
        assert!(shadow_filtered.is_empty());

        // Step 5: Decision log entry is serializable
        let entry_json = serde_json::to_string(&entry).unwrap();
        let _: crate::decision_log::DecisionLogEntry =
            serde_json::from_str(&entry_json).unwrap();
    }

    #[test]
    fn test_build_decision_log_skip_pinned_reason_format() {
        // Verify the reason string format for ExistingPinned.
        use crate::claims::{ResolutionAction, SkipReason};
        let actions = vec![ResolutionAction::SkipNew {
            reason: SkipReason::ExistingPinned,
            existing_id: "0xold".to_string(),
            new_id: "0xnew".to_string(),
            entity_id: Some("ent".to_string()),
            similarity: Some(0.7),
            winner_score: None,
            loser_score: None,
            winner_components: None,
            loser_components: None,
        }];
        let entries = build_decision_log_entries(&actions, "{}", &std::collections::HashMap::new(), "active", NOW);
        assert_eq!(entries[0].reason.as_deref(), Some("existing_pinned"));
    }
}
