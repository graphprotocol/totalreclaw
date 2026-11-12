//! Forget guard: stop auto-extraction from re-creating a fact the user forgot.
//!
//! internal#605 / #640. Forget writes an on-chain tombstone. Auto-extraction
//! keeps running on later turns, on Hermes compaction and at session end, and
//! it reads text that still restates the forgotten fact: the turn that said
//! it, the forget turn itself ("forget that Inês proposed PgBouncer"), or an
//! assistant answer built from the host's own transcript search. The dedup
//! context only holds ACTIVE facts, so nothing tells the write path that the
//! user deleted this. The fact comes back.
//!
//! The client keeps its own list of facts the user explicitly forgot
//! (encrypted, client-side; see the Python `ForgetLedger`). Before it stores
//! an extracted fact, it asks this module whether the candidate restates one
//! of them. Conflicts are settled at write time, client-side; recall never
//! sees this list.
//!
//! Two pure checks live here so every client gets the same answer:
//!
//! - [`match_forgotten`]: does a candidate restate a forgotten fact?
//! - [`is_forget_request`]: is a candidate a note about a deletion
//!   ("user asked to forget X")? Such notes restate the forgotten content and
//!   are never useful memories.
//!
//! ## Matching rule
//!
//! Cosine alone can't tell "the same fact" from "the same slot, new value":
//! with Harrier, "favorite color is blue" vs "... green" scores 0.94, and
//! "lives in Lisbon" vs "lives in Porto" 0.90. So a match needs both a
//! semantic signal and a lexical one. For each forgotten entry:
//!
//! 1. `exact`: the normalized texts are equal ([`crate::fingerprint::normalize_text`]).
//!    Works without embeddings.
//! 2. Otherwise both embeddings are required. Let `A` = content tokens of the
//!    forgotten text and `B` = content tokens of the candidate, both non-empty
//!    and sharing at least one token.
//!    - `subset`: `B ⊆ A` (the candidate adds no new content word) and
//!      cosine >= [`SUBSET_MIN_COSINE`]. Catches "Inês proposed PgBouncer but
//!      this is now considered outdated" and "User is not vegetarian".
//!    - `superset`: `A ⊆ B` (the candidate keeps every content word and adds
//!      detail) and cosine >= [`SUPERSET_MIN_COSINE`]. Catches "User has been
//!      vegetarian since 2019".
//!    - `anchor`: the forgotten text has 2+ anchor tokens (a word written
//!      with a capital letter, or containing a digit: names, products,
//!      places, years), the candidate contains at least 2 of them and at
//!      least half of them, and cosine >= [`ANCHOR_MIN_COSINE`]. Catches a
//!      reworded restatement such as "Inês proposed putting PgBouncer in front
//!      of Postgres", which both drops and adds ordinary words.
//!
//!    Otherwise a candidate that both drops and adds content words never
//!    matches. That keeps value swaps out: "favorite color is green" after
//!    "... blue" was forgotten, or "brother Rui lives in Berlin" after "sister
//!    Ana lives in Berlin" (one shared anchor is not enough).
//!
//! The first matching entry, in list order, wins.
//!
//! ## Content tokens
//!
//! NFKD, drop combining marks (so "Inês" == "Ines"), split on any
//! non-alphanumeric character, lowercase, keep tokens of 3+ characters that
//! are not in [`STOPWORDS`]. The stopword list removes function words plus
//! the verbs and nouns that deletion notes and restatements add ("forget",
//! "outdated", "memory", "named", "called"). A kept token is an anchor when
//! its original first character is uppercase or any character is numeric.

use std::collections::BTreeSet;
use std::sync::OnceLock;

use regex::Regex;
use serde::{Deserialize, Serialize};
use unicode_normalization::char::is_combining_mark;
use unicode_normalization::UnicodeNormalization;

use crate::fingerprint::normalize_text;

/// Minimum cosine for a `subset` match (candidate adds no content word).
pub const SUBSET_MIN_COSINE: f64 = 0.70;

