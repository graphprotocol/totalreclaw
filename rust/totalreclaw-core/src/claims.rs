//! Knowledge Graph claim types (Phase 1 Stage 1a).

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClaimCategory {
    #[serde(rename = "fact")]
    Fact,
    #[serde(rename = "pref")]
    Preference,
    #[serde(rename = "dec")]
    Decision,
    #[serde(rename = "epi")]
    Episodic,
    #[serde(rename = "goal")]
    Goal,
    #[serde(rename = "ctx")]
    Context,
    #[serde(rename = "sum")]
    Summary,
    /// A reusable operational rule, non-obvious gotcha, or convention the user
    /// wants to remember for next time. Distinct from decisions (which have
    /// reasoning for a specific choice) and preferences (personal tastes):
    /// rules are impersonal, actionable, and transferable. Phase 2.2 addition.
    #[serde(rename = "rule")]
    Rule,
    #[serde(rename = "ent")]
    Entity,
    #[serde(rename = "dig")]
    Digest,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClaimStatus {
    #[serde(rename = "a")]
    Active,
    #[serde(rename = "s")]
    Superseded,
    #[serde(rename = "r")]
    Retracted,
    #[serde(rename = "c")]
    Contradicted,
    #[serde(rename = "p")]
    Pinned,
}

impl Default for ClaimStatus {
    fn default() -> Self {
        ClaimStatus::Active
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EntityType {
    Person,
    Project,
    Tool,
    Company,
    Concept,
    Place,
}

fn is_one(n: &u32) -> bool {
    *n == 1
}

fn is_active(s: &ClaimStatus) -> bool {
    matches!(s, ClaimStatus::Active)
}

fn is_empty_vec<T>(v: &Vec<T>) -> bool {
    v.is_empty()
}

fn default_corroboration() -> u32 {
    1
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EntityRef {
    #[serde(rename = "n")]
    pub name: String,
    #[serde(rename = "tp")]
    pub entity_type: EntityType,
    #[serde(rename = "r", skip_serializing_if = "Option::is_none", default)]
    pub role: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Claim {
    #[serde(rename = "t")]
    pub text: String,
    #[serde(rename = "c")]
    pub category: ClaimCategory,
    #[serde(rename = "cf")]
    pub confidence: f64,
    #[serde(rename = "i")]
    pub importance: u8,
    #[serde(
        rename = "cc",
        skip_serializing_if = "is_one",
        default = "default_corroboration"
    )]
    pub corroboration_count: u32,
    #[serde(rename = "sa")]
    pub source_agent: String,
    #[serde(rename = "sc", skip_serializing_if = "Option::is_none", default)]
    pub source_conversation: Option<String>,
    #[serde(rename = "ea", skip_serializing_if = "Option::is_none", default)]
    pub extracted_at: Option<String>,
    #[serde(rename = "e", skip_serializing_if = "is_empty_vec", default)]
    pub entities: Vec<EntityRef>,
    #[serde(rename = "sup", skip_serializing_if = "Option::is_none", default)]
    pub supersedes: Option<String>,
    #[serde(rename = "sby", skip_serializing_if = "Option::is_none", default)]
    pub superseded_by: Option<String>,
    #[serde(rename = "vf", skip_serializing_if = "Option::is_none", default)]
    pub valid_from: Option<String>,
    #[serde(rename = "st", skip_serializing_if = "is_active", default)]
    pub status: ClaimStatus,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Entity {
    pub id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub entity_type: EntityType,
    pub aliases: Vec<String>,
    pub claim_ids: Vec<String>,
    pub first_seen: String,
    pub last_seen: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DigestClaim {
    pub text: String,
    pub category: ClaimCategory,
    pub confidence: f64,
    pub age: String,
}

/// Tie-zone score tolerance for contradiction resolution.
///
/// When the formula winner beats the loser by less than this amount, the
/// decision is treated as a tie and both claims stay active. Calibrated against
/// the 2026-04-14 false-positive where the gap was 9 parts per million.
pub const TIE_ZONE_SCORE_TOLERANCE: f64 = 0.01;

/// Check whether a v0 [`Claim`] has pinned status.
///
/// v0 pin state is carried by the compact `st == "p"` sentinel
/// ([`ClaimStatus::Pinned`]). For v1 claims use
/// [`is_pinned_memory_claim_v1`] or the JSON-level [`is_pinned_json`].
pub fn is_pinned_claim(claim: &Claim) -> bool {
    matches!(claim.status, ClaimStatus::Pinned)
}

/// Check whether a v1.1 [`MemoryClaimV1`] is pinned.
///
/// Returns `true` when [`MemoryClaimV1::pin_status`] is `Some(PinStatus::Pinned)`.
/// `None` and `Some(PinStatus::Unpinned)` both mean unpinned (spec §pin-semantics).
pub fn is_pinned_memory_claim_v1(claim: &MemoryClaimV1) -> bool {
    matches!(claim.pin_status, Some(PinStatus::Pinned))
}

/// Check whether a JSON-serialized claim has pinned status, recognizing both
/// the legacy v0 [`Claim`] shape (`st == "p"`) and the v1.1
/// [`MemoryClaimV1`] shape (`pin_status == "pinned"`).
///
/// Dispatch rule: if the JSON parses as a `MemoryClaimV1` (it contains the
/// required v1 fields `id`, `text`, `type`, `source`, `created_at` with a
/// closed-enum `type` token), the v1 check is authoritative. Otherwise we
/// fall through to the v0 parser. Returns `false` for any JSON that fails
/// both parsers or has neither sentinel set.
///
/// Guarantee: for every input accepted by pre-v1.1 `is_pinned_json`, the
/// return value is unchanged. New return: `true` when the input is a v1.1
/// blob with `pin_status == "pinned"`.
pub fn is_pinned_json(claim_json: &str) -> bool {
    // Try v1.1 first — a well-formed MemoryClaimV1 won't match the v0 shape
    // (different required fields, different type enum), so no ambiguity.
    if let Ok(v1) = serde_json::from_str::<MemoryClaimV1>(claim_json) {
        if is_pinned_memory_claim_v1(&v1) {
            return true;
        }
        // Parsed as v1 but pin_status != pinned — authoritative, do NOT
        // fall through to the v0 parser (would be a type error anyway).
        return false;
    }
    // Fall back to v0 short-key parser.
    match serde_json::from_str::<Claim>(claim_json) {
        Ok(claim) => is_pinned_claim(&claim),
        Err(_) => false,
    }
}

/// The action to take after checking pin status and tie-zone guard during
/// contradiction resolution.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ResolutionAction {
    /// No contradiction detected — pass through.
    NoContradiction,
    /// New claim wins; supersede the existing claim.
    SupersedeExisting {
        existing_id: String,
        new_id: String,
        similarity: f64,
        score_gap: f64,
        /// Entity that triggered the contradiction (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        entity_id: Option<String>,
        /// Winner's score (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        winner_score: Option<f64>,
        /// Loser's score (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        loser_score: Option<f64>,
        /// Winner's per-component score breakdown (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        winner_components: Option<crate::contradiction::ScoreComponents>,
        /// Loser's per-component score breakdown (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        loser_components: Option<crate::contradiction::ScoreComponents>,
    },
    /// Skip the new claim (existing wins or is pinned).
    SkipNew {
        reason: SkipReason,
        existing_id: String,
        new_id: String,
        /// Entity that triggered the contradiction (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        entity_id: Option<String>,
        /// Similarity between the claims (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        similarity: Option<f64>,
        /// Winner's score (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        winner_score: Option<f64>,
        /// Loser's score (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        loser_score: Option<f64>,
        /// Winner's per-component score breakdown (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        winner_components: Option<crate::contradiction::ScoreComponents>,
        /// Loser's per-component score breakdown (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        loser_components: Option<crate::contradiction::ScoreComponents>,
    },
    /// Score gap is within tie-zone tolerance; keep both claims.
    TieLeaveBoth {
        existing_id: String,
        new_id: String,
        similarity: f64,
        score_gap: f64,
        /// Entity that triggered the contradiction (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        entity_id: Option<String>,
        /// Winner's score (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        winner_score: Option<f64>,
        /// Loser's score (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        loser_score: Option<f64>,
        /// Winner's per-component score breakdown (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        winner_components: Option<crate::contradiction::ScoreComponents>,
        /// Loser's per-component score breakdown (populated by orchestration).
        #[serde(skip_serializing_if = "Option::is_none", default)]
        loser_components: Option<crate::contradiction::ScoreComponents>,
    },
}

/// Why a new claim was skipped in favour of the existing one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SkipReason {
    /// The existing claim is pinned and cannot be superseded.
    ExistingPinned,
    /// The existing claim scored higher than the new one.
    ExistingWins,
    /// The similarity was below the contradiction threshold.
    BelowThreshold,
}

/// Apply pin-status and tie-zone checks to a resolution outcome.
///
/// - If the existing claim is pinned, returns `SkipNew { ExistingPinned }`.
/// - If `resolution_winner` == `existing_claim_id`, returns `SkipNew { ExistingWins }`.
/// - If `resolution_winner` == `new_claim_id` but `score_gap < tie_zone_tolerance`,
///   returns `TieLeaveBoth`.
/// - Otherwise returns `SupersedeExisting`.
pub fn respect_pin_in_resolution(
    existing_claim_json: &str,
    new_claim_id: &str,
    existing_claim_id: &str,
    resolution_winner: &str,
    score_gap: f64,
    similarity: f64,
    tie_zone_tolerance: f64,
) -> ResolutionAction {
    // Check if existing claim is pinned.
    if is_pinned_json(existing_claim_json) {
        return ResolutionAction::SkipNew {
            reason: SkipReason::ExistingPinned,
            existing_id: existing_claim_id.to_string(),
            new_id: new_claim_id.to_string(),
            entity_id: None,
            similarity: None,
            winner_score: None,
            loser_score: None,
            winner_components: None,
            loser_components: None,
        };
    }

    // If the existing claim wins the formula, skip the new one.
    if resolution_winner == existing_claim_id {
        return ResolutionAction::SkipNew {
            reason: SkipReason::ExistingWins,
            existing_id: existing_claim_id.to_string(),
            new_id: new_claim_id.to_string(),
            entity_id: None,
            similarity: None,
            winner_score: None,
            loser_score: None,
            winner_components: None,
            loser_components: None,
        };
    }

    // New claim wins — check tie zone.
    if score_gap.abs() < tie_zone_tolerance {
        return ResolutionAction::TieLeaveBoth {
            existing_id: existing_claim_id.to_string(),
            new_id: new_claim_id.to_string(),
            similarity,
            score_gap,
            entity_id: None,
            winner_score: None,
            loser_score: None,
            winner_components: None,
            loser_components: None,
        };
    }

    // New claim wins clearly.
    ResolutionAction::SupersedeExisting {
        existing_id: existing_claim_id.to_string(),
        new_id: new_claim_id.to_string(),
        similarity,
        score_gap,
        entity_id: None,
        winner_score: None,
        loser_score: None,
        winner_components: None,
        loser_components: None,
    }
}

/// Normalize an entity name per §15.8: NFC(lowercase(trim(collapse_whitespace(name)))).
pub fn normalize_entity_name(name: &str) -> String {
    use unicode_normalization::UnicodeNormalization;
    let mut collapsed = String::with_capacity(name.len());
    let mut in_ws = false;
    let mut any = false;
    for ch in name.chars() {
        if ch.is_whitespace() {
            if any && !in_ws {
                collapsed.push(' ');
                in_ws = true;
            }
        } else {
            collapsed.push(ch);
            in_ws = false;
            any = true;
        }
    }
    let trimmed = collapsed.trim_end_matches(' ').to_string();
    let lowered: String = trimmed.chars().flat_map(|c| c.to_lowercase()).collect();
    lowered.nfc().collect()
}

/// Deterministic entity ID: first 8 bytes of SHA256(normalized name) as hex.
pub fn deterministic_entity_id(name: &str) -> String {
    use sha2::{Digest as _, Sha256};
    let normalized = normalize_entity_name(name);
    let hash = Sha256::digest(normalized.as_bytes());
    hex::encode(&hash[..8])
}

/// Parse a decrypted blob as a Claim, falling back to legacy formats per §15.2.
pub fn parse_claim_or_legacy(decrypted: &str) -> Claim {
    if let Ok(claim) = serde_json::from_str::<Claim>(decrypted) {
        return claim;
    }
    let (text, source_agent) =
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(decrypted) {
            match value {
                serde_json::Value::String(s) => (s, "unknown".to_string()),
                serde_json::Value::Object(map) => {
                    let text = map
                        .get("t")
                        .or_else(|| map.get("text"))
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                        .unwrap_or_else(|| decrypted.to_string());
                    let agent = map
                        .get("a")
                        .or_else(|| {
                            map.get("metadata")
                                .and_then(|m| m.as_object())
                                .and_then(|m| m.get("source"))
                        })
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                        .unwrap_or_else(|| "unknown".to_string());
                    (text, agent)
                }
                _ => (decrypted.to_string(), "unknown".to_string()),
            }
        } else {
            (decrypted.to_string(), "unknown".to_string())
        };
    Claim {
        text,
        category: ClaimCategory::Fact,
        confidence: 0.7,
        importance: 5,
        corroboration_count: 1,
        source_agent,
        source_conversation: None,
        extracted_at: None,
        entities: Vec::new(),
        supersedes: None,
        superseded_by: None,
        valid_from: None,
        status: ClaimStatus::Active,
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Digest {
    pub version: u64,
    pub compiled_at: String,
    pub fact_count: u32,
    pub entity_count: u32,
    pub contradiction_count: u32,
    pub identity: String,
    pub top_claims: Vec<DigestClaim>,
    pub recent_decisions: Vec<DigestClaim>,
    pub active_projects: Vec<String>,
    pub active_contradictions: u32,
    pub prompt_text: String,
}

// ---------------------------------------------------------------------------
// Memory Taxonomy v1 (spec: docs/specs/totalreclaw/memory-taxonomy-v1.md)
//
// These types are additive and coexist with the v0 `Claim` / `ClaimCategory`
// types during the migration window. v0 types MUST NOT be removed until all
// clients have migrated and v1 has locked post-WildChat validation.
// ---------------------------------------------------------------------------

/// The required `schema_version` value for all v1 claims.
///
/// Fixed at `"1.0"` per spec §schema. Receivers MUST refuse to read claims
/// with unknown schema versions (fail-safe default).
pub const MEMORY_CLAIM_V1_SCHEMA_VERSION: &str = "1.0";

/// v1 memory type — closed enum of 6 speech-act-grounded categories.
///
/// Each value maps to one of Searle's illocutionary classes. See spec
/// §type-semantics for boundary tests and legacy-type absorption.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Hash)]
#[serde(rename_all = "lowercase")]
pub enum MemoryTypeV1 {
    /// Assertive speech act — state of the world. Absorbs legacy
    /// fact / context / decision.
    Claim,
    /// Expressive speech act — likes / dislikes / tastes.
    Preference,
    /// Imperative speech act — rules the user wants applied going forward
    /// (absorbs legacy `rule`).
    Directive,
    /// Commissive speech act — future intent (absorbs legacy `goal`).
    Commitment,
    /// Narrative — notable past events (absorbs legacy `episodic`).
    Episode,
    /// Derived synthesis — only valid with source in {derived, assistant}.
    Summary,
}

/// Provenance source for a memory claim.
///
/// Per spec §provenance-filter, `source` is a first-class ranking signal.
/// The v1 retrieval Tier 1 pipeline applies a source-weighted multiplier
/// to the final RRF score so assistant-authored facts don't drown out
/// user-authored claims.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Hash)]
#[serde(rename_all = "kebab-case")]
pub enum MemorySource {
    /// User explicitly stated the claim (highest trust).
    User,
    /// Extractor confidently inferred from user signals.
    UserInferred,
    /// Assistant authored — heavy penalty at retrieval.
    Assistant,
    /// Imported from another system (e.g. Mem0, ChatGPT, Claude memory).
    External,
    /// Computed (digests, summaries, consolidation).
    Derived,
}

/// Life-domain scope for a memory claim. Open-extensible per client,
/// but every v1-compliant client MUST accept all values defined here
/// when reading from a vault written by another client.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Hash)]
#[serde(rename_all = "lowercase")]
pub enum MemoryScope {
    Work,
    Personal,
    Health,
    Family,
    Creative,
    Finance,
    Misc,
    Unspecified,
}

/// Temporal stability of a memory claim. Assigned in the comparative
/// rescoring pass, not at single-claim extraction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Hash)]
#[serde(rename_all = "lowercase")]
pub enum MemoryVolatility {
    /// Unlikely to change for years (name, allergies, birthplace).
    Stable,
    /// Changes occasionally (job, active project, partner's name).
    Updatable,
    /// Short-lived (today's task, this week's itinerary).
    Ephemeral,
}

/// Pin state for a v1.1 memory claim.
///
/// Added as an additive extension in spec v1.1 (2026-04-19) so the pin path
/// can emit canonical v1 blobs (outer protobuf `version = 4`, inner
/// `schema_version = "1.0"`) while still carrying the user's explicit pin
/// intent. See `docs/specs/totalreclaw/memory-taxonomy-v1.md#pin-semantics`.
///
/// Absence of the field is equivalent to `Unpinned` — readers MUST tolerate
/// either representation. [`is_pinned_claim`] / [`is_pinned_json`] also return
/// `true` for legacy v0 short-key blobs where `st == "p"`, so cross-version
/// vaults produce uniform pin-detection semantics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Hash)]
#[serde(rename_all = "snake_case")]
pub enum PinStatus {
    /// User explicitly pinned — immune to auto-supersede / auto-retract.
    Pinned,
    /// Standard behavior (default when absent).
    Unpinned,
}

impl PinStatus {
    /// Case-insensitive parser; returns `Unpinned` for unknown input.
    pub fn from_str_lossy(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "pinned" => PinStatus::Pinned,
            "unpinned" => PinStatus::Unpinned,
            _ => PinStatus::Unpinned,
        }
    }
}

/// Entity type for v1 structured entity references. Mirrors the v0
/// [`EntityType`] enum and uses the same string-level encoding so
/// v1 claims can cross-reference v0 entities.
pub type MemoryEntityType = EntityType;

/// Structured entity reference inside a v1 claim.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MemoryEntityV1 {
    /// Prefer proper nouns; specific not generic.
    pub name: String,
    #[serde(rename = "type")]
    pub entity_type: MemoryEntityType,
    /// Optional semantic role (e.g. "chooser", "employer", "rejected").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
}

fn default_schema_version_v1() -> String {
    MEMORY_CLAIM_V1_SCHEMA_VERSION.to_string()
}

fn is_default_schema_version_v1(v: &str) -> bool {
    v == MEMORY_CLAIM_V1_SCHEMA_VERSION
}

fn default_scope_v1() -> MemoryScope {
    MemoryScope::Unspecified
}

fn is_default_scope_v1(s: &MemoryScope) -> bool {
    matches!(s, MemoryScope::Unspecified)
}

fn default_volatility_v1() -> MemoryVolatility {
    MemoryVolatility::Updatable
}

fn is_default_volatility_v1(v: &MemoryVolatility) -> bool {
    matches!(v, MemoryVolatility::Updatable)
}

fn is_empty_entities_v1(v: &[MemoryEntityV1]) -> bool {
    v.is_empty()
}

/// A v1 memory claim per the Memory Taxonomy v1 spec.
///
/// All required fields are always serialized. Optional fields default to the
/// spec-defined sentinel (`scope` defaults to `Unspecified`, `volatility` to
/// `Updatable`) and are preserved on round-trip.
///
/// See `docs/specs/totalreclaw/memory-taxonomy-v1.md` for field semantics.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MemoryClaimV1 {
    // ── REQUIRED ─────────────────────────────────────────────────
    /// UUIDv7 (time-ordered, no separate created_at needed for sort).
    pub id: String,
    /// Human-readable, 5-512 UTF-8 chars.
    pub text: String,
    #[serde(rename = "type")]
    pub memory_type: MemoryTypeV1,
    pub source: MemorySource,
    /// ISO8601 UTC (redundant w/ UUIDv7 but explicit per spec).
    pub created_at: String,
    #[serde(
        default = "default_schema_version_v1",
        skip_serializing_if = "is_default_schema_version_v1"
    )]
    pub schema_version: String,

    // ── ORTHOGONAL AXES (defaults applied if absent) ─────────────
    #[serde(
        default = "default_scope_v1",
        skip_serializing_if = "is_default_scope_v1"
    )]
    pub scope: MemoryScope,
    #[serde(
        default = "default_volatility_v1",
        skip_serializing_if = "is_default_volatility_v1"
    )]
    pub volatility: MemoryVolatility,

    // ── STRUCTURED FIELDS ────────────────────────────────────────
    #[serde(default, skip_serializing_if = "is_empty_entities_v1")]
    pub entities: Vec<MemoryEntityV1>,
    /// Separate `reasoning` field for decision-style claims (replaces old
    /// `decision` type). Populate for `type: claim` where the user expressed
    /// a decision-with-reasoning.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reasoning: Option<String>,
    /// ISO8601 UTC expiration; set by extractor per type+volatility heuristic.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<String>,

    // ── ADVISORY (receivers MAY recompute) ───────────────────────
    /// 1-10, auto-ranked in comparative pass. Advisory.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub importance: Option<u8>,
    /// 0-1, extractor self-assessment.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
    /// Claim ID that overrides this (tombstone chain).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub superseded_by: Option<String>,

    // ── PIN STATE (v1.1, additive) ───────────────────────────────
    /// User-controlled pin state. When `Some(PinStatus::Pinned)`, the claim
    /// is immune to auto-supersede / auto-retract — see
    /// [`respect_pin_in_resolution`]. Absence is equivalent to `Unpinned` on
    /// the wire; [`is_pinned_claim`] also honors the legacy v0 sentinel
    /// (`Claim::status == ClaimStatus::Pinned`) so mixed-version vaults
    /// produce uniform pin-detection semantics.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pin_status: Option<PinStatus>,
}

