//! PII / credential filter — regex + validator tier (PRD-04 F7, DEP-4).
//!
//! [`pii_filter`] classifies a candidate memory text before any client stores it:
//!
//! * [`PiiDecision::Allow`] — nothing sensitive found; store normally.
//! * [`PiiDecision::Block`] — a high-confidence secret or identifier (a
//!   validator passed, or a keyword names the value). Automatic extraction
//!   drops it; an explicit `totalreclaw_remember` returns block-and-tell
//!   (not stored, category + [`pii_block_message`]) unless the call carries
//!   `allow_sensitive: true`.
//! * [`PiiDecision::Flag`] — sensitive-shaped but unconfirmed (checksum failed,
//!   or no naming keyword). Automatic extraction drops it; an explicit
//!   remember stores it.
//!
//! Scope (PRD-04 DEP-4): credentials (the 14 [`crate::secrets`] classes, more
//! provider prefixes — GitHub fine-grained, GitLab, Google, npm, PyPI, Groq,
//! xAI, OpenRouter, Telegram bot — keyword-assigned keys/tokens, upper-case
//! env-var assignments, bearer/basic/`Authorization` headers, passwords, PINs,
//! URL credentials), payment cards (brand prefix + Luhn), IBAN (country
//! length + mod-97), government ids (PT NIF/CC, ES DNI/NIE, IT codice fiscale,
//! FR NIR, US SSN, BR CPF, keyword-named passport/licence/national/NINO/NHS/
//! Aadhaar numbers), private keys (64/128-hex, PEM, xprv, keyword-named
//! base58/WIF) and BIP-39 seed phrases. E-mail addresses, phone numbers and
//! names are deliberately NOT detected ("my doctor is Dr. X" is allowed).
//!
//! This is a regex tier: it catches the shapes in its corpus and misses
//! phrasings it has never seen. The extraction prompt's Rule 7 is the other
//! layer.
//!
//! Regex/validator only — no ML. Pure and deterministic, so Rust, WASM
//! (`piiFilter`) and PyO3 (`pii_filter`) return identical decisions
//! (corpus: `tests/parity/pii/corpus.json`). The matched text is never
//! returned — callers only ever see the category tag.

use std::sync::LazyLock;

use regex::Regex;

use crate::secrets::{self, SecretKind};

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/// What kind of sensitive value was found. Tags are stable wire values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PiiCategory {
    ApiKey,
    AuthToken,
    Password,
    PrivateKey,
    SeedPhrase,
    PaymentCard,
    BankAccount,
    GovernmentId,
}

impl PiiCategory {
    /// Every category, in evaluation order. When one text contains several
    /// sensitive values, the first category (in this order) that reaches
    /// `Block` wins; otherwise the first that reaches `Flag`.
    pub const ALL: [PiiCategory; 8] = [
        PiiCategory::PrivateKey,
        PiiCategory::SeedPhrase,
        PiiCategory::ApiKey,
        PiiCategory::AuthToken,
        PiiCategory::Password,
        PiiCategory::PaymentCard,
        PiiCategory::BankAccount,
        PiiCategory::GovernmentId,
    ];

    /// Stable snake_case tag (wire value in bindings, tool results and logs).
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ApiKey => "api_key",
            Self::AuthToken => "auth_token",
            Self::Password => "password",
            Self::PrivateKey => "private_key",
            Self::SeedPhrase => "seed_phrase",
            Self::PaymentCard => "payment_card",
            Self::BankAccount => "bank_account",
            Self::GovernmentId => "government_id",
        }
    }
}

impl std::fmt::Display for PiiCategory {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Outcome of [`pii_filter`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PiiDecision {
    Allow,
    Block { category: PiiCategory },
    Flag { category: PiiCategory },
}

impl PiiDecision {
    /// `"allow"`, `"block"` or `"flag"`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Block { .. } => "block",
            Self::Flag { .. } => "flag",
        }
    }

    /// The category for `Block`/`Flag`; `None` for `Allow`.
    pub fn category(&self) -> Option<PiiCategory> {
        match self {
            Self::Allow => None,
            Self::Block { category } | Self::Flag { category } => Some(*category),
        }
    }

    /// True only for `Allow` — the one decision automatic extraction stores.
    pub fn is_allow(&self) -> bool {
        matches!(self, Self::Allow)
    }

    /// Canonical JSON returned by the WASM (`piiFilter`) and PyO3
    /// (`pii_filter`) bindings. Fixed key order:
    /// `{"decision":"allow"}` or
    /// `{"decision":"block"|"flag","category":"<tag>","message":"<pii_block_message>"}`.
    pub fn to_json(&self) -> String {
        #[derive(serde::Serialize)]
        struct Wire {
            decision: &'static str,
            #[serde(skip_serializing_if = "Option::is_none")]
            category: Option<&'static str>,
            #[serde(skip_serializing_if = "Option::is_none")]
            message: Option<&'static str>,
        }
        let category = self.category();
        let wire = Wire {
            decision: self.kind(),
            category: category.map(PiiCategory::as_str),
            message: category.map(pii_block_message),
        };
        serde_json::to_string(&wire).expect("serializing three optional &str fields cannot fail")
    }
}

/// User-facing block-and-tell text for `category`. Word-identical across
/// every client because every client reads it from here.
pub fn pii_block_message(category: PiiCategory) -> &'static str {
    match category {
        PiiCategory::ApiKey => "Not saved: this looks like it contains an API key. TotalReclaw doesn't store secrets unless you confirm you want it saved.",
        PiiCategory::AuthToken => "Not saved: this looks like it contains an access token. TotalReclaw doesn't store secrets unless you confirm you want it saved.",
        PiiCategory::Password => "Not saved: this looks like it contains a password or PIN. TotalReclaw doesn't store secrets unless you confirm you want it saved.",
        PiiCategory::PrivateKey => "Not saved: this looks like it contains a private key. TotalReclaw doesn't store secrets unless you confirm you want it saved.",
        PiiCategory::SeedPhrase => "Not saved: this looks like it contains a wallet recovery phrase. TotalReclaw doesn't store secrets unless you confirm you want it saved.",
        PiiCategory::PaymentCard => "Not saved: this looks like it contains a payment card number. TotalReclaw doesn't store financial identifiers unless you confirm you want it saved.",
        PiiCategory::BankAccount => "Not saved: this looks like it contains a bank account number (IBAN). TotalReclaw doesn't store financial identifiers unless you confirm you want it saved.",
        PiiCategory::GovernmentId => "Not saved: this looks like it contains a government ID number. TotalReclaw doesn't store identity numbers unless you confirm you want it saved.",
    }
}

