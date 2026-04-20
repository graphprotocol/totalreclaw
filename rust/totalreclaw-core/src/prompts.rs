//! Canonical LLM prompts — single source of truth for all TotalReclaw clients.
//!
//! As of core 2.2.0 the canonical extraction + compaction system prompts live
//! here and are embedded at compile time via [`include_str!`]. TypeScript
//! (via WASM) and Python (via PyO3) consumers call the thin accessors in
//! [`crate::wasm`] / [`crate::python`] and get byte-identical bytes on every
//! platform.
//!
//! Why hoist:
//!   * Before 2.2.0, each client (`skill/plugin`, `python/agent/extraction.py`,
//!     `skill-nanoclaw`) kept its own copy. Prompt drift between clients
//!     produced real bugs — the 2026-04-18 v1 QA uncovered the NanoClaw
//!     BASE_SYSTEM_PROMPT diverging from the plugin/Python shape (no Rule 6
//!     meta-filter, `summary` mis-listed in the ADD output shape).
//!   * One source → identical behaviour across clients, and the spec
//!     (`docs/specs/totalreclaw/memory-taxonomy-v1.md`) can point at a single
//!     file.
//!
//! Canonical shape comes from the plugin / Python pipeline (Rule 6 meta-request
//! filter included — see `docs/notes/extraction-prompt-map.md`).

/// Canonical v1 merged-topic extraction system prompt.
///
/// Two-phase output (topics + facts). Emits v1 taxonomy types only.
/// Includes Rule 6 (product-meta request filter) that was introduced in
/// Python 2.0.2 / plugin 3.0.0 after the v1 QA surfaced spurious extraction
/// of product setup utterances as user preferences.
pub const EXTRACTION_SYSTEM_PROMPT: &str = include_str!("prompts/extraction.md");

/// Canonical v1 compaction system prompt.
///
/// Same two-phase shape as [`EXTRACTION_SYSTEM_PROMPT`] but tuned for
/// end-of-context compaction: importance floor 5 (not 6), explicit
/// format-agnostic-parsing section for bullet lists / headers / prose.
pub const COMPACTION_SYSTEM_PROMPT: &str = include_str!("prompts/compaction.md");

/// Return the canonical v1 extraction system prompt.
///
/// This is the function cross-client consumers should call so the embedded
/// contents are never accidentally duplicated into TS/Python source trees.
pub fn get_extraction_system_prompt() -> &'static str {
    EXTRACTION_SYSTEM_PROMPT
}

/// Return the canonical v1 compaction system prompt.
///
/// Mirrors [`get_extraction_system_prompt`] but for the end-of-context
/// compaction surface.
pub fn get_compaction_system_prompt() -> &'static str {
    COMPACTION_SYSTEM_PROMPT
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extraction_prompt_is_nonempty() {
        assert!(!EXTRACTION_SYSTEM_PROMPT.is_empty());
        assert!(EXTRACTION_SYSTEM_PROMPT.len() > 1000, "extraction prompt suspiciously short");
    }

    #[test]
    fn compaction_prompt_is_nonempty() {
        assert!(!COMPACTION_SYSTEM_PROMPT.is_empty());
        assert!(COMPACTION_SYSTEM_PROMPT.len() > 1000, "compaction prompt suspiciously short");
    }

    #[test]
    fn extraction_prompt_stable_across_calls() {
        // Embedded via include_str! — each call returns a &'static str
        // pointing at the same compile-time buffer.
        let a = get_extraction_system_prompt();
        let b = get_extraction_system_prompt();
        assert_eq!(a, b);
        assert_eq!(a.as_ptr(), b.as_ptr(), "prompt should be a single static buffer");
    }

    #[test]
    fn compaction_prompt_stable_across_calls() {
        let a = get_compaction_system_prompt();
        let b = get_compaction_system_prompt();
        assert_eq!(a, b);
        assert_eq!(a.as_ptr(), b.as_ptr());
    }

    #[test]
    fn extraction_prompt_mentions_v1_types() {
        // The canonical prompt must list all 6 v1 types.
        for t in &["claim", "preference", "directive", "commitment", "episode", "summary"] {
            assert!(
                EXTRACTION_SYSTEM_PROMPT.contains(t),
                "extraction prompt missing v1 type {:?}",
                t
            );
        }
    }

    #[test]
    fn extraction_prompt_has_merged_topic_shape() {
        // Two explicit phases + the merged output shape.
        assert!(EXTRACTION_SYSTEM_PROMPT.contains("PHASE 1"));
        assert!(EXTRACTION_SYSTEM_PROMPT.contains("PHASE 2"));
        assert!(EXTRACTION_SYSTEM_PROMPT.contains("\"topics\""));
        assert!(EXTRACTION_SYSTEM_PROMPT.contains("\"facts\""));
    }

    #[test]
    fn extraction_prompt_includes_rule_6_meta_filter() {
        // Canonical source includes the meta-request filter (Rule 6 in Python).
        assert!(
            EXTRACTION_SYSTEM_PROMPT.contains("META-request")
                || EXTRACTION_SYSTEM_PROMPT.contains("META-requests"),
            "extraction prompt missing Rule 6 meta-filter language"
        );
        assert!(EXTRACTION_SYSTEM_PROMPT.contains("TotalReclaw"));
    }

    #[test]
    fn compaction_prompt_admits_importance_5() {
        // Compaction floor is 5 (one below the default 6).
        assert!(
            COMPACTION_SYSTEM_PROMPT.contains("5+")
                || COMPACTION_SYSTEM_PROMPT.contains("5 "),
            "compaction prompt must mention importance floor 5"
        );
    }

    #[test]
    fn compaction_prompt_includes_rule_6_meta_filter() {
        assert!(
            COMPACTION_SYSTEM_PROMPT.contains("META-request")
                || COMPACTION_SYSTEM_PROMPT.contains("META-requests"),
            "compaction prompt missing Rule 6 meta-filter language"
        );
        assert!(COMPACTION_SYSTEM_PROMPT.contains("TotalReclaw"));
    }

    #[test]
    fn prompts_are_distinct() {
        // They share structure but the compaction prompt has the
        // "LAST CHANCE" framing + format-agnostic section.
        assert_ne!(EXTRACTION_SYSTEM_PROMPT, COMPACTION_SYSTEM_PROMPT);
        assert!(COMPACTION_SYSTEM_PROMPT.contains("LAST CHANCE"));
        assert!(COMPACTION_SYSTEM_PROMPT.contains("FORMAT-AGNOSTIC"));
    }
}