/// Minimum cosine for a `superset` match (candidate keeps every content word).
pub const SUPERSET_MIN_COSINE: f64 = 0.80;

/// Minimum cosine for an `anchor` match (shared names / products / places).
pub const ANCHOR_MIN_COSINE: f64 = 0.80;

/// Words ignored when comparing content tokens. Sorted; lookups use binary
/// search. Mirrored byte-for-byte by the Python fallback.
pub const STOPWORDS: &[&str] = &[
    "about",
    "again",
    "all",
    "also",
    "and",
    "any",
    "anymore",
    "are",
    "asked",
    "assistant",
    "been",
    "being",
    "but",
    "called",
    "can",
    "considered",
    "could",
    "currently",
    "delete",
    "deleted",
    "did",
    "does",
    "doing",
    "done",
    "erase",
    "erased",
    "for",
    "forget",
    "forgot",
    "forgotten",
    "from",
    "had",
    "has",
    "have",
    "her",
    "him",
    "his",
    "into",
    "its",
    "just",
    "know",
    "knows",
    "like",
    "liked",
    "likes",
    "longer",
    "memories",
    "memory",
    "mentioned",
    "more",
    "most",
    "named",
    "not",
    "noted",
    "now",
    "only",
    "onto",
    "our",
    "outdated",
    "over",
    "prefer",
    "preferred",
    "prefers",
    "previously",
    "really",
    "record",
    "records",
    "remember",
    "remembered",
    "removal",
    "remove",
    "removed",
    "said",
    "same",
    "say",
    "says",
    "she",
    "should",
    "some",
    "still",
    "store",
    "stored",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "think",
    "thinks",
    "this",
    "those",
    "today",
    "told",
    "too",
    "use",
    "used",
    "user",
    "users",
    "uses",
    "using",
    "very",
    "want",
    "wanted",
    "wants",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "will",
    "with",
    "would",
    "you",
    "your",
];

/// A fact the user explicitly forgot, as the client's ledger holds it.
#[derive(Debug, Clone, Deserialize)]
pub struct ForgottenEntry {
    /// On-chain id of the tombstoned fact.
    pub id: String,
    /// Decrypted text of the forgotten fact.
    #[serde(default)]
    pub text: String,
    /// Decrypted embedding of the forgotten fact, if known.
    #[serde(default)]
    pub embedding: Option<Vec<f64>>,
}

/// Why a candidate was matched to a forgotten entry.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ForgottenMatch {
    /// Id of the forgotten fact the candidate restates.
    pub id: String,
    /// `"exact"`, `"subset"`, `"superset"` or `"anchor"`.
    pub reason: String,
    /// Cosine similarity, when both embeddings were usable.
    pub cosine: Option<f64>,
}

/// Content tokens and anchor tokens of `text` (see the module docs).
pub fn guard_tokens_with_anchors(text: &str) -> (BTreeSet<String>, BTreeSet<String>) {
    let stripped: String = text.nfkd().filter(|c| !is_combining_mark(*c)).collect();
    let mut tokens = BTreeSet::new();
    let mut anchors = BTreeSet::new();
    for word in stripped.split(|c: char| !c.is_alphanumeric()) {
        let lower = word.to_lowercase();
        if lower.chars().count() < 3 || STOPWORDS.binary_search(&lower.as_str()).is_ok() {
            continue;
        }
        let first_upper = word.chars().next().is_some_and(char::is_uppercase);
        if first_upper || word.chars().any(char::is_numeric) {
            anchors.insert(lower.clone());
        }
        tokens.insert(lower);
    }
    (tokens, anchors)
}

/// Content tokens of `text` (see the module docs). Sorted, unique.
pub fn guard_tokens(text: &str) -> BTreeSet<String> {
    guard_tokens_with_anchors(text).0
}

fn cosine(a: &[f64], b: &[f64]) -> Option<f64> {
    if a.is_empty() || a.len() != b.len() {
        return None;
    }
    let mut dot = 0.0;
    let mut na = 0.0;
    let mut nb = 0.0;
    for (x, y) in a.iter().zip(b.iter()) {
        dot += x * y;
        na += x * x;
        nb += y * y;
    }
    let denom = na.sqrt() * nb.sqrt();
    if denom == 0.0 {
        None
    } else {
        Some(dot / denom)
    }
}

