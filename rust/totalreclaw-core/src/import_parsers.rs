//! Import parsers — pure format→chunks parsers for AI memory/conversation exports.
//!
//! Shared across every client (TypeScript via WASM, Python via PyO3) so the
//! format logic lives in ONE place instead of being duplicated per client.
//!
//! All functions here are pure: no I/O, no async, no network. The client layer
//! owns file reading, the 500MB/RAM preflight, the LLM-extraction call, and the
//! encrypt+store step. This module only turns export bytes into normalized
//! `ParsedChunk`s the import engine can feed to extraction.
//!
//! # Scope
//!
//! - `parse_gemini` — Google Takeout "My Activity" JSON (`MyActivity.json`) and
//!   pasted "Saved info" bullet lists.
//!
//! The legacy Gemini **HTML** export is intentionally NOT handled here: scraping
//! it needs a regex engine, and pulling `regex` into the core crate would bloat
//! the WASM bundle for every browser client — for a format Google is phasing out
//! in favor of JSON. HTML stays a thin client-native shim. See
//! `core-hoist-backlog.md`.

use regex::Regex;
use serde::{Deserialize, Serialize};
use std::sync::LazyLock;

/// Maximum messages per conversation chunk for LLM extraction.
const CHUNK_SIZE: usize = 20;

/// Gap (seconds) between entries that starts a new pseudo-session.
const SESSION_GAP_SECONDS: i64 = 30 * 60;

// ---------------------------------------------------------------------------
// Output types (mirror the client adapters' ConversationChunk: role + text)
// ---------------------------------------------------------------------------

/// A message within a parsed conversation chunk.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ParsedMessage {
    pub role: String,
    pub text: String,
}

/// A chunk of conversation messages for LLM-based fact extraction.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ParsedChunk {
    pub title: String,
    pub messages: Vec<ParsedMessage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
}

/// The result of parsing one export. The client converts this into its own
/// `AdapterParseResult`. `facts` is omitted here — Gemini is conversation-based
/// (chunks only); pre-structured sources (Mem0) will add a `facts` vec later.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ParseResult {
    pub chunks: Vec<ParsedChunk>,
    pub total_messages: usize,
    pub warnings: Vec<String>,
    pub errors: Vec<String>,
    /// Wire-format tag, e.g. "gemini-my-activity-json" / "gemini-saved-info-text".
    pub format: String,
    /// Number of raw input records seen (JSON path).
    pub records_count: usize,
    /// Records skipped because their `header` was not a Gemini one.
    pub skipped: usize,
}

