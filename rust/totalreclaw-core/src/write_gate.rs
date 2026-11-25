//! Store-time write gate: should a new fact be written, or is it already stored?
//!
//! Every client write path (auto-extraction, explicit `remember`, the Hermes
//! USER.md mirror and reconcile) asks [`decide_write`] before it stores a fact.
//! The caller supplies the candidate set: facts the subgraph returned for the
//! new text (topical recall) plus facts this process wrote in the last few
//! minutes, which the subgraph may not have indexed yet. Choosing and
//! excluding candidates (recently forgotten ids, the fact an edit replaces) is
//! the caller's job; this module only decides.
//!
//! # Decision
//!
//! 1. **Exact.** [`normalize_fact_text`] of the new text equals that of a
//!    candidate → [`WriteGateAction::Skip`] in every mode. A pinned candidate
//!    is preferred as the reported match. Nothing is replaced: a duplicate of
//!    a pinned fact leaves the pin untouched.
//! 2. **Near duplicate.** Cosine similarity between the new embedding and a
//!    candidate embedding is `>= threshold` (default
//!    [`STORE_DEDUP_COSINE_THRESHOLD`] = 0.85). The best match is reported.
//!    What happens depends on the mode:
//!    - [`WriteGateMode::Auto`] (auto-extraction): `Skip`. Pinned candidates
//!      are not considered, because the extraction prompt asks the LLM to
//!      ADD new information about a pinned fact instead of updating it, and
//!      a cosine match cannot tell a restatement from a changed value.
//!    - [`WriteGateMode::Explicit`] (a user's explicit remember):
//!      [`WriteGateAction::Confirm`]. Nothing is written; the caller shows the
//!      existing fact and lets the agent retry with `force` if the new text
//!      adds or changes information.
//!    - [`WriteGateMode::ExactOnly`] (USER.md mirror, reconcile, forced
//!      remember): near duplicates are stored. These paths have no LLM to
//!      judge the match.
//! 2b. **Contained** ([`WriteGateMode::Explicit`] only). No near duplicate
//!    exists, but the new text is a shorter statement of one stored fact:
//!    every name (anchor) in it appears in that fact, every content word does
//!    too (or it has at least one name), no relation word ("sister", "boss")
//!    appears on only one side, and cosine is `>= `[`CONTAINED_MIN_COSINE`]
//!    (0.65). The best cosine wins, the earlier candidate on a tie. The result
//!    is [`WriteGateAction::Confirm`] with kind `contained`. A short rewording
//!    of a long fact scores 0.63 to 0.77, below the 0.85 floor (internal #777),
//!    and a lower floor would also catch value changes, which score in the same
//!    band; the lexical test is what tells them apart. This rule is
//!    explicit-only: in auto mode a match is a silent skip that cannot be
//!    undone, auto-extraction already has its LLM dedup context, and a changed
//!    value in the same slot must still store.
//! 3. Otherwise [`WriteGateAction::Store`].
//!
//! Why a cosine match is not a silent skip everywhere: with the Harrier
//! embeddings, value changes score as high as paraphrases ("lives in Lisbon"
//! vs "lives in Porto" 0.898; "30 minutes" vs "60 minutes" 0.959; "is
//! vegetarian" vs "is vegetarian." 0.938). A silent skip would drop
//! corrections.
//!
//! Cross-language fixture: `tests/parity/fixtures/write-gate-v1.json`
//! (generator `tests/parity/fixtures/generate-write-gate-v1.py`), checked here,
//! by `tests/parity/write-gate-parity.test.ts` (WASM) and by
//! `python/tests/test_write_gate_parity.py` (PyO3 and the pure-Python
//! fallback in `totalreclaw.write_gate`).

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use unicode_normalization::UnicodeNormalization;

use crate::forget_guard::{adds_relation_word, guard_tokens, guard_tokens_with_anchors};

pub use crate::consolidation::STORE_DEDUP_COSINE_THRESHOLD;

/// Minimum cosine for the explicit-mode `contained` rule.
pub const CONTAINED_MIN_COSINE: f64 = 0.65;