/// Classify `text`. Never panics, never allocates the matched value into the
/// result.
pub fn pii_filter(text: &str) -> PiiDecision {
    let mut first_flag: Option<PiiCategory> = None;
    for category in PiiCategory::ALL {
        match detect(category, text) {
            Level::Block => return PiiDecision::Block { category },
            Level::Flag => {
                if first_flag.is_none() {
                    first_flag = Some(category);
                }
            }
            Level::None => {}
        }
    }
    match first_flag {
        Some(category) => PiiDecision::Flag { category },
        None => PiiDecision::Allow,
    }
}

// ---------------------------------------------------------------------------
// Detector plumbing
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Level {
    None,
    Flag,
    Block,
}

fn detect(category: PiiCategory, text: &str) -> Level {
    match category {
        PiiCategory::PrivateKey => detect_private_key(text),
        PiiCategory::SeedPhrase => detect_seed_phrase(text),
        PiiCategory::ApiKey => detect_api_key(text),
        PiiCategory::AuthToken => detect_auth_token(text),
        PiiCategory::Password => detect_password(text),
        PiiCategory::PaymentCard => detect_payment_card(text),
        PiiCategory::BankAccount => detect_bank_account(text),
        PiiCategory::GovernmentId => detect_government_id(text),
    }
}

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("pii.rs regex literals are valid")
}

/// Lower-cased last `n` chars before byte offset `pos` (`pos` is a regex
/// match start, so always a char boundary).
fn lower_window_before(text: &str, pos: usize, n: usize) -> String {
    let mut chars: Vec<char> = text[..pos].chars().rev().take(n).collect();
    chars.reverse();
    chars.into_iter().collect::<String>().to_lowercase()
}

fn has_ascii_letter_and_digit(v: &str) -> bool {
    v.chars().any(|c| c.is_ascii_alphabetic()) && v.chars().any(|c| c.is_ascii_digit())
}

// ---------------------------------------------------------------------------
// Private keys
// ---------------------------------------------------------------------------

static PEM_PRIVATE_KEY_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"));
static XPRV_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"\b[xyzt]prv[1-9A-HJ-NP-Za-km-z]{100,112}\b"));
static HEX_KEY_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"\b(?:0[xX])?(?:[0-9a-fA-F]{128}|[0-9a-fA-F]{64})\b"));
/// Long base58 run (Bitcoin WIF, Solana / other base58-encoded secret keys).
/// Only a `Block` when a private-key keyword precedes it.
static BASE58_KEY_RE: LazyLock<Regex> = LazyLock::new(|| re(r"\b[1-9A-HJ-NP-Za-km-z]{44,100}\b"));
/// Bitcoin WIF private key shape (uncompressed `5…`, compressed `K…`/`L…`).
static WIF_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"\b(?:5[HJK][1-9A-HJ-NP-Za-km-z]{49}|[KL][1-9A-HJ-NP-Za-km-z]{51})\b"));

/// A 64/128-hex run is a `Block` when one of these appears in the 40 chars
/// before it; otherwise a `Flag` (tx hashes and SHA-256 digests share the
/// shape of a raw secp256k1 key).
const PRIVATE_KEY_KEYWORDS: [&str; 10] = [
    "private key",
    "private_key",
    "privatekey",
    "privkey",
    "priv key",
    "secret key",
    "secret_key",
    "signing key",
    "wallet key",
    "seed",
];

fn detect_private_key(text: &str) -> Level {
    if PEM_PRIVATE_KEY_RE.is_match(text) || XPRV_RE.is_match(text) {
        return Level::Block;
    }
    let mut level = Level::None;
    for m in HEX_KEY_RE.find_iter(text) {
        let window = lower_window_before(text, m.start(), 40);
        if PRIVATE_KEY_KEYWORDS.iter().any(|k| window.contains(k)) {
            return Level::Block;
        }
        level = Level::Flag;
    }
    for m in BASE58_KEY_RE.find_iter(text) {
        let window = lower_window_before(text, m.start(), 40);
        if PRIVATE_KEY_KEYWORDS.iter().any(|k| window.contains(k)) {
            return Level::Block;
        }
    }
    if WIF_RE.is_match(text) {
        level = Level::Flag;
    }
    level
}

// ---------------------------------------------------------------------------
// BIP-39 seed phrases
// ---------------------------------------------------------------------------

/// ASCII letter runs are candidate words; digit runs (list numbering such as
/// "1." / "2)") are transparent; everything else separates tokens.
static WORD_TOKEN_RE: LazyLock<Regex> = LazyLock::new(|| re(r"[A-Za-z]+|[0-9]+"));

/// Valid BIP-39 mnemonic lengths, longest first.
const MNEMONIC_LENGTHS: [usize; 5] = [24, 21, 18, 15, 12];

/// Words that name a recovery phrase. With one of these in the 40 chars
/// before a wordlist run (or inside it), a checksum-invalid run of any length
/// >= 12 is flagged.
const SEED_PHRASE_KEYWORDS: [&str; 7] = [
    "seed", "mnemonic", "recovery", "phrase", "backup", "wallet", "words",
];

fn detect_seed_phrase(text: &str) -> Level {
    let lang = bip39::Language::English;
    let mut level = Level::None;
    let mut run: Vec<String> = Vec::new();
    let mut run_start = 0usize;
    for m in WORD_TOKEN_RE.find_iter(text) {
        let token = m.as_str();
        if token.as_bytes()[0].is_ascii_digit() {
            continue;
        }
        let lower = token.to_ascii_lowercase();
        if lang.find_word(&lower).is_some() {
            if run.is_empty() {
                run_start = m.start();
            }
            run.push(lower);
        } else {
            level = level.max(evaluate_word_run(text, run_start, &run));
            if level == Level::Block {
                return level;
            }
            run.clear();
        }
    }
    level.max(evaluate_word_run(text, run_start, &run))
}