/// An intermediate user-prompt + model-reply pair with a timestamp.
struct Entry {
    user_prompt: String,
    ai_response: String,
    ts_iso: Option<String>,
    ts_unix: i64,
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

/// Parse a Gemini export string in any of its three shapes:
///   - `MyActivity.json` (Google Data Portability "My Activity" JSON)
///   - `My Activity.html` (legacy Takeout HTML render)
///   - a pasted "Saved info" bullet list
///
/// One entry point for every client (TS via WASM, Python via PyO3) so the format
/// logic — including the locale-robust, lossless timestamp handling — lives in
/// exactly one place.
pub fn parse_gemini(input: &str) -> ParseResult {
    let trimmed = input.trim_start();
    let first = trimmed.chars().next();
    if matches!(first, Some('[') | Some('{')) {
        return parse_gemini_json(trimmed);
    }
    if first == Some('<') || input.contains("outer-cell") {
        return parse_gemini_html(input);
    }
    parse_gemini_saved_info(input)
}

// ---------------------------------------------------------------------------
// MyActivity.json
// ---------------------------------------------------------------------------

fn parse_gemini_json(content: &str) -> ParseResult {
    let mut r = ParseResult {
        format: "gemini-my-activity-json".to_string(),
        ..Default::default()
    };

    let data: serde_json::Value = match serde_json::from_str(content) {
        Ok(v) => v,
        Err(e) => {
            r.errors
                .push(format!("Failed to parse Gemini MyActivity JSON: {e}"));
            return r;
        }
    };

    let records: Vec<serde_json::Value> = if let Some(arr) = data.as_array() {
        arr.clone()
    } else if let Some(items) = data.get("items").and_then(|v| v.as_array()) {
        items.clone()
    } else if data.get("title").is_some() {
        vec![data.clone()]
    } else {
        r.errors.push(
            "Unrecognized Gemini JSON format. Expected a \"My Activity\" array \
             of activity records (MyActivity.json)."
                .to_string(),
        );
        return r;
    };

    r.records_count = records.len();

    let mut entries: Vec<Entry> = Vec::new();
    for rec in &records {
        if !rec.is_object() {
            continue;
        }

        // Keep only Gemini records when a header signals the product. Guards a
        // combined My Activity export that interleaves Search/Maps/etc.
        let header = rec
            .get("header")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if !header.is_empty() && !header.to_lowercase().contains("gemini") {
            r.skipped += 1;
            continue;
        }

        let title = rec.get("title").and_then(|v| v.as_str()).unwrap_or("").trim();
        let user_prompt = strip_prompted_prefix(title);
        let ai_response = extract_json_response(rec);

        if user_prompt.chars().count() < 3 && ai_response.chars().count() < 3 {
            continue;
        }

        let ts_iso = rec
            .get("time")
            .and_then(|v| v.as_str())
            .and_then(parse_iso8601);
        let ts_unix = ts_iso.as_deref().and_then(to_unix).unwrap_or(0);

        entries.push(Entry {
            user_prompt,
            ai_response,
            ts_iso,
            ts_unix,
        });
    }

    if entries.is_empty() {
        r.warnings
            .push("No Gemini activity records found in the JSON file.".to_string());
        return r;
    }

    // Stable sort keeps original order among equal/zero timestamps.
    entries.sort_by_key(|e| e.ts_unix);
    entries_to_chunks(&entries, &mut r);
    r
}

/// Strip the model reply out of a My Activity record: `subtitles[].name` joined,
/// falling back to `description` (some Takeout revisions put it there).
fn extract_json_response(rec: &serde_json::Value) -> String {
    if let Some(subs) = rec.get("subtitles").and_then(|v| v.as_array()) {
        let names: Vec<&str> = subs
            .iter()
            .filter_map(|s| s.get("name").and_then(|n| n.as_str()))
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .collect();
        if !names.is_empty() {
            return names.join(" ").trim().to_string();
        }
    }
    if let Some(d) = rec.get("description").and_then(|v| v.as_str()) {
        let d = d.trim();
        if !d.is_empty() {
            return d.to_string();
        }
    }
    String::new()
}

/// Strip a leading `Prompted` marker (followed by any whitespace, incl. the
/// non-breaking space Google emits). Only strips when "Prompted" is a real
/// prefix word, never inside "Promptedly".
fn strip_prompted_prefix(title: &str) -> String {
    if let Some(rest) = title.strip_prefix("Prompted") {
        if rest.starts_with(char::is_whitespace) {
            return rest.trim_start().to_string();
        }
    }
    title.to_string()
}

// ---------------------------------------------------------------------------
// Saved info paste (plain text, one fact per line)
// ---------------------------------------------------------------------------

fn parse_gemini_saved_info(content: &str) -> ParseResult {
    let mut r = ParseResult {
        format: "gemini-saved-info-text".to_string(),
        ..Default::default()
    };

    let mut total_lines = 0usize;
    let mut cleaned: Vec<String> = Vec::new();
    for raw in content.split('\n') {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        total_lines += 1;
        if is_saved_info_header(line) {
            continue;
        }
        let stripped = strip_bullet(line);
        if stripped.chars().count() >= 3 {
            cleaned.push(stripped);
        }
    }

    for batch_start in (0..cleaned.len()).step_by(CHUNK_SIZE) {
        let end = (batch_start + CHUNK_SIZE).min(cleaned.len());
        let messages: Vec<ParsedMessage> = cleaned[batch_start..end]
            .iter()
            .map(|t| ParsedMessage {
                role: "user".to_string(),
                text: t.clone(),
            })
            .collect();
        r.chunks.push(ParsedChunk {
            title: format!("Gemini saved info ({}-{})", batch_start + 1, end),
            messages,
            timestamp: None,
        });
    }

    r.total_messages = cleaned.len();
    r.records_count = total_lines;
    if cleaned.is_empty() {
        r.warnings
            .push("No saved-info items found in the pasted text.".to_string());
    }
    r
}

fn is_saved_info_header(line: &str) -> bool {
    let l = line.trim().trim_end_matches(':').trim().to_lowercase();
    matches!(
        l.as_str(),
        "saved info" | "gemini saved info" | "personal context" | "things gemini knows"
    )
}

fn strip_bullet(line: &str) -> String {
    let t = line.trim();
    // Bullet markers: -, *, • followed by whitespace.
    for b in ['-', '*', '\u{2022}'] {
        if let Some(rest) = t.strip_prefix(b) {
            if rest.starts_with(char::is_whitespace) {
                return rest.trim_start().to_string();
            }
        }
    }
    // Numbered: leading ASCII digits then '.' or ')' then whitespace.
    let digit_len = t.chars().take_while(|c| c.is_ascii_digit()).count();
    if digit_len > 0 {
        let rest = &t[digit_len..];
        let after = rest
            .strip_prefix('.')
            .or_else(|| rest.strip_prefix(')'));
        if let Some(after) = after {
            if after.starts_with(char::is_whitespace) {
                return after.trim_start().to_string();
            }
        }
    }
    t.to_string()
}

// ---------------------------------------------------------------------------
// HTML export (legacy Google Takeout "My Activity.html")
// ---------------------------------------------------------------------------
//
// Universal + lossless by design:
//   * The timestamp DELIMITER that separates prompt from reply is matched with a
//     letter-class month (`\p{L}{3,}`), so the split works in ANY language.
//   * The month NAME -> number mapping is best-effort (multi-locale table). An
//     unrecognized month yields `timestamp = None` but NEVER drops the turn —
//     content is never lost just because the date is in an unsupported locale.

/// Timestamp delimiter: `D <Month> YYYY, HH:MM:SS TZ`. Month + TZ are any-language
/// letters so the delimiter is found regardless of locale.
static TS_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(\d{1,2})\s+(\p{L}{3,})\s+(\d{4}),\s+(\d{2}):(\d{2}):(\d{2})\s+\p{L}+").unwrap()
});
/// "Prompted" followed by any whitespace (incl. the non-breaking space Google emits).
static PROMPTED_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"Prompted\s").unwrap());
static END_RESP_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?s)</div>\s*<div class="content-cell"#).unwrap());
static BR_LEAD_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^\s*<br\s*/?>\s*").unwrap());
static BR_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)<br\s*/?>").unwrap());
static BLOCK_END_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)</(p|li|h[1-6])>").unwrap());
static HR_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)<hr\s*/?>").unwrap());
static TAG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]+>").unwrap());
static MULTINEWLINE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\n{3,}").unwrap());