/// Characters stripped from both ends of a fact by [`normalize_fact_text`]:
/// sentence punctuation, quotes, brackets, list bullets and Markdown emphasis.
/// `+`, `#`, `%`, `$` and digits are kept ("C++", "C#", "100%").
pub const EDGE_PUNCTUATION: &[char] = &[
    '.', ',', ';', ':', '!', '?', '\'', '"', '`', '(', ')', '[', ']', '{', '}', '<', '>', '-',
    '*', '_', '~', '\u{2022}', // bullet
    '\u{00A1}', '\u{00BF}', // inverted ! ?
    '\u{00AB}', '\u{00BB}', '\u{2039}', '\u{203A}', // guillemets
    '\u{3002}', '\u{FF0C}', '\u{FF1B}', '\u{FF1A}', '\u{FF01}', '\u{FF1F}', '\u{3001}', // CJK
];

/// Map typographic variants to their ASCII form, so "User’s" equals "User's".
fn fold_char(c: char) -> Option<&'static str> {
    match c {
        '\u{2018}' | '\u{2019}' | '\u{201B}' | '\u{2032}' | '\u{00B4}' => Some("'"),
        '\u{201C}' | '\u{201D}' | '\u{201E}' | '\u{2033}' => Some("\""),
        '\u{2010}' | '\u{2011}' | '\u{2012}' | '\u{2013}' | '\u{2014}' | '\u{2015}' => Some("-"),
        '\u{2026}' => Some("..."),
        _ => None,
    }
}

/// Normalize a fact for the exact-duplicate check.
///
/// Steps, in order:
/// 1. Unicode NFC.
/// 2. Lowercase.
/// 3. Fold typographic quotes, dashes and the ellipsis to ASCII.
/// 4. Collapse runs of Unicode whitespace (`char::is_whitespace`) to one space.
/// 5. Strip [`EDGE_PUNCTUATION`] and whitespace from both ends, repeatedly.
///
/// "User is vegetarian", "user is vegetarian." and "- User is VEGETARIAN!"
/// all normalize to `user is vegetarian`. Inner punctuation is kept, so
/// "knows C" and "knows C++" stay different.
pub fn normalize_fact_text(text: &str) -> String {
    let lowered: String = text.nfc().collect::<String>().to_lowercase();
    let mut folded = String::with_capacity(lowered.len());
    for c in lowered.chars() {
        match fold_char(c) {
            Some(s) => folded.push_str(s),
            None => folded.push(c),
        }
    }
    let mut collapsed = String::with_capacity(folded.len());
    let mut prev_ws = false;
    for c in folded.chars() {
        if c.is_whitespace() {
            if !prev_ws {
                collapsed.push(' ');
            }
            prev_ws = true;
        } else {
            collapsed.push(c);
            prev_ws = false;
        }
    }
    collapsed
        .trim_matches(|c: char| c.is_whitespace() || EDGE_PUNCTUATION.contains(&c))
        .to_string()
}

/// How the caller treats a near-duplicate match. See the module doc.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WriteGateMode {
    /// Auto-extraction: skip near duplicates of unpinned facts.
    Auto,
    /// Explicit remember: ask for confirmation on a near duplicate.
    Explicit,
    /// Mirror / reconcile / forced remember: only exact duplicates are skipped.
    ExactOnly,
}

impl WriteGateMode {
    /// Parse `"auto"`, `"explicit"` or `"exact_only"`.
    pub fn parse(s: &str) -> crate::Result<Self> {
        match s {
            "auto" => Ok(Self::Auto),
            "explicit" => Ok(Self::Explicit),
            "exact_only" => Ok(Self::ExactOnly),
            other => Err(crate::Error::Parse(format!(
                "decide_write: unknown mode {other:?} (expected auto, explicit or exact_only)"
            ))),
        }
    }
}

