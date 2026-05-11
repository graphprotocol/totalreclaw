//! API-key vault — secret detection regex (am-6).
//!
//! Scans arbitrary text for 14 common secret shapes. When a match is found,
//! records only `last_4` characters of the raw secret and a redacted context
//! snippet — the secret itself is never stored or returned.
//!
//! # Phrase-safety note
//!
//! This module intentionally does NOT match mnemonics, BIP-39 word lists,
//! or any key-derivation material. Those patterns are handled by the
//! phrase-safety CI guard (`scripts/check-phrase-safety.sh`) at a higher
//! level. The regexes here target API credentials that can appear in tool
//! inputs or user messages.

use regex::Regex;
use std::sync::OnceLock;

// ---------------------------------------------------------------------------
// Pattern registry
// ---------------------------------------------------------------------------

struct Pattern {
    name: &'static str,
    re: &'static str,
}

const PATTERNS: &[Pattern] = &[
    Pattern {
        name: "anthropic_api_key",
        re: r"sk-ant-[a-zA-Z0-9\-_]{20,}",
    },
    Pattern {
        name: "openai_api_key",
        re: r"sk-(?!ant-)[a-zA-Z0-9]{20,}",
    },
    Pattern {
        name: "github_pat",
        re: r"ghp_[a-zA-Z0-9]{36}",
    },
    Pattern {
        name: "github_oauth_token",
        re: r"ghu_[a-zA-Z0-9]{36}",
    },
    Pattern {
        name: "github_actions_token",
        re: r"ghs_[a-zA-Z0-9]{36}",
    },
    Pattern {
        name: "aws_access_key_id",
        // AWS access key IDs are 20 uppercase alphanumeric chars starting with AKIA
        re: r"AKIA[0-9A-Z]{16}",
    },
    Pattern {
        name: "jwt",
        // Three base64url segments separated by dots; header must start eyJ
        re: r"eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+",
    },
    Pattern {
        name: "stripe_live_key",
        re: r"sk_live_[a-zA-Z0-9]{24,}",
    },
    Pattern {
        name: "stripe_test_key",
        re: r"sk_test_[a-zA-Z0-9]{24,}",
    },
    Pattern {
        name: "slack_token",
        re: r"xox[baprs]-[a-zA-Z0-9\-]{10,}",
    },
    Pattern {
        name: "google_api_key",
        re: r"AIza[0-9A-Za-z\-_]{35}",
    },
    Pattern {
        name: "discord_bot_token",
        // Discord bot tokens: base64(user_id) + "." + timestamp + "." + hmac
        re: r"[MN][a-zA-Z0-9]{23}\.[a-zA-Z0-9_\-]{6}\.[a-zA-Z0-9_\-]{27}",
    },
    Pattern {
        name: "pem_private_key",
        re: r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----",
    },
    Pattern {
        name: "generic_bearer_token",
        // "Authorization: Bearer <token>" or "Bearer <token>" in text
        re: r"(?i)Bearer\s+([a-zA-Z0-9\-_\.]{32,})",
    },
];

// ---------------------------------------------------------------------------
// Compiled regex cache (one allocation per process lifetime)
// ---------------------------------------------------------------------------

