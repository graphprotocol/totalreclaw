//! Natural-language pin/unpin intent classifier (kg-2 / F1 Pin UX 2.2.8).
//!
//! Phase 1: substring-based classifier over normalized lowercase text. Cross-client
//! by construction (single source of truth in `totalreclaw-core`), exposed via
//! WASM + PyO3 so OpenClaw plugin, MCP server, NanoClaw, Hermes, and ZeroClaw all
//! hit identical recognition. A user pinning on OpenClaw and unpinning on Hermes
//! against the same vault produces the same state as pinning + unpinning on a
//! single client.
//!
//! Grammar default per `docs/plans/2026-04-28-f1-pin-ux-defaults.md` §Q3.
//! Phase 2 (LLM-shimmed disambiguation) and Phase 3 (trained tiny classifier)
//! are deferred — this module exposes only the Phase 1 surface.

use serde::{Deserialize, Serialize};

/// Recognised intent buckets.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PinIntentKind {
    /// Default natural-language pin (decays, surfaces conflicts).
    Soft,
    /// Explicit permanent-rule pin (no decay, silent conflict drop).
    Hard,
    /// Remove an existing pin (any tier).
    Unpin,
    /// Promote the most-recently-pinned fact in the conversation from soft to hard.
    TierSwapToHard,
    /// Demote the most-recently-pinned fact in the conversation from hard to soft.
    TierSwapToSoft,
}

/// A classified pin/unpin intent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PinIntent {
    pub kind: PinIntentKind,
    /// The trigger phrase substring (as stored in the grammar) that fired the match.
    /// Useful for client UIs that want to acknowledge "I pinned this because you said …".
    pub matched_phrase: String,
}

/// Configurable trigger-phrase sets. All phrases must be lowercase and
/// normalized — the classifier lowercases input before matching.
///
/// Default phrase set is the recommended one in
/// `docs/plans/2026-04-28-f1-pin-ux-defaults.md` Q3, locked by Pedro 2026-04-28.
#[derive(Debug, Clone)]
pub struct PinIntentGrammar {
    pub soft_phrases: Vec<&'static str>,
    pub hard_phrases: Vec<&'static str>,
    pub unpin_phrases: Vec<&'static str>,
    pub tier_swap_to_hard_phrases: Vec<&'static str>,
    pub tier_swap_to_soft_phrases: Vec<&'static str>,
}

impl Default for PinIntentGrammar {
    fn default() -> Self {
        default_grammar()
    }
}

/// The locked-default grammar.
pub fn default_grammar() -> PinIntentGrammar {
    PinIntentGrammar {
        // Soft pin: everyday "remember this" gestures.
        soft_phrases: vec![
            "remember this",
            "pin this",
            "remember that i",
            "save that for later",
            "don't forget i",
            "dont forget i",
            "pin that",
        ],
        // Hard pin: explicit permanent commitments.
        hard_phrases: vec![
            "always remember i",
            "never forget i",
            "this is a permanent rule",
            "permanent rule",
            "rule of thumb:",
            "rule of thumb,",
        ],
        // Unpin: remove an existing pin (tier-agnostic).
        unpin_phrases: vec![
            "unpin that",
            "unpin this",
            "no longer pin",
            "remove pin from",
            "remove the pin from",
            "i changed my mind about",
        ],
        // Tier swap: promote most-recent soft → hard.
        tier_swap_to_hard_phrases: vec![
            "make that a hard pin",
            "make this a hard pin",
            "promote that pin",
            "upgrade that pin to hard",
        ],
        // Tier swap: demote most-recent hard → soft.
        tier_swap_to_soft_phrases: vec![
            "downgrade that pin to soft",
            "make that a soft pin",
            "make this a soft pin",
        ],
    }
}

/// Classify with the default grammar. See [`classify_pin_intent_with`] for the
/// tunable form.
pub fn classify_pin_intent(text: &str) -> Option<PinIntent> {
    classify_pin_intent_with(text, &default_grammar())
}