/// `Block` if any 12/15/18/21/24-word window of the run is a checksum-valid
/// BIP-39 mnemonic. Otherwise `Flag` if the run is exactly a mnemonic length
/// (12/15/18/21/24) or a [`SEED_PHRASE_KEYWORDS`] word names it — a
/// mistyped phrase still looks like this, while a long list of everyday
/// words ("apple banana bread …") usually does not. `None` for shorter runs.
fn evaluate_word_run(text: &str, run_start: usize, run: &[String]) -> Level {
    if run.len() < 12 {
        return Level::None;
    }
    for len in MNEMONIC_LENGTHS {
        if run.len() < len {
            continue;
        }
        for start in 0..=(run.len() - len) {
            let candidate = run[start..start + len].join(" ");
            if bip39::Mnemonic::parse(candidate.as_str()).is_ok() {
                return Level::Block;
            }
        }
    }
    let window = lower_window_before(text, run_start, 40);
    let named = SEED_PHRASE_KEYWORDS
        .iter()
        .any(|k| window.contains(k) || run.iter().any(|w| w == k));
    if MNEMONIC_LENGTHS.contains(&run.len()) || named {
        Level::Flag
    } else {
        Level::None
    }
}

// ---------------------------------------------------------------------------
// API keys and auth tokens
// ---------------------------------------------------------------------------

/// Optional qualifier between a key/token keyword and its value:
/// "api key for the weather service is …", "token for my bot: …".
const KEY_QUALIFIER: &str =
    r"(?:\s+(?:for|to|of|on|from)\s+(?:(?:the|my|our|a|an)\s+)?[\w.@\-]+(?:\s+[\w.@\-]+)?)?";

static API_KEY_ASSIGN_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(&format!(
        r#"(?i)(?:api[_ -]?key|apikey|secret[_ -]?key|client[_ -]?secret|app[_ -]?secret){KEY_QUALIFIER}\s*(?:is|was|:|=)\s*["'`]?([A-Za-z0-9_\-./+=:]{{16,}})"#
    ))
});
static TOKEN_ASSIGN_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(&format!(
        r#"(?i)(?:(?:access|auth|refresh|session|private|bearer|bot|api|app|integration|personal[_ -]access|secret)[_ -]?)?token{KEY_QUALIFIER}\s*(?:is|was|:|=)\s*["'`]?([A-Za-z0-9_\-./+=:]{{16,}})"#
    ))
});
/// Provider key formats not in [`crate::secrets`] (which stays the 14-class
/// scanner other features depend on): GitHub fine-grained PAT, GitLab PAT,
/// Google API key, npm, PyPI, Groq, xAI, OpenRouter.
static PROVIDER_KEY_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"\b(?:github_pat_[A-Za-z0-9_]{60,}|glpat-[A-Za-z0-9_\-]{20,}|AIza[0-9A-Za-z_\-]{35}|npm_[A-Za-z0-9]{36}|pypi-[A-Za-z0-9_\-]{50,}|gsk_[A-Za-z0-9]{40,}|xai-[A-Za-z0-9]{40,}|sk-or-v1-[0-9a-f]{64})",
    )
});
/// Keyword immediately followed by a long value, no "is"/":" ("AWS secret
/// access key wJalr…", "token FAKE…"). Only a `Block` when the value is
/// strongly secret-shaped ([`looks_like_strong_key`]).
static API_KEY_BARE_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r#"(?i)\b(?:api[_ -]?key|apikey|secret[_ -]?key|secret[_ ]access[_ ]key|access[_ -]?key|client[_ -]?secret)\s+["'`]?([A-Za-z0-9_\-./+=]{20,})"#,
    )
});
static TOKEN_BARE_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r#"(?i)\btoken\s+["'`]?([A-Za-z0-9_\-./+=]{20,})"#));
/// `Authorization: token|bearer|apikey <value>` header.
static AUTH_HEADER_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)\bauthorization\s*[:=]\s*(?:token|bearer|apikey|api-key|key)\s+([A-Za-z0-9\-._~+/]{16,}=*)",
    )
});
/// Upper-case env-var assignments: `DB_PASSWORD=…`, `GITHUB_TOKEN=…`.
static ENV_PASSWORD_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(r#"\b[A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|_PWD|_PASS|_PASSPHRASE)\s*[=:]\s*["']?([^\s"']{6,})"#)
});
static ENV_TOKEN_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r#"\b[A-Z][A-Z0-9_]*(?:_TOKEN|_SECRET|_APIKEY|_SECRET_KEY|_ACCESS_KEY|_PRIVATE_KEY)\s*[=:]\s*["']?([A-Za-z0-9_\-./+=:]{16,})"#,
    )
});

/// Letter + digit, and mixed case or a symbol, and not a public `0x` hex
/// address/hash (a token contract address often follows the word "token").
fn looks_like_strong_key(v: &str) -> bool {
    if v.len() > 2
        && (v.starts_with("0x") || v.starts_with("0X"))
        && v[2..].chars().all(|c| c.is_ascii_hexdigit())
    {
        return false;
    }
    let mixed_case =
        v.chars().any(|c| c.is_ascii_uppercase()) && v.chars().any(|c| c.is_ascii_lowercase());
    let has_symbol = v.chars().any(|c| "_-./+=".contains(c));
    has_ascii_letter_and_digit(v) && (mixed_case || has_symbol)
}

fn any_capture(pattern: &Regex, text: &str, ok: fn(&str) -> bool) -> bool {
    pattern
        .captures_iter(text)
        .any(|c| c.get(1).is_some_and(|v| ok(v.as_str())))
}

/// Telegram Bot API token: numeric bot id, `:`, then the `AA…` secret.
static TELEGRAM_BOT_TOKEN_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"\b[0-9]{6,12}:AA[A-Za-z0-9_\-]{30,40}\b"));
static BEARER_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)\bbearer\s+([A-Za-z0-9\-._~+/]{20,}=*)"));
static BASIC_AUTH_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)\bauthorization\s*:\s*basic\s+[A-Za-z0-9+/]{12,}={0,2}"));

fn any_capture_has_letter_and_digit(pattern: &Regex, text: &str) -> bool {
    pattern.captures_iter(text).any(|c| {
        c.get(1)
            .is_some_and(|v| has_ascii_letter_and_digit(v.as_str()))
    })
}

fn detect_api_key(text: &str) -> Level {
    if secrets::detect_secrets(text)
        .iter()
        .any(|m| m.kind != SecretKind::Jwt)
        || any_capture_has_letter_and_digit(&API_KEY_ASSIGN_RE, text)
        || TELEGRAM_BOT_TOKEN_RE.is_match(text)
        || PROVIDER_KEY_RE.is_match(text)
        || any_capture(&API_KEY_BARE_RE, text, looks_like_strong_key)
        || any_capture(&ENV_TOKEN_RE, text, has_ascii_letter_and_digit)
    {
        Level::Block
    } else {
        Level::None
    }
}