/// An existing fact the new one is compared against.
///
/// Unknown JSON fields are ignored. A JSON `null` `id` / `text` reads as an
/// empty string; a candidate with an empty `id` is never matched.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct WriteGateCandidate {
    #[serde(default, deserialize_with = "null_as_empty")]
    pub id: String,
    #[serde(default, deserialize_with = "null_as_empty")]
    pub text: String,
    #[serde(default)]
    pub embedding: Option<Vec<f32>>,
    #[serde(default, deserialize_with = "null_as_false")]
    pub pinned: bool,
}

fn null_as_empty<'de, D>(deserializer: D) -> Result<String, D::Error>
where
    D: serde::Deserializer<'de>,
{
    Ok(Option::<String>::deserialize(deserializer)?.unwrap_or_default())
}

fn null_as_false<'de, D>(deserializer: D) -> Result<bool, D::Error>
where
    D: serde::Deserializer<'de>,
{
    Ok(Option::<bool>::deserialize(deserializer)?.unwrap_or(false))
}

/// What the caller should do with the new fact.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WriteGateAction {
    /// Write it.
    Store,
    /// Do not write it; it is already stored.
    Skip,
    /// Do not write it yet; a near duplicate exists (explicit mode only).
    Confirm,
}

/// Which rule produced the match.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WriteGateMatchKind {
    Exact,
    NearDuplicate,
    /// A shorter text whose words and names all appear in one stored fact
    /// (explicit mode only).
    Contained,
}

/// The existing fact that blocked (or nearly blocked) the write.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WriteGateMatch {
    pub fact_id: String,
    pub text: String,
    pub kind: WriteGateMatchKind,
    /// 1.0 for an exact match, otherwise the cosine similarity.
    pub similarity: f64,
    pub pinned: bool,
}

/// Result of [`decide_write`]. `matched` is `None` exactly when `action` is
/// `Store`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WriteGateDecision {
    pub action: WriteGateAction,
    #[serde(rename = "match")]
    pub matched: Option<WriteGateMatch>,
}

impl WriteGateDecision {
    fn store() -> Self {
        Self { action: WriteGateAction::Store, matched: None }
    }
}

fn cosine(a: &[f32], b: &[f32]) -> f64 {
    crate::reranker::cosine_similarity_f32(a, b)
}

/// The new text says nothing the existing fact does not: shorter, every name
/// (anchor) of the new text appears in it, and either every content word does
/// or the text has at least one name; no relation word on only one side.
fn is_contained(
    new_tokens: &BTreeSet<String>,
    new_anchors: &BTreeSet<String>,
    existing_text: &str,
) -> bool {
    let existing = guard_tokens(existing_text);
    !new_tokens.is_empty()
        && new_tokens.len() < existing.len()
        && new_anchors.is_subset(&existing)
        && (new_tokens.is_subset(&existing) || !new_anchors.is_empty())
        && !adds_relation_word(new_tokens, &existing)
        && !adds_relation_word(&existing, new_tokens)
}