/// Return the first forgotten entry that `text` restates, or `None`.
///
/// `embedding` is the candidate's embedding; pass `None` when it could not be
/// computed (only `exact` matches are possible then).
pub fn match_forgotten(
    text: &str,
    embedding: Option<&[f64]>,
    entries: &[ForgottenEntry],
) -> Option<ForgottenMatch> {
    if text.trim().is_empty() || entries.is_empty() {
        return None;
    }
    let norm = normalize_text(text);
    let cand_tokens = guard_tokens(text);

    for entry in entries {
        let cos = match (embedding, entry.embedding.as_deref()) {
            (Some(a), Some(b)) => cosine(a, b),
            _ => None,
        };
        if !entry.text.trim().is_empty() && normalize_text(&entry.text) == norm {
            return Some(ForgottenMatch {
                id: entry.id.clone(),
                reason: "exact".to_string(),
                cosine: cos,
            });
        }
        let Some(cos) = cos else { continue };
        let (entry_tokens, entry_anchors) = guard_tokens_with_anchors(&entry.text);
        if entry_tokens.is_empty() || cand_tokens.is_empty() {
            continue;
        }
        if entry_tokens.intersection(&cand_tokens).next().is_none() {
            continue;
        }
        if cand_tokens.is_subset(&entry_tokens) && cos >= SUBSET_MIN_COSINE {
            return Some(ForgottenMatch {
                id: entry.id.clone(),
                reason: "subset".to_string(),
                cosine: Some(cos),
            });
        }
        if entry_tokens.is_subset(&cand_tokens) && cos >= SUPERSET_MIN_COSINE {
            return Some(ForgottenMatch {
                id: entry.id.clone(),
                reason: "superset".to_string(),
                cosine: Some(cos),
            });
        }
        let shared_anchors = entry_anchors.intersection(&cand_tokens).count();
        if entry_anchors.len() >= 2
            && shared_anchors >= 2
            && shared_anchors * 2 >= entry_anchors.len()
            && cos >= ANCHOR_MIN_COSINE
        {
            return Some(ForgottenMatch {
                id: entry.id.clone(),
                reason: "anchor".to_string(),
                cosine: Some(cos),
            });
        }
    }
    None
}

/// JSON wrapper behind the PyO3 / WASM bindings.
///
/// `embedding_json` is a JSON array of numbers or `null`. `entries_json` is a
/// JSON array of `{ id, text, embedding? }`. Returns `"null"` or the match as
/// JSON `{ id, reason, cosine }`.
pub fn match_forgotten_json(
    text: &str,
    embedding_json: &str,
    entries_json: &str,
) -> Result<String, String> {
    let embedding: Option<Vec<f64>> =
        serde_json::from_str(embedding_json).map_err(|e| format!("bad embedding JSON: {e}"))?;
    let entries: Vec<ForgottenEntry> =
        serde_json::from_str(entries_json).map_err(|e| format!("bad entries JSON: {e}"))?;
    let found = match_forgotten(text, embedding.as_deref(), &entries);
    serde_json::to_string(&found).map_err(|e| e.to_string())
}