fn detect_auth_token(text: &str) -> Level {
    if secrets::detect_secrets(text)
        .iter()
        .any(|m| m.kind == SecretKind::Jwt)
        || BASIC_AUTH_RE.is_match(text)
        || any_capture_has_letter_and_digit(&BEARER_RE, text)
        || any_capture_has_letter_and_digit(&TOKEN_ASSIGN_RE, text)
        || any_capture_has_letter_and_digit(&AUTH_HEADER_RE, text)
        || any_capture(&TOKEN_BARE_RE, text, looks_like_strong_key)
    {
        Level::Block
    } else {
        Level::None
    }
}

// ---------------------------------------------------------------------------
// Passwords and PINs
// ---------------------------------------------------------------------------

static URL_CREDENTIALS_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)\b[a-z][a-z0-9+.\-]{1,15}://[^\s:/@]+:[^\s/@]{3,}@[^\s/@]+"));
static PASSWORD_ASSERT_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r#"(?i)\b(?:password|passwd|pwd|passphrase|passcode|(?:wi-?fi|wlan|router|network)\s+pass|senha|palavra-passe|contraseña|mot de passe|passwort|kennwort)(?:\s+(?:for|to|of|on|do|da|de|para|du|für)\s+(?:(?:the|my|our|a|o|minha|meu|mi|mon|ma|mein|meine|la|el)\s+)?[\w.@\-]+(?:\s+[\w.@\-]+)?)?\s*(?:is|was|:|=|é|era|es|est|ist)\s*["'`]?(\S{6,64})"#,
    )
});
/// Keyword immediately followed by the value, no "is"/":" ("login admin /
/// password Hunter2!x"). Only a `Block` when the value is strongly
/// secret-shaped ([`looks_like_strong_secret`]).
static PASSWORD_BARE_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(r#"(?i)\b(?:password|passwd|pwd|senha|contraseña|passwort|kennwort)\s+["'`]?(\S{6,64})"#)
});
static PIN_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)\b(?:pin(?:\s+(?:code|number))?|c[oó]digo\s+pin)(?:\s+(?:for|to|of|on|do|da|de|para)\s+(?:(?:the|my|our|a|an|o)\s+)?[\w\-]+(?:\s+[\w\-]+)?\s*(?:is|was|:|=|é)|\s*(?:is|was|:|=|é|#)?)\s*[0-9]{4,8}\b",
    )
});

/// A password value must look like a secret, not like prose: at least 6 chars
/// (after trailing punctuation) and letter+digit, or a symbol plus a letter
/// or digit.
fn looks_like_secret(raw: &str) -> bool {
    let v = raw.trim_end_matches(|c: char| {
        matches!(
            c,
            '.' | ',' | ';' | ':' | '!' | '?' | ')' | '"' | '\'' | '`'
        )
    });
    if v.chars().count() < 6 || v.contains("://") {
        return false;
    }
    let has_letter = v.chars().any(|c| c.is_alphabetic());
    let has_digit = v.chars().any(|c| c.is_ascii_digit());
    let has_symbol = v.chars().any(|c| "!@#$%^&*_+=?~|<>{}[]()".contains(c));
    (has_letter && has_digit) || (has_symbol && (has_letter || has_digit))
}

/// Stricter than [`looks_like_secret`], for a value with no "is"/":" in
/// front of it: letter + digit, plus a symbol or mixed case.
fn looks_like_strong_secret(raw: &str) -> bool {
    let v = raw.trim_end_matches(|c: char| {
        matches!(
            c,
            '.' | ',' | ';' | ':' | '!' | '?' | ')' | '"' | '\'' | '`'
        )
    });
    if v.chars().count() < 6 || v.contains("://") {
        return false;
    }
    let has_letter = v.chars().any(|c| c.is_alphabetic());
    let has_digit = v.chars().any(|c| c.is_ascii_digit());
    let has_symbol = v.chars().any(|c| "!@#$%^&*_+=?~|<>{}[]()-".contains(c));
    let mixed_case = v.chars().any(|c| c.is_uppercase()) && v.chars().any(|c| c.is_lowercase());
    has_letter && has_digit && (has_symbol || mixed_case)
}

fn detect_password(text: &str) -> Level {
    if URL_CREDENTIALS_RE.is_match(text) || PIN_RE.is_match(text) {
        return Level::Block;
    }
    let asserted = PASSWORD_ASSERT_RE
        .captures_iter(text)
        .any(|c| c.get(1).is_some_and(|v| looks_like_secret(v.as_str())))
        || PASSWORD_BARE_RE.captures_iter(text).any(|c| {
            c.get(1)
                .is_some_and(|v| looks_like_strong_secret(v.as_str()))
        })
        || any_capture(&ENV_PASSWORD_RE, text, looks_like_secret);
    if asserted {
        Level::Block
    } else {
        Level::None
    }
}

// ---------------------------------------------------------------------------
// Payment cards
// ---------------------------------------------------------------------------

/// Contiguous 13–19 digits, or the printed groupings 4-4-4-4(+1..3),
/// 4-6-5 (Amex) and 4-6-4 (Diners), separated by single spaces, dashes or dots.
static CARD_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"\b(?:[0-9]{13,19}|[0-9]{4}[ \-.][0-9]{4}[ \-.][0-9]{4}[ \-.][0-9]{4}(?:[ \-.]?[0-9]{1,3})?|[0-9]{4}[ \-.][0-9]{6}[ \-.][0-9]{4,5})\b",
    )
});

fn detect_payment_card(text: &str) -> Level {
    for m in CARD_RE.find_iter(text) {
        // "+<country code>..." is a phone number, never a card.
        if text[..m.start()].ends_with('+') {
            continue;
        }
        let digits: Vec<u32> = m.as_str().chars().filter_map(|c| c.to_digit(10)).collect();
        if card_brand_ok(&digits) && luhn_ok(&digits) {
            return Level::Block;
        }
    }
    Level::None
}

/// Issuer prefix (IIN) + length table: Visa, Mastercard (51–55, 2221–2720),
/// Amex, Discover, JCB, Diners, UnionPay, Maestro.
fn card_brand_ok(d: &[u32]) -> bool {
    let n = d.len();
    if !(13..=19).contains(&n) {
        return false;
    }
    let prefix = |k: usize| d[..k].iter().fold(0u32, |acc, v| acc * 10 + v);
    let (p1, p2, p3, p4) = (prefix(1), prefix(2), prefix(3), prefix(4));
    if p1 == 4 {
        return n == 13 || n == 16 || n == 19;
    }
    if (51..=55).contains(&p2) || (2221..=2720).contains(&p4) {
        return n == 16;
    }
    if p2 == 34 || p2 == 37 {
        return n == 15;
    }
    if p4 == 6011
        || p2 == 65
        || (644..=649).contains(&p3)
        || (3528..=3589).contains(&p4)
        || p2 == 62
    {
        return n >= 16;
    }
    if (300..=305).contains(&p3) || p2 == 36 || p2 == 38 || p2 == 39 {
        return n >= 14;
    }
    p2 == 50 || (56..=58).contains(&p2) || p2 == 63 || p2 == 67
}

