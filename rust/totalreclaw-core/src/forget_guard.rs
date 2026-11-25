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
//!    - `clause`: some clause of the candidate states the forgotten fact, with
//!      extra words around it, and the cosine is >= [`CLAUSE_MIN_COSINE`].
//!      Catches a padded restatement whose whole-sentence cosine is diluted
//!      below the `superset` floor, such as "User is vegetarian (does not eat
//!      meat) and always orders the lentil curry near Bolhão" against "User is
//!      vegetarian." (internal Hermes QA of 2.6.0rc1, H13). A clause is the
//!      stretch of the candidate between `( ) [ ] { } , ; : . ! ? — –`, the
//!      literal ` - ` (space, hyphen, space) and the whole words `and`, `but`,
//!      `so`, `because`, `while`, `which`, `who` (case-insensitive). A clause
//!      matches when its content-token set EQUALS the forgotten text's
//!      non-empty token set AND the clause has more words than that set. Words
//!      are counted the way the tokenizer sees them (NFKD, combining marks
//!      dropped, split on any non-alphanumeric character), before stopwords and
//!      the 3-character floor are applied, so "User is vegetarian" (3 words,
//!      1 token) counts and a bare "vegetarian" fragment does not. Equality
//!      keeps value swaps and subject swaps out: their clause has a different
//!      token set.
//!
//!    - `paraphrase`: the candidate shares no content token with the forgotten
//!      text, the cosine is >= [`PARAPHRASE_MIN_COSINE`], and none of these
//!      exclusions hold: the forgotten text or the candidate has an anchor (a
//!      name, place, number or date, so the candidate names something else or
//!      changed the value); both token sets
//!      have the same size (a one-for-one swap, "vegetarian" -> "vegan"); the
//!      candidate adds a relation word (a new subject). Catches "User doesn't
//!      eat meat" after "User is vegetarian" was forgotten (internal #715).
//!
//!    Relation words ([`RELATION_WORDS`]: family, friends, colleagues and common
//!    roles such as "doctor" or "landlord") veto `superset`, `anchor` and
//!    `paraphrase`: a candidate that adds one the forgotten text lacks is
//!    about someone else ("User's sister is vegetarian" after "User is
//!    vegetarian" was forgotten). The list is finite: a subject noun outside it
//!    (a pet, an employer, a person's name written lowercase) is not vetoed
//!    unless the anchor rules catch it. `subset` cannot add words, and
//!    `clause` needs an equal token set, so neither needs the veto.
//!
//!    Otherwise a candidate that both drops and adds content words never
//!    matches. That keeps value swaps out: "favorite color is green" after
//!    "... blue" was forgotten, or "brother Rui lives in Berlin" after "sister
//!    Ana lives in Berlin" (one shared anchor is not enough).
//!
//! The first matching entry, in list order, wins.
//!
//! ## Release
//!
//! An explicit remember of a forgotten fact brings it back, so the client
//! drops the matching ledger entry. [`forget_release_ids`] is more permissive
//! than [`match_forgotten`]: it releases an entry when `match_forgotten`
//! matches it, or when the remember text "mentions" it (every content token of
//! the entry is in the text and cosine >= [`RELEASE_MIN_COSINE`]). The agent
//! writes long, padded remember texts, and a missed release silently blocks
//! every later auto-extracted restatement of a fact the user just re-asserted.
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

/// Minimum cosine for a `clause` match (one clause states the forgotten fact).
pub const CLAUSE_MIN_COSINE: f64 = 0.60;

/// Minimum cosine for a `paraphrase` match (no shared content token).
pub const PARAPHRASE_MIN_COSINE: f64 = 0.80;

/// Minimum cosine for releasing an entry the remember text mentions.
pub const RELEASE_MIN_COSINE: f64 = 0.60;

/// Words that end a clause (matched as whole words, case-insensitive).
const CLAUSE_CONJUNCTIONS: &[&str] = &["and", "but", "so", "because", "while", "which", "who"];

