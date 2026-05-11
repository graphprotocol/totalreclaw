//! Secret detection — regex scanner that identifies API keys and tokens in text.
//!
//! Scans arbitrary text (tool outputs, conversation snippets, code) for
//! common API-key and token shapes. Never stores the raw secret value —
//! only the last 4 characters and surrounding context (with the match
//! replaced by a redaction marker) are surfaced to callers.
//!
//! # Usage
//!
//! ```rust
//! use totalreclaw_core::secrets::{scan_for_secrets, redact_secrets, SECRET_CLAIM_SUBTYPE};
//!
//! let text = "Set ANTHROPIC_API_KEY=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA in your env";
//! let found = scan_for_secrets(text);
//! assert_eq!(found[0].kind.as_str(), "anthropic_api_key");
//! assert_eq!(found[0].last_4.len(), 4);
//!
//! // When building a claim for a detected secret, use this subtype:
//! let _ = SECRET_CLAIM_SUBTYPE; // "secret"
//! ```

use regex::Regex;
use std::sync::OnceLock;

/// Number of characters of context captured before and after each match.
const CONTEXT_WINDOW: usize = 30;

/// Wire `code_subtype` value for claims that record a detected secret.
///
/// Callers that persist detection results as memory claims should set the
/// `code_subtype` field (or equivalent metadata key) to this value so that
/// retrieval surfaces can filter / display them uniformly.
pub const SECRET_CLAIM_SUBTYPE: &str = "secret";

// ---------------------------------------------------------------------------
// SecretKind
// ---------------------------------------------------------------------------

/// The variety of secret matched by the scanner (one entry per pattern).
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SecretKind {
    AnthropicApiKey,
    OpenAiProjectKey,
    OpenAiClassicKey,
    GitHubClassicPat,
    GitHubUserToken,
    GitHubServerToken,
    GitHubFinegrainedPat,
    AwsAccessKeyId,
    JsonWebToken,
    GoogleApiKey,
    SlackToken,
    StripeKey,
    SendGridKey,
    HuggingFaceToken,
}

impl SecretKind {
    /// Stable kebab-string identifier used in redaction markers and claim metadata.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AnthropicApiKey => "anthropic_api_key",
            Self::OpenAiProjectKey => "openai_project_key",
            Self::OpenAiClassicKey => "openai_classic_key",
            Self::GitHubClassicPat => "github_classic_pat",
            Self::GitHubUserToken => "github_user_token",
            Self::GitHubServerToken => "github_server_token",
            Self::GitHubFinegrainedPat => "github_finegrained_pat",
            Self::AwsAccessKeyId => "aws_access_key_id",
            Self::JsonWebToken => "json_web_token",
            Self::GoogleApiKey => "google_api_key",
            Self::SlackToken => "slack_token",
            Self::StripeKey => "stripe_key",
            Self::SendGridKey => "sendgrid_key",
            Self::HuggingFaceToken => "huggingface_token",
        }
    }
}

// ---------------------------------------------------------------------------
// DetectedSecret
// ---------------------------------------------------------------------------

/// A secret found in scanned text.
///
/// The raw secret value is NEVER included. Only `last_4` (the final 4
/// characters of the matched value) and `observed_context` (up to
/// [`CONTEXT_WINDOW`] chars on each side of the match, with the secret
/// replaced by `[REDACTED:<kind>]`) are stored.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct DetectedSecret {
    /// The type of secret detected.
    pub kind: SecretKind,
    /// Last 4 characters of the matched secret value.
    pub last_4: String,
    /// Surrounding context with the secret replaced by a redaction marker.
    pub observed_context: String,
}

// ---------------------------------------------------------------------------
// Pattern table (compiled once, reused across calls)
// ---------------------------------------------------------------------------

struct Pattern {
    kind: SecretKind,
    re: Regex,
}

static PATTERNS: OnceLock<Vec<Pattern>> = OnceLock::new();