fn luhn_ok(d: &[u32]) -> bool {
    let sum: u32 = d
        .iter()
        .rev()
        .enumerate()
        .map(|(i, &v)| {
            if i % 2 == 1 {
                let x = v * 2;
                if x > 9 {
                    x - 9
                } else {
                    x
                }
            } else {
                v
            }
        })
        .sum();
    sum % 10 == 0
}

// ---------------------------------------------------------------------------
// IBAN
// ---------------------------------------------------------------------------

static IBAN_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)\b[A-Z]{2}[0-9]{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b"));

/// ISO 13616 registry lengths (SEPA + common non-SEPA countries).
fn iban_length(country: &str) -> Option<usize> {
    Some(match country {
        "NO" => 15,
        "BE" => 16,
        "DK" | "FI" | "FO" | "GL" | "NL" => 18,
        "MK" | "SI" => 19,
        "AT" | "BA" | "EE" | "KZ" | "LT" | "LU" | "XK" => 20,
        "CH" | "HR" | "LI" | "LV" => 21,
        "BG" | "BH" | "CR" | "DE" | "GB" | "GE" | "IE" | "ME" | "RS" => 22,
        "AE" | "GI" | "IL" => 23,
        "AD" | "CZ" | "ES" | "MD" | "PK" | "RO" | "SA" | "SE" | "SK" | "TN" | "VG" => 24,
        "PT" => 25,
        "IS" | "TR" => 26,
        "FR" | "GR" | "IT" | "MC" | "MR" | "SM" => 27,
        "AL" | "AZ" | "CY" | "DO" | "GT" | "HU" | "LB" | "PL" => 28,
        "BR" | "PS" | "QA" | "UA" => 29,
        "JO" | "KW" | "MU" => 30,
        "MT" => 31,
        _ => return None,
    })
}

fn iban_mod97_ok(iban: &str) -> bool {
    let rearranged = iban[4..].bytes().chain(iban[..4].bytes());
    let mut rem: u32 = 0;
    for b in rearranged {
        rem = match b {
            b'0'..=b'9' => (rem * 10 + u32::from(b - b'0')) % 97,
            b'A'..=b'Z' => (rem * 100 + u32::from(b - b'A') + 10) % 97,
            _ => return false,
        };
    }
    rem == 1
}

fn detect_bank_account(text: &str) -> Level {
    for m in IBAN_RE.find_iter(text) {
        if !m.as_str().is_ascii() {
            continue;
        }
        let compact: String = m
            .as_str()
            .chars()
            .filter(|c| *c != ' ')
            .map(|c| c.to_ascii_uppercase())
            .collect();
        let Some(len) = iban_length(&compact[..2]) else {
            continue;
        };
        // The regex may run into a following short word ("... 7034 is");
        // the registry length says exactly where the IBAN ends.
        if compact.len() >= len && iban_mod97_ok(&compact[..len]) {
            return Level::Block;
        }
    }
    Level::None
}

// ---------------------------------------------------------------------------
// Government identifiers
// ---------------------------------------------------------------------------

fn detect_government_id(text: &str) -> Level {
    let detectors: [fn(&str) -> Level; 9] = [
        detect_pt_nif,
        detect_pt_cc,
        detect_es_dni_nie,
        detect_it_codice_fiscale,
        detect_fr_nir,
        detect_us_ssn,
        detect_keyword_named_id,
        detect_keyword_spaced_id,
        detect_br_cpf,
    ];
    let mut level = Level::None;
    for detector in detectors {
        level = level.max(detector(text));
        if level == Level::Block {
            break;
        }
    }
    level
}

fn digits_of(s: &str) -> Vec<u32> {
    s.chars().filter_map(|c| c.to_digit(10)).collect()
}

// PT NIF / NIPC — keyword required (a bare 9-digit number is usually a phone
// number). Valid check digit → Block; invalid → Flag.
static PT_NIF_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)\b(?:nif|nipc|contribuinte|tax\s+id|tax\s+number|taxpayer\s+number|vat\s+number|vat\s+id)\b[^0-9\n]{0,20}([0-9]{3} ?[0-9]{3} ?[0-9]{3})\b",
    )
});

fn pt_nif_ok(d: &[u32]) -> bool {
    if d.len() != 9 || d[0] == 0 {
        return false;
    }
    let sum: u32 = d[..8]
        .iter()
        .enumerate()
        .map(|(i, v)| v * (9 - i as u32))
        .sum();
    let r = sum % 11;
    let check = if r < 2 { 0 } else { 11 - r };
    d[8] == check
}

fn detect_pt_nif(text: &str) -> Level {
    let mut level = Level::None;
    for caps in PT_NIF_RE.captures_iter(text) {
        if pt_nif_ok(&digits_of(&caps[1])) {
            return Level::Block;
        }
        level = Level::Flag;
    }
    level
}

// PT Cartão de Cidadão document number "DDDDDDDD C LL C". Check: Luhn over
// base-36 values (official algorithm). Valid → Block; shape only → Flag.
static PT_CC_RE: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)\b[0-9]{8} ?[0-9] ?[A-Z]{2}[0-9]\b"));

fn pt_cc_ok(compact: &str) -> bool {
    let mut sum = 0u32;
    let mut second = false;
    for c in compact.chars().rev() {
        let Some(mut v) = c.to_digit(36) else {
            return false;
        };
        if second {
            v *= 2;
            if v > 9 {
                v -= 9;
            }
        }
        sum += v;
        second = !second;
    }
    sum % 10 == 0
}

fn detect_pt_cc(text: &str) -> Level {
    let mut level = Level::None;
    for m in PT_CC_RE.find_iter(text) {
        if !m.as_str().is_ascii() {
            continue;
        }
        let compact: String = m.as_str().chars().filter(|c| *c != ' ').collect();
        if pt_cc_ok(&compact) {
            return Level::Block;
        }
        level = Level::Flag;
    }
    level
}