impl MemoryTypeV1 {
    /// Case-insensitive parser that returns a fallback default for unknown input.
    ///
    /// Returns `MemoryTypeV1::Claim` for any unrecognised (or empty) string.
    /// Used at boundaries where robustness beats strictness — e.g. parsing
    /// decrypted blobs written by another client version.
    pub fn from_str_lossy(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "claim" => MemoryTypeV1::Claim,
            "preference" => MemoryTypeV1::Preference,
            "directive" => MemoryTypeV1::Directive,
            "commitment" => MemoryTypeV1::Commitment,
            "episode" => MemoryTypeV1::Episode,
            "summary" => MemoryTypeV1::Summary,
            _ => MemoryTypeV1::Claim,
        }
    }
}

impl MemorySource {
    /// Case-insensitive parser that returns a fallback for unknown input.
    ///
    /// Returns `MemorySource::UserInferred` for any unrecognised string. This
    /// choice matches the retrieval Tier 1 policy for legacy claims without a
    /// `source` field — they receive a moderate fallback weight rather than
    /// being penalised as hard as `assistant` or promoted as high as `user`.
    pub fn from_str_lossy(s: &str) -> Self {
        // Accept both kebab-case and underscored / space variants to be
        // generous about cross-client serialization drift.
        let normalized: String = s
            .trim()
            .to_ascii_lowercase()
            .chars()
            .map(|c| if c == '_' || c == ' ' { '-' } else { c })
            .collect();
        match normalized.as_str() {
            "user" => MemorySource::User,
            "user-inferred" => MemorySource::UserInferred,
            "assistant" => MemorySource::Assistant,
            "external" => MemorySource::External,
            "derived" => MemorySource::Derived,
            _ => MemorySource::UserInferred,
        }
    }
}