/// Classify with a caller-supplied grammar. Lowercase-normalizes the input
/// before substring matching.
///
/// Priority order (most-specific first):
/// 1. Tier-swap (most explicit, requires "pin" + tier word)
/// 2. Hard pin
/// 3. Unpin
/// 4. Soft pin
///
/// This ordering matters because text can legitimately contain multiple
/// trigger words, e.g. "always remember i prefer Vim, but unpin the old one"
/// should classify as Hard (the active intent of the sentence), not Unpin.
/// The simple priority rule covers the common cases. Ambiguous mixed-intent
/// cases (Phase 2) will be deferred to an LLM shim, not handled here.
pub fn classify_pin_intent_with(text: &str, grammar: &PinIntentGrammar) -> Option<PinIntent> {
    let normalized = text.to_lowercase();
    if normalized.trim().is_empty() {
        return None;
    }

    if let Some(matched) = first_match(&normalized, &grammar.tier_swap_to_hard_phrases) {
        return Some(PinIntent {
            kind: PinIntentKind::TierSwapToHard,
            matched_phrase: matched.to_string(),
        });
    }
    if let Some(matched) = first_match(&normalized, &grammar.tier_swap_to_soft_phrases) {
        return Some(PinIntent {
            kind: PinIntentKind::TierSwapToSoft,
            matched_phrase: matched.to_string(),
        });
    }
    if let Some(matched) = first_match(&normalized, &grammar.hard_phrases) {
        return Some(PinIntent {
            kind: PinIntentKind::Hard,
            matched_phrase: matched.to_string(),
        });
    }
    if let Some(matched) = first_match(&normalized, &grammar.unpin_phrases) {
        return Some(PinIntent {
            kind: PinIntentKind::Unpin,
            matched_phrase: matched.to_string(),
        });
    }
    if let Some(matched) = first_match(&normalized, &grammar.soft_phrases) {
        return Some(PinIntent {
            kind: PinIntentKind::Soft,
            matched_phrase: matched.to_string(),
        });
    }
    None
}