// ES DNI "12345678Z" / NIE "X1234567L" — mod-23 control letter. Valid → Block.
static ES_DNI_RE: LazyLock<Regex> = LazyLock::new(|| re(r"\b([0-9]{8})[ \-]?([A-Z])\b"));
static ES_NIE_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"\b([XYZ])[ \-]?([0-9]{7})[ \-]?([A-Z])\b"));
const ES_CONTROL_LETTERS: &[u8; 23] = b"TRWAGMYFPDXBNJZSQVHLCKE";

fn es_control_ok(number: u32, letter: &str) -> bool {
    letter.as_bytes().first() == Some(&ES_CONTROL_LETTERS[(number % 23) as usize])
}

fn detect_es_dni_nie(text: &str) -> Level {
    for caps in ES_DNI_RE.captures_iter(text) {
        if let Ok(n) = caps[1].parse::<u32>() {
            if es_control_ok(n, &caps[2]) {
                return Level::Block;
            }
        }
    }
    for caps in ES_NIE_RE.captures_iter(text) {
        let prefix = match &caps[1] {
            "X" => "0",
            "Y" => "1",
            _ => "2",
        };
        if let Ok(n) = format!("{}{}", prefix, &caps[2]).parse::<u32>() {
            if es_control_ok(n, &caps[3]) {
                return Level::Block;
            }
        }
    }
    Level::None
}

// IT codice fiscale (16 chars, omocodia letters allowed in digit slots; at
// least one digit required so prose words never match). Valid check letter →
// Block; shape only → Flag.
static IT_CF_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)\b[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]\b",
    )
});
/// Odd-position (1-based) values for 0–9 / A–Z (digit d shares letter d's value).
const IT_CF_ODD: [u32; 26] = [
    1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23,
];

fn it_cf_ok(cf: &[u8]) -> bool {
    if cf.len() != 16 {
        return false;
    }
    let mut sum = 0u32;
    for (i, &b) in cf[..15].iter().enumerate() {
        let idx = if b.is_ascii_digit() {
            usize::from(b - b'0')
        } else {
            usize::from(b - b'A')
        };
        sum += if i % 2 == 0 {
            IT_CF_ODD[idx]
        } else {
            idx as u32
        };
    }
    cf[15] == b'A' + (sum % 26) as u8
}

fn detect_it_codice_fiscale(text: &str) -> Level {
    let mut level = Level::None;
    for m in IT_CF_RE.find_iter(text) {
        let s = m.as_str();
        if !s.is_ascii() || !s.bytes().any(|b| b.is_ascii_digit()) {
            continue;
        }
        if it_cf_ok(s.to_ascii_uppercase().as_bytes()) {
            return Level::Block;
        }
        level = Level::Flag;
    }
    level
}

// FR NIR (numéro de sécurité sociale): 13 digits + 2-digit key
// (key = 97 − n mod 97, Corsica 2A/2B → 19/18). Valid → Block.
static FR_NIR_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"\b([12]) ?([0-9]{2}) ?(0[1-9]|1[0-2]|[2-9][0-9]) ?([0-9]{2}|2A|2B) ?([0-9]{3}) ?([0-9]{3}) ?([0-9]{2})\b",
    )
});

fn detect_fr_nir(text: &str) -> Level {
    for caps in FR_NIR_RE.captures_iter(text) {
        let department = match &caps[4] {
            "2A" => "19",
            "2B" => "18",
            other => other,
        };
        let body = format!(
            "{}{}{}{}{}{}",
            &caps[1], &caps[2], &caps[3], department, &caps[5], &caps[6]
        );
        if let (Ok(n), Ok(key)) = (body.parse::<u64>(), caps[7].parse::<u64>()) {
            if 97 - n % 97 == key {
                return Level::Block;
            }
        }
    }
    Level::None
}

// US SSN. Keyword + valid ranges → Block; keyword + invalid ranges → Flag;
// bare dashed "AAA-GG-SSSS" with valid ranges → Flag.
static US_SSN_KEYWORD_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)\b(?:ssn|social\s+security(?:\s+(?:number|no\.?|#))?)\b[^0-9\n]{0,20}([0-9]{3})[ \-]?([0-9]{2})[ \-]?([0-9]{4})\b",
    )
});
static US_SSN_DASHED_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"\b([0-9]{3})-([0-9]{2})-([0-9]{4})\b"));

fn ssn_ranges_ok(area: &str, group: &str, serial: &str) -> bool {
    let (Ok(a), Ok(g), Ok(s)) = (
        area.parse::<u32>(),
        group.parse::<u32>(),
        serial.parse::<u32>(),
    ) else {
        return false;
    };
    a != 0 && a != 666 && a < 900 && g != 0 && s != 0
}

fn detect_us_ssn(text: &str) -> Level {
    let mut level = Level::None;
    for caps in US_SSN_KEYWORD_RE.captures_iter(text) {
        if ssn_ranges_ok(&caps[1], &caps[2], &caps[3]) {
            return Level::Block;
        }
        level = Level::Flag;
    }
    for caps in US_SSN_DASHED_RE.captures_iter(text) {
        if ssn_ranges_ok(&caps[1], &caps[2], &caps[3]) {
            level = Level::Flag;
        }
    }
    level
}

// Keyword-named identity numbers (passport, id card, driving licence, BSN,
// Steuer-ID, PESEL, …): a single 6–16-alphanumeric token with >= 5 digits
// right after the keyword → Block.
static ID_KEYWORD_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)\b(?:passport|passaporte|pasaporte|passeport|reisepass|national\s+id|national\s+identity\s+(?:card|number)|identity\s+card|id\s+card|citizen\s+card|cart[aã]o\s+de\s+cidad[aã]o|bilhete\s+de\s+identidade|driver'?s\s+licen[cs]e|driving\s+licen[cs]e|carta\s+de\s+condu[cç][aã]o|personalausweis|steuer-?id|steueridentifikationsnummer|bsn|burgerservicenummer|pesel)(?:[\s:#.\-]*(?:number|nummer|n[uú]mero|no\.?|nr\.?|n\.?[ºo°]\.?|num\.?|is|é|ist|hat|die|lautet|#))*[\s:#.\-]{0,4}([A-Z0-9][A-Z0-9\-]{4,15})\b",
    )
});