impl MemoryScope {
    /// Case-insensitive parser that returns `Unspecified` for unknown input.
    ///
    /// The scope enum is open-extensible per spec §cross-client-guarantees, so
    /// receivers MUST tolerate unknown scope values when reading from a vault
    /// written by another client — we coerce to `Unspecified` rather than
    /// guess which v1 bucket to funnel them into.
    pub fn from_str_lossy(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "work" => MemoryScope::Work,
            "personal" => MemoryScope::Personal,
            "health" => MemoryScope::Health,
            "family" => MemoryScope::Family,
            "creative" => MemoryScope::Creative,
            "finance" => MemoryScope::Finance,
            "misc" => MemoryScope::Misc,
            "unspecified" => MemoryScope::Unspecified,
            _ => MemoryScope::Unspecified,
        }
    }
}

impl MemoryVolatility {
    /// Case-insensitive parser; returns `Updatable` (the spec default) for
    /// unknown input.
    pub fn from_str_lossy(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "stable" => MemoryVolatility::Stable,
            "updatable" => MemoryVolatility::Updatable,
            "ephemeral" => MemoryVolatility::Ephemeral,
            _ => MemoryVolatility::Updatable,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal_claim() -> Claim {
        Claim {
            text: "prefers PostgreSQL".to_string(),
            category: ClaimCategory::Preference,
            confidence: 0.9,
            importance: 8,
            corroboration_count: 1,
            source_agent: "oc".to_string(),
            source_conversation: None,
            extracted_at: None,
            entities: vec![EntityRef {
                name: "PostgreSQL".to_string(),
                entity_type: EntityType::Tool,
                role: None,
            }],
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        }
    }

    fn full_claim() -> Claim {
        Claim {
            text: "Pedro chose PostgreSQL over MySQL because relational modeling is cleaner for our domain".to_string(),
            category: ClaimCategory::Decision,
            confidence: 0.92,
            importance: 9,
            corroboration_count: 3,
            source_agent: "openclaw-plugin".to_string(),
            source_conversation: Some("conv-abc-123".to_string()),
            extracted_at: Some("2026-04-12T10:00:00Z".to_string()),
            entities: vec![
                EntityRef {
                    name: "Pedro".to_string(),
                    entity_type: EntityType::Person,
                    role: Some("chooser".to_string()),
                },
                EntityRef {
                    name: "PostgreSQL".to_string(),
                    entity_type: EntityType::Tool,
                    role: Some("chosen".to_string()),
                },
            ],
            supersedes: Some("0xabc".to_string()),
            superseded_by: None,
            valid_from: Some("2026-04-01T00:00:00Z".to_string()),
            status: ClaimStatus::Superseded,
        }
    }

    // === Serde round-trip ===

    #[test]
    fn test_full_claim_round_trip() {
        let c = full_claim();
        let json = serde_json::to_string(&c).unwrap();
        let back: Claim = serde_json::from_str(&json).unwrap();
        assert_eq!(c, back);
    }

    #[test]
    fn test_minimal_claim_round_trip() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        let back: Claim = serde_json::from_str(&json).unwrap();
        assert_eq!(c, back);
    }