fn parse_gemini_html(html: &str) -> ParseResult {
    let mut r = ParseResult {
        format: "gemini-takeout-html".to_string(),
        ..Default::default()
    };

    // Split into cells by the outer-cell marker (Rust regex has no lookahead, so
    // we slice between successive marker offsets).
    let marker = "<div class=\"outer-cell";
    let starts: Vec<usize> = html.match_indices(marker).map(|(i, _)| i).collect();
    if starts.is_empty() {
        r.warnings
            .push("No conversation entries found in the HTML file.".to_string());
        return r;
    }
    r.records_count = starts.len();

    let mut entries: Vec<Entry> = Vec::new();
    for (k, &start) in starts.iter().enumerate() {
        let end = if k + 1 < starts.len() {
            starts[k + 1]
        } else {
            html.len()
        };
        if let Some(entry) = parse_html_cell(&html[start..end]) {
            entries.push(entry);
        }
    }

    if entries.is_empty() {
        r.warnings
            .push("No conversation entries found in the HTML file.".to_string());
        return r;
    }

    // Stable sort: undated entries (ts_unix == 0) keep their document order.
    entries.sort_by_key(|e| e.ts_unix);
    entries_to_chunks(&entries, &mut r);
    r
}

fn parse_html_cell(cell: &str) -> Option<Entry> {
    // Only "Prompted" cells are conversation turns; canvas/feedback cells lack it.
    let pm = PROMPTED_RE.find(cell)?;
    let after_prompted = &cell[pm.end()..];

    if let Some(cap) = TS_RE.captures(after_prompted) {
        let m0 = cap.get(0).unwrap();
        let user_prompt = strip_html(&decode_entities(&after_prompted[..m0.start()]));

        let mut after_ts = &after_prompted[m0.end()..];
        if let Some(br) = BR_LEAD_RE.find(after_ts) {
            after_ts = &after_ts[br.end()..];
        }
        let raw_resp = match END_RESP_RE.find(after_ts) {
            Some(end) => &after_ts[..end.start()],
            None => after_ts,
        };
        let ai_response = strip_html(&decode_entities(raw_resp));

        if user_prompt.chars().count() < 3 && ai_response.chars().count() < 3 {
            return None;
        }
        let (ts_iso, ts_unix) = html_timestamp(&cap);
        Some(Entry {
            user_prompt,
            ai_response,
            ts_iso,
            ts_unix,
        })
    } else {
        // Lossless: no parseable date delimiter -> keep the prompt content anyway.
        let user_prompt = strip_html(&decode_entities(after_prompted));
        if user_prompt.chars().count() < 3 {
            return None;
        }
        Some(Entry {
            user_prompt,
            ai_response: String::new(),
            ts_iso: None,
            ts_unix: 0,
        })
    }
}