fn patterns() -> &'static [Pattern] {
    PATTERNS.get_or_init(|| {
        // Ordered from most-specific to least-specific to avoid the
        // `sk-ant-` prefix being swallowed by the generic `sk-` pattern.
        [
            // 1. Anthropic — sk-ant- prefix + dashes/underscores
            (SecretKind::AnthropicApiKey,      r"sk-ant-[A-Za-z0-9\-_]{20,}"),
            // 2. OpenAI project key — sk-proj-
            (SecretKind::OpenAiProjectKey,     r"sk-proj-[A-Za-z0-9\-_]{50,}"),
            // 3. OpenAI classic key — sk- + exactly 48 alphanumeric (no dashes)
            (SecretKind::OpenAiClassicKey,     r"sk-[A-Za-z0-9]{48}"),
            // 4. GitHub classic PAT (ghp_)
            (SecretKind::GitHubClassicPat,     r"ghp_[A-Za-z0-9]{36}"),
            // 5. GitHub user-to-server token (ghu_)
            (SecretKind::GitHubUserToken,      r"ghu_[A-Za-z0-9]{36}"),
            // 6. GitHub server-to-server token (ghs_)
            (SecretKind::GitHubServerToken,    r"ghs_[A-Za-z0-9]{36}"),
            // 7. GitHub fine-grained PAT
            (SecretKind::GitHubFinegrainedPat, r"github_pat_[A-Za-z0-9_]{22,}"),
            // 8. AWS Access Key ID (standard prefixes: AKIA, AGPA, AIDA, AROA, etc.)
            (SecretKind::AwsAccessKeyId,       r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}"),
            // 9. JSON Web Token (two base64url header.payload segments + signature)
            (SecretKind::JsonWebToken,         r"eyJ[A-Za-z0-9\-_=]{4,}\.eyJ[A-Za-z0-9\-_=]{4,}\.[A-Za-z0-9\-_.+/=]*"),
            // 10. Google API key
            (SecretKind::GoogleApiKey,         r"AIza[0-9A-Za-z\-_]{35}"),
            // 11. Slack token (bot, user, app, refresh)
            (SecretKind::SlackToken,           r"xox[bpas]-[0-9A-Za-z]{8,}-[0-9A-Za-z]{8,}(?:-[0-9A-Za-z]{24,})?"),
            // 12. Stripe live or test key
            (SecretKind::StripeKey,            r"(?:sk|pk)_(?:live|test)_[0-9a-zA-Z]{24,}"),
            // 13. SendGrid API key
            (SecretKind::SendGridKey,          r"SG\.[a-zA-Z0-9\-_]{22,68}"),
            // 14. Hugging Face token
            (SecretKind::HuggingFaceToken,     r"hf_[A-Za-z0-9]{30,}"),
        ]
        .into_iter()
        .map(|(kind, pat)| Pattern {
            kind,
            re: Regex::new(pat).expect("static secret regex must compile"),
        })
        .collect()
    })
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Return the largest byte position ≤ `pos` that is a valid UTF-8 char boundary.
fn floor_char_boundary(text: &str, pos: usize) -> usize {
    let clamped = pos.min(text.len());
    (0..=clamped)
        .rev()
        .find(|&i| text.is_char_boundary(i))
        .unwrap_or(0)
}