    #[test]
    fn test_minimal_claim_omits_defaults() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        // status=Active -> omitted
        assert!(
            !json.contains("\"st\""),
            "status should be omitted when Active: {}",
            json
        );
        // corroboration_count=1 -> omitted
        assert!(
            !json.contains("\"cc\""),
            "corroboration_count should be omitted when 1: {}",
            json
        );
        // None options omitted
        assert!(!json.contains("\"sup\""));
        assert!(!json.contains("\"sby\""));
        assert!(!json.contains("\"vf\""));
        assert!(!json.contains("\"ea\""));
        assert!(!json.contains("\"sc\""));
    }

    #[test]
    fn test_minimal_claim_short_keys_present() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        assert!(json.contains("\"t\":"));
        assert!(json.contains("\"c\":\"pref\""));
        assert!(json.contains("\"cf\":"));
        assert!(json.contains("\"i\":"));
        assert!(json.contains("\"sa\":"));
        assert!(json.contains("\"e\":"));
        // Entity short keys
        assert!(json.contains("\"n\":\"PostgreSQL\""));
        assert!(json.contains("\"tp\":\"tool\""));
        // role None -> omitted
        assert!(!json.contains("\"r\":"));
    }

    #[test]
    fn test_category_short_strings() {
        let pairs = [
            (ClaimCategory::Fact, "fact"),
            (ClaimCategory::Preference, "pref"),
            (ClaimCategory::Decision, "dec"),
            (ClaimCategory::Episodic, "epi"),
            (ClaimCategory::Goal, "goal"),
            (ClaimCategory::Context, "ctx"),
            (ClaimCategory::Summary, "sum"),
            (ClaimCategory::Rule, "rule"),
            (ClaimCategory::Entity, "ent"),
            (ClaimCategory::Digest, "dig"),
        ];
        for (cat, expected) in pairs {
            let json = serde_json::to_string(&cat).unwrap();
            assert_eq!(json, format!("\"{}\"", expected));
            let back: ClaimCategory = serde_json::from_str(&json).unwrap();
            assert_eq!(cat, back);
        }
    }

    #[test]
    fn test_status_short_strings() {
        let pairs = [
            (ClaimStatus::Active, "a"),
            (ClaimStatus::Superseded, "s"),
            (ClaimStatus::Retracted, "r"),
            (ClaimStatus::Contradicted, "c"),
            (ClaimStatus::Pinned, "p"),
        ];
        for (st, expected) in pairs {
            let json = serde_json::to_string(&st).unwrap();
            assert_eq!(json, format!("\"{}\"", expected));
            let back: ClaimStatus = serde_json::from_str(&json).unwrap();
            assert_eq!(st, back);
        }
    }

    #[test]
    fn test_entity_type_short_strings() {
        let pairs = [
            (EntityType::Person, "person"),
            (EntityType::Project, "project"),
            (EntityType::Tool, "tool"),
            (EntityType::Company, "company"),
            (EntityType::Concept, "concept"),
            (EntityType::Place, "place"),
        ];
        for (et, expected) in pairs {
            let json = serde_json::to_string(&et).unwrap();
            assert_eq!(json, format!("\"{}\"", expected));
        }
    }

    #[test]
    fn test_reference_claim_exact_bytes() {
        // Byte-level canonical: lock down the exact JSON output of a fixed claim.
        // This test MUST match what Python + TS produce for cross-language parity.
        let c = Claim {
            text: "prefers PostgreSQL".to_string(),
            category: ClaimCategory::Preference,
            confidence: 0.9,
            importance: 8,
            corroboration_count: 1,
            source_agent: "oc".to_string(),
            source_conversation: None,
            extracted_at: None,
            entities: vec![EntityRef {
                name: "PostgreSQL".to_string(),
                entity_type: EntityType::Tool,
                role: None,
            }],
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        };
        let json = serde_json::to_string(&c).unwrap();
        let expected = r#"{"t":"prefers PostgreSQL","c":"pref","cf":0.9,"i":8,"sa":"oc","e":[{"n":"PostgreSQL","tp":"tool"}]}"#;
        assert_eq!(json, expected);
    }

    #[test]
    fn test_typical_claim_byte_size() {
        // Spec §14d / §15.7 targets ~90 bytes metadata overhead (text excluded).
        // Build a claim with exactly 120-byte text and verify metadata overhead stays near target.
        let text = "a".repeat(120);
        let c = Claim {
            text: text.clone(),
            category: ClaimCategory::Preference,
            confidence: 0.9,
            importance: 8,
            corroboration_count: 1,
            source_agent: "oc".to_string(),
            source_conversation: None,
            extracted_at: None,
            entities: vec![EntityRef {
                name: "PostgreSQL".to_string(),
                entity_type: EntityType::Tool,
                role: None,
            }],
            supersedes: None,
            superseded_by: None,
            valid_from: None,
            status: ClaimStatus::Active,
        };
        let json = serde_json::to_string(&c).unwrap();
        let metadata_overhead = json.len() - text.len();
        assert!(
            metadata_overhead <= 95,
            "metadata overhead should be <=95 bytes, got {}: {}",
            metadata_overhead,
            json
        );
        // Total blob for this claim should comfortably stay under 220 bytes.
        assert!(
            json.len() <= 220,
            "total claim JSON should be <=220 bytes, got {}: {}",
            json.len(),
            json
        );
    }

    #[test]
    fn test_deserialize_with_missing_defaults() {
        // A minimal compact blob with no status/cc/options should parse cleanly.
        let json = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        let c: Claim = serde_json::from_str(json).unwrap();
        assert_eq!(c.status, ClaimStatus::Active);
        assert_eq!(c.corroboration_count, 1);
        assert!(c.entities.is_empty());
        assert!(c.extracted_at.is_none());
    }

    // === Entity normalization ===

    #[test]
    fn test_normalize_simple_lowercase() {
        assert_eq!(normalize_entity_name("PostgreSQL"), "postgresql");
    }

    #[test]
    fn test_normalize_collapse_and_trim() {
        assert_eq!(normalize_entity_name("  Node  JS  "), "node js");
    }

    #[test]
    fn test_normalize_preserves_punctuation() {
        assert_eq!(normalize_entity_name("Node.js"), "node.js");
    }

    #[test]
    fn test_normalize_empty() {
        assert_eq!(normalize_entity_name(""), "");
    }

    #[test]
    fn test_normalize_whitespace_only() {
        assert_eq!(normalize_entity_name("   \t  "), "");
    }

    #[test]
    fn test_normalize_nfc_idempotent_on_precomposed() {
        // Precomposed é (U+00E9)
        assert_eq!(normalize_entity_name("José"), "josé");
    }

    #[test]
    fn test_normalize_nfc_merges_combining() {
        // NFD: 'e' + combining acute U+0301 -> precomposed é after NFC
        let nfd = "Jose\u{0301}";
        let nfc = "josé";
        assert_eq!(normalize_entity_name(nfd), nfc);
    }

    #[test]
    fn test_normalize_unicode_combining_same_id() {
        // "PostgréSQL" with precomposed vs combining should yield same normalized and same ID
        let a = "Postgre\u{0301}SQL"; // NFD
        let b = "PostgréSQL"; // NFC precomposed
        assert_eq!(normalize_entity_name(a), normalize_entity_name(b));
        assert_eq!(deterministic_entity_id(a), deterministic_entity_id(b));
    }

    #[test]
    fn test_normalize_internal_multispace() {
        assert_eq!(normalize_entity_name("Foo\t\n Bar"), "foo bar");
    }

    // === Deterministic entity ID ===

    #[test]
    fn test_entity_id_case_insensitive() {
        let a = deterministic_entity_id("Pedro");
        let b = deterministic_entity_id("pedro");
        let c = deterministic_entity_id("  PEDRO  ");
        assert_eq!(a, b);
        assert_eq!(b, c);
    }

    #[test]
    fn test_entity_id_different_names_differ() {
        let a = deterministic_entity_id("Pedro");
        let b = deterministic_entity_id("Sarah");
        assert_ne!(a, b);
    }

    #[test]
    fn test_entity_id_format() {
        let id = deterministic_entity_id("anything");
        assert_eq!(id.len(), 16);
        assert!(id.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn test_entity_id_known_answer_pedro() {
        // Known answer for cross-language parity. SHA256("pedro")[..8] as hex.
        // Locked in as the canonical value — parity tests in TS/Python must match.
        let id = deterministic_entity_id("pedro");
        assert_eq!(id, "ee5cd7d5d96c8874");
    }

    #[test]
    fn test_entity_id_known_answer_postgresql() {
        let id = deterministic_entity_id("PostgreSQL");
        // normalized -> "postgresql"
        let again = deterministic_entity_id("postgresql");
        assert_eq!(id, again);
    }

    // === Legacy parser ===

    #[test]
    fn test_parse_full_claim_json() {
        let c = full_claim();
        let json = serde_json::to_string(&c).unwrap();
        let parsed = parse_claim_or_legacy(&json);
        assert_eq!(parsed, c);
    }

    #[test]
    fn test_parse_legacy_object_format() {
        let json = r#"{"t":"hello","a":"oc","s":"extract"}"#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, "hello");
        assert_eq!(parsed.source_agent, "oc");
        assert_eq!(parsed.category, ClaimCategory::Fact);
        assert_eq!(parsed.confidence, 0.7);
        assert_eq!(parsed.importance, 5);
        assert_eq!(parsed.corroboration_count, 1);
        assert_eq!(parsed.status, ClaimStatus::Active);
        assert!(parsed.entities.is_empty());
        assert!(parsed.extracted_at.is_none());
    }

    #[test]
    fn test_parse_legacy_string_format() {
        let json = r#""just text""#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, "just text");
        assert_eq!(parsed.source_agent, "unknown");
        assert_eq!(parsed.category, ClaimCategory::Fact);
    }

    #[test]
    fn test_parse_legacy_raw_text() {
        // Not JSON at all
        let parsed = parse_claim_or_legacy("hello world");
        assert_eq!(parsed.text, "hello world");
        assert_eq!(parsed.source_agent, "unknown");
    }

    #[test]
    fn test_parse_legacy_malformed_json() {
        // Looks like JSON, isn't
        let parsed = parse_claim_or_legacy("{not valid json");
        assert_eq!(parsed.text, "{not valid json");
        assert_eq!(parsed.source_agent, "unknown");
    }

    #[test]
    fn test_parse_legacy_missing_text() {
        // Legacy object with no text-like field falls back to the raw blob as text
        let json = r#"{"a":"oc"}"#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, json);
        assert_eq!(parsed.source_agent, "oc");
    }

    #[test]
    fn test_parse_plugin_legacy_doc_format() {
        // The OpenClaw plugin previously wrote blobs as {text, metadata}.
        // Upgrading users must still read their old facts correctly.
        let json = r#"{"text":"prefers PostgreSQL","metadata":{"type":"preference","importance":0.9,"source":"auto-extraction","created_at":"2026-03-01T00:00:00Z"}}"#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, "prefers PostgreSQL");
        assert_eq!(parsed.source_agent, "auto-extraction");
        assert_eq!(parsed.category, ClaimCategory::Fact);
        assert_eq!(parsed.status, ClaimStatus::Active);
    }

    #[test]
    fn test_parse_plugin_legacy_doc_without_metadata_source() {
        let json = r#"{"text":"lives in Lisbon"}"#;
        let parsed = parse_claim_or_legacy(json);
        assert_eq!(parsed.text, "lives in Lisbon");
        assert_eq!(parsed.source_agent, "unknown");
    }

    #[test]
    fn test_legacy_round_trip_via_claim() {
        // Parse a legacy blob, re-serialize as a full claim, re-parse, must be equal.
        let parsed1 = parse_claim_or_legacy(r#"{"t":"hello","a":"oc","s":"extract"}"#);
        let json = serde_json::to_string(&parsed1).unwrap();
        let parsed2 = parse_claim_or_legacy(&json);
        assert_eq!(parsed1, parsed2);
    }

    #[test]
    fn test_parse_never_panics_on_random_input() {
        for s in ["", "   ", "null", "[1,2,3]", "42", "true", "\"\""] {
            let _ = parse_claim_or_legacy(s);
        }
    }

    #[test]
    fn test_claim_category_default_status_omitted_in_serialization() {
        // Sanity: build a claim with status Active and verify status absent
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        assert!(!json.contains("\"st\":"));
    }

    #[test]
    fn test_non_default_status_serialized() {
        let mut c = minimal_claim();
        c.status = ClaimStatus::Superseded;
        let json = serde_json::to_string(&c).unwrap();
        assert!(json.contains("\"st\":\"s\""));
    }

    #[test]
    fn test_non_default_corroboration_serialized() {
        let mut c = minimal_claim();
        c.corroboration_count = 5;
        let json = serde_json::to_string(&c).unwrap();
        assert!(json.contains("\"cc\":5"));
    }

    // === Pin status semantics ===

    #[test]
    fn test_is_pinned_claim_true_for_pinned() {
        let mut c = minimal_claim();
        c.status = ClaimStatus::Pinned;
        assert!(is_pinned_claim(&c));
    }

    #[test]
    fn test_is_pinned_claim_false_for_active() {
        let c = minimal_claim();
        assert!(!is_pinned_claim(&c));
    }

    #[test]
    fn test_is_pinned_claim_false_for_superseded() {
        let mut c = minimal_claim();
        c.status = ClaimStatus::Superseded;
        assert!(!is_pinned_claim(&c));
    }

    #[test]
    fn test_is_pinned_json_valid_pinned() {
        let mut c = minimal_claim();
        c.status = ClaimStatus::Pinned;
        let json = serde_json::to_string(&c).unwrap();
        assert!(is_pinned_json(&json));
    }

    #[test]
    fn test_is_pinned_json_valid_active() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        assert!(!is_pinned_json(&json));
    }

    #[test]
    fn test_is_pinned_json_invalid_json() {
        assert!(!is_pinned_json("not json at all"));
    }

    #[test]
    fn test_is_pinned_json_missing_status_field() {
        // Minimal JSON without status -> defaults to Active
        let json = r#"{"t":"hi","c":"fact","cf":0.9,"i":5,"sa":"oc"}"#;
        assert!(!is_pinned_json(json));
    }

    #[test]
    fn test_is_pinned_json_empty_string() {
        assert!(!is_pinned_json(""));
    }

    // === ResolutionAction / respect_pin_in_resolution ===

    #[test]
    fn test_respect_pin_pinned_existing_returns_skip() {
        let mut c = minimal_claim();
        c.status = ClaimStatus::Pinned;
        let json = serde_json::to_string(&c).unwrap();
        let action = respect_pin_in_resolution(
            &json,
            "new_id",
            "existing_id",
            "new_id",
            0.5,
            0.7,
            TIE_ZONE_SCORE_TOLERANCE,
        );
        assert_eq!(
            action,
            ResolutionAction::SkipNew {
                reason: SkipReason::ExistingPinned,
                existing_id: "existing_id".to_string(),
                new_id: "new_id".to_string(),
                entity_id: None,
                similarity: None,
                winner_score: None,
                loser_score: None,
                winner_components: None,
                loser_components: None,
            }
        );
    }

    #[test]
    fn test_respect_pin_existing_wins_returns_skip() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        let action = respect_pin_in_resolution(
            &json,
            "new_id",
            "existing_id",
            "existing_id",
            0.5,
            0.7,
            TIE_ZONE_SCORE_TOLERANCE,
        );
        assert_eq!(
            action,
            ResolutionAction::SkipNew {
                reason: SkipReason::ExistingWins,
                existing_id: "existing_id".to_string(),
                new_id: "new_id".to_string(),
                entity_id: None,
                similarity: None,
                winner_score: None,
                loser_score: None,
                winner_components: None,
                loser_components: None,
            }
        );
    }

    #[test]
    fn test_respect_pin_tie_zone_returns_tie() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        let action = respect_pin_in_resolution(
            &json,
            "new_id",
            "existing_id",
            "new_id",
            0.005,
            0.7,
            TIE_ZONE_SCORE_TOLERANCE,
        );
        match &action {
            ResolutionAction::TieLeaveBoth { score_gap, .. } => {
                assert!(score_gap.abs() < TIE_ZONE_SCORE_TOLERANCE);
            }
            _ => panic!("expected TieLeaveBoth, got {:?}", action),
        }
    }

    #[test]
    fn test_respect_pin_clear_win_returns_supersede() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        let action = respect_pin_in_resolution(
            &json,
            "new_id",
            "existing_id",
            "new_id",
            0.15,
            0.7,
            TIE_ZONE_SCORE_TOLERANCE,
        );
        match &action {
            ResolutionAction::SupersedeExisting { score_gap, .. } => {
                assert!(*score_gap > TIE_ZONE_SCORE_TOLERANCE);
            }
            _ => panic!("expected SupersedeExisting, got {:?}", action),
        }
    }

    #[test]
    fn test_resolution_action_serde_round_trip() {
        let action = ResolutionAction::SupersedeExisting {
            existing_id: "ex".to_string(),
            new_id: "nw".to_string(),
            similarity: 0.7,
            score_gap: 0.15,
            entity_id: None,
            winner_score: None,
            loser_score: None,
            winner_components: None,
            loser_components: None,
        };
        let json = serde_json::to_string(&action).unwrap();
        let back: ResolutionAction = serde_json::from_str(&json).unwrap();
        assert_eq!(action, back);
    }

    #[test]
    fn test_skip_reason_serde() {
        let pairs = [
            (SkipReason::ExistingPinned, "\"existing_pinned\""),
            (SkipReason::ExistingWins, "\"existing_wins\""),
            (SkipReason::BelowThreshold, "\"below_threshold\""),
        ];
        for (reason, expected) in pairs {
            let json = serde_json::to_string(&reason).unwrap();
            assert_eq!(json, expected);
        }
    }

    // === Memory Taxonomy v1 — enum serde & from_str_lossy ===

    #[test]
    fn test_memory_type_v1_serde_round_trip() {
        let pairs = [
            (MemoryTypeV1::Claim, "\"claim\""),
            (MemoryTypeV1::Preference, "\"preference\""),
            (MemoryTypeV1::Directive, "\"directive\""),
            (MemoryTypeV1::Commitment, "\"commitment\""),
            (MemoryTypeV1::Episode, "\"episode\""),
            (MemoryTypeV1::Summary, "\"summary\""),
        ];
        for (variant, expected) in pairs {
            let json = serde_json::to_string(&variant).unwrap();
            assert_eq!(json, expected);
            let back: MemoryTypeV1 = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, back);
        }
    }

    #[test]
    fn test_memory_source_serde_round_trip() {
        let pairs = [
            (MemorySource::User, "\"user\""),
            (MemorySource::UserInferred, "\"user-inferred\""),
            (MemorySource::Assistant, "\"assistant\""),
            (MemorySource::External, "\"external\""),
            (MemorySource::Derived, "\"derived\""),
        ];
        for (variant, expected) in pairs {
            let json = serde_json::to_string(&variant).unwrap();
            assert_eq!(json, expected);
            let back: MemorySource = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, back);
        }
    }

    #[test]
    fn test_memory_scope_serde_round_trip() {
        let pairs = [
            (MemoryScope::Work, "\"work\""),
            (MemoryScope::Personal, "\"personal\""),
            (MemoryScope::Health, "\"health\""),
            (MemoryScope::Family, "\"family\""),
            (MemoryScope::Creative, "\"creative\""),
            (MemoryScope::Finance, "\"finance\""),
            (MemoryScope::Misc, "\"misc\""),
            (MemoryScope::Unspecified, "\"unspecified\""),
        ];
        for (variant, expected) in pairs {
            let json = serde_json::to_string(&variant).unwrap();
            assert_eq!(json, expected);
            let back: MemoryScope = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, back);
        }
    }

    #[test]
    fn test_memory_volatility_serde_round_trip() {
        let pairs = [
            (MemoryVolatility::Stable, "\"stable\""),
            (MemoryVolatility::Updatable, "\"updatable\""),
            (MemoryVolatility::Ephemeral, "\"ephemeral\""),
        ];
        for (variant, expected) in pairs {
            let json = serde_json::to_string(&variant).unwrap();
            assert_eq!(json, expected);
            let back: MemoryVolatility = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, back);
        }
    }

    #[test]
    fn test_memory_type_v1_from_str_lossy_known() {
        assert_eq!(MemoryTypeV1::from_str_lossy("claim"), MemoryTypeV1::Claim);
        assert_eq!(
            MemoryTypeV1::from_str_lossy("preference"),
            MemoryTypeV1::Preference
        );
        assert_eq!(
            MemoryTypeV1::from_str_lossy("directive"),
            MemoryTypeV1::Directive
        );
        assert_eq!(
            MemoryTypeV1::from_str_lossy("commitment"),
            MemoryTypeV1::Commitment
        );
        assert_eq!(
            MemoryTypeV1::from_str_lossy("episode"),
            MemoryTypeV1::Episode
        );
        assert_eq!(
            MemoryTypeV1::from_str_lossy("summary"),
            MemoryTypeV1::Summary
        );
    }

    #[test]
    fn test_memory_type_v1_from_str_lossy_mixed_case() {
        assert_eq!(MemoryTypeV1::from_str_lossy("CLAIM"), MemoryTypeV1::Claim);
        assert_eq!(
            MemoryTypeV1::from_str_lossy("Preference"),
            MemoryTypeV1::Preference
        );
        assert_eq!(
            MemoryTypeV1::from_str_lossy("  directive  "),
            MemoryTypeV1::Directive
        );
    }

    #[test]
    fn test_memory_type_v1_from_str_lossy_unknown_defaults_to_claim() {
        assert_eq!(
            MemoryTypeV1::from_str_lossy("nonsense"),
            MemoryTypeV1::Claim
        );
        assert_eq!(MemoryTypeV1::from_str_lossy(""), MemoryTypeV1::Claim);
        // Legacy v0 tokens must also fall through to Claim — they're handled
        // by the dedicated normalize_legacy_to_v1 adapter (not implemented
        // here), not by from_str_lossy.
        assert_eq!(MemoryTypeV1::from_str_lossy("fact"), MemoryTypeV1::Claim);
        assert_eq!(MemoryTypeV1::from_str_lossy("rule"), MemoryTypeV1::Claim);
    }

    #[test]
    fn test_memory_source_from_str_lossy_known() {
        assert_eq!(MemorySource::from_str_lossy("user"), MemorySource::User);
        assert_eq!(
            MemorySource::from_str_lossy("user-inferred"),
            MemorySource::UserInferred
        );
        assert_eq!(
            MemorySource::from_str_lossy("assistant"),
            MemorySource::Assistant
        );
        assert_eq!(
            MemorySource::from_str_lossy("external"),
            MemorySource::External
        );
        assert_eq!(
            MemorySource::from_str_lossy("derived"),
            MemorySource::Derived
        );
    }

    #[test]
    fn test_memory_source_from_str_lossy_underscore_variant() {
        // Some clients may serialize as user_inferred instead of user-inferred.
        assert_eq!(
            MemorySource::from_str_lossy("user_inferred"),
            MemorySource::UserInferred
        );
        assert_eq!(
            MemorySource::from_str_lossy("USER_INFERRED"),
            MemorySource::UserInferred
        );
    }

    #[test]
    fn test_memory_source_from_str_lossy_unknown_defaults_to_user_inferred() {
        // Policy: unknown sources fall back to user-inferred (moderate weight).
        assert_eq!(
            MemorySource::from_str_lossy("bot"),
            MemorySource::UserInferred
        );
        assert_eq!(MemorySource::from_str_lossy(""), MemorySource::UserInferred);
    }

    #[test]
    fn test_memory_scope_from_str_lossy_known_and_unknown() {
        assert_eq!(MemoryScope::from_str_lossy("work"), MemoryScope::Work);
        assert_eq!(
            MemoryScope::from_str_lossy("UNSPECIFIED"),
            MemoryScope::Unspecified
        );
        // Unknown scope values (the enum is open-extensible) coerce to Unspecified.
        assert_eq!(
            MemoryScope::from_str_lossy("gaming"),
            MemoryScope::Unspecified
        );
        assert_eq!(MemoryScope::from_str_lossy(""), MemoryScope::Unspecified);
    }

    #[test]
    fn test_memory_volatility_from_str_lossy_known_and_unknown() {
        assert_eq!(
            MemoryVolatility::from_str_lossy("stable"),
            MemoryVolatility::Stable
        );
        assert_eq!(
            MemoryVolatility::from_str_lossy("EPHEMERAL"),
            MemoryVolatility::Ephemeral
        );
        // Unknown -> default Updatable.
        assert_eq!(
            MemoryVolatility::from_str_lossy("permanent"),
            MemoryVolatility::Updatable
        );
        assert_eq!(
            MemoryVolatility::from_str_lossy(""),
            MemoryVolatility::Updatable
        );
    }

    // === MemoryClaimV1 struct round-trip & defaults ===

    fn minimal_v1_claim() -> MemoryClaimV1 {
        MemoryClaimV1 {
            id: "01900000-0000-7000-8000-000000000000".to_string(),
            text: "prefers PostgreSQL".to_string(),
            memory_type: MemoryTypeV1::Preference,
            source: MemorySource::User,
            created_at: "2026-04-17T10:00:00Z".to_string(),
            schema_version: MEMORY_CLAIM_V1_SCHEMA_VERSION.to_string(),
            scope: MemoryScope::Unspecified,
            volatility: MemoryVolatility::Updatable,
            entities: Vec::new(),
            reasoning: None,
            expires_at: None,
            importance: None,
            confidence: None,
            superseded_by: None,
            pin_status: None,
        }
    }

    fn full_v1_claim() -> MemoryClaimV1 {
        MemoryClaimV1 {
            id: "01900000-0000-7000-8000-000000000001".to_string(),
            text: "Chose PostgreSQL for the analytics store".to_string(),
            memory_type: MemoryTypeV1::Claim,
            source: MemorySource::UserInferred,
            created_at: "2026-04-17T10:00:00Z".to_string(),
            schema_version: MEMORY_CLAIM_V1_SCHEMA_VERSION.to_string(),
            scope: MemoryScope::Work,
            volatility: MemoryVolatility::Stable,
            entities: vec![MemoryEntityV1 {
                name: "PostgreSQL".to_string(),
                entity_type: EntityType::Tool,
                role: Some("chosen".to_string()),
            }],
            reasoning: Some("data is relational and needs ACID".to_string()),
            expires_at: None,
            importance: Some(8),
            confidence: Some(0.92),
            superseded_by: None,
            pin_status: None,
        }
    }

    #[test]
    fn test_memory_claim_v1_minimal_round_trip() {
        let c = minimal_v1_claim();
        let json = serde_json::to_string(&c).unwrap();
        let back: MemoryClaimV1 = serde_json::from_str(&json).unwrap();
        assert_eq!(c, back);
    }

    #[test]
    fn test_memory_claim_v1_full_round_trip() {
        let c = full_v1_claim();
        let json = serde_json::to_string(&c).unwrap();
        let back: MemoryClaimV1 = serde_json::from_str(&json).unwrap();
        assert_eq!(c, back);
    }

    #[test]
    fn test_memory_claim_v1_minimal_omits_defaults() {
        let c = minimal_v1_claim();
        let json = serde_json::to_string(&c).unwrap();
        // Schema version at default ("1.0") MUST still be serialized explicitly? Spec says required.
        // Our serializer omits when equal to default to keep blobs tiny, but deserialization
        // must ALWAYS provide it via default. Confirm absence here, presence of default on parse below.
        assert!(!json.contains("schema_version"));
        // scope=Unspecified -> omitted
        assert!(!json.contains("scope"));
        // volatility=Updatable -> omitted
        assert!(!json.contains("volatility"));
        // None options omitted
        assert!(!json.contains("reasoning"));
        assert!(!json.contains("expires_at"));
        assert!(!json.contains("importance"));
        assert!(!json.contains("confidence"));
        assert!(!json.contains("superseded_by"));
        // Entity list empty -> omitted
        assert!(!json.contains("entities"));
        // pin_status = None -> omitted (v1.1 additive, absence == "unpinned")
        assert!(!json.contains("pin_status"));
    }

    #[test]
    fn test_memory_claim_v1_deserialize_fills_defaults() {
        // Minimal JSON with only required fields (+no schema_version) must
        // parse and surface the spec-defined defaults.
        let json = r#"{
            "id":"01900000-0000-7000-8000-000000000000",
            "text":"prefers PostgreSQL",
            "type":"preference",
            "source":"user",
            "created_at":"2026-04-17T10:00:00Z"
        }"#;
        let c: MemoryClaimV1 = serde_json::from_str(json).unwrap();
        assert_eq!(c.schema_version, MEMORY_CLAIM_V1_SCHEMA_VERSION);
        assert_eq!(c.scope, MemoryScope::Unspecified);
        assert_eq!(c.volatility, MemoryVolatility::Updatable);
        assert!(c.entities.is_empty());
        assert!(c.reasoning.is_none());
        assert!(c.expires_at.is_none());
        assert!(c.importance.is_none());
        assert!(c.confidence.is_none());
        assert!(c.superseded_by.is_none());
        assert!(c.pin_status.is_none());
    }

    #[test]
    fn test_memory_claim_v1_full_keeps_non_default_fields() {
        let c = full_v1_claim();
        let json = serde_json::to_string(&c).unwrap();
        assert!(json.contains("\"scope\":\"work\""));
        assert!(json.contains("\"volatility\":\"stable\""));
        assert!(json.contains("\"reasoning\":"));
        assert!(json.contains("\"importance\":8"));
        assert!(json.contains("\"confidence\":0.92"));
        assert!(json.contains("\"entities\":"));
        assert!(json.contains("\"type\":\"claim\""));
        assert!(json.contains("\"source\":\"user-inferred\""));
    }

    #[test]
    fn test_memory_claim_v1_reference_exact_bytes() {
        // Byte-level canonical — locks TS/Python parity.
        let c = MemoryClaimV1 {
            id: "01900000-0000-7000-8000-000000000000".to_string(),
            text: "prefers PostgreSQL".to_string(),
            memory_type: MemoryTypeV1::Preference,
            source: MemorySource::User,
            created_at: "2026-04-17T10:00:00Z".to_string(),
            schema_version: MEMORY_CLAIM_V1_SCHEMA_VERSION.to_string(),
            scope: MemoryScope::Unspecified,
            volatility: MemoryVolatility::Updatable,
            entities: Vec::new(),
            reasoning: None,
            expires_at: None,
            importance: None,
            confidence: None,
            superseded_by: None,
            pin_status: None,
        };
        let json = serde_json::to_string(&c).unwrap();
        let expected = r#"{"id":"01900000-0000-7000-8000-000000000000","text":"prefers PostgreSQL","type":"preference","source":"user","created_at":"2026-04-17T10:00:00Z"}"#;
        assert_eq!(json, expected);
    }

    #[test]
    fn test_memory_claim_v1_rejects_wrong_type_token() {
        // Legacy v0 token "fact" is invalid for v1's closed type enum.
        let json = r#"{
            "id":"01900000-0000-7000-8000-000000000000",
            "text":"hi",
            "type":"fact",
            "source":"user",
            "created_at":"2026-04-17T10:00:00Z"
        }"#;
        let result: std::result::Result<MemoryClaimV1, _> = serde_json::from_str(json);
        assert!(result.is_err(), "v1 must reject legacy token 'fact'");
    }

    #[test]
    fn test_memory_claim_v1_schema_version_preserved_if_non_default() {
        // If a client serializes schema_version explicitly (e.g. "1.0" with
        // future "1.1" coming), we must preserve it verbatim on round-trip.
        let json = r#"{
            "id":"01900000-0000-7000-8000-000000000000",
            "text":"hi",
            "type":"claim",
            "source":"user",
            "created_at":"2026-04-17T10:00:00Z",
            "schema_version":"1.0"
        }"#;
        let c: MemoryClaimV1 = serde_json::from_str(json).unwrap();
        assert_eq!(c.schema_version, "1.0");
    }

    // === Pin status (v1.1, additive) ===

    #[test]
    fn test_pin_status_serde_round_trip() {
        let pairs = [
            (PinStatus::Pinned, "\"pinned\""),
            (PinStatus::Unpinned, "\"unpinned\""),
        ];
        for (variant, expected) in pairs {
            let json = serde_json::to_string(&variant).unwrap();
            assert_eq!(json, expected);
            let back: PinStatus = serde_json::from_str(&json).unwrap();
            assert_eq!(variant, back);
        }
    }

    #[test]
    fn test_pin_status_from_str_lossy() {
        assert_eq!(PinStatus::from_str_lossy("pinned"), PinStatus::Pinned);
        assert_eq!(PinStatus::from_str_lossy("PINNED"), PinStatus::Pinned);
        assert_eq!(PinStatus::from_str_lossy("unpinned"), PinStatus::Unpinned);
        assert_eq!(PinStatus::from_str_lossy(""), PinStatus::Unpinned);
        assert_eq!(PinStatus::from_str_lossy("bogus"), PinStatus::Unpinned);
    }

    #[test]
    fn test_memory_claim_v1_pin_status_absent_by_default() {
        // Minimal v1 blob MUST omit pin_status when None.
        let c = minimal_v1_claim();
        assert!(c.pin_status.is_none());
        let json = serde_json::to_string(&c).unwrap();
        assert!(
            !json.contains("pin_status"),
            "pin_status should be omitted when None: {}",
            json
        );
    }

    #[test]
    fn test_memory_claim_v1_pinned_round_trip() {
        // A pinned v1 blob round-trips cleanly and keeps the field.
        let mut c = minimal_v1_claim();
        c.pin_status = Some(PinStatus::Pinned);
        let json = serde_json::to_string(&c).unwrap();
        assert!(json.contains("\"pin_status\":\"pinned\""));
        let back: MemoryClaimV1 = serde_json::from_str(&json).unwrap();
        assert_eq!(c, back);
    }

    #[test]
    fn test_memory_claim_v1_unpinned_round_trip_explicit() {
        // Explicit "unpinned" is preserved verbatim on round-trip (NOT dropped).
        let mut c = minimal_v1_claim();
        c.pin_status = Some(PinStatus::Unpinned);
        let json = serde_json::to_string(&c).unwrap();
        assert!(json.contains("\"pin_status\":\"unpinned\""));
        let back: MemoryClaimV1 = serde_json::from_str(&json).unwrap();
        assert_eq!(c, back);
        assert_eq!(back.pin_status, Some(PinStatus::Unpinned));
    }

    #[test]
    fn test_memory_claim_v1_deserialize_without_pin_status_field() {
        // A v1 blob that predates v1.1 has no pin_status field. Deserialize
        // must tolerate it and default to None.
        let json = r#"{
            "id":"01900000-0000-7000-8000-000000000000",
            "text":"hi",
            "type":"claim",
            "source":"user",
            "created_at":"2026-04-17T10:00:00Z"
        }"#;
        let c: MemoryClaimV1 = serde_json::from_str(json).unwrap();
        assert!(c.pin_status.is_none());
    }

    #[test]
    fn test_is_pinned_memory_claim_v1_true_when_pinned() {
        let mut c = minimal_v1_claim();
        c.pin_status = Some(PinStatus::Pinned);
        assert!(is_pinned_memory_claim_v1(&c));
    }

    #[test]
    fn test_is_pinned_memory_claim_v1_false_when_unpinned_or_absent() {
        let c = minimal_v1_claim();
        assert!(!is_pinned_memory_claim_v1(&c));
        let mut c2 = minimal_v1_claim();
        c2.pin_status = Some(PinStatus::Unpinned);
        assert!(!is_pinned_memory_claim_v1(&c2));
    }

    // === is_pinned_json — unified v0 + v1.1 ===

    #[test]
    fn test_is_pinned_json_v1_pinned() {
        let mut c = minimal_v1_claim();
        c.pin_status = Some(PinStatus::Pinned);
        let json = serde_json::to_string(&c).unwrap();
        assert!(is_pinned_json(&json), "v1 pinned blob must be detected");
    }

    #[test]
    fn test_is_pinned_json_v1_unpinned() {
        let mut c = minimal_v1_claim();
        c.pin_status = Some(PinStatus::Unpinned);
        let json = serde_json::to_string(&c).unwrap();
        assert!(!is_pinned_json(&json), "v1 unpinned blob must NOT be detected as pinned");
    }

    #[test]
    fn test_is_pinned_json_v1_no_pin_status_field() {
        // Pre-v1.1 v1 blob without pin_status — not pinned.
        let json = r#"{
            "id":"01900000-0000-7000-8000-000000000000",
            "text":"hi",
            "type":"claim",
            "source":"user",
            "created_at":"2026-04-17T10:00:00Z"
        }"#;
        assert!(!is_pinned_json(json));
    }

    #[test]
    fn test_is_pinned_json_v0_pinned_backcompat() {
        // Legacy v0 short-key blob with st=p — MUST still be detected.
        let mut c = minimal_claim();
        c.status = ClaimStatus::Pinned;
        let json = serde_json::to_string(&c).unwrap();
        assert!(is_pinned_json(&json), "v0 st=p must still trigger is_pinned_json");
    }

    #[test]
    fn test_is_pinned_json_v0_active_backcompat() {
        let c = minimal_claim();
        let json = serde_json::to_string(&c).unwrap();
        assert!(!is_pinned_json(&json));
    }

    #[test]
    fn test_is_pinned_json_invalid_input_returns_false() {
        assert!(!is_pinned_json(""));
        assert!(!is_pinned_json("not json"));
        assert!(!is_pinned_json("{}"));
        assert!(!is_pinned_json("[1,2,3]"));
    }

    #[test]
    fn test_is_pinned_json_v1_dispatch_does_not_fall_through_to_v0() {
        // A v1 blob that parses successfully but isn't pinned MUST NOT be
        // re-parsed via the v0 path (different semantics — `st` field is
        // absent on v1 blobs but a stray `st` key on an otherwise-v1 JSON
        // blob could be interpreted as the v0 sentinel if we fell through).
        //
        // Craft a v1 blob with a literal `st: "p"` field. The v1 parser
        // ignores unknown fields — wait, serde's default is to reject unknown
        // fields NO — the default is to IGNORE unknown fields unless
        // deny_unknown_fields is set. MemoryClaimV1 doesn't deny, so it will
        // accept the v1 blob with st=p and, per the dispatch rule, return
        // the v1 answer (not pinned) without falling through.
        let json = r#"{
            "id":"01900000-0000-7000-8000-000000000000",
            "text":"hi",
            "type":"claim",
            "source":"user",
            "created_at":"2026-04-17T10:00:00Z",
            "st":"p"
        }"#;
        assert!(
            !is_pinned_json(json),
            "v1-shaped blob with stray v0 sentinel must NOT be treated as pinned"
        );
    }

    #[test]
    fn test_respect_pin_in_resolution_v1_pinned_blob() {
        // Use respect_pin_in_resolution with a v1 pinned blob — the existing
        // claim should be detected as pinned through the unified is_pinned_json.
        let mut c = minimal_v1_claim();
        c.pin_status = Some(PinStatus::Pinned);
        let json = serde_json::to_string(&c).unwrap();
        let action = respect_pin_in_resolution(
            &json,
            "new_id",
            "existing_id",
            "new_id",
            0.5,
            0.7,
            TIE_ZONE_SCORE_TOLERANCE,
        );
        assert_eq!(
            action,
            ResolutionAction::SkipNew {
                reason: SkipReason::ExistingPinned,
                existing_id: "existing_id".to_string(),
                new_id: "new_id".to_string(),
                entity_id: None,
                similarity: None,
                winner_score: None,
                loser_score: None,
                winner_components: None,
                loser_components: None,
            },
            "v1 pinned blob must trigger SkipNew::ExistingPinned"
        );
    }
}