/// Build (iso, unix) from a TS_RE capture. Unknown month -> (None, 0): lossless.
fn html_timestamp(cap: &regex::Captures) -> (Option<String>, i64) {
    let day: u32 = cap[1].parse().unwrap_or(0);
    let month = match month_from_token(&cap[2]) {
        Some(m) => m,
        None => return (None, 0),
    };
    let year: i32 = cap[3].parse().unwrap_or(0);
    let (h, mi, s): (u32, u32, u32) = (
        cap[4].parse().unwrap_or(0),
        cap[5].parse().unwrap_or(0),
        cap[6].parse().unwrap_or(0),
    );
    use chrono::TimeZone;
    match chrono::Utc.with_ymd_and_hms(year, month, day, h, mi, s) {
        chrono::LocalResult::Single(dt) => (Some(dt.to_rfc3339()), dt.timestamp()),
        _ => (None, 0),
    }
}

/// Month token -> 1-12 across common locales (en/pt/es/fr/de/it), by first 3
/// letters. Tolerates "Sept". Unknown -> None (caller keeps the entry, ts None).
fn month_from_token(token: &str) -> Option<u32> {
    let lower = token.to_lowercase();
    let key: String = lower.chars().take(3).collect();
    let n = match key.as_str() {
        "jan" | "gen" => 1,
        "feb" | "fev" | "fév" => 2,
        "mar" | "mär" => 3,
        "apr" | "abr" | "avr" => 4,
        "may" | "mai" | "mag" => 5,
        "jun" | "giu" | "jui" => 6, // "juin"/"juil" both start "jui"; June wins (July is rarer mis-key)
        "jul" | "lug" => 7,
        "aug" | "ago" | "aoû" | "aou" => 8,
        "sep" | "set" => 9,
        "oct" | "okt" | "ott" | "out" => 10,
        "nov" => 11,
        "dec" | "dez" | "dic" | "déc" => 12,
        _ => return None,
    };
    Some(n)
}

fn decode_entities(t: &str) -> String {
    t.replace("&#39;", "'")
        .replace("&quot;", "\"")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
}

fn strip_html(html: &str) -> String {
    let s = BR_RE.replace_all(html, "\n");
    let s = BLOCK_END_RE.replace_all(&s, "\n");
    let s = HR_RE.replace_all(&s, "\n---\n");
    let s = TAG_RE.replace_all(&s, "");
    let s = MULTINEWLINE_RE.replace_all(&s, "\n\n");
    s.trim().to_string()
}

// ---------------------------------------------------------------------------
// Shared: entries -> sessions -> chunks
// ---------------------------------------------------------------------------