/// Decide whether a new fact should be written. See the module doc.
///
/// `embedding` may be empty (no embedding available): only the exact rule
/// runs. Candidates whose embedding is missing or has a different length are
/// skipped by the cosine rule. Among cosine matches the highest similarity
/// wins; on a tie the earlier candidate wins.
pub fn decide_write(
    text: &str,
    embedding: &[f32],
    candidates: &[WriteGateCandidate],
    threshold: f64,
    mode: WriteGateMode,
) -> WriteGateDecision {
    let wanted = normalize_fact_text(text);
    if !wanted.is_empty() {
        let mut exact: Option<&WriteGateCandidate> = None;
        for c in candidates {
            if c.id.is_empty() || normalize_fact_text(&c.text) != wanted {
                continue;
            }
            if exact.is_none() || (c.pinned && !exact.unwrap().pinned) {
                exact = Some(c);
            }
            if c.pinned {
                break;
            }
        }
        if let Some(c) = exact {
            return WriteGateDecision {
                action: WriteGateAction::Skip,
                matched: Some(WriteGateMatch {
                    fact_id: c.id.clone(),
                    text: c.text.clone(),
                    kind: WriteGateMatchKind::Exact,
                    similarity: 1.0,
                    pinned: c.pinned,
                }),
            };
        }
    }

    if mode == WriteGateMode::ExactOnly || embedding.is_empty() {
        return WriteGateDecision::store();
    }

    let mut best: Option<(&WriteGateCandidate, f64)> = None;
    for c in candidates {
        if c.id.is_empty() || (mode == WriteGateMode::Auto && c.pinned) {
            continue;
        }
        let Some(emb) = c.embedding.as_deref() else { continue };
        if emb.len() != embedding.len() {
            continue;
        }
        let sim = cosine(embedding, emb);
        if sim >= threshold && best.is_none_or(|(_, b)| sim > b) {
            best = Some((c, sim));
        }
    }
    if best.is_none() && mode == WriteGateMode::Explicit {
        let (new_tokens, new_anchors) = guard_tokens_with_anchors(text);
        let mut contained: Option<(&WriteGateCandidate, f64)> = None;
        for c in candidates {
            if c.id.is_empty() {
                continue;
            }
            let Some(emb) = c.embedding.as_deref() else { continue };
            if emb.len() != embedding.len() {
                continue;
            }
            let sim = cosine(embedding, emb);
            if sim >= CONTAINED_MIN_COSINE
                && contained.is_none_or(|(_, b)| sim > b)
                && is_contained(&new_tokens, &new_anchors, &c.text)
            {
                contained = Some((c, sim));
            }
        }
        if let Some((c, sim)) = contained {
            return WriteGateDecision {
                action: WriteGateAction::Confirm,
                matched: Some(WriteGateMatch {
                    fact_id: c.id.clone(),
                    text: c.text.clone(),
                    kind: WriteGateMatchKind::Contained,
                    similarity: sim,
                    pinned: c.pinned,
                }),
            };
        }
    }
    match best {
        None => WriteGateDecision::store(),
        Some((c, sim)) => WriteGateDecision {
            action: if mode == WriteGateMode::Auto {
                WriteGateAction::Skip
            } else {
                WriteGateAction::Confirm
            },
            matched: Some(WriteGateMatch {
                fact_id: c.id.clone(),
                text: c.text.clone(),
                kind: WriteGateMatchKind::NearDuplicate,
                similarity: sim,
                pinned: c.pinned,
            }),
        },
    }
}