// Keyword-named ids printed with spaces: UK National Insurance number
// ("QQ 12 34 56 C"), Indian Aadhaar ("2345 6789 0124") and UK NHS number
// ("943 476 5919"). Keyword → Block.
static SPACED_ID_KEYWORD_RE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)\b(?:national\s+insurance(?:\s+(?:number|no\.?))?|nino|ni\s+number|aadhaa?r(?:\s+(?:number|no\.?|card))?|nhs\s+(?:number|no\.?))\b[^0-9A-Za-z\n]{0,12}(?:is\s+|é\s+)?([A-Z]{2} ?[0-9]{2} ?[0-9]{2} ?[0-9]{2} ?[A-D]|[0-9]{4} ?[0-9]{4} ?[0-9]{4}|[0-9]{3} ?[0-9]{3} ?[0-9]{4})\b",
    )
});

// BR CPF "123.456.789-09". Keyword + valid check digits → Block; keyword +
// invalid, or the dotted shape with valid check digits and no keyword → Flag.
static BR_CPF_KEYWORD_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)\bcpf\b[^0-9\n]{0,12}([0-9]{3}\.?[0-9]{3}\.?[0-9]{3}-?[0-9]{2})\b"));
static BR_CPF_DOTTED_RE: LazyLock<Regex> =
    LazyLock::new(|| re(r"\b[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9]{2}\b"));

fn br_cpf_ok(d: &[u32]) -> bool {
    if d.len() != 11 || d.iter().all(|&v| v == d[0]) {
        return false;
    }
    let check = |n: usize| {
        let sum: u32 = d[..n]
            .iter()
            .enumerate()
            .map(|(i, v)| v * (n as u32 + 1 - i as u32))
            .sum();
        (sum * 10) % 11 % 10
    };
    check(9) == d[9] && check(10) == d[10]
}

fn detect_br_cpf(text: &str) -> Level {
    let mut level = Level::None;
    for caps in BR_CPF_KEYWORD_RE.captures_iter(text) {
        if br_cpf_ok(&digits_of(&caps[1])) {
            return Level::Block;
        }
        level = Level::Flag;
    }
    if BR_CPF_DOTTED_RE
        .find_iter(text)
        .any(|m| br_cpf_ok(&digits_of(m.as_str())))
    {
        level = Level::Flag;
    }
    level
}

fn detect_keyword_spaced_id(text: &str) -> Level {
    if SPACED_ID_KEYWORD_RE.is_match(text) {
        Level::Block
    } else {
        Level::None
    }
}