fn entries_to_chunks(entries: &[Entry], r: &mut ParseResult) {
    let sessions = group_sessions(entries);
    let mut total_messages = 0usize;

    for session in &sessions {
        let mut messages: Vec<ParsedMessage> = Vec::new();
        for e in session {
            if !e.user_prompt.is_empty() {
                messages.push(ParsedMessage {
                    role: "user".to_string(),
                    text: e.user_prompt.clone(),
                });
            }
            if !e.ai_response.is_empty() {
                messages.push(ParsedMessage {
                    role: "assistant".to_string(),
                    text: e.ai_response.clone(),
                });
            }
        }
        if messages.is_empty() {
            continue;
        }
        total_messages += messages.len();
        let timestamp = session[0].ts_iso.clone();

        let total_chunks = messages.len().div_ceil(CHUNK_SIZE);
        for (i, batch_start) in (0..messages.len()).step_by(CHUNK_SIZE).enumerate() {
            let end = (batch_start + CHUNK_SIZE).min(messages.len());
            let title = if total_chunks > 1 {
                format!("Gemini session (part {}/{})", i + 1, total_chunks)
            } else {
                "Gemini session".to_string()
            };
            r.chunks.push(ParsedChunk {
                title,
                messages: messages[batch_start..end].to_vec(),
                timestamp: timestamp.clone(),
            });
        }
    }

    r.total_messages = total_messages;
}

fn group_sessions(entries: &[Entry]) -> Vec<Vec<&Entry>> {
    let mut sessions: Vec<Vec<&Entry>> = Vec::new();
    if entries.is_empty() {
        return sessions;
    }
    let mut current: Vec<&Entry> = vec![&entries[0]];
    for i in 1..entries.len() {
        let gap = entries[i].ts_unix - entries[i - 1].ts_unix;
        if gap > SESSION_GAP_SECONDS {
            sessions.push(std::mem::take(&mut current));
            current = vec![&entries[i]];
        } else {
            current.push(&entries[i]);
        }
    }
    if !current.is_empty() {
        sessions.push(current);
    }
    sessions
}

// ---------------------------------------------------------------------------
// Timestamp helpers (chrono — no regex)
// ---------------------------------------------------------------------------

fn parse_iso8601(raw: &str) -> Option<String> {
    let s = raw.trim();
    if s.is_empty() {
        return None;
    }
    let dt = chrono::DateTime::parse_from_rfc3339(s).ok()?;
    Some(dt.with_timezone(&chrono::Utc).to_rfc3339())
}

fn to_unix(iso: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(iso)
        .ok()
        .map(|d| d.timestamp())
}

// ---------------------------------------------------------------------------
// WASM bindings
// ---------------------------------------------------------------------------

#[cfg(feature = "wasm")]
mod wasm_bindings {
    use super::*;
    use wasm_bindgen::prelude::*;

    /// Parse a Gemini export (JSON or saved-info text) into a `ParseResult`.
    #[wasm_bindgen(js_name = "parseGemini")]
    pub fn parse_gemini_wasm(input: &str) -> Result<JsValue, JsError> {
        let result = parse_gemini(input);
        serde_wasm_bindgen::to_value(&result).map_err(|e| JsError::new(&e.to_string()))
    }
}

// ---------------------------------------------------------------------------
// PyO3 bindings
// ---------------------------------------------------------------------------