fn first_match<'a>(haystack: &str, needles: &[&'a str]) -> Option<&'a str> {
    needles.iter().find(|n| haystack.contains(*n)).copied()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn classify(text: &str) -> Option<PinIntentKind> {
        classify_pin_intent(text).map(|i| i.kind)
    }

    // --- Soft pin grammar ---

    #[test]
    fn soft_pin_remember_this() {
        assert_eq!(classify("Remember this for the project."), Some(PinIntentKind::Soft));
    }

    #[test]
    fn soft_pin_pin_this() {
        assert_eq!(classify("Pin this fact please"), Some(PinIntentKind::Soft));
    }

    #[test]
    fn soft_pin_remember_that_i() {
        assert_eq!(classify("Remember that I use Postgres"), Some(PinIntentKind::Soft));
    }

    #[test]
    fn soft_pin_save_that_for_later() {
        assert_eq!(classify("Save that for later"), Some(PinIntentKind::Soft));
    }

    #[test]
    fn soft_pin_dont_forget_i_apostrophe() {
        assert_eq!(classify("Don't forget I prefer Vim"), Some(PinIntentKind::Soft));
    }

    #[test]
    fn soft_pin_dont_forget_no_apostrophe() {
        assert_eq!(classify("dont forget i use vim"), Some(PinIntentKind::Soft));
    }

    // --- Hard pin grammar ---

    #[test]
    fn hard_pin_always_remember() {
        assert_eq!(classify("Always remember I am allergic to peanuts"), Some(PinIntentKind::Hard));
    }

    #[test]
    fn hard_pin_never_forget() {
        assert_eq!(classify("Never forget I live in Lisbon"), Some(PinIntentKind::Hard));
    }

    #[test]
    fn hard_pin_permanent_rule() {
        assert_eq!(classify("This is a permanent rule: always use https"), Some(PinIntentKind::Hard));
    }

    #[test]
    fn hard_pin_rule_of_thumb_colon() {
        assert_eq!(classify("Rule of thumb: never commit secrets"), Some(PinIntentKind::Hard));
    }

    #[test]
    fn hard_outranks_soft_when_both_present() {
        // "always remember i" is hard; the sentence also contains "pin this" (soft).
        // Priority order MUST surface hard.
        let text = "Always remember I prefer Vim, and pin this too";
        assert_eq!(classify(text), Some(PinIntentKind::Hard));
    }

    // --- Unpin grammar ---

    #[test]
    fn unpin_unpin_that() {
        assert_eq!(classify("Unpin that fact"), Some(PinIntentKind::Unpin));
    }

    #[test]
    fn unpin_no_longer_pin() {
        assert_eq!(classify("Please no longer pin my Vim preference"), Some(PinIntentKind::Unpin));
    }

    #[test]
    fn unpin_remove_pin_from() {
        assert_eq!(classify("Remove pin from that fact"), Some(PinIntentKind::Unpin));
    }

    #[test]
    fn unpin_changed_my_mind() {
        assert_eq!(classify("I changed my mind about Vim, use VS Code"), Some(PinIntentKind::Unpin));
    }

    // --- Tier swap grammar ---

    #[test]
    fn tier_swap_to_hard() {
        assert_eq!(classify("Make that a hard pin"), Some(PinIntentKind::TierSwapToHard));
    }

    #[test]
    fn tier_swap_to_soft() {
        assert_eq!(classify("Downgrade that pin to soft"), Some(PinIntentKind::TierSwapToSoft));
    }

    #[test]
    fn tier_swap_outranks_hard_pin_keyword() {
        // "Make that a hard pin" contains "hard pin" but should classify as
        // TierSwapToHard (more specific intent — promoting an existing pin)
        // rather than Hard (creating a new permanent pin).
        let text = "make that a hard pin";
        assert_eq!(classify(text), Some(PinIntentKind::TierSwapToHard));
    }

    // --- Negatives + edge cases ---

    #[test]
    fn no_pin_intent_plain_chat() {
        assert_eq!(classify("hello how are you"), None);
    }

    #[test]
    fn empty_string() {
        assert_eq!(classify(""), None);
    }

    #[test]
    fn whitespace_only() {
        assert_eq!(classify("   \t\n  "), None);
    }

    #[test]
    fn casing_insensitive() {
        assert_eq!(classify("REMEMBER THIS"), Some(PinIntentKind::Soft));
        assert_eq!(classify("AlWaYs RemEmBer I am here"), Some(PinIntentKind::Hard));
    }

    #[test]
    fn matched_phrase_is_returned() {
        let intent = classify_pin_intent("Pin this fact").unwrap();
        assert_eq!(intent.kind, PinIntentKind::Soft);
        assert_eq!(intent.matched_phrase, "pin this");
    }

    #[test]
    fn custom_grammar_overrides_defaults() {
        let custom = PinIntentGrammar {
            soft_phrases: vec!["lock this in soft"],
            hard_phrases: vec!["lock this in hard"],
            unpin_phrases: vec![],
            tier_swap_to_hard_phrases: vec![],
            tier_swap_to_soft_phrases: vec![],
        };
        // Default-only phrase no longer matches
        assert_eq!(classify_pin_intent_with("remember this", &custom), None);
        // Custom phrase matches
        let intent = classify_pin_intent_with("Please lock this in soft", &custom).unwrap();
        assert_eq!(intent.kind, PinIntentKind::Soft);
        assert_eq!(intent.matched_phrase, "lock this in soft");
    }

    // --- Serde round-trip ---

    #[test]
    fn pin_intent_serde_roundtrip() {
        let i = PinIntent {
            kind: PinIntentKind::Soft,
            matched_phrase: "pin this".to_string(),
        };
        let json = serde_json::to_string(&i).unwrap();
        let back: PinIntent = serde_json::from_str(&json).unwrap();
        assert_eq!(i, back);
    }

    #[test]
    fn pin_intent_kind_lowercase_json() {
        let json = serde_json::to_string(&PinIntentKind::TierSwapToHard).unwrap();
        assert_eq!(json, "\"tier_swap_to_hard\"");
    }
}