fn detect_keyword_named_id(text: &str) -> Level {
    for caps in ID_KEYWORD_RE.captures_iter(text) {
        let token = &caps[1];
        let digits = token.chars().filter(|c| c.is_ascii_digit()).count();
        let alnum = token.chars().filter(|c| c.is_ascii_alphanumeric()).count();
        if digits >= 5 && (6..=16).contains(&alnum) {
            return Level::Block;
        }
    }
    Level::None
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn block(category: PiiCategory) -> PiiDecision {
        PiiDecision::Block { category }
    }

    fn flag(category: PiiCategory) -> PiiDecision {
        PiiDecision::Flag { category }
    }

    #[test]
    fn category_tags_are_stable_snake_case() {
        let tags: Vec<&str> = PiiCategory::ALL.iter().map(|c| c.as_str()).collect();
        assert_eq!(
            tags,
            vec![
                "private_key",
                "seed_phrase",
                "api_key",
                "auth_token",
                "password",
                "payment_card",
                "bank_account",
                "government_id"
            ]
        );
    }

    #[test]
    fn every_category_has_a_block_message() {
        for c in PiiCategory::ALL {
            let msg = pii_block_message(c);
            assert!(msg.starts_with("Not saved: "), "{c}: {msg}");
            assert!(msg.contains("unless you confirm"), "{c}: {msg}");
        }
    }

    #[test]
    fn allow_json_shape() {
        assert_eq!(PiiDecision::Allow.to_json(), r#"{"decision":"allow"}"#);
    }

    #[test]
    fn block_json_shape_has_fixed_key_order() {
        let json = block(PiiCategory::PaymentCard).to_json();
        assert!(
            json.starts_with(
                r#"{"decision":"block","category":"payment_card","message":"Not saved: "#
            ),
            "{json}"
        );
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["message"], pii_block_message(PiiCategory::PaymentCard));
    }

    #[test]
    fn flag_json_shape() {
        let v: serde_json::Value =
            serde_json::from_str(&flag(PiiCategory::SeedPhrase).to_json()).unwrap();
        assert_eq!(v["decision"], "flag");
        assert_eq!(v["category"], "seed_phrase");
    }

    #[test]
    fn doctor_name_is_allowed() {
        assert_eq!(pii_filter("my doctor is Dr. X"), PiiDecision::Allow);
    }

    #[test]
    fn empty_text_is_allowed() {
        assert_eq!(pii_filter(""), PiiDecision::Allow);
        assert_eq!(pii_filter("   \n\t"), PiiDecision::Allow);
    }

    #[test]
    fn luhn_rejects_near_miss_card() {
        assert_eq!(pii_filter("card 4111 1111 1111 1112"), PiiDecision::Allow);
        assert_eq!(
            pii_filter("card 4111 1111 1111 1111"),
            block(PiiCategory::PaymentCard)
        );
    }

    #[test]
    fn phone_number_with_plus_is_never_a_card() {
        assert_eq!(pii_filter("+4111111111111111"), PiiDecision::Allow);
    }

    #[test]
    fn iban_checksum_failure_is_allowed() {
        assert_eq!(
            pii_filter("PT50 0002 0123 1234 5678 9015 5"),
            PiiDecision::Allow
        );
    }

    #[test]
    fn seed_phrase_valid_vs_invalid_checksum() {
        let valid = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
        assert_eq!(pii_filter(valid), block(PiiCategory::SeedPhrase));
        let invalid = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon";
        assert_eq!(pii_filter(invalid), flag(PiiCategory::SeedPhrase));
        let eleven =
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
        assert_eq!(pii_filter(eleven), PiiDecision::Allow);
    }

    #[test]
    fn hex_key_needs_keyword_for_block() {
        let hex = "1234567890abcdef".repeat(4);
        assert_eq!(
            pii_filter(&format!("private key {hex}")),
            block(PiiCategory::PrivateKey)
        );
        assert_eq!(pii_filter(&hex), flag(PiiCategory::PrivateKey));
        // 40-hex Ethereum address is public, not a key.
        assert_eq!(
            pii_filter("0x742d35Cc6634C0532925a3b844Bc454e4438f44e"),
            PiiDecision::Allow
        );
    }

    #[test]
    fn block_wins_over_flag_across_categories() {
        let text = format!("{} and card 4111111111111111", "fedcba9876543210".repeat(4));
        assert_eq!(pii_filter(&text), block(PiiCategory::PaymentCard));
    }

    #[test]
    fn multibyte_text_never_panics() {
        for text in [
            "é".repeat(50) + " sk-proj-" + &"a".repeat(48) + " " + &"ã".repeat(50),
            "ſſ12 ſſſſ ſſſſ ſſſſ".to_string(),
            "KK12 3456 7890 1234 5".to_string(),
            "Número de identificação fiscal: ñ".to_string(),
            "🔑".repeat(100),
        ] {
            let _ = pii_filter(&text);
        }
    }

    #[test]
    fn runtime_built_provider_keys_block_as_api_key() {
        // Shapes GitHub push protection would reject as committed literals
        // (so they are NOT in tests/parity/pii/corpus.json) — built here.
        let cases = [
            format!("HF token hf_{}", "a".repeat(37)),
            format!("slack bot xox{}-{}", "b", "1234567890ABCDEF".repeat(2)),
            format!("SendGrid SG.{}.{}", "a".repeat(22), "b".repeat(43)),
            format!("Twilio SK{}", "a1b2c3d4".repeat(4)),
            format!("stripe live key sk_live_{}", "x".repeat(24)),
        ];
        for text in &cases {
            assert_eq!(pii_filter(text), block(PiiCategory::ApiKey));
        }
        // Markers assembled at runtime so no complete PEM block is committed.
        let pem = format!(
            "-----BEGIN RSA {k}-----\nMIIE{}\n-----{} RSA {k}-----",
            "A".repeat(16),
            "END",
            k = "PRIVATE KEY"
        );
        assert_eq!(pii_filter(&pem), block(PiiCategory::PrivateKey));
    }

    #[test]
    fn runtime_built_extra_provider_keys_block_as_api_key() {
        // Formats outside `secrets.rs`, built at runtime for the same
        // push-protection reason as above.
        let cases = [
            format!("new token github_pat_{}", "A1b2C3d4E5".repeat(7)),
            format!("GITLAB_TOKEN glpat-{}", "a1B2c3D4e5".repeat(2)),
            format!(
                "maps key AIza{}",
                "B1c2D3e4F5g6H7i8J9k0L".repeat(2).get(..35).unwrap()
            ),
            format!(
                "//registry.npmjs.org/:_authToken npm_{}",
                "a1B2c3D4e5F6".repeat(3)
            ),
            format!("upload with pypi-{}", "AgEIcHlwaS5vcmc".repeat(4)),
            format!("GROQ gsk_{}", "a1B2c3D4e5".repeat(5)),
            format!("grok xai-{}", "a1B2c3D4e5".repeat(5)),
            format!("openrouter sk-or-v1-{}", "0123456789abcdef".repeat(4)),
        ];
        for text in &cases {
            assert_eq!(
                pii_filter(text),
                block(PiiCategory::ApiKey),
                "{}",
                &text[..12]
            );
        }
    }

    #[test]
    fn base58_key_needs_keyword_and_bare_wif_is_flagged() {
        // Bitcoin wiki public example WIF.
        let wif = "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ";
        assert_eq!(
            pii_filter(&format!("private key {wif}")),
            block(PiiCategory::PrivateKey)
        );
        assert_eq!(pii_filter(wif), flag(PiiCategory::PrivateKey));
        // A long base58 run with no keyword (e.g. an IPFS CID) is allowed.
        assert_eq!(
            pii_filter("pinned at QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG"),
            PiiDecision::Allow
        );
    }

    #[test]
    fn long_everyday_word_list_is_not_a_seed_phrase() {
        // 13 consecutive wordlist words, no checksum, no keyword → allow.
        let list =
            "list apple banana bread butter cheese chicken coffee cream egg garlic lemon milk";
        assert_eq!(pii_filter(list), PiiDecision::Allow);
        // The same run named as a recovery phrase is flagged.
        assert_eq!(
            pii_filter(&format!("my recovery words: {list}")),
            flag(PiiCategory::SeedPhrase)
        );
    }

    #[test]
    fn password_keyword_without_connector_needs_strong_value() {
        assert_eq!(
            pii_filter("login admin / password Hunter2!x"),
            block(PiiCategory::Password)
        );
        assert_eq!(
            pii_filter("My password manager is Bitwarden"),
            PiiDecision::Allow
        );
        assert_eq!(pii_filter("password reset123 was sent"), PiiDecision::Allow);
    }

    #[test]
    fn token_after_the_word_token_is_not_a_contract_address() {
        assert_eq!(
            pii_filter("the USDC token 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 on mainnet"),
            PiiDecision::Allow
        );
        assert_eq!(
            pii_filter("Authorization: token FAKEghp1234567890abcdefFAKE"),
            block(PiiCategory::AuthToken)
        );
    }

    #[test]
    fn pin_qualifier_needs_a_connector() {
        assert_eq!(
            pii_filter("The PIN for the house alarm is 2580"),
            block(PiiCategory::Password)
        );
        assert_eq!(
            pii_filter("Drop a pin on the map 2024 trip"),
            PiiDecision::Allow
        );
    }

    #[test]
    fn env_var_secrets_are_blocked() {
        assert_eq!(
            pii_filter("DB_PASSWORD=s3cr3t-Value"),
            block(PiiCategory::Password)
        );
        assert_eq!(
            pii_filter("GITHUB_TOKEN=FAKE0123456789abcdefFAKE"),
            block(PiiCategory::ApiKey)
        );
        assert_eq!(
            pii_filter("Change the DB_PASSWORD variable"),
            PiiDecision::Allow
        );
    }

    #[test]
    fn br_cpf_check_digits() {
        assert_eq!(
            pii_filter("CPF 123.456.789-09"),
            block(PiiCategory::GovernmentId)
        );
        assert_eq!(
            pii_filter("CPF 123.456.789-00"),
            flag(PiiCategory::GovernmentId)
        );
        assert_eq!(
            pii_filter("ref 123.456.789-09"),
            flag(PiiCategory::GovernmentId)
        );
        assert_eq!(
            pii_filter("CPF 111.111.111-11"),
            flag(PiiCategory::GovernmentId)
        );
    }

    #[test]
    fn secrets_in_accented_text_are_still_blocked() {
        let text = format!("A minha chave é {}", format!("sk-proj-{}", "x".repeat(48)));
        assert_eq!(pii_filter(&text), block(PiiCategory::ApiKey));
    }
}