#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "parse_gemini")]
fn py_parse_gemini(input: &str) -> pyo3::PyResult<String> {
    let result = parse_gemini(input);
    serde_json::to_string(&result)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

#[cfg(feature = "python")]
pub fn register_python_functions(
    m: &pyo3::Bound<'_, pyo3::types::PyModule>,
) -> pyo3::PyResult<()> {
    use pyo3::types::PyModuleMethods;
    m.add_function(pyo3::wrap_pyfunction!(py_parse_gemini, m)?)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn record(title: &str, time: &str, response: Option<&str>) -> serde_json::Value {
        let mut v = serde_json::json!({
            "header": "Gemini Apps",
            "title": title,
            "time": time,
            "products": ["Gemini Apps"],
        });
        if let Some(resp) = response {
            v["subtitles"] = serde_json::json!([{ "name": resp }]);
        }
        v
    }

    fn all_texts(r: &ParseResult) -> Vec<String> {
        r.chunks
            .iter()
            .flat_map(|c| c.messages.iter().map(|m| m.text.clone()))
            .collect()
    }

    #[test]
    fn parses_my_activity_json_into_chunks() {
        let data = serde_json::json!([
            record("Prompted Plan a 3-day trip to Lisbon", "2026-05-14T09:21:03.512Z",
                   Some("Here's a 3-day Lisbon itinerary: day 1 Alfama...")),
            record("Prompted What's a good pastel de nata recipe?", "2026-05-14T09:25:10.000Z",
                   Some("Use puff pastry and an egg custard...")),
        ]);
        let r = parse_gemini(&data.to_string());
        assert!(r.errors.is_empty());
        assert_eq!(r.format, "gemini-my-activity-json");
        assert!(!r.chunks.is_empty());

        let texts = all_texts(&r);
        let roles: Vec<&str> = r
            .chunks
            .iter()
            .flat_map(|c| c.messages.iter().map(|m| m.role.as_str()))
            .collect();
        assert!(roles.contains(&"user") && roles.contains(&"assistant"));
        assert!(texts.iter().any(|t| t.contains("Plan a 3-day trip to Lisbon")));
        assert!(texts.iter().all(|t| !t.starts_with("Prompted ")));
        assert!(texts.iter().any(|t| t.contains("Lisbon itinerary")));
    }

    #[test]
    fn strips_prompted_prefix() {
        let data = serde_json::json!([record(
            "Prompted Remember I am vegetarian",
            "2026-05-14T10:00:00Z",
            Some("Noted.")
        )]);
        let r = parse_gemini(&data.to_string());
        let users: Vec<String> = r
            .chunks
            .iter()
            .flat_map(|c| c.messages.iter())
            .filter(|m| m.role == "user")
            .map(|m| m.text.clone())
            .collect();
        assert_eq!(users, vec!["Remember I am vegetarian".to_string()]);
    }

    #[test]
    fn response_falls_back_to_description() {
        let mut rec = record("Prompted Tell me a joke", "2026-05-14T11:00:00Z", None);
        rec["description"] = serde_json::json!("Why did the chicken cross the road?");
        let data = serde_json::json!([rec]);
        let r = parse_gemini(&data.to_string());
        let asst: Vec<String> = r
            .chunks
            .iter()
            .flat_map(|c| c.messages.iter())
            .filter(|m| m.role == "assistant")
            .map(|m| m.text.clone())
            .collect();
        assert!(asst.iter().any(|t| t.contains("chicken cross the road")));
    }

    #[test]
    fn skips_non_gemini_records() {
        let mut search = record("Searched for cat pictures", "2026-05-14T12:00:00Z", None);
        search["header"] = serde_json::json!("Search");
        let data = serde_json::json!([
            search,
            record("Prompted What is 2+2?", "2026-05-14T12:01:00Z", Some("4")),
        ]);
        let r = parse_gemini(&data.to_string());
        assert_eq!(r.skipped, 1);
        let texts = all_texts(&r);
        assert!(texts.iter().any(|t| t.contains("What is 2+2?")));
        assert!(!texts.iter().any(|t| t.contains("cat pictures")));
    }

    #[test]
    fn title_without_prompted_prefix() {
        let data = serde_json::json!([record(
            "How do I boil an egg",
            "2026-05-14T13:00:00Z",
            Some("Boil water...")
        )]);
        let r = parse_gemini(&data.to_string());
        let users: Vec<String> = r
            .chunks
            .iter()
            .flat_map(|c| c.messages.iter())
            .filter(|m| m.role == "user")
            .map(|m| m.text.clone())
            .collect();
        assert_eq!(users, vec!["How do I boil an egg".to_string()]);
    }

    #[test]
    fn preserves_iso8601_timestamp() {
        let data = serde_json::json!([record("Prompted hi", "2026-05-14T09:21:03.512Z", Some("hello"))]);
        let r = parse_gemini(&data.to_string());
        let ts = r.chunks[0].timestamp.as_ref().unwrap();
        assert!(ts.starts_with("2026-05-14T09:21:03"), "got {ts}");
    }

    #[test]
    fn empty_json_array_warns_not_errors() {
        let r = parse_gemini("[]");
        assert!(r.errors.is_empty());
        assert!(r.chunks.is_empty());
        assert!(!r.warnings.is_empty());
    }

    #[test]
    fn parses_saved_info_bullets() {
        let text = "Saved info\n- I work as a software engineer\n- I prefer concise answers\n* My dog is named Biscuit\n";
        let r = parse_gemini(text);
        assert!(r.errors.is_empty());
        assert_eq!(r.format, "gemini-saved-info-text");
        let texts = all_texts(&r);
        assert!(texts.contains(&"I work as a software engineer".to_string()));
        assert!(texts.contains(&"I prefer concise answers".to_string()));
        assert!(texts.contains(&"My dog is named Biscuit".to_string()));
        assert!(!texts.contains(&"Saved info".to_string()));
        assert!(texts.iter().all(|t| !t.starts_with("- ") && !t.starts_with("* ")));
    }

    // ── HTML (legacy Takeout render) ──────────────────────────────────────

    fn html_cell(prompt: &str, ts: &str, response: &str) -> String {
        format!(
            "<div class=\"outer-cell x\"><div class=\"content-cell\">\
             Prompted\u{a0}{prompt}<br>{ts}<br>{response}\
             </div><div class=\"content-cell\">meta</div></div>"
        )
    }

    #[test]
    fn html_parses_prompt_and_response() {
        let html = html_cell(
            "What is the capital of Portugal?",
            "1 Apr 2026, 18:39:35 WEST",
            "The capital of Portugal is Lisbon.",
        );
        let r = parse_gemini(&html);
        assert!(r.errors.is_empty());
        assert_eq!(r.format, "gemini-takeout-html");
        let texts = all_texts(&r);
        assert!(texts.iter().any(|t| t.contains("capital of Portugal")));
        assert!(texts.iter().any(|t| t.contains("Lisbon")));
    }

    #[test]
    fn html_handles_sept_4letter_month() {
        // Real Google Takeout (en-GB) writes September as "Sept" (4 chars).
        let html = html_cell(
            "What is Scientology?",
            "15 Sept 2024, 00:49:15 WEST",
            "A set of beliefs and practices.",
        );
        let r = parse_gemini(&html);
        let ts = r.chunks[0].timestamp.as_ref().unwrap();
        assert!(ts.contains("-09-"), "Sept must map to month 09, got {ts}");
        assert!(all_texts(&r).iter().any(|t| t.contains("Scientology")));
    }

    #[test]
    fn html_handles_non_english_month() {
        // Portuguese locale: "set" = September.
        let html = html_cell(
            "Qual a capital de Portugal?",
            "15 set 2024, 10:00:00 WEST",
            "Lisboa.",
        );
        let r = parse_gemini(&html);
        let ts = r.chunks[0].timestamp.as_ref().unwrap();
        assert!(ts.contains("-09-"), "pt 'set' must map to 09, got {ts}");
    }

    #[test]
    fn html_lossless_on_unknown_month() {
        // Unknown-locale month -> entry is KEPT (content preserved), ts None.
        let html = html_cell(
            "Where is Beirut?",
            "15 Xyz 2024, 10:00:00 WEST",
            "Beirut is the capital of Lebanon.",
        );
        let r = parse_gemini(&html);
        assert!(
            all_texts(&r).iter().any(|t| t.contains("Beirut")),
            "unknown month must not drop the turn"
        );
        assert!(r.chunks[0].timestamp.is_none());
    }

    #[test]
    fn html_lossless_when_no_timestamp() {
        // No date delimiter at all -> still keep the prompt content.
        let html = "<div class=\"outer-cell\"><div class=\"content-cell\">\
                    Prompted\u{a0}Remember my favorite colour is teal\
                    </div></div>";
        let r = parse_gemini(html);
        assert!(all_texts(&r).iter().any(|t| t.contains("favorite colour is teal")));
    }

    #[test]
    fn numbered_saved_info_markers_stripped() {
        let text = "1. First thing\n2) Second thing";
        let r = parse_gemini(text);
        let texts = all_texts(&r);
        assert!(texts.contains(&"First thing".to_string()));
        assert!(texts.contains(&"Second thing".to_string()));
    }
}