/// Characters that end a clause.
const CLAUSE_PUNCTUATION: &[char] = &[
    '(', ')', '[', ']', '{', '}', ',', ';', ':', '.', '!', '?', '—', '–',
];

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

/// Words that name a person other than the user. A candidate that adds one the
/// forgotten text lacks is about someone else. Sorted; lookups use binary
/// search. Mirrored byte-for-byte by the Python fallback.
pub const RELATION_WORDS: &[&str] = &[
    "aunt",
    "aunts",
    "baby",
    "boss",
    "boyfriend",
    "brother",
    "brothers",
    "child",
    "children",
    "coach",
    "colleague",
    "colleagues",
    "cousin",
    "cousins",
    "coworker",
    "coworkers",
    "dad",
    "daughter",
    "daughters",
    "dentist",
    "doctor",
    "family",
    "father",
    "flatmate",
    "friend",
    "friends",
    "girlfriend",
    "grandfather",
    "grandma",
    "grandmother",
    "grandpa",
    "grandparent",
    "grandparents",
    "husband",
    "inlaw",
    "inlaws",
    "kid",
    "kids",
    "landlord",
    "laws",
    "manager",
    "mom",
    "mother",
    "mum",
    "neighbor",
    "neighbour",
    "nephew",
    "niece",
    "parent",
    "parents",
    "partner",
    "roommate",
    "sister",
    "sisters",
    "son",
    "sons",
    "spouse",
    "teacher",
    "teammate",
    "teammates",
    "tenant",
    "therapist",
    "uncle",
    "wife",
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
    /// `"exact"`, `"subset"`, `"superset"`, `"anchor"`, `"clause"` or `"paraphrase"`.
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

/// True when `cand` has a relation word that `base` lacks.
pub fn adds_relation_word(base: &BTreeSet<String>, cand: &BTreeSet<String>) -> bool {
    cand.iter()
        .any(|t| !base.contains(t) && RELATION_WORDS.binary_search(&t.as_str()).is_ok())
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

/// One clause of a candidate: its content tokens and its word count (words as
/// the tokenizer splits them, before stopword / length filtering).
fn candidate_clauses(text: &str) -> Vec<(BTreeSet<String>, usize)> {
    let mut out = Vec::new();
    for part in text.split(" - ") {
        for seg in part.split(CLAUSE_PUNCTUATION) {
            let stripped: String = seg.nfkd().filter(|c| !is_combining_mark(*c)).collect();
            let mut words: Vec<&str> = Vec::new();
            let mut flush = |words: &mut Vec<&str>| {
                if !words.is_empty() {
                    let tokens = guard_tokens(&words.join(" "));
                    if !tokens.is_empty() {
                        out.push((tokens, words.len()));
                    }
                    words.clear();
                }
            };
            for w in stripped.split(|c: char| !c.is_alphanumeric()) {
                if w.is_empty() {
                    continue;
                }
                let lower = w.to_lowercase();
                if CLAUSE_CONJUNCTIONS.contains(&lower.as_str()) {
                    flush(&mut words);
                } else {
                    words.push(w);
                }
            }
            flush(&mut words);
        }
    }
    out
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
    let (cand_tokens, cand_anchors) = guard_tokens_with_anchors(text);
    let clauses = candidate_clauses(text);

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
        let adds_relation = adds_relation_word(&entry_tokens, &cand_tokens);
        if entry_tokens.intersection(&cand_tokens).next().is_none() {
            // No shared content word: only a pure-semantic paraphrase can match.
            // Anchors (names, places, numbers) on either side mean the candidate
            // names something else or changed the value; equal token counts are a one-for-one swap
            // ("vegetarian" -> "vegan"); a new relation word is a new subject.
            if entry_anchors.is_empty()
                && cand_anchors.is_empty()
                && !adds_relation
                && entry_tokens.len() != cand_tokens.len()
                && cos >= PARAPHRASE_MIN_COSINE
            {
                return Some(ForgottenMatch {
                    id: entry.id.clone(),
                    reason: "paraphrase".to_string(),
                    cosine: Some(cos),
                });
            }
            continue;
        }
        if cand_tokens.is_subset(&entry_tokens) && cos >= SUBSET_MIN_COSINE {
            return Some(ForgottenMatch {
                id: entry.id.clone(),
                reason: "subset".to_string(),
                cosine: Some(cos),
            });
        }
        if !adds_relation && entry_tokens.is_subset(&cand_tokens) && cos >= SUPERSET_MIN_COSINE {
            return Some(ForgottenMatch {
                id: entry.id.clone(),
                reason: "superset".to_string(),
                cosine: Some(cos),
            });
        }
        let shared_anchors = entry_anchors.intersection(&cand_tokens).count();
        if !adds_relation
            && entry_anchors.len() >= 2
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
        if cos >= CLAUSE_MIN_COSINE
            && clauses
                .iter()
                .any(|(tokens, words)| *tokens == entry_tokens && *words > entry_tokens.len())
        {
            return Some(ForgottenMatch {
                id: entry.id.clone(),
                reason: "clause".to_string(),
                cosine: Some(cos),
            });
        }
    }
    None
}

/// Ids of the forgotten entries an explicit remember of `text` releases.
///
/// An entry is released when [`match_forgotten`] matches it (any reason except `paraphrase`), or
/// when `text` mentions it: the entry's content-token set is non-empty, is a
/// subset of the text's, and cosine >= [`RELEASE_MIN_COSINE`]. Without an
/// embedding (the text's or the entry's) only an `exact` match releases.
pub fn forget_release_ids(
    text: &str,
    embedding: Option<&[f64]>,
    entries: &[ForgottenEntry],
) -> Vec<String> {
    if text.trim().is_empty() || entries.is_empty() {
        return Vec::new();
    }
    let cand_tokens = guard_tokens(text);
    let mut ids = Vec::new();
    for entry in entries {
        // A `paraphrase` match does not release: an unprompted agent remember
        // must not silently un-forget a fact on meaning alone.
        if let Some(m) = match_forgotten(text, embedding, std::slice::from_ref(entry)) {
            if m.reason != "paraphrase" {
                ids.push(entry.id.clone());
                continue;
            }
        }
        let cos = match (embedding, entry.embedding.as_deref()) {
            (Some(a), Some(b)) => cosine(a, b),
            _ => None,
        };
        let entry_tokens = guard_tokens(&entry.text);
        if let Some(cos) = cos {
            if !entry_tokens.is_empty()
                && entry_tokens.is_subset(&cand_tokens)
                && cos >= RELEASE_MIN_COSINE
            {
                ids.push(entry.id.clone());
            }
        }
    }
    ids
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

/// JSON wrapper behind the PyO3 / WASM bindings for [`forget_release_ids`].
///
/// Same inputs as [`match_forgotten_json`]. Returns a JSON array of entry ids.
pub fn forget_release_ids_json(
    text: &str,
    embedding_json: &str,
    entries_json: &str,
) -> Result<String, String> {
    let embedding: Option<Vec<f64>> =
        serde_json::from_str(embedding_json).map_err(|e| format!("bad embedding JSON: {e}"))?;
    let entries: Vec<ForgottenEntry> =
        serde_json::from_str(entries_json).map_err(|e| format!("bad entries JSON: {e}"))?;
    let ids = forget_release_ids(text, embedding.as_deref(), &entries);
    serde_json::to_string(&ids).map_err(|e| e.to_string())
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

    fn at(c: f64) -> Vec<f64> {
        vec![c, (1.0 - c * c).sqrt()]
    }

    const H13_C1: &str = "User is vegetarian (does not eat meat) and always orders the lentil curry at their usual Indian restaurant near Bolhão.";
    const H13_R: &str = "User is vegetarian again (does not eat meat), resuming as of November 18, 2026 — they had lapsed from vegetarianism at some point before this and have now returned to it.";
    const VEG_ID: &str = "45649345-c4d0-42e0-94bb-74027213bbfa";

    fn veg() -> Vec<ForgottenEntry> {
        vec![entry(VEG_ID, "User is vegetarian.", Some(vec![1.0, 0.0]))]
    }

    #[test]
    fn clause_padded_restatement_h13() {
        let m = match_forgotten(H13_C1, Some(&at(0.71)), &veg()).unwrap();
        assert_eq!(m.reason, "clause");
        assert_eq!(m.id, VEG_ID);
        assert!(match_forgotten(H13_C1, Some(&at(0.55)), &veg()).is_none());
    }

    #[test]
    fn clause_padded_remember_h13() {
        let m = match_forgotten(H13_R, Some(&at(0.69)), &veg()).unwrap();
        assert_eq!(m.reason, "clause");
    }

    #[test]
    fn clause_fragment_needs_more_than_the_tokens() {
        assert!(match_forgotten(
            "User's sister Ana is a nurse, and vegetarian.",
            Some(&at(0.75)),
            &veg()
        )
        .is_none());
    }

    #[test]
    fn clause_value_swap() {
        let e = vec![entry(
            "c",
            "User's favorite color is blue",
            Some(vec![1.0, 0.0]),
        )];
        assert!(match_forgotten(
            "User's favorite color is green, and they repainted the kitchen.",
            Some(&at(0.70)),
            &e
        )
        .is_none());
    }

    #[test]
    fn clause_subject_swap() {
        let e = vec![entry("p", "User lives in Porto", Some(vec![1.0, 0.0]))];
        assert!(match_forgotten(
            "User's brother Rui lives in Porto and works at Farfetch.",
            Some(&at(0.75)),
            &e
        )
        .is_none());
    }

    #[test]
    fn clause_moved_from() {
        let e = vec![entry("p", "User lives in Porto", Some(vec![1.0, 0.0]))];
        assert!(match_forgotten(
            "User lives in Lisbon (moved from Porto in 2024).",
            Some(&at(0.79)),
            &e
        )
        .is_none());
    }

    #[test]
    fn release_ids_h13() {
        let entries = vec![
            entry(VEG_ID, "User is vegetarian.", Some(vec![1.0, 0.0])),
            entry(
                "a3c1f7ca-0000-4000-8000-000000000000",
                "User decided to use glm-4.5-flash for memory extraction from now on.",
                Some(vec![0.0, 1.0]),
            ),
        ];
        // The glm entry has a high cosine here (0.72) but shares no content token: kept.
        let ids = forget_release_ids(H13_R, Some(&at(0.69)), &entries);
        assert_eq!(ids, vec![VEG_ID.to_string()]);
    }

    #[test]
    fn release_mentions() {
        let ids = forget_release_ids(
            "User follows a vegetarian diet again as of November 2026",
            Some(&at(0.73)),
            &veg(),
        );
        assert_eq!(ids, vec![VEG_ID.to_string()]);
        assert!(forget_release_ids(
            "User eats a plant-based diet again",
            Some(&at(0.72)),
            &veg()
        )
        .is_empty());
    }

    #[test]
    fn release_needs_embedding_except_exact() {
        let e = vec![
            entry("a", "User is vegetarian.", None),
            entry("b", "User follows a vegetarian diet", Some(vec![1.0, 0.0])),
        ];
        // No candidate embedding: only exact releases.
        assert_eq!(
            forget_release_ids("user is   VEGETARIAN.", None, &e),
            vec!["a".to_string()]
        );
        assert!(
            forget_release_ids("User follows a vegetarian diet again as of 2026", None, &e)
                .is_empty()
        );
        // Candidate embedding, entry without one: not released (not exact).
        assert!(forget_release_ids(
            "User is vegetarian again as of 2026",
            Some(&at(0.9)),
            &e[..1]
        )
        .is_empty());
    }

    #[test]
    fn relation_words_are_sorted_and_unique() {
        for w in RELATION_WORDS.windows(2) {
            assert!(w[0] < w[1], "RELATION_WORDS not sorted/unique at {:?}", w);
        }
    }

    #[test]
    fn paraphrase_disjoint_tokens_matches() {
        let m = match_forgotten("User doesn't eat meat.", Some(&at(0.83)), &veg()).unwrap();
        assert_eq!(m.reason, "paraphrase");
        assert!(match_forgotten("User doesn't eat meat.", Some(&at(0.79)), &veg()).is_none());
    }

    #[test]
    fn paraphrase_never_when_entry_has_anchor() {
        let e = vec![entry("p", "User lives in Porto", Some(vec![1.0, 0.0]))];
        assert!(match_forgotten("User moved to Lisbon.", Some(&at(0.82)), &e).is_none());
    }

    #[test]
    fn paraphrase_never_for_same_size_swap() {
        assert!(match_forgotten("User is vegan.", Some(&at(0.93)), &veg()).is_none());
    }

    #[test]
    fn paraphrase_never_adds_relation() {
        assert!(match_forgotten("User's sister doesn't eat meat.", Some(&at(0.85)), &veg()).is_none());
    }

    #[test]
    fn paraphrase_needs_content_tokens() {
        assert!(match_forgotten("OK.", Some(&at(0.95)), &veg()).is_none());
    }

    #[test]
    fn superset_and_anchor_veto_relation_words() {
        assert!(match_forgotten("User's sister is vegetarian.", Some(&at(0.87)), &veg()).is_none());
        assert_eq!(
            match_forgotten("User has been vegetarian since 2019", Some(&at(0.86)), &veg())
                .unwrap()
                .reason,
            "superset"
        );
        let e = vec![entry(
            "t",
            "User lives in Porto and works at Tidewater",
            Some(vec![1.0, 0.0]),
        )];
        assert!(match_forgotten(
            "User's brother lives in Porto and works at Tidewater",
            Some(&at(0.90)),
            &e
        )
        .is_none());
    }

    #[test]
    fn release_skips_paraphrase() {
        assert!(forget_release_ids("User doesn't eat meat.", Some(&at(0.83)), &veg()).is_empty());
    }

    #[test]
    fn labelled_set_precision_recall() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../../tests/parity/fixtures/paraphrase-labels-v1.json"
        ))
        .unwrap();
        let (mut tp, mut fp, mut fneg, mut tn) = (0u32, 0u32, 0u32, 0u32);
        let mut matched_must_store: Vec<String> = Vec::new();
        let must_store: Vec<String> = serde_json::from_value(fixture["must_store"].clone()).unwrap();
        for row in fixture["forget"].as_array().unwrap() {
            let name = row["name"].as_str().unwrap();
            let c = row["cos"].as_f64().unwrap();
            let label = row["label"].as_bool().unwrap();
            let got = match_forgotten(
                row["text"].as_str().unwrap(),
                Some(&at(c)),
                &[entry("e", row["entry"].as_str().unwrap(), Some(vec![1.0, 0.0]))],
            );
            let pred = got.is_some();
            eprintln!(
                "{name:24} cos={c:.4} label={label} -> {:?}",
                got.as_ref().map(|m| m.reason.as_str())
            );
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
        eprintln!("forget guard: TP {tp} FP {fp} FN {fneg} TN {tn}: P {p:.2} R {r:.2}");
        let want = &fixture["expected_counts"]["forget"];
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
        assert_eq!(
            fixture["clause_min_cosine"].as_f64(),
            Some(CLAUSE_MIN_COSINE)
        );
        assert_eq!(
            fixture["release_min_cosine"].as_f64(),
            Some(RELEASE_MIN_COSINE)
        );
        assert_eq!(
            fixture["paraphrase_min_cosine"].as_f64(),
            Some(PARAPHRASE_MIN_COSINE)
        );
        for case in fixture["release_cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let out = forget_release_ids_json(
                case["text"].as_str().unwrap(),
                &case["embedding"].to_string(),
                &case["entries"].to_string(),
            )
            .unwrap();
            let got: Vec<String> = serde_json::from_str(&out).unwrap();
            let want: Vec<String> = serde_json::from_value(case["expected"].clone()).unwrap();
            assert_eq!(got, want, "release {name}");
        }
    }
}