const FORGET_REQUEST_PATTERNS: &[&str] = &[
    // "asked (the assistant) to forget X" / "wants you to forget X"
    r"\b(?:asked|asks|ask|wants|wanted|want|requested|requests|request|told|tells|instructed|chose|decided)\b[^.;]{0,40}?\bto\s+(?:forget|unlearn)\b",
    // "removed X from (the) memory / vault"
    r"\b(?:forget|forgot|forgotten|delete|deleted|remove|removed|erase|erased|purge|purged|wipe|wiped|drop|dropped|clear|cleared)\b[^.;]{0,60}?\bfrom\s+(?:the\s+|your\s+|my\s+|its\s+|their\s+)?(?:memory|memories|vault|records)\b",
    // "asked for that record to be removed" / "the memory was deleted"
    r"\b(?:that|this|the)\s+(?:memory|memories|record|entry|fact)\b[^.;]{0,40}?\b(?:forgotten|deleted|removed|erased|purged|wiped)\b",
    // "no longer wants it stored / remembered"
    r"\b(?:no\s+longer|doesn['’]?t|does\s+not|don['’]?t|do\s+not)\s+wants?\b[^.;]{0,40}?\b(?:remembered|stored|saved|kept\s+in\s+memory)\b",
    // "requested deletion of X"
    r"\b(?:asked|requested|wants|wanted)\b[^.;]{0,30}?\b(?:removal|deletion|erasure)\s+of\b",
];

fn forget_request_regexes() -> &'static Vec<Regex> {
    static RES: OnceLock<Vec<Regex>> = OnceLock::new();
    RES.get_or_init(|| {
        FORGET_REQUEST_PATTERNS
            .iter()
            .map(|p| Regex::new(&format!("(?i){p}")).expect("forget-request pattern compiles"))
            .collect()
    })
}