/// JSON-in / JSON-out wrapper behind the WASM and PyO3 bindings.
///
/// - `embedding_json`: JSON array of numbers, or `null` / `[]` for none.
/// - `candidates_json`: JSON array of `{id, text, embedding?, pinned?}`;
///   `null` is an empty list.
/// - `threshold`: `None` → [`STORE_DEDUP_COSINE_THRESHOLD`]; must be finite
///   and in `(0, 1]`.
/// - `mode`: `"auto"`, `"explicit"` or `"exact_only"`.
///
/// Returns the serialized [`WriteGateDecision`]:
/// `{"action": "store"|"skip"|"confirm", "match": null | {fact_id, text,
/// kind, similarity, pinned}}`.
pub fn decide_write_json(
    text: &str,
    embedding_json: &str,
    candidates_json: &str,
    threshold: Option<f64>,
    mode: &str,
) -> crate::Result<String> {
    let mode = WriteGateMode::parse(mode)?;
    let threshold = threshold.unwrap_or(STORE_DEDUP_COSINE_THRESHOLD);
    if !threshold.is_finite() || threshold <= 0.0 || threshold > 1.0 {
        return Err(crate::Error::Parse(format!(
            "decide_write: threshold must be in (0, 1], got {threshold}"
        )));
    }
    let embedding: Option<Vec<f32>> = serde_json::from_str(embedding_json)
        .map_err(|e| crate::Error::Parse(format!("decide_write: invalid embedding JSON: {e}")))?;
    let candidates: Option<Vec<WriteGateCandidate>> = serde_json::from_str(candidates_json)
        .map_err(|e| crate::Error::Parse(format!("decide_write: invalid candidates JSON: {e}")))?;
    let decision = decide_write(
        text,
        embedding.as_deref().unwrap_or(&[]),
        candidates.as_deref().unwrap_or(&[]),
        threshold,
        mode,
    );
    serde_json::to_string(&decision)
        .map_err(|e| crate::Error::Parse(format!("decide_write: serialization error: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cand(id: &str, text: &str, emb: Option<Vec<f32>>, pinned: bool) -> WriteGateCandidate {
        WriteGateCandidate { id: id.into(), text: text.into(), embedding: emb, pinned }
    }

    #[test]
    fn normalization_folds_case_space_and_edge_punctuation() {
        for v in [
            "User is vegetarian",
            "User is vegetarian.",
            "  user   is VEGETARIAN!  ",
            "\"User is vegetarian.\"",
            "- User is vegetarian",
            "**User is vegetarian**",
            "User is vegetarian…",
            "(User is vegetarian)",
        ] {
            assert_eq!(normalize_fact_text(v), "user is vegetarian", "{v:?}");
        }
    }

    #[test]
    fn normalization_keeps_inner_punctuation_and_symbols() {
        assert_eq!(normalize_fact_text("User knows C++"), "user knows c++");
        assert_ne!(normalize_fact_text("User knows C"), normalize_fact_text("User knows C++"));
        assert_eq!(normalize_fact_text("User knows C#."), "user knows c#");
        assert_eq!(normalize_fact_text("Battery at 100%"), "battery at 100%");
        assert_eq!(normalize_fact_text("1:1 with Inês on Tuesdays"), "1:1 with inês on tuesdays");
    }

    #[test]
    fn normalization_folds_typographic_variants() {
        assert_eq!(normalize_fact_text("User’s laptop"), normalize_fact_text("User's laptop"));
        assert_eq!(normalize_fact_text("Lisbon — Porto"), normalize_fact_text("Lisbon - Porto"));
        assert_eq!(normalize_fact_text("“quoted”"), "quoted");
    }

    #[test]
    fn normalization_applies_nfc() {
        // "é" precomposed vs "e" + combining acute.
        assert_eq!(normalize_fact_text("caf\u{00E9}"), normalize_fact_text("cafe\u{0301}"));
    }

    #[test]
    fn punctuation_only_text_normalizes_to_empty_and_never_matches() {
        assert_eq!(normalize_fact_text(" ... "), "");
        let d = decide_write("...", &[], &[cand("a", "!!!", None, false)], 0.85, WriteGateMode::Auto);
        assert_eq!(d.action, WriteGateAction::Store);
    }

    #[test]
    fn exact_match_skips_in_every_mode() {
        let cands = [cand("f1", "User is vegetarian.", None, false)];
        for mode in [WriteGateMode::Auto, WriteGateMode::Explicit, WriteGateMode::ExactOnly] {
            let d = decide_write("user is vegetarian", &[], &cands, 0.85, mode);
            assert_eq!(d.action, WriteGateAction::Skip, "{mode:?}");
            let m = d.matched.unwrap();
            assert_eq!(m.fact_id, "f1");
            assert_eq!(m.kind, WriteGateMatchKind::Exact);
            assert_eq!(m.similarity, 1.0);
        }
    }

    #[test]
    fn exact_match_prefers_pinned_candidate() {
        let cands = [
            cand("a", "User is vegetarian", None, false),
            cand("p", "User is vegetarian.", None, true),
        ];
        let d = decide_write("User is vegetarian", &[], &cands, 0.85, WriteGateMode::Auto);
        let m = d.matched.unwrap();
        assert_eq!(m.fact_id, "p");
        assert!(m.pinned);
    }

    #[test]
    fn near_duplicate_action_depends_on_mode() {
        let cands = [cand("f1", "User follows a vegetarian diet", Some(vec![0.99, 0.1, 0.0]), false)];
        let e = [1.0f32, 0.0, 0.0];
        let auto = decide_write("User is vegetarian", &e, &cands, 0.85, WriteGateMode::Auto);
        assert_eq!(auto.action, WriteGateAction::Skip);
        assert_eq!(auto.matched.as_ref().unwrap().kind, WriteGateMatchKind::NearDuplicate);
        let explicit = decide_write("User is vegetarian", &e, &cands, 0.85, WriteGateMode::Explicit);
        assert_eq!(explicit.action, WriteGateAction::Confirm);
        let exact_only = decide_write("User is vegetarian", &e, &cands, 0.85, WriteGateMode::ExactOnly);
        assert_eq!(exact_only, WriteGateDecision::store());
    }

    #[test]
    fn auto_mode_ignores_pinned_near_duplicates() {
        let cands = [cand("p", "1:1 with Inês on Tuesdays", Some(vec![1.0, 0.0]), true)];
        let d = decide_write("1:1 with Inês moved to Thursdays", &[0.99, 0.05], &cands, 0.85, WriteGateMode::Auto);
        assert_eq!(d.action, WriteGateAction::Store);
        let d = decide_write("1:1 with Inês moved to Thursdays", &[0.99, 0.05], &cands, 0.85, WriteGateMode::Explicit);
        assert_eq!(d.action, WriteGateAction::Confirm);
        assert!(d.matched.unwrap().pinned);
    }

    #[test]
    fn below_threshold_is_stored() {
        let cands = [cand("f1", "User prefers green tea", Some(vec![0.0, 1.0]), false)];
        let d = decide_write("User drinks black coffee", &[1.0, 0.0], &cands, 0.85, WriteGateMode::Auto);
        assert_eq!(d, WriteGateDecision::store());
    }

    #[test]
    fn best_cosine_match_wins_and_ties_keep_first() {
        let cands = [
            cand("a", "x", Some(vec![0.9, 0.3]), false),
            cand("b", "y", Some(vec![1.0, 0.01]), false),
            cand("c", "z", Some(vec![1.0, 0.01]), false),
        ];
        let d = decide_write("new", &[1.0, 0.0], &cands, 0.85, WriteGateMode::Auto);
        assert_eq!(d.matched.unwrap().fact_id, "b");
    }

    #[test]
    fn mismatched_dims_missing_embeddings_and_empty_ids_are_skipped() {
        let cands = [
            cand("a", "x", Some(vec![1.0, 0.0, 0.0]), false),
            cand("b", "y", None, false),
            cand("", "z", Some(vec![1.0, 0.0]), false),
            cand("", "new", None, false),
        ];
        let d = decide_write("new", &[1.0, 0.0], &cands, 0.85, WriteGateMode::Auto);
        assert_eq!(d, WriteGateDecision::store());
    }

    #[test]
    fn json_wrapper_round_trip_and_defaults() {
        let out = decide_write_json(
            "User is vegetarian.",
            "null",
            r#"[{"id":"f1","text":"user is vegetarian","embedding":null,"extra":1}]"#,
            None,
            "auto",
        )
        .unwrap();
        let v: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["action"], "skip");
        assert_eq!(v["match"]["fact_id"], "f1");
        assert_eq!(v["match"]["kind"], "exact");
        assert_eq!(v["match"]["pinned"], false);

        let out = decide_write_json("anything", "[]", "null", None, "explicit").unwrap();
        assert_eq!(out, r#"{"action":"store","match":null}"#);
    }

    #[test]
    fn json_wrapper_rejects_bad_input() {
        assert!(decide_write_json("t", "[]", "[]", None, "sometimes").is_err());
        assert!(decide_write_json("t", "[]", "[]", Some(0.0), "auto").is_err());
        assert!(decide_write_json("t", "[]", "[]", Some(1.5), "auto").is_err());
        assert!(decide_write_json("t", "[]", "[]", Some(f64::NAN), "auto").is_err());
        assert!(decide_write_json("t", "nope", "[]", None, "auto").is_err());
        assert!(decide_write_json("t", "[]", r#"{"id":"a"}"#, None, "auto").is_err());
    }

    const TIDE: &str = "User moved from Lisbon to Porto (around Oct/Nov 2026) to join Tidewater, a small climate-tech startup, where they are building the company's carbon-accounting dashboard.";
    const TIDE_NEW: &str = "User lives in Porto and works at Tidewater.";

    fn at32(c: f64) -> Vec<f32> {
        vec![c as f32, (1.0 - c * c).sqrt() as f32]
    }

    fn explicit(text: &str, cands: &[WriteGateCandidate]) -> WriteGateDecision {
        decide_write(text, &[1.0, 0.0], cands, STORE_DEDUP_COSINE_THRESHOLD, WriteGateMode::Explicit)
    }

    #[test]
    fn contained_short_rewording_confirms_in_explicit_mode() {
        let d = explicit(TIDE_NEW, &[cand("t", TIDE, Some(at32(0.71)), false)]);
        assert_eq!(d.action, WriteGateAction::Confirm);
        let m = d.matched.unwrap();
        assert_eq!(m.kind, WriteGateMatchKind::Contained);
        assert!((m.similarity - 0.71).abs() < 1e-6);
    }

    #[test]
    fn contained_is_explicit_only() {
        for mode in [WriteGateMode::Auto, WriteGateMode::ExactOnly] {
            let d = decide_write(
                TIDE_NEW,
                &[1.0, 0.0],
                &[cand("t", TIDE, Some(at32(0.71)), false)],
                STORE_DEDUP_COSINE_THRESHOLD,
                mode,
            );
            assert_eq!(d.action, WriteGateAction::Store, "{mode:?}");
        }
    }

    #[test]
    fn contained_below_floor_stores() {
        let d = explicit(TIDE_NEW, &[cand("t", TIDE, Some(at32(0.64)), false)]);
        assert_eq!(d.action, WriteGateAction::Store);
    }

    #[test]
    fn contained_new_anchor_stores() {
        let d = explicit("User moved to Braga.", &[cand("t", TIDE, Some(at32(0.70)), false)]);
        assert_eq!(d.action, WriteGateAction::Store);
    }

    #[test]
    fn contained_relation_words_differ_stores() {
        let d = explicit(
            "User lives in Porto.",
            &[cand(
                "b",
                "User's brother Rui lives in Porto and works at Farfetch.",
                Some(at32(0.80)),
                false,
            )],
        );
        assert_eq!(d.action, WriteGateAction::Store);
    }

    #[test]
    fn contained_needs_shorter_text() {
        let d = explicit(
            "User lives in Porto near the river with two cats.",
            &[cand("p", "User lives in Porto.", Some(at32(0.80)), false)],
        );
        assert_eq!(d.action, WriteGateAction::Store);
    }

    #[test]
    fn near_duplicate_beats_contained() {
        let d = explicit(
            TIDE_NEW,
            &[
                cand("t", TIDE, Some(at32(0.72)), false),
                cand("n", "User is based in Porto", Some(at32(0.90)), false),
            ],
        );
        assert_eq!(d.action, WriteGateAction::Confirm);
        let m = d.matched.unwrap();
        assert_eq!(m.kind, WriteGateMatchKind::NearDuplicate);
        assert_eq!(m.fact_id, "n");
    }

    #[test]
    fn contained_best_similarity_wins() {
        let d = explicit(
            TIDE_NEW,
            &[
                cand("t", TIDE, Some(at32(0.70)), false),
                cand(
                    "u",
                    "User lives in Porto and works at Tidewater as a data engineer since 2026.",
                    Some(at32(0.80)),
                    false,
                ),
            ],
        );
        assert_eq!(d.matched.unwrap().fact_id, "u");
    }

    #[test]
    fn contained_reports_pinned() {
        let d = explicit(TIDE_NEW, &[cand("t", TIDE, Some(at32(0.71)), true)]);
        assert_eq!(d.action, WriteGateAction::Confirm);
        assert!(d.matched.unwrap().pinned);
    }

    #[test]
    fn contained_without_embedding_stores() {
        let d = explicit(TIDE_NEW, &[cand("t", TIDE, None, false)]);
        assert_eq!(d.action, WriteGateAction::Store);
    }

    #[test]
    fn labelled_set_precision_recall() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../../tests/parity/fixtures/paraphrase-labels-v1.json"
        ))
        .unwrap();
        let must_store: Vec<String> = serde_json::from_value(fixture["must_store"].clone()).unwrap();
        let (mut tp, mut fp, mut fneg, mut tn) = (0u32, 0u32, 0u32, 0u32);
        let mut matched_must_store: Vec<String> = Vec::new();
        for row in fixture["gate"].as_array().unwrap() {
            let name = row["name"].as_str().unwrap();
            let c = row["cos"].as_f64().unwrap();
            let label = row["label"].as_bool().unwrap();
            let d = decide_write(
                row["text"].as_str().unwrap(),
                &[1.0, 0.0],
                &[cand("e", row["existing"].as_str().unwrap(), Some(at32(c)), false)],
                STORE_DEDUP_COSINE_THRESHOLD,
                WriteGateMode::Explicit,
            );
            let pred = d.action != WriteGateAction::Store;
            eprintln!("{name:24} cos={c:.4} label={label} -> {:?}", d.matched.as_ref().map(|m| m.kind));
            match (pred, label) {
                (true, true) => tp += 1,
                (true, false) => fp += 1,
                (false, true) => fneg += 1,
                (false, false) => tn += 1,
            }
            if pred && must_store.iter().any(|m| m == name) {
                matched_must_store.push(name.to_string());
            }
        }
        let p = tp as f64 / (tp + fp).max(1) as f64;
        let r = tp as f64 / (tp + fneg).max(1) as f64;
        eprintln!("write gate: TP {tp} FP {fp} FN {fneg} TN {tn}: P {p:.2} R {r:.2}");
        let want = &fixture["expected_counts"]["gate"];
        assert_eq!(
            (tp, fp, fneg, tn),
            (
                want["tp"].as_u64().unwrap() as u32,
                want["fp"].as_u64().unwrap() as u32,
                want["fn"].as_u64().unwrap() as u32,
                want["tn"].as_u64().unwrap() as u32
            )
        );
        assert!(matched_must_store.is_empty(), "must_store matched: {matched_must_store:?}");
    }

    #[test]
    fn default_threshold_is_store_dedup_threshold() {
        assert_eq!(STORE_DEDUP_COSINE_THRESHOLD, 0.85);
    }

    #[test]
    fn parity_fixture_vectors() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../../tests/parity/fixtures/write-gate-v1.json"
        ))
        .expect("tests/parity/fixtures/write-gate-v1.json must parse");
        assert_eq!(fixture["default_threshold"].as_f64().unwrap(), STORE_DEDUP_COSINE_THRESHOLD);
        assert_eq!(fixture["contained_min_cosine"].as_f64().unwrap(), CONTAINED_MIN_COSINE);
        for case in fixture["normalize"].as_array().unwrap() {
            let input = case["input"].as_str().unwrap();
            assert_eq!(normalize_fact_text(input), case["expected"].as_str().unwrap(), "normalize {input:?}");
        }
        let cases = fixture["decide"].as_array().unwrap();
        assert!(cases.len() >= 10);
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let out = decide_write_json(
                case["text"].as_str().unwrap(),
                &case["embedding"].to_string(),
                &case["candidates"].to_string(),
                case["threshold"].as_f64(),
                case["mode"].as_str().unwrap(),
            )
            .unwrap_or_else(|e| panic!("case {name}: {e}"));
            let got: serde_json::Value = serde_json::from_str(&out).unwrap();
            let want = &case["expected"];
            assert_eq!(got["action"], want["action"], "case {name}");
            if want["match"].is_null() {
                assert!(got["match"].is_null(), "case {name}");
            } else {
                for k in ["fact_id", "text", "kind", "pinned"] {
                    assert_eq!(got["match"][k], want["match"][k], "case {name} field {k}");
                }
                let (g, w) = (
                    got["match"]["similarity"].as_f64().unwrap(),
                    want["match"]["similarity"].as_f64().unwrap(),
                );
                assert!((g - w).abs() < 1e-6, "case {name}: similarity {g} vs {w}");
            }
        }
    }
}