/// Return the smallest byte position ≥ `pos` that is a valid UTF-8 char boundary.
fn ceil_char_boundary(text: &str, pos: usize) -> usize {
    let clamped = pos.min(text.len());
    (clamped..=text.len())
        .find(|&i| text.is_char_boundary(i))
        .unwrap_or(text.len())
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Scan `text` for secrets. Returns one [`DetectedSecret`] per match found.
///
/// Patterns are evaluated in priority order (most-specific first). Overlapping
/// matches from different patterns are all reported; deduplication is the
/// caller's responsibility if needed. The raw secret value is never included
/// in any returned field.
pub fn scan_for_secrets(text: &str) -> Vec<DetectedSecret> {
    let mut found = Vec::new();

    for pat in patterns() {
        for m in pat.re.find_iter(text) {
            let val = m.as_str();

            let last_4 = if val.len() >= 4 {
                val[val.len() - 4..].to_string()
            } else {
                val.to_string()
            };

            // Context window — snap to valid UTF-8 char boundaries.
            let ctx_start = floor_char_boundary(text, m.start().saturating_sub(CONTEXT_WINDOW));
            let ctx_end = ceil_char_boundary(text, m.end() + CONTEXT_WINDOW);

            let prefix = &text[ctx_start..m.start()];
            let suffix = &text[m.end()..ctx_end];
            let observed_context =
                format!("{}[REDACTED:{}]{}", prefix, pat.kind.as_str(), suffix);

            found.push(DetectedSecret {
                kind: pat.kind,
                last_4,
                observed_context,
            });
        }
    }

    found
}

/// Return a copy of `text` with every detected secret replaced by `[REDACTED:<kind>]`.
///
/// When two patterns would match overlapping regions, the first match (by start
/// position) takes precedence and the overlapping region is not re-processed.
pub fn redact_secrets(text: &str) -> String {
    // Collect all (start, end, kind) matches across all patterns.
    let mut matches: Vec<(usize, usize, SecretKind)> = patterns()
        .iter()
        .flat_map(|p| {
            p.re.find_iter(text)
                .map(|m| (m.start(), m.end(), p.kind))
                .collect::<Vec<_>>()
        })
        .collect();

    // Sort ascending by start; drop overlapping (keep the first).
    matches.sort_by_key(|&(s, _, _)| s);
    let mut deduped: Vec<(usize, usize, SecretKind)> = Vec::new();
    for m in matches {
        if let Some(&(_, prev_end, _)) = deduped.last() {
            if m.0 < prev_end {
                continue; // overlapping — skip
            }
        }
        deduped.push(m);
    }

    // Replace in reverse order so earlier byte offsets stay valid.
    let mut result = text.to_string();
    for (start, end, kind) in deduped.into_iter().rev() {
        result.replace_range(start..end, &format!("[REDACTED:{}]", kind.as_str()));
    }

    result
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // ── helpers ─────────────────────────────────────────────────────────────

    fn first(text: &str, kind: SecretKind) -> Option<DetectedSecret> {
        scan_for_secrets(text)
            .into_iter()
            .find(|d| d.kind == kind)
    }

    fn detects(text: &str, kind: SecretKind) -> bool {
        first(text, kind).is_some()
    }

    // ── per-pattern corpus ───────────────────────────────────────────────────

    #[test]
    fn detects_anthropic_key() {
        let key = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-BBBBBBBBAAAAAAAAAAA";
        assert!(detects(&format!("key={key}"), SecretKind::AnthropicApiKey));
    }

    #[test]
    fn detects_openai_project_key() {
        let key = format!("sk-proj-{}", "A".repeat(80));
        assert!(detects(&key, SecretKind::OpenAiProjectKey));
    }

    #[test]
    fn detects_openai_classic_key() {
        // Exactly 48 alphanumeric after "sk-"
        let key = format!("sk-{}", "T3stK3yABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ABCDE");
        assert_eq!(key[3..].len(), 48, "fixture must be exactly 48 chars after sk-");
        assert!(detects(&key, SecretKind::OpenAiClassicKey));
    }

    #[test]
    fn detects_github_classic_pat() {
        let key = format!("ghp_{}", "A".repeat(36));
        assert!(detects(&key, SecretKind::GitHubClassicPat));
    }

    #[test]
    fn detects_github_user_token() {
        let key = format!("ghu_{}", "B".repeat(36));
        assert!(detects(&key, SecretKind::GitHubUserToken));
    }

    #[test]
    fn detects_github_server_token() {
        let key = format!("ghs_{}", "C".repeat(36));
        assert!(detects(&key, SecretKind::GitHubServerToken));
    }

    #[test]
    fn detects_github_finegrained_pat() {
        let key = format!("github_pat_{}", "A".repeat(40));
        assert!(detects(&key, SecretKind::GitHubFinegrainedPat));
    }

    #[test]
    fn detects_aws_access_key_id() {
        // Standard format: AKIA + 16 uppercase alphanumeric
        let key = "AKIAIOSFODNN7EXAMPLE";
        assert!(detects(key, SecretKind::AwsAccessKeyId));
    }

    #[test]
    fn detects_jwt() {
        // Minimal realistic JWT (header.payload.signature, each base64url-encoded)
        let jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.\
                   eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.\
                   SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c";
        assert!(detects(jwt, SecretKind::JsonWebToken));
    }

    #[test]
    fn detects_google_api_key() {
        let key = format!("AIza{}", "A".repeat(35));
        assert!(detects(&key, SecretKind::GoogleApiKey));
    }

    #[test]
    fn detects_slack_token() {
        // Constructed at runtime so the string literal doesn't trip push-protection scanners.
        let key = format!("xox{}-{}-{}-{}", "b", "12345678901", "12345678901", "aBcDeFgHiJkLmNoPqRsTuVwX");
        assert!(detects(&key, SecretKind::SlackToken));
    }

    #[test]
    fn detects_stripe_live_key() {
        let key = format!("sk_live_{}", "A".repeat(24));
        assert!(detects(&key, SecretKind::StripeKey));
    }

    #[test]
    fn detects_stripe_test_key() {
        let key = format!("pk_test_{}", "B".repeat(24));
        assert!(detects(&key, SecretKind::StripeKey));
    }

    #[test]
    fn detects_sendgrid_key() {
        let key = format!("SG.{}.{}", "A".repeat(22), "B".repeat(43));
        assert!(detects(&key, SecretKind::SendGridKey));
    }

    #[test]
    fn detects_huggingface_token() {
        let key = format!("hf_{}", "A".repeat(34));
        assert!(detects(&key, SecretKind::HuggingFaceToken));
    }

    // ── structural invariants ────────────────────────────────────────────────

    #[test]
    fn no_match_on_clean_text() {
        let texts = [
            "Hello, world!",
            "My favourite colour is blue.",
            "The quick brown fox jumps over the lazy dog.",
            "sk-short",     // too short
            "ghp_short",    // too short
            "AKIAEXAMPLE",  // too short
        ];
        for t in texts {
            assert!(
                scan_for_secrets(t).is_empty(),
                "expected no secrets in {t:?}"
            );
        }
    }

    #[test]
    fn last_4_is_correct() {
        let key = format!("ghp_{}", "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890");
        let found = first(&key, SecretKind::GitHubClassicPat).expect("should detect");
        // Last 4 chars of the 40-char key
        assert_eq!(found.last_4, &key[key.len() - 4..]);
    }

    #[test]
    fn observed_context_contains_redaction_marker() {
        let key = format!("TOKEN=ghp_{} end", "A".repeat(36));
        let found = first(&key, SecretKind::GitHubClassicPat).expect("should detect");
        assert!(
            found.observed_context.contains("[REDACTED:github_classic_pat]"),
            "context should contain redaction marker, got: {}",
            found.observed_context
        );
        assert!(found.observed_context.contains("TOKEN="));
        assert!(found.observed_context.contains("end"));
    }

    #[test]
    fn raw_secret_absent_from_observed_context() {
        let raw = format!("ghp_{}", "X".repeat(36));
        let text = format!("key={raw} done");
        let found = first(&text, SecretKind::GitHubClassicPat).expect("should detect");
        assert!(
            !found.observed_context.contains(&raw),
            "raw secret must not appear in observed_context"
        );
    }

    #[test]
    fn redact_secrets_replaces_all() {
        let key1 = format!("ghp_{}", "A".repeat(36));
        let key2 = format!("AKIAIOSFODNN7EXAMPLE");
        let text = format!("first={key1} second={key2}");
        let redacted = redact_secrets(&text);
        assert!(!redacted.contains(&key1));
        assert!(!redacted.contains(&key2));
        assert!(redacted.contains("[REDACTED:github_classic_pat]"));
        assert!(redacted.contains("[REDACTED:aws_access_key_id]"));
    }

    #[test]
    fn redact_secrets_preserves_surrounding_text() {
        let key = format!("ghp_{}", "B".repeat(36));
        let text = format!("before={key} after");
        let redacted = redact_secrets(&text);
        assert!(redacted.contains("before="));
        assert!(redacted.contains(" after"));
        assert!(!redacted.contains(&key));
    }

    #[test]
    fn anthropic_key_not_matched_by_openai_classic_pattern() {
        // An Anthropic key contains dashes, so it can never match the OpenAI
        // classic pattern (which requires exactly 48 alphanumeric chars, no dashes).
        let key = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-BBBBBBBBaaaaaa";
        let found = scan_for_secrets(key);
        let kinds: Vec<_> = found.iter().map(|d| d.kind).collect();
        assert!(
            kinds.contains(&SecretKind::AnthropicApiKey),
            "should detect as Anthropic"
        );
        assert!(
            !kinds.contains(&SecretKind::OpenAiClassicKey),
            "must not match OpenAI classic pattern"
        );
    }

    #[test]
    fn secret_claim_subtype_constant() {
        assert_eq!(SECRET_CLAIM_SUBTYPE, "secret");
    }
}