/// True when `text` is a note about deleting a memory ("User asked to forget
/// that Inês proposed PgBouncer"), not a fact about the user.
///
/// High precision by design: a plain "forget" or "delete" is not enough
/// ("User tends to forget appointments" and "User wants to delete their
/// Facebook account" are real facts). The match needs a request verb before
/// "to forget", or a memory/record target next to the deletion verb.
pub fn is_forget_request(text: &str) -> bool {
    if text.trim().is_empty() {
        return false;
    }
    let folded: String = text
        .nfkd()
        .filter(|c| !is_combining_mark(*c))
        .collect::<String>()
        .to_lowercase();
    forget_request_regexes()
        .iter()
        .any(|re| re.is_match(&folded))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(id: &str, text: &str, emb: Option<Vec<f64>>) -> ForgottenEntry {
        ForgottenEntry {
            id: id.into(),
            text: text.into(),
            embedding: emb,
        }
    }

    #[test]
    fn stopwords_are_sorted_and_unique() {
        for w in STOPWORDS.windows(2) {
            assert!(w[0] < w[1], "STOPWORDS not sorted/unique at {:?}", w);
        }
    }

    #[test]
    fn tokens_fold_accents_and_drop_stopwords() {
        let t = guard_tokens("Inês proposed using PgBouncer — the user's idea!");
        let got: Vec<&str> = t.iter().map(String::as_str).collect();
        assert_eq!(got, vec!["idea", "ines", "pgbouncer", "proposed"]);
    }

    #[test]
    fn exact_match_needs_no_embedding() {
        let e = vec![entry("f1", "User is vegetarian", None)];
        let m = match_forgotten("  user IS   vegetarian ", None, &e).unwrap();
        assert_eq!(m.reason, "exact");
        assert_eq!(m.id, "f1");
        assert_eq!(m.cosine, None);
    }

    #[test]
    fn value_swap_never_matches() {
        let e = vec![entry(
            "f1",
            "User's favorite color is blue",
            Some(vec![1.0, 0.0]),
        )];
        // cosine 1.0, but "green" is new and "blue" is gone.
        assert!(match_forgotten("User's favorite color is green", Some(&[1.0, 0.0]), &e).is_none());
    }

    #[test]
    fn subset_and_superset_thresholds() {
        let e = vec![entry(
            "f1",
            "Inês proposed using PgBouncer for connection pooling",
            Some(vec![1.0, 0.0]),
        )];
        // cos(θ) for [cos, sin]
        let at = |c: f64| vec![c, (1.0 - c * c).sqrt()];
        let sub = "Ines proposed PgBouncer but this is now considered outdated";
        assert_eq!(
            match_forgotten(sub, Some(&at(0.75)), &e).unwrap().reason,
            "subset"
        );
        assert!(match_forgotten(sub, Some(&at(0.65)), &e).is_none());

        let e2 = vec![entry("v", "User is vegetarian", Some(vec![1.0, 0.0]))];
        let sup = "User has been vegetarian since 2019";
        assert_eq!(
            match_forgotten(sup, Some(&at(0.85)), &e2).unwrap().reason,
            "superset"
        );
        assert!(match_forgotten(sup, Some(&at(0.75)), &e2).is_none());
    }

    #[test]
    fn dimension_mismatch_skips_semantic_rules() {
        let e = vec![entry("f1", "User is vegetarian", Some(vec![1.0, 0.0, 0.0]))];
        assert!(match_forgotten("User is not vegetarian", Some(&[1.0, 0.0]), &e).is_none());
    }

    #[test]
    fn forget_request_detection() {
        for yes in [
            "User asked the assistant to forget that Inês proposed PgBouncer",
            "User wants to forget that they are vegetarian",
            "User previously identified as vegetarian and asked for that record to be removed",
            "User removed the PgBouncer note from memory",
            "User no longer wants their dietary preference stored",
            "User doesn’t want this remembered",
            "User requested deletion of the vegetarian record",
        ] {
            assert!(is_forget_request(yes), "should match: {yes}");
        }
        for no in [
            "User tends to forget appointments",
            "User wants to delete their Facebook account",
            "User removed old records from the database",
            "User is vegetarian",
            "Inês proposed PgBouncer",
            "",
        ] {
            assert!(!is_forget_request(no), "should not match: {no}");
        }
    }

    #[test]
    fn json_wrapper_round_trip() {
        let out = match_forgotten_json(
            "User is vegetarian",
            "null",
            r#"[{"id":"f1","text":"user is vegetarian"}]"#,
        )
        .unwrap();
        assert_eq!(out, r#"{"id":"f1","reason":"exact","cosine":null}"#);
        assert_eq!(match_forgotten_json("x", "null", "[]").unwrap(), "null");
        assert!(match_forgotten_json("x", "nope", "[]").is_err());
    }

    #[test]
    fn parity_fixture_vectors() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../../tests/parity/fixtures/forget-guard-v1.json"
        ))
        .unwrap();
        for case in fixture["match_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let out = match_forgotten_json(
                case["text"].as_str().unwrap(),
                &case["embedding"].to_string(),
                &case["entries"].to_string(),
            )
            .unwrap();
            let got: serde_json::Value = serde_json::from_str(&out).unwrap();
            let want = &case["expected"];
            if want.is_null() {
                assert!(got.is_null(), "{name}: expected no match, got {got}");
            } else {
                assert_eq!(got["id"], want["id"], "{name}");
                assert_eq!(got["reason"], want["reason"], "{name}");
            }
        }
        for case in fixture["request_cases"].as_array().unwrap() {
            let text = case["text"].as_str().unwrap();
            assert_eq!(
                is_forget_request(text),
                case["expected"].as_bool().unwrap(),
                "is_forget_request({text:?})"
            );
        }
        for case in fixture["token_cases"].as_array().unwrap() {
            let text = case["text"].as_str().unwrap();
            let want: Vec<String> = serde_json::from_value(case["expected"].clone()).unwrap();
            let got: Vec<String> = guard_tokens(text).into_iter().collect();
            assert_eq!(got, want, "guard_tokens({text:?})");
        }
        for case in fixture["anchor_cases"].as_array().unwrap() {
            let text = case["text"].as_str().unwrap();
            let want: Vec<String> = serde_json::from_value(case["expected"].clone()).unwrap();
            let got: Vec<String> = guard_tokens_with_anchors(text).1.into_iter().collect();
            assert_eq!(got, want, "anchors({text:?})");
        }
        assert_eq!(
            fixture["subset_min_cosine"].as_f64(),
            Some(SUBSET_MIN_COSINE)
        );
        assert_eq!(
            fixture["superset_min_cosine"].as_f64(),
            Some(SUPERSET_MIN_COSINE)
        );
        assert_eq!(
            fixture["anchor_min_cosine"].as_f64(),
            Some(ANCHOR_MIN_COSINE)
        );
    }
}