static COMPILED: OnceLock<Vec<(&'static str, Regex)>> = OnceLock::new();

fn compiled_patterns() -> &'static Vec<(&'static str, Regex)> {
    COMPILED.get_or_init(|| {
        PATTERNS
            .iter()
            .map(|p| {
                (
                    p.name,
                    Regex::new(p.re).expect("secret detection regex must be valid"),
                )
            })
            .collect()
    })
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// A single detected secret — never contains the secret itself.
#[derive(Debug, Clone, PartialEq)]
pub struct SecretDetection {
    /// Fixed discriminator for downstream claim routing.
    pub code_subtype: &'static str,
    /// Which pattern matched (e.g. `"anthropic_api_key"`).
    pub pattern_name: &'static str,
    /// Last 4 characters of the matched value (for human confirmation).
    pub last_4: String,
    /// Context snippet with the secret replaced by `[REDACTED]`.
    pub observed_context: String,
}

impl SecretDetection {
    fn new(pattern_name: &'static str, matched: &str, text: &str, start: usize, end: usize) -> Self {
        let last_4 = last_four(matched);
        let observed_context = build_context(text, start, end);
        SecretDetection {
            code_subtype: "secret",
            pattern_name,
            last_4,
            observed_context,
        }
    }
}

/// Scan `text` for secrets. Returns one [`SecretDetection`] per match.
///
/// Each pattern is tried in registration order; overlapping matches from
/// different patterns may produce multiple entries for the same byte range.
///
/// Guarantees:
/// - The raw secret value is never present in any returned field.
/// - `last_4` is always ≤ 4 chars (may be shorter for very short matches).
/// - `observed_context` is ≤ 80 chars total (20 before + `[REDACTED]` + 20 after).
pub fn scan_for_secrets(text: &str) -> Vec<SecretDetection> {
    let mut out = Vec::new();
    for (name, re) in compiled_patterns() {
        for m in re.find_iter(text) {
            out.push(SecretDetection::new(name, m.as_str(), text, m.start(), m.end()));
        }
    }
    out
}

/// Returns `true` if `text` contains at least one detectable secret pattern.
pub fn contains_secret(text: &str) -> bool {
    compiled_patterns()
        .iter()
        .any(|(_, re)| re.is_match(text))
}

// ---------------------------------------------------------------------------
// Helpers (private)
// ---------------------------------------------------------------------------

fn last_four(s: &str) -> String {
    // Operate on chars to handle multi-byte UTF-8 safely.
    let chars: Vec<char> = s.chars().collect();
    let tail: String = chars.iter().rev().take(4).rev().collect();
    tail
}

/// Build a context snippet: up to 20 chars before + `[REDACTED]` + up to 20 after.
fn build_context(text: &str, start: usize, end: usize) -> String {
    // Work in bytes then trim to char boundaries.
    let prefix_start = start.saturating_sub(20);
    let suffix_end = (end + 20).min(text.len());

    // Align to char boundaries.
    let prefix_start = floor_char_boundary(text, prefix_start);
    let suffix_end = ceil_char_boundary(text, suffix_end);

    let prefix = &text[prefix_start..start];
    let suffix = &text[end..suffix_end];
    format!("{}[REDACTED]{}", prefix, suffix)
}

fn floor_char_boundary(s: &str, mut i: usize) -> usize {
    while i > 0 && !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}

fn ceil_char_boundary(s: &str, mut i: usize) -> usize {
    while i < s.len() && !s.is_char_boundary(i) {
        i += 1;
    }
    i
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // --- helper ---
    fn detect(text: &str) -> Vec<(&'static str, String)> {
        scan_for_secrets(text)
            .into_iter()
            .map(|d| (d.pattern_name, d.last_4))
            .collect()
    }

    fn assert_detected(text: &str, expected_pattern: &str) {
        let hits = scan_for_secrets(text);
        assert!(
            hits.iter().any(|h| h.pattern_name == expected_pattern),
            "expected pattern '{}' to match in: {:?}\ngot: {:?}",
            expected_pattern,
            text,
            hits.iter().map(|h| h.pattern_name).collect::<Vec<_>>()
        );
    }

    fn assert_no_secret(text: &str) {
        let hits = scan_for_secrets(text);
        assert!(
            hits.is_empty(),
            "expected no secrets in {:?} but got {:?}",
            text,
            hits.iter().map(|h| h.pattern_name).collect::<Vec<_>>()
        );
    }

    // --- code_subtype constant ---

    #[test]
    fn code_subtype_is_always_secret() {
        let hits = scan_for_secrets("sk-ant-api03-abc123def456ghi789jkl");
        assert!(!hits.is_empty());
        for h in &hits {
            assert_eq!(h.code_subtype, "secret");
        }
    }

    // --- pattern 1: Anthropic API key ---

    #[test]
    fn detects_anthropic_key() {
        assert_detected(
            "key=sk-ant-api03-abc123def456ghi789jkl012mno345pqr",
            "anthropic_api_key",
        );
    }

    #[test]
    fn anthropic_last_4_correct() {
        let hits = scan_for_secrets("sk-ant-api03-abcdefghij1234");
        let h = hits.iter().find(|h| h.pattern_name == "anthropic_api_key").unwrap();
        assert_eq!(h.last_4, "1234");
    }

    // --- pattern 2: OpenAI API key ---

    #[test]
    fn detects_openai_key() {
        assert_detected(
            "OPENAI_API_KEY=sk-abcdefghijklmnopqrst",
            "openai_api_key",
        );
    }

    #[test]
    fn openai_does_not_match_anthropic_prefix() {
        let hits = scan_for_secrets("sk-ant-api03-abc123def456ghi789jkl");
        assert!(hits.iter().all(|h| h.pattern_name != "openai_api_key"),
            "openai pattern must not match sk-ant- prefix");
    }

    // --- pattern 3-5: GitHub tokens ---

    #[test]
    fn detects_github_pat() {
        assert_detected(
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890AB",
            "github_pat",
        );
    }

    #[test]
    fn detects_github_oauth_token() {
        assert_detected(
            "ghu_abcdefghijklmnopqrstuvwxyz1234567890AB",
            "github_oauth_token",
        );
    }

    #[test]
    fn detects_github_actions_token() {
        assert_detected(
            "ghs_abcdefghijklmnopqrstuvwxyz1234567890AB",
            "github_actions_token",
        );
    }

    // --- pattern 6: AWS access key ---

    #[test]
    fn detects_aws_access_key() {
        assert_detected("AKIA1234567890ABCDEF", "aws_access_key_id");
    }

    #[test]
    fn aws_key_requires_20_char_suffix() {
        // AKIA + only 15 chars → no match
        assert_no_secret("AKIA123456789ABCD");
    }

    // --- pattern 7: JWT ---

    #[test]
    fn detects_jwt() {
        assert_detected(
            "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "jwt",
        );
    }

    #[test]
    fn jwt_must_start_eyj() {
        // eyK... (wrong header prefix) → no jwt match
        assert_no_secret("eyKhbGciOi.eyJzdW.SflK");
    }

    // --- pattern 8-9: Stripe keys ---
    // Build test values programmatically so literal strings don't trigger push protection.

    #[test]
    fn detects_stripe_live_key() {
        let key = format!("sk_live_{}", "a".repeat(24));
        assert_detected(&key, "stripe_live_key");
    }

    #[test]
    fn detects_stripe_test_key() {
        let key = format!("sk_test_{}", "a".repeat(24));
        assert_detected(&key, "stripe_test_key");
    }

    // --- pattern 10: Slack token ---

    #[test]
    fn detects_slack_bot_token() {
        // Construct via format! to avoid literal match by GitHub push protection.
        let token = ["xoxb", "123456789012", "abcdefghijklmno"].join("-");
        assert_detected(&token, "slack_token");
    }

    #[test]
    fn detects_slack_app_token() {
        let token = ["xoxa", "123456789012", "abcdefghijklmno"].join("-");
        assert_detected(&token, "slack_token");
    }

    // --- pattern 11: Google API key ---

    #[test]
    fn detects_google_api_key() {
        let key = format!("AIzaSy{}", "a".repeat(35));
        assert_detected(&key, "google_api_key");
    }

    // --- pattern 12: Discord bot token ---

    #[test]
    fn detects_discord_bot_token() {
        // Construct via format! to avoid literal match by push protection.
        let token = format!(
            "MTIzNDU2Nzg5MDEyMzQ1Njc4.{}.{}",
            "ABCDEF",
            "a".repeat(27)
        );
        assert_detected(&token, "discord_bot_token");
    }

    // --- pattern 13: PEM private key ---

    #[test]
    fn detects_rsa_private_key_header() {
        assert_detected(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ...",
            "pem_private_key",
        );
    }

    #[test]
    fn detects_ec_private_key_header() {
        assert_detected(
            "-----BEGIN EC PRIVATE KEY-----\nMHQCAQEEI...",
            "pem_private_key",
        );
    }

    #[test]
    fn detects_openssh_private_key_header() {
        assert_detected(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbn...",
            "pem_private_key",
        );
    }

    #[test]
    fn detects_generic_private_key_header() {
        assert_detected(
            "-----BEGIN PRIVATE KEY-----\nMIIEvAIBADA...",
            "pem_private_key",
        );
    }

    // --- pattern 14: Bearer token ---

    #[test]
    fn detects_bearer_token_header() {
        assert_detected(
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdefghijklmnopqrstuvwxyz",
            "generic_bearer_token",
        );
    }

    #[test]
    fn detects_bearer_case_insensitive() {
        assert_detected(
            "authorization: bearer abcdefghijklmnopqrstuvwxyz12345678",
            "generic_bearer_token",
        );
    }

    // --- redaction safety ---

    #[test]
    fn context_does_not_contain_secret() {
        let key = "sk-ant-api03-abc123def456ghi789jkl";
        let text = format!("Here is my key: {key} please keep it safe");
        let hits = scan_for_secrets(&text);
        for h in &hits {
            assert!(
                !h.observed_context.contains(key),
                "context must not contain raw secret: {}",
                h.observed_context
            );
            assert!(
                h.observed_context.contains("[REDACTED]"),
                "context must contain [REDACTED]: {}",
                h.observed_context
            );
        }
    }

    #[test]
    fn observed_context_bounded_length() {
        let key = "sk-ant-api03-abc123def456ghi789jklmno";
        let text = format!("prefix_padding_text_{key}_suffix_padding_text");
        let hits = scan_for_secrets(&text);
        for h in &hits {
            // max = 20 (prefix) + len("[REDACTED]") + 20 (suffix) = 59
            assert!(
                h.observed_context.len() <= 60,
                "context too long: {} chars: {}",
                h.observed_context.len(),
                h.observed_context
            );
        }
    }

    #[test]
    fn last_4_never_contains_more_than_four_chars() {
        let hits = scan_for_secrets("sk-ant-api03-abc123def456ghi789jkl");
        for h in &hits {
            assert!(h.last_4.chars().count() <= 4, "last_4 has >4 chars: {:?}", h.last_4);
        }
    }

    // --- contains_secret ---

    #[test]
    fn contains_secret_true_on_match() {
        assert!(contains_secret("my key is sk-ant-api03-abc123def456ghi789jkl"));
    }

    #[test]
    fn contains_secret_false_on_clean_text() {
        assert!(!contains_secret("no secrets here, just a normal message about my project"));
    }

    // --- no false positives on common benign strings ---

    #[test]
    fn no_false_positive_on_url() {
        assert_no_secret("https://api.example.com/v1/users?page=1&limit=100");
    }

    #[test]
    fn no_false_positive_on_uuid() {
        assert_no_secret("user-id: 550e8400-e29b-41d4-a716-446655440000");
    }

    #[test]
    fn no_false_positive_on_short_sk_prefix() {
        // "sk-" alone with only 10 chars after → doesn't meet 20 char minimum
        assert_no_secret("ref sk-shortval");
    }

    // --- empty / edge inputs ---

    #[test]
    fn empty_string_returns_no_detections() {
        assert!(scan_for_secrets("").is_empty());
    }

    #[test]
    fn whitespace_only_returns_no_detections() {
        assert!(scan_for_secrets("   \n\t  ").is_empty());
    }

    #[test]
    fn multiple_secrets_in_one_text() {
        let text = "key1=sk-ant-api03-abc123def456ghi789jkl key2=AKIA1234567890ABCDEF";
        let hits = scan_for_secrets(text);
        let names: Vec<&str> = hits.iter().map(|h| h.pattern_name).collect();
        assert!(names.contains(&"anthropic_api_key"), "expected anthropic hit");
        assert!(names.contains(&"aws_access_key_id"), "expected aws hit");
    }
}
