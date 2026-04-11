//! Smart import profiling for TotalReclaw conversation imports.
//!
//! Implements a two-pass pipeline that first builds a holistic user profile
//! from conversation chunks, then uses it to triage (EXTRACT or SKIP) and
//! guide extraction with enriched context.
//!
//! All functions are pure — no I/O, no async, no network calls. LLM calls
//! are made by the client layer (TypeScript or Python); this module only
//! constructs prompts and parses responses.
//!
//! # Pipeline
//!
//! 1. **Summarize** — `chunks_to_summaries()` extracts first+last messages
//! 2. **Profile batch** — `build_profile_batch_prompt()` + `parse_profile_batch_response()`
//! 3. **Profile merge** — `build_profile_merge_prompt()` + `parse_profile_response()`
//! 4. **Triage** — `build_triage_prompt()` + `parse_triage_response()`
//! 5. **Enrich** — `enrich_extraction_prompt()` injects profile into extraction prompt

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

/// A single conversation chunk from an import adapter (e.g. Gemini, ChatGPT).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConversationChunk {
    pub index: usize,
    pub title: Option<String>,
    pub messages: Vec<ChunkMessage>,
    pub timestamp: Option<String>,
}

/// A message within a conversation chunk.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChunkMessage {
    pub role: String,
    pub content: String,
}

/// Condensed summary of a conversation chunk (first + last message).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChunkSummary {
    pub index: usize,
    pub title: String,
    pub first_message: String,
    pub last_message: String,
    pub message_count: usize,
    pub timestamp: Option<String>,
}

/// Partial user profile extracted from a batch of conversation summaries.
///
/// All fields are optional because a single batch may not contain evidence
/// for every category.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PartialProfile {
    pub identity: Option<String>,
    pub themes: Option<Vec<String>>,
    pub projects: Option<Vec<String>>,
    pub stack: Option<Vec<String>>,
    pub decisions: Option<Vec<String>>,
    pub interests: Option<Vec<String>>,
    pub skip_patterns: Option<Vec<String>>,
}

/// Complete user profile after merging all partial profiles.
///
/// Non-optional fields — the merge prompt is expected to produce a
/// complete profile. Parsing falls back to empty vecs/strings on failure.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct UserProfile {
    pub identity: Option<String>,
    pub themes: Vec<String>,
    pub projects: Vec<String>,
    pub stack: Vec<String>,
    pub decisions: Vec<String>,
    pub interests: Vec<String>,
    pub skip_patterns: Vec<String>,
}

/// Triage decision for a single conversation chunk.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChunkDecision {
    pub chunk_index: usize,
    pub decision: TriageDecision,
    pub reason: String,
}

/// Whether a chunk should be processed for fact extraction or skipped.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum TriageDecision {
    Extract,
    Skip,
}

impl std::fmt::Display for TriageDecision {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TriageDecision::Extract => write!(f, "EXTRACT"),
            TriageDecision::Skip => write!(f, "SKIP"),
        }
    }
}

// ---------------------------------------------------------------------------
// Step 0: Chunk summarization
// ---------------------------------------------------------------------------

/// Extract first and last user messages from each conversation chunk.
///
/// For each chunk, finds the first user message and the last message
/// (of any role) to produce a compact summary for profiling.
pub fn chunks_to_summaries(chunks: &[ConversationChunk]) -> Vec<ChunkSummary> {
    chunks
        .iter()
        .map(|chunk| {
            let first_message = chunk
                .messages
                .iter()
                .find(|m| m.role == "user")
                .map(|m| truncate_message(&m.content, 300))
                .unwrap_or_default();

            let last_message = chunk
                .messages
                .last()
                .map(|m| truncate_message(&m.content, 300))
                .unwrap_or_default();

            let title = chunk
                .title
                .clone()
                .unwrap_or_else(|| "Untitled".to_string());

            ChunkSummary {
                index: chunk.index,
                title,
                first_message,
                last_message,
                message_count: chunk.messages.len(),
                timestamp: chunk.timestamp.clone(),
            }
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Step 1a: Batch profile prompt
// ---------------------------------------------------------------------------

/// Build the profiling prompt for a batch of conversation summaries.
///
/// Groups up to 50 summaries into a single prompt that asks the LLM to
/// describe who the user is based on their conversation patterns.
pub fn build_profile_batch_prompt(summaries: &[ChunkSummary]) -> String {
    let mut conversations = String::new();

    for summary in summaries {
        let ts = summary
            .timestamp
            .as_deref()
            .map(|t| format!(" ({})", t))
            .unwrap_or_default();

        conversations.push_str(&format!(
            "{}. \"{}\"{}  [{} messages]\n   First: \"{}\"\n   Last:  \"{}\"\n\n",
            summary.index + 1,
            summary.title,
            ts,
            summary.message_count,
            summary.first_message,
            summary.last_message,
        ));
    }

    format!(
        r#"Analyze these conversation excerpts and describe who this user is.

For each conversation, you see the title, the user's first message, and the final message in the thread.

[CONVERSATIONS]
{conversations}
Based on ALL conversations above, produce a structured JSON profile of this user. Include only what is evidenced — do not speculate.

Return a JSON object (no markdown fences, no commentary):
{{
  "identity": "name, role, location, company if mentioned — or null if unknown",
  "themes": ["recurring topics that appear multiple times"],
  "projects": ["named projects or ongoing work streams"],
  "stack": ["languages, frameworks, tools, cloud providers, databases"],
  "decisions": ["specific choices made, e.g. 'chose Polars over Pandas for large dataset performance'"],
  "interests": ["personal interests, hobbies, preferences, workflow habits"],
  "skip_patterns": ["types of conversations that are generic Q&A with no personal value, e.g. 'recipe lookups', 'weather queries'"]
}}"#
    )
}

/// Parse the LLM response from a batch profiling call into a `PartialProfile`.
///
/// Lenient: handles JSON with or without code fences, missing fields,
/// and various formatting quirks.
pub fn parse_profile_batch_response(llm_output: &str) -> PartialProfile {
    let cleaned = strip_code_fences(llm_output.trim());

    match serde_json::from_str::<serde_json::Value>(&cleaned) {
        Ok(serde_json::Value::Object(obj)) => parse_partial_profile_from_object(&obj),
        _ => {
            // Try to find a JSON object in the response
            if let Some(start) = cleaned.find('{') {
                if let Some(end) = cleaned.rfind('}') {
                    if start < end {
                        let slice = &cleaned[start..=end];
                        if let Ok(serde_json::Value::Object(obj)) =
                            serde_json::from_str::<serde_json::Value>(slice)
                        {
                            return parse_partial_profile_from_object(&obj);
                        }
                    }
                }
            }
            PartialProfile::default()
        }
    }
}

// ---------------------------------------------------------------------------
// Step 1b: Profile merge prompt
// ---------------------------------------------------------------------------

/// Build the merge prompt that combines N partial profiles into one.
///
/// Each partial profile is serialized as JSON for the LLM to consolidate.
pub fn build_profile_merge_prompt(partials: &[PartialProfile]) -> String {
    let mut profiles_text = String::new();

    for (i, partial) in partials.iter().enumerate() {
        let json = serde_json::to_string_pretty(partial).unwrap_or_default();
        profiles_text.push_str(&format!("--- Batch {} ---\n{}\n\n", i + 1, json));
    }

    format!(
        r#"You are merging multiple partial user profiles into one coherent profile.

Each profile below was extracted from a different batch of the same user's conversations. Combine them by:
- Deduplicating entries that are the same concept (e.g. "Python" appearing in multiple batches)
- Keeping the most specific version when entries overlap (e.g. "Python 3.11" over "Python")
- Preserving all unique information across batches
- For identity, merge fragments into the most complete description

[PARTIAL PROFILES]
{profiles_text}
Return a single merged JSON profile (no markdown fences, no commentary):
{{
  "identity": "most complete identity description, or null if no identity info found",
  "themes": ["all unique recurring themes, deduplicated"],
  "projects": ["all unique projects, deduplicated"],
  "stack": ["all unique technologies, deduplicated, most specific versions"],
  "decisions": ["all unique decisions with reasoning"],
  "interests": ["all unique interests, deduplicated"],
  "skip_patterns": ["all unique skip patterns, deduplicated"]
}}"#
    )
}

/// Parse the LLM response from the merge call into a `UserProfile`.
///
/// This is the final profile — all fields resolve to non-optional types
/// (empty vec if missing).
pub fn parse_profile_response(llm_output: &str) -> UserProfile {
    let cleaned = strip_code_fences(llm_output.trim());

    let obj = match serde_json::from_str::<serde_json::Value>(&cleaned) {
        Ok(serde_json::Value::Object(obj)) => obj,
        _ => {
            // Try to find a JSON object in the response
            if let Some(start) = cleaned.find('{') {
                if let Some(end) = cleaned.rfind('}') {
                    if start < end {
                        let slice = &cleaned[start..=end];
                        if let Ok(serde_json::Value::Object(obj)) =
                            serde_json::from_str::<serde_json::Value>(slice)
                        {
                            obj
                        } else {
                            return UserProfile::default();
                        }
                    } else {
                        return UserProfile::default();
                    }
                } else {
                    return UserProfile::default();
                }
            } else {
                return UserProfile::default();
            }
        }
    };

    UserProfile {
        identity: obj
            .get("identity")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty() && *s != "null")
            .map(|s| s.to_string()),
        themes: extract_string_array(&obj, "themes"),
        projects: extract_string_array(&obj, "projects"),
        stack: extract_string_array(&obj, "stack"),
        decisions: extract_string_array(&obj, "decisions"),
        interests: extract_string_array(&obj, "interests"),
        skip_patterns: extract_string_array(&obj, "skip_patterns"),
    }
}

// ---------------------------------------------------------------------------
// Step 1.5: Triage prompt
// ---------------------------------------------------------------------------

/// Build the triage prompt that classifies each chunk as EXTRACT or SKIP.
///
/// Uses the merged profile for context and processes summaries in batches.
pub fn build_triage_prompt(profile: &UserProfile, summaries: &[ChunkSummary]) -> String {
    let profile_text = format_profile_for_prompt(profile);

    let mut conversations = String::new();

    for summary in summaries {
        let ts = summary
            .timestamp
            .as_deref()
            .map(|t| format!(" | {}", t))
            .unwrap_or_default();

        conversations.push_str(&format!(
            "{}. Title: \"{}\"{}  [{} msgs]\n   First: \"{}\"\n   Last:  \"{}\"\n",
            summary.index + 1,
            summary.title,
            ts,
            summary.message_count,
            summary.first_message,
            summary.last_message,
        ));
    }

    format!(
        r#"You are classifying conversations for import into a personal memory system.

[USER PROFILE]
{profile_text}

For each conversation below, decide:
- EXTRACT: Contains personal facts, decisions, preferences, project details, technical choices, or anything this specific user would want remembered long-term. When in doubt, lean toward EXTRACT.
- SKIP: Generic Q&A, one-off lookups, trivial requests with no personal value (e.g. "what's the capital of France?", recipe requests, simple translations).

[CONVERSATIONS]
{conversations}
Return a JSON array (no markdown fences, no commentary):
[{{"index": 0, "decision": "EXTRACT", "reason": "discusses their ML pipeline project"}}, ...]

Use 0-based indices matching the conversation numbers minus 1 (conversation 1 = index 0)."#
    )
}

/// Parse the LLM triage response into a list of `ChunkDecision`s.
///
/// Lenient: handles JSON arrays, missing fields, and case-insensitive decisions.
pub fn parse_triage_response(llm_output: &str) -> Vec<ChunkDecision> {
    let cleaned = strip_code_fences(llm_output.trim());

    let arr = match serde_json::from_str::<serde_json::Value>(&cleaned) {
        Ok(serde_json::Value::Array(arr)) => arr,
        _ => {
            // Try to find a JSON array in the response
            if let Some(start) = cleaned.find('[') {
                if let Some(end) = cleaned.rfind(']') {
                    if start < end {
                        let slice = &cleaned[start..=end];
                        if let Ok(serde_json::Value::Array(arr)) =
                            serde_json::from_str::<serde_json::Value>(slice)
                        {
                            arr
                        } else {
                            return Vec::new();
                        }
                    } else {
                        return Vec::new();
                    }
                } else {
                    return Vec::new();
                }
            } else {
                return Vec::new();
            }
        }
    };

    let mut decisions = Vec::new();

    for entry in arr {
        let obj = match entry.as_object() {
            Some(o) => o,
            None => continue,
        };

        let chunk_index = match obj
            .get("index")
            .or_else(|| obj.get("chunk_index"))
            .and_then(|v| v.as_u64())
        {
            Some(i) => i as usize,
            None => continue,
        };

        let decision_str = obj
            .get("decision")
            .and_then(|v| v.as_str())
            .unwrap_or("EXTRACT");

        let decision = match decision_str.to_uppercase().as_str() {
            "SKIP" => TriageDecision::Skip,
            _ => TriageDecision::Extract, // default to EXTRACT when in doubt
        };

        let reason = obj
            .get("reason")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        decisions.push(ChunkDecision {
            chunk_index,
            decision,
            reason,
        });
    }

    decisions
}

// ---------------------------------------------------------------------------
// Step 2: Extraction prompt enrichment
// ---------------------------------------------------------------------------

/// Inject a user profile into the base extraction prompt for context-aware extraction.
///
/// Prepends a profile context block so the LLM can focus on facts relevant
/// to this specific user's work, decisions, and preferences.
pub fn enrich_extraction_prompt(profile: &UserProfile, base_prompt: &str) -> String {
    let profile_text = format_profile_for_prompt(profile);

    format!(
        r#"You are extracting facts from a conversation by a user with this background:

{profile_text}

Given this context:
- Focus on facts relevant to their work, projects, and technical decisions
- Extract preferences and workflow habits specific to this user
- For decisions, always include the reasoning ("chose X because Y")
- Skip generic information that is not specific to this user
- Prioritize information that connects to their known projects and themes

{base_prompt}"#
    )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Strip markdown code fences from an LLM response.
fn strip_code_fences(s: &str) -> String {
    let mut result = s.to_string();
    if result.starts_with("```") {
        // Remove opening fence (```json, ```JSON, ``` etc.)
        if let Some(pos) = result.find('\n') {
            result = result[pos + 1..].to_string();
        }
        // Remove closing fence
        if result.ends_with("```") {
            result = result[..result.len() - 3].trim_end().to_string();
        }
    }
    result
}

/// Truncate a message to a maximum length, appending "..." if truncated.
fn truncate_message(s: &str, max_len: usize) -> String {
    let trimmed = s.trim();
    if trimmed.len() <= max_len {
        trimmed.to_string()
    } else {
        format!("{}...", &trimmed[..max_len])
    }
}

/// Extract a string array from a JSON object field.
///
/// Returns empty vec if the field is missing or not an array.
fn extract_string_array(
    obj: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Vec<String> {
    obj.get(key)
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

/// Parse a PartialProfile from a JSON object.
fn parse_partial_profile_from_object(
    obj: &serde_json::Map<String, serde_json::Value>,
) -> PartialProfile {
    PartialProfile {
        identity: obj
            .get("identity")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty() && *s != "null")
            .map(|s| s.to_string()),
        themes: non_empty_array(obj, "themes"),
        projects: non_empty_array(obj, "projects"),
        stack: non_empty_array(obj, "stack"),
        decisions: non_empty_array(obj, "decisions"),
        interests: non_empty_array(obj, "interests"),
        skip_patterns: non_empty_array(obj, "skip_patterns"),
    }
}

/// Extract an optional non-empty string array from a JSON object.
fn non_empty_array(
    obj: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Option<Vec<String>> {
    let arr = extract_string_array(obj, key);
    if arr.is_empty() {
        None
    } else {
        Some(arr)
    }
}

/// Format a UserProfile as human-readable text for prompt injection.
fn format_profile_for_prompt(profile: &UserProfile) -> String {
    let mut parts = Vec::new();

    if let Some(ref identity) = profile.identity {
        parts.push(format!("Identity: {}", identity));
    }

    if !profile.themes.is_empty() {
        parts.push(format!("Recurring themes: {}", profile.themes.join(", ")));
    }

    if !profile.projects.is_empty() {
        parts.push(format!("Active projects: {}", profile.projects.join(", ")));
    }

    if !profile.stack.is_empty() {
        parts.push(format!("Technical stack: {}", profile.stack.join(", ")));
    }

    if !profile.decisions.is_empty() {
        parts.push(format!(
            "Key decisions:\n{}",
            profile
                .decisions
                .iter()
                .map(|d| format!("  - {}", d))
                .collect::<Vec<_>>()
                .join("\n")
        ));
    }

    if !profile.interests.is_empty() {
        parts.push(format!("Interests: {}", profile.interests.join(", ")));
    }

    if !profile.skip_patterns.is_empty() {
        parts.push(format!(
            "Skip patterns (generic Q&A): {}",
            profile.skip_patterns.join(", ")
        ));
    }

    if parts.is_empty() {
        "No profile information available.".to_string()
    } else {
        parts.join("\n")
    }
}

// ---------------------------------------------------------------------------
// WASM bindings (feature-gated)
// ---------------------------------------------------------------------------

#[cfg(feature = "wasm")]
mod wasm_bindings {
    use super::*;
    use wasm_bindgen::prelude::*;

    /// Convert conversation chunks to summaries.
    ///
    /// `chunks_json`: JSON array of ConversationChunk objects.
    /// Returns a JsValue (JSON array of ChunkSummary objects).
    #[wasm_bindgen(js_name = "chunksToSummaries")]
    pub fn wasm_chunks_to_summaries(chunks_json: &str) -> Result<JsValue, JsError> {
        let chunks: Vec<ConversationChunk> = serde_json::from_str(chunks_json)
            .map_err(|e| JsError::new(&format!("Invalid chunks JSON: {}", e)))?;
        let summaries = chunks_to_summaries(&chunks);
        serde_wasm_bindgen::to_value(&summaries).map_err(|e| JsError::new(&e.to_string()))
    }

    /// Build the profiling prompt for a batch of conversation summaries.
    ///
    /// `summaries_json`: JSON array of ChunkSummary objects.
    /// Returns the prompt string.
    #[wasm_bindgen(js_name = "buildProfileBatchPrompt")]
    pub fn wasm_build_profile_batch_prompt(summaries_json: &str) -> Result<String, JsError> {
        let summaries: Vec<ChunkSummary> = serde_json::from_str(summaries_json)
            .map_err(|e| JsError::new(&format!("Invalid summaries JSON: {}", e)))?;
        Ok(build_profile_batch_prompt(&summaries))
    }

    /// Parse a batch profiling LLM response into a PartialProfile.
    ///
    /// `llm_output`: Raw LLM response string.
    /// Returns a JsValue (PartialProfile object).
    #[wasm_bindgen(js_name = "parseProfileBatchResponse")]
    pub fn wasm_parse_profile_batch_response(llm_output: &str) -> Result<JsValue, JsError> {
        let profile = parse_profile_batch_response(llm_output);
        serde_wasm_bindgen::to_value(&profile).map_err(|e| JsError::new(&e.to_string()))
    }

    /// Build the merge prompt that combines partial profiles.
    ///
    /// `partials_json`: JSON array of PartialProfile objects.
    /// Returns the prompt string.
    #[wasm_bindgen(js_name = "buildProfileMergePrompt")]
    pub fn wasm_build_profile_merge_prompt(partials_json: &str) -> Result<String, JsError> {
        let partials: Vec<PartialProfile> = serde_json::from_str(partials_json)
            .map_err(|e| JsError::new(&format!("Invalid partials JSON: {}", e)))?;
        Ok(build_profile_merge_prompt(&partials))
    }

    /// Parse the merge LLM response into a UserProfile.
    ///
    /// `llm_output`: Raw LLM response string.
    /// Returns a JsValue (UserProfile object).
    #[wasm_bindgen(js_name = "parseProfileResponse")]
    pub fn wasm_parse_profile_response(llm_output: &str) -> Result<JsValue, JsError> {
        let profile = parse_profile_response(llm_output);
        serde_wasm_bindgen::to_value(&profile).map_err(|e| JsError::new(&e.to_string()))
    }

    /// Build the triage prompt for classifying chunks.
    ///
    /// `profile_json`: JSON string of a UserProfile.
    /// `summaries_json`: JSON array of ChunkSummary objects.
    /// Returns the prompt string.
    #[wasm_bindgen(js_name = "buildTriagePrompt")]
    pub fn wasm_build_triage_prompt(
        profile_json: &str,
        summaries_json: &str,
    ) -> Result<String, JsError> {
        let profile: UserProfile = serde_json::from_str(profile_json)
            .map_err(|e| JsError::new(&format!("Invalid profile JSON: {}", e)))?;
        let summaries: Vec<ChunkSummary> = serde_json::from_str(summaries_json)
            .map_err(|e| JsError::new(&format!("Invalid summaries JSON: {}", e)))?;
        Ok(build_triage_prompt(&profile, &summaries))
    }

    /// Parse the triage LLM response into chunk decisions.
    ///
    /// `llm_output`: Raw LLM response string.
    /// Returns a JsValue (JSON array of ChunkDecision objects).
    #[wasm_bindgen(js_name = "parseTriageResponse")]
    pub fn wasm_parse_triage_response(llm_output: &str) -> Result<JsValue, JsError> {
        let decisions = parse_triage_response(llm_output);
        serde_wasm_bindgen::to_value(&decisions).map_err(|e| JsError::new(&e.to_string()))
    }

    /// Enrich an extraction prompt with user profile context.
    ///
    /// `profile_json`: JSON string of a UserProfile.
    /// `base_prompt`: The base extraction prompt to enrich.
    /// Returns the enriched prompt string.
    #[wasm_bindgen(js_name = "enrichExtractionPrompt")]
    pub fn wasm_enrich_extraction_prompt(
        profile_json: &str,
        base_prompt: &str,
    ) -> Result<String, JsError> {
        let profile: UserProfile = serde_json::from_str(profile_json)
            .map_err(|e| JsError::new(&format!("Invalid profile JSON: {}", e)))?;
        Ok(enrich_extraction_prompt(&profile, base_prompt))
    }
}

// ---------------------------------------------------------------------------
// PyO3 bindings (feature-gated)
// ---------------------------------------------------------------------------

/// PyO3 binding: Convert conversation chunks to summaries.
///
/// Args:
///     chunks_json: JSON array of ConversationChunk objects.
///
/// Returns:
///     JSON string of ChunkSummary array.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "chunks_to_summaries")]
fn py_chunks_to_summaries(chunks_json: &str) -> pyo3::PyResult<String> {
    let chunks: Vec<ConversationChunk> = serde_json::from_str(chunks_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid chunks JSON: {}", e)))?;
    let summaries = chunks_to_summaries(&chunks);
    serde_json::to_string(&summaries)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// PyO3 binding: Build the profiling prompt for a batch of summaries.
///
/// Args:
///     summaries_json: JSON array of ChunkSummary objects.
///
/// Returns:
///     Prompt string.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "build_profile_batch_prompt")]
fn py_build_profile_batch_prompt(summaries_json: &str) -> pyo3::PyResult<String> {
    let summaries: Vec<ChunkSummary> = serde_json::from_str(summaries_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid summaries JSON: {}", e)))?;
    Ok(build_profile_batch_prompt(&summaries))
}

/// PyO3 binding: Parse a batch profiling LLM response into a PartialProfile.
///
/// Args:
///     llm_output: Raw LLM response string.
///
/// Returns:
///     JSON string of PartialProfile.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "parse_profile_batch_response")]
fn py_parse_profile_batch_response(llm_output: &str) -> pyo3::PyResult<String> {
    let profile = parse_profile_batch_response(llm_output);
    serde_json::to_string(&profile)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// PyO3 binding: Build the merge prompt for partial profiles.
///
/// Args:
///     partials_json: JSON array of PartialProfile objects.
///
/// Returns:
///     Prompt string.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "build_profile_merge_prompt")]
fn py_build_profile_merge_prompt(partials_json: &str) -> pyo3::PyResult<String> {
    let partials: Vec<PartialProfile> = serde_json::from_str(partials_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid partials JSON: {}", e)))?;
    Ok(build_profile_merge_prompt(&partials))
}

/// PyO3 binding: Parse the merge LLM response into a UserProfile.
///
/// Args:
///     llm_output: Raw LLM response string.
///
/// Returns:
///     JSON string of UserProfile.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "parse_profile_response")]
fn py_parse_profile_response(llm_output: &str) -> pyo3::PyResult<String> {
    let profile = parse_profile_response(llm_output);
    serde_json::to_string(&profile)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// PyO3 binding: Build the triage prompt.
///
/// Args:
///     profile_json: JSON string of a UserProfile.
///     summaries_json: JSON array of ChunkSummary objects.
///
/// Returns:
///     Prompt string.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "build_triage_prompt")]
fn py_build_triage_prompt(profile_json: &str, summaries_json: &str) -> pyo3::PyResult<String> {
    let profile: UserProfile = serde_json::from_str(profile_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid profile JSON: {}", e)))?;
    let summaries: Vec<ChunkSummary> = serde_json::from_str(summaries_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid summaries JSON: {}", e)))?;
    Ok(build_triage_prompt(&profile, &summaries))
}

/// PyO3 binding: Parse the triage LLM response.
///
/// Args:
///     llm_output: Raw LLM response string.
///
/// Returns:
///     JSON string of ChunkDecision array.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "parse_triage_response")]
fn py_parse_triage_response(llm_output: &str) -> pyo3::PyResult<String> {
    let decisions = parse_triage_response(llm_output);
    serde_json::to_string(&decisions)
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))
}

/// PyO3 binding: Enrich an extraction prompt with profile context.
///
/// Args:
///     profile_json: JSON string of a UserProfile.
///     base_prompt: The base extraction prompt to enrich.
///
/// Returns:
///     Enriched prompt string.
#[cfg(feature = "python")]
#[pyo3::prelude::pyfunction]
#[pyo3(name = "enrich_extraction_prompt")]
fn py_enrich_extraction_prompt(profile_json: &str, base_prompt: &str) -> pyo3::PyResult<String> {
    let profile: UserProfile = serde_json::from_str(profile_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid profile JSON: {}", e)))?;
    Ok(enrich_extraction_prompt(&profile, base_prompt))
}

/// Register smart import functions on the PyO3 module.
///
/// Called from the main `python.rs` module registration.
#[cfg(feature = "python")]
pub fn register_python_functions(
    m: &pyo3::prelude::Bound<'_, pyo3::prelude::PyModule>,
) -> pyo3::PyResult<()> {
    use pyo3::prelude::*;
    m.add_function(pyo3::wrap_pyfunction!(py_chunks_to_summaries, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_build_profile_batch_prompt, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_parse_profile_batch_response, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_build_profile_merge_prompt, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_parse_profile_response, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_build_triage_prompt, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_parse_triage_response, m)?)?;
    m.add_function(pyo3::wrap_pyfunction!(py_enrich_extraction_prompt, m)?)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- Test data helpers --

    fn make_chunks() -> Vec<ConversationChunk> {
        vec![
            ConversationChunk {
                index: 0,
                title: Some("Kubernetes setup".to_string()),
                messages: vec![
                    ChunkMessage {
                        role: "user".to_string(),
                        content: "How do I set up a Kubernetes cluster on GCP?".to_string(),
                    },
                    ChunkMessage {
                        role: "assistant".to_string(),
                        content: "Here's how to set up GKE...".to_string(),
                    },
                    ChunkMessage {
                        role: "user".to_string(),
                        content: "Thanks, the nodepool autoscaling config worked perfectly.".to_string(),
                    },
                ],
                timestamp: Some("2025-01-15T10:00:00Z".to_string()),
            },
            ConversationChunk {
                index: 1,
                title: Some("Pasta recipe".to_string()),
                messages: vec![
                    ChunkMessage {
                        role: "user".to_string(),
                        content: "How do I make pasta carbonara?".to_string(),
                    },
                    ChunkMessage {
                        role: "assistant".to_string(),
                        content: "Classic carbonara uses eggs, pecorino, guanciale...".to_string(),
                    },
                ],
                timestamp: None,
            },
            ConversationChunk {
                index: 2,
                title: None,
                messages: vec![
                    ChunkMessage {
                        role: "user".to_string(),
                        content: "I'm switching from pandas to polars for our 50GB dataset pipeline".to_string(),
                    },
                    ChunkMessage {
                        role: "assistant".to_string(),
                        content: "Good choice. Polars handles large datasets much better...".to_string(),
                    },
                    ChunkMessage {
                        role: "user".to_string(),
                        content: "The benchmark shows 3x speedup. Deploying to production next week.".to_string(),
                    },
                ],
                timestamp: Some("2025-02-01T14:30:00Z".to_string()),
            },
        ]
    }

    fn make_profile() -> UserProfile {
        UserProfile {
            identity: Some("Maria, senior data engineer at DataCorp, Berlin".to_string()),
            themes: vec![
                "data pipeline optimization".to_string(),
                "cloud infrastructure".to_string(),
                "team tooling".to_string(),
            ],
            projects: vec![
                "recommendation engine v2".to_string(),
                "polars migration".to_string(),
            ],
            stack: vec![
                "Python".to_string(),
                "Polars".to_string(),
                "GCP".to_string(),
                "Kubernetes".to_string(),
            ],
            decisions: vec![
                "chose Polars over Pandas for 3x performance on 50GB datasets".to_string(),
                "chose GCP over AWS for existing Terraform expertise".to_string(),
            ],
            interests: vec!["dark mode".to_string(), "JetBrains IDEs".to_string()],
            skip_patterns: vec![
                "recipe lookups".to_string(),
                "weather queries".to_string(),
                "translation requests".to_string(),
            ],
        }
    }

    // -- chunks_to_summaries --

    #[test]
    fn test_chunks_to_summaries_basic() {
        let chunks = make_chunks();
        let summaries = chunks_to_summaries(&chunks);

        assert_eq!(summaries.len(), 3);

        // First chunk
        assert_eq!(summaries[0].index, 0);
        assert_eq!(summaries[0].title, "Kubernetes setup");
        assert_eq!(
            summaries[0].first_message,
            "How do I set up a Kubernetes cluster on GCP?"
        );
        assert_eq!(
            summaries[0].last_message,
            "Thanks, the nodepool autoscaling config worked perfectly."
        );
        assert_eq!(summaries[0].message_count, 3);
        assert_eq!(
            summaries[0].timestamp,
            Some("2025-01-15T10:00:00Z".to_string())
        );

        // Second chunk (no timestamp)
        assert_eq!(summaries[1].index, 1);
        assert_eq!(summaries[1].title, "Pasta recipe");
        assert!(summaries[1].timestamp.is_none());

        // Third chunk (no title)
        assert_eq!(summaries[2].index, 2);
        assert_eq!(summaries[2].title, "Untitled");
    }

    #[test]
    fn test_chunks_to_summaries_empty() {
        let summaries = chunks_to_summaries(&[]);
        assert!(summaries.is_empty());
    }

    #[test]
    fn test_chunks_to_summaries_no_user_message() {
        let chunks = vec![ConversationChunk {
            index: 0,
            title: Some("System only".to_string()),
            messages: vec![ChunkMessage {
                role: "assistant".to_string(),
                content: "Hello, how can I help?".to_string(),
            }],
            timestamp: None,
        }];
        let summaries = chunks_to_summaries(&chunks);
        assert_eq!(summaries[0].first_message, ""); // no user message
        assert_eq!(summaries[0].last_message, "Hello, how can I help?");
    }

    #[test]
    fn test_chunks_to_summaries_truncation() {
        let long_msg = "x".repeat(500);
        let chunks = vec![ConversationChunk {
            index: 0,
            title: Some("Long".to_string()),
            messages: vec![ChunkMessage {
                role: "user".to_string(),
                content: long_msg,
            }],
            timestamp: None,
        }];
        let summaries = chunks_to_summaries(&chunks);
        assert_eq!(summaries[0].first_message.len(), 303); // 300 + "..."
        assert!(summaries[0].first_message.ends_with("..."));
    }

    // -- build_profile_batch_prompt --

    #[test]
    fn test_build_profile_batch_prompt_structure() {
        let chunks = make_chunks();
        let summaries = chunks_to_summaries(&chunks);
        let prompt = build_profile_batch_prompt(&summaries);

        assert!(prompt.contains("Analyze these conversation excerpts"));
        assert!(prompt.contains("Kubernetes setup"));
        assert!(prompt.contains("Pasta recipe"));
        assert!(prompt.contains("\"identity\""));
        assert!(prompt.contains("\"themes\""));
        assert!(prompt.contains("\"projects\""));
        assert!(prompt.contains("\"stack\""));
        assert!(prompt.contains("\"decisions\""));
        assert!(prompt.contains("\"interests\""));
        assert!(prompt.contains("\"skip_patterns\""));
        assert!(prompt.contains("2025-01-15T10:00:00Z"));
    }

    #[test]
    fn test_build_profile_batch_prompt_empty() {
        let prompt = build_profile_batch_prompt(&[]);
        assert!(prompt.contains("Analyze these conversation excerpts"));
        assert!(prompt.contains("[CONVERSATIONS]"));
    }

    // -- parse_profile_batch_response --

    #[test]
    fn test_parse_profile_batch_response_valid_json() {
        let input = r#"{
            "identity": "Maria, data engineer at DataCorp",
            "themes": ["data pipelines", "cloud migration"],
            "projects": ["polars migration"],
            "stack": ["Python", "Polars", "GCP"],
            "decisions": ["chose Polars over Pandas for performance"],
            "interests": ["dark mode"],
            "skip_patterns": ["recipe lookups"]
        }"#;

        let profile = parse_profile_batch_response(input);
        assert_eq!(
            profile.identity,
            Some("Maria, data engineer at DataCorp".to_string())
        );
        assert_eq!(profile.themes, Some(vec!["data pipelines".to_string(), "cloud migration".to_string()]));
        assert_eq!(profile.projects, Some(vec!["polars migration".to_string()]));
        assert_eq!(profile.stack, Some(vec!["Python".to_string(), "Polars".to_string(), "GCP".to_string()]));
        assert_eq!(profile.decisions, Some(vec!["chose Polars over Pandas for performance".to_string()]));
        assert_eq!(profile.interests, Some(vec!["dark mode".to_string()]));
        assert_eq!(profile.skip_patterns, Some(vec!["recipe lookups".to_string()]));
    }

    #[test]
    fn test_parse_profile_batch_response_with_code_fences() {
        let input = r#"```json
{
    "identity": "Maria, data engineer",
    "themes": ["ML pipelines"],
    "projects": [],
    "stack": ["Python"],
    "decisions": [],
    "interests": [],
    "skip_patterns": []
}
```"#;

        let profile = parse_profile_batch_response(input);
        assert_eq!(
            profile.identity,
            Some("Maria, data engineer".to_string())
        );
        assert_eq!(profile.themes, Some(vec!["ML pipelines".to_string()]));
        // Empty arrays produce None for PartialProfile
        assert!(profile.projects.is_none());
    }

    #[test]
    fn test_parse_profile_batch_response_partial_fields() {
        let input = r#"{"identity": "John", "stack": ["Rust", "TypeScript"]}"#;

        let profile = parse_profile_batch_response(input);
        assert_eq!(profile.identity, Some("John".to_string()));
        assert_eq!(
            profile.stack,
            Some(vec!["Rust".to_string(), "TypeScript".to_string()])
        );
        assert!(profile.themes.is_none());
        assert!(profile.projects.is_none());
    }

    #[test]
    fn test_parse_profile_batch_response_null_identity() {
        let input = r#"{"identity": null, "themes": ["testing"]}"#;

        let profile = parse_profile_batch_response(input);
        assert!(profile.identity.is_none());
        assert_eq!(profile.themes, Some(vec!["testing".to_string()]));
    }

    #[test]
    fn test_parse_profile_batch_response_invalid_json() {
        let profile = parse_profile_batch_response("not json at all");
        assert!(profile.identity.is_none());
        assert!(profile.themes.is_none());
    }

    #[test]
    fn test_parse_profile_batch_response_empty() {
        let profile = parse_profile_batch_response("");
        assert!(profile.identity.is_none());
    }

    #[test]
    fn test_parse_profile_batch_response_json_with_surrounding_text() {
        let input = r#"Here is the profile:
        {"identity": "Bob the Builder", "themes": ["construction"]}
        Hope this helps!"#;

        let profile = parse_profile_batch_response(input);
        assert_eq!(
            profile.identity,
            Some("Bob the Builder".to_string())
        );
        assert_eq!(profile.themes, Some(vec!["construction".to_string()]));
    }

    // -- build_profile_merge_prompt --

    #[test]
    fn test_build_profile_merge_prompt_structure() {
        let partials = vec![
            PartialProfile {
                identity: Some("Maria, data engineer".to_string()),
                themes: Some(vec!["data pipelines".to_string()]),
                ..Default::default()
            },
            PartialProfile {
                identity: Some("Maria, senior data engineer at DataCorp".to_string()),
                stack: Some(vec!["Python".to_string(), "GCP".to_string()]),
                ..Default::default()
            },
        ];

        let prompt = build_profile_merge_prompt(&partials);
        assert!(prompt.contains("merging multiple partial user profiles"));
        assert!(prompt.contains("Batch 1"));
        assert!(prompt.contains("Batch 2"));
        assert!(prompt.contains("Maria, data engineer"));
        assert!(prompt.contains("Maria, senior data engineer at DataCorp"));
        assert!(prompt.contains("\"identity\""));
    }

    #[test]
    fn test_build_profile_merge_prompt_single_partial() {
        let partials = vec![PartialProfile {
            identity: Some("Alice".to_string()),
            ..Default::default()
        }];

        let prompt = build_profile_merge_prompt(&partials);
        assert!(prompt.contains("Batch 1"));
        assert!(prompt.contains("Alice"));
    }

    // -- parse_profile_response (merge) --

    #[test]
    fn test_parse_profile_response_valid() {
        let input = r#"{
            "identity": "Maria, senior data engineer at DataCorp, Berlin",
            "themes": ["data pipeline optimization", "cloud infrastructure"],
            "projects": ["recommendation engine v2", "polars migration"],
            "stack": ["Python", "Polars", "GCP", "Kubernetes"],
            "decisions": ["chose Polars over Pandas for 3x performance"],
            "interests": ["dark mode", "JetBrains IDEs"],
            "skip_patterns": ["recipe lookups", "weather queries"]
        }"#;

        let profile = parse_profile_response(input);
        assert_eq!(
            profile.identity,
            Some("Maria, senior data engineer at DataCorp, Berlin".to_string())
        );
        assert_eq!(profile.themes.len(), 2);
        assert_eq!(profile.projects.len(), 2);
        assert_eq!(profile.stack.len(), 4);
        assert_eq!(profile.decisions.len(), 1);
        assert_eq!(profile.interests.len(), 2);
        assert_eq!(profile.skip_patterns.len(), 2);
    }

    #[test]
    fn test_parse_profile_response_missing_fields() {
        let input = r#"{"identity": "Bob", "themes": ["testing"]}"#;

        let profile = parse_profile_response(input);
        assert_eq!(profile.identity, Some("Bob".to_string()));
        assert_eq!(profile.themes, vec!["testing".to_string()]);
        assert!(profile.projects.is_empty());
        assert!(profile.stack.is_empty());
    }

    #[test]
    fn test_parse_profile_response_invalid_json() {
        let profile = parse_profile_response("garbage");
        assert!(profile.identity.is_none());
        assert!(profile.themes.is_empty());
    }

    #[test]
    fn test_parse_profile_response_string_null_identity() {
        let input = r#"{"identity": "null", "themes": ["a"]}"#;
        let profile = parse_profile_response(input);
        assert!(profile.identity.is_none()); // "null" string should be treated as None
    }

    #[test]
    fn test_parse_profile_response_with_fences() {
        let input = "```json\n{\"identity\": \"Alice\", \"themes\": [\"coding\"]}\n```";
        let profile = parse_profile_response(input);
        assert_eq!(profile.identity, Some("Alice".to_string()));
        assert_eq!(profile.themes, vec!["coding".to_string()]);
    }

    // -- build_triage_prompt --

    #[test]
    fn test_build_triage_prompt_structure() {
        let profile = make_profile();
        let chunks = make_chunks();
        let summaries = chunks_to_summaries(&chunks);
        let prompt = build_triage_prompt(&profile, &summaries);

        assert!(prompt.contains("classifying conversations"));
        assert!(prompt.contains("[USER PROFILE]"));
        assert!(prompt.contains("Maria, senior data engineer at DataCorp, Berlin"));
        assert!(prompt.contains("data pipeline optimization"));
        assert!(prompt.contains("[CONVERSATIONS]"));
        assert!(prompt.contains("Kubernetes setup"));
        assert!(prompt.contains("Pasta recipe"));
        assert!(prompt.contains("EXTRACT"));
        assert!(prompt.contains("SKIP"));
    }

    #[test]
    fn test_build_triage_prompt_empty_profile() {
        let profile = UserProfile::default();
        let summaries = vec![ChunkSummary {
            index: 0,
            title: "Test".to_string(),
            first_message: "Hello".to_string(),
            last_message: "Bye".to_string(),
            message_count: 2,
            timestamp: None,
        }];
        let prompt = build_triage_prompt(&profile, &summaries);
        assert!(prompt.contains("No profile information available"));
    }

    // -- parse_triage_response --

    #[test]
    fn test_parse_triage_response_valid_json() {
        let input = r#"[
            {"index": 0, "decision": "EXTRACT", "reason": "discusses their K8s setup"},
            {"index": 1, "decision": "SKIP", "reason": "generic recipe lookup"},
            {"index": 2, "decision": "EXTRACT", "reason": "polars migration decision"}
        ]"#;

        let decisions = parse_triage_response(input);
        assert_eq!(decisions.len(), 3);
        assert_eq!(decisions[0].chunk_index, 0);
        assert_eq!(decisions[0].decision, TriageDecision::Extract);
        assert_eq!(decisions[0].reason, "discusses their K8s setup");
        assert_eq!(decisions[1].chunk_index, 1);
        assert_eq!(decisions[1].decision, TriageDecision::Skip);
        assert_eq!(decisions[2].chunk_index, 2);
        assert_eq!(decisions[2].decision, TriageDecision::Extract);
    }

    #[test]
    fn test_parse_triage_response_with_code_fences() {
        let input = "```json\n[{\"index\": 0, \"decision\": \"SKIP\", \"reason\": \"trivial\"}]\n```";
        let decisions = parse_triage_response(input);
        assert_eq!(decisions.len(), 1);
        assert_eq!(decisions[0].decision, TriageDecision::Skip);
    }

    #[test]
    fn test_parse_triage_response_case_insensitive() {
        let input = r#"[
            {"index": 0, "decision": "extract", "reason": "test"},
            {"index": 1, "decision": "skip", "reason": "test"},
            {"index": 2, "decision": "Skip", "reason": "test"},
            {"index": 3, "decision": "EXTRACT", "reason": "test"}
        ]"#;

        let decisions = parse_triage_response(input);
        assert_eq!(decisions.len(), 4);
        assert_eq!(decisions[0].decision, TriageDecision::Extract);
        assert_eq!(decisions[1].decision, TriageDecision::Skip);
        assert_eq!(decisions[2].decision, TriageDecision::Skip);
        assert_eq!(decisions[3].decision, TriageDecision::Extract);
    }

    #[test]
    fn test_parse_triage_response_defaults_to_extract() {
        let input = r#"[{"index": 0, "decision": "UNKNOWN", "reason": "unclear"}]"#;
        let decisions = parse_triage_response(input);
        assert_eq!(decisions[0].decision, TriageDecision::Extract);
    }

    #[test]
    fn test_parse_triage_response_missing_fields() {
        let input = r#"[
            {"decision": "SKIP", "reason": "no index"},
            {"index": 1},
            {"index": 2, "decision": "EXTRACT"}
        ]"#;

        let decisions = parse_triage_response(input);
        // First entry skipped (no index), second and third parsed
        assert_eq!(decisions.len(), 2);
        assert_eq!(decisions[0].chunk_index, 1);
        assert_eq!(decisions[0].decision, TriageDecision::Extract); // default
        assert_eq!(decisions[1].chunk_index, 2);
        assert_eq!(decisions[1].reason, ""); // missing reason defaults to empty
    }

    #[test]
    fn test_parse_triage_response_chunk_index_field() {
        let input = r#"[{"chunk_index": 5, "decision": "SKIP", "reason": "test"}]"#;
        let decisions = parse_triage_response(input);
        assert_eq!(decisions.len(), 1);
        assert_eq!(decisions[0].chunk_index, 5);
    }

    #[test]
    fn test_parse_triage_response_invalid_json() {
        let decisions = parse_triage_response("not json");
        assert!(decisions.is_empty());
    }

    #[test]
    fn test_parse_triage_response_empty_array() {
        let decisions = parse_triage_response("[]");
        assert!(decisions.is_empty());
    }

    #[test]
    fn test_parse_triage_response_json_in_text() {
        let input = r#"Here are my decisions:
        [{"index": 0, "decision": "EXTRACT", "reason": "important"}]
        That's it."#;

        let decisions = parse_triage_response(input);
        assert_eq!(decisions.len(), 1);
        assert_eq!(decisions[0].decision, TriageDecision::Extract);
    }

    // -- enrich_extraction_prompt --

    #[test]
    fn test_enrich_extraction_prompt_injects_profile() {
        let profile = make_profile();
        let base = "Extract facts from this conversation.";
        let enriched = enrich_extraction_prompt(&profile, base);

        assert!(enriched.contains("Maria, senior data engineer at DataCorp, Berlin"));
        assert!(enriched.contains("data pipeline optimization"));
        assert!(enriched.contains("recommendation engine v2"));
        assert!(enriched.contains("Python"));
        assert!(enriched.contains("chose Polars over Pandas"));
        assert!(enriched.contains("dark mode"));
        assert!(enriched.contains("recipe lookups"));
        assert!(enriched.contains(base));
    }

    #[test]
    fn test_enrich_extraction_prompt_empty_profile() {
        let profile = UserProfile::default();
        let base = "Extract facts.";
        let enriched = enrich_extraction_prompt(&profile, base);

        assert!(enriched.contains("No profile information available"));
        assert!(enriched.contains(base));
    }

    #[test]
    fn test_enrich_extraction_prompt_preserves_base() {
        let profile = make_profile();
        let base = "Return a JSON array of facts with type and importance.";
        let enriched = enrich_extraction_prompt(&profile, base);
        assert!(enriched.ends_with(base));
    }

    // -- Helper tests --

    #[test]
    fn test_strip_code_fences_json() {
        assert_eq!(strip_code_fences("```json\n{}\n```"), "{}");
    }

    #[test]
    fn test_strip_code_fences_bare() {
        assert_eq!(strip_code_fences("```\n[]\n```"), "[]");
    }

    #[test]
    fn test_strip_code_fences_no_fences() {
        assert_eq!(strip_code_fences("plain text"), "plain text");
    }

    #[test]
    fn test_truncate_message_short() {
        assert_eq!(truncate_message("hello", 10), "hello");
    }

    #[test]
    fn test_truncate_message_exact() {
        assert_eq!(truncate_message("hello", 5), "hello");
    }

    #[test]
    fn test_truncate_message_long() {
        let result = truncate_message("hello world", 5);
        assert_eq!(result, "hello...");
    }

    #[test]
    fn test_truncate_message_trims_whitespace() {
        assert_eq!(truncate_message("  hello  ", 10), "hello");
    }

    #[test]
    fn test_triage_decision_display() {
        assert_eq!(format!("{}", TriageDecision::Extract), "EXTRACT");
        assert_eq!(format!("{}", TriageDecision::Skip), "SKIP");
    }

    #[test]
    fn test_triage_decision_serde_roundtrip() {
        let decision = ChunkDecision {
            chunk_index: 3,
            decision: TriageDecision::Extract,
            reason: "important project context".to_string(),
        };
        let json = serde_json::to_string(&decision).unwrap();
        assert!(json.contains("\"EXTRACT\""));

        let deserialized: ChunkDecision = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.chunk_index, 3);
        assert_eq!(deserialized.decision, TriageDecision::Extract);
    }

    #[test]
    fn test_partial_profile_serde_roundtrip() {
        let profile = PartialProfile {
            identity: Some("Test User".to_string()),
            themes: Some(vec!["testing".to_string()]),
            projects: None,
            stack: Some(vec!["Rust".to_string()]),
            decisions: None,
            interests: None,
            skip_patterns: None,
        };

        let json = serde_json::to_string(&profile).unwrap();
        let deserialized: PartialProfile = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.identity, Some("Test User".to_string()));
        assert_eq!(deserialized.themes, Some(vec!["testing".to_string()]));
        assert!(deserialized.projects.is_none());
    }

    #[test]
    fn test_user_profile_serde_roundtrip() {
        let profile = make_profile();
        let json = serde_json::to_string(&profile).unwrap();
        let deserialized: UserProfile = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.identity, profile.identity);
        assert_eq!(deserialized.themes, profile.themes);
        assert_eq!(deserialized.stack, profile.stack);
    }

    #[test]
    fn test_format_profile_for_prompt_full() {
        let profile = make_profile();
        let text = format_profile_for_prompt(&profile);

        assert!(text.contains("Identity: Maria"));
        assert!(text.contains("Recurring themes: data pipeline optimization"));
        assert!(text.contains("Active projects: recommendation engine v2"));
        assert!(text.contains("Technical stack: Python"));
        assert!(text.contains("Key decisions:"));
        assert!(text.contains("  - chose Polars over Pandas"));
        assert!(text.contains("Interests: dark mode"));
        assert!(text.contains("Skip patterns (generic Q&A): recipe lookups"));
    }

    #[test]
    fn test_format_profile_for_prompt_empty() {
        let profile = UserProfile::default();
        let text = format_profile_for_prompt(&profile);
        assert_eq!(text, "No profile information available.");
    }

    // -- Integration-style tests with realistic LLM outputs --

    #[test]
    fn test_full_pipeline_profile_batch_response() {
        // Simulate a realistic LLM response with extra commentary
        let llm_output = r#"Based on the conversations, here's the user profile:

```json
{
  "identity": "Sarah Chen, ML engineer at TechStartup, San Francisco",
  "themes": ["model training", "deployment pipelines", "GPU optimization"],
  "projects": ["recommendation-v3", "model-serving-infra"],
  "stack": ["Python", "PyTorch", "CUDA", "Docker", "AWS SageMaker"],
  "decisions": ["switched from TensorFlow to PyTorch for better debugging", "chose spot instances for training to save 60% cost"],
  "interests": ["vim keybindings", "mechanical keyboards"],
  "skip_patterns": ["asking about weather", "general Python syntax questions"]
}
```

I hope this profile is helpful!"#;

        let profile = parse_profile_batch_response(llm_output);
        assert_eq!(
            profile.identity,
            Some("Sarah Chen, ML engineer at TechStartup, San Francisco".to_string())
        );
        assert_eq!(profile.themes.as_ref().unwrap().len(), 3);
        assert_eq!(profile.projects.as_ref().unwrap().len(), 2);
        assert_eq!(profile.stack.as_ref().unwrap().len(), 5);
        assert_eq!(profile.decisions.as_ref().unwrap().len(), 2);
    }

    #[test]
    fn test_full_pipeline_triage_response() {
        // Simulate a realistic triage response
        let llm_output = r#"Looking at the user's profile and conversations, here are my classifications:

[
  {"index": 0, "decision": "EXTRACT", "reason": "Discusses Kubernetes cluster setup on GCP, directly related to their cloud infrastructure work"},
  {"index": 1, "decision": "SKIP", "reason": "Generic recipe lookup with no personal value"},
  {"index": 2, "decision": "EXTRACT", "reason": "Contains key decision to switch from pandas to polars with benchmark data and deployment timeline"}
]"#;

        let decisions = parse_triage_response(llm_output);
        assert_eq!(decisions.len(), 3);

        let extract_count = decisions
            .iter()
            .filter(|d| d.decision == TriageDecision::Extract)
            .count();
        let skip_count = decisions
            .iter()
            .filter(|d| d.decision == TriageDecision::Skip)
            .count();
        assert_eq!(extract_count, 2);
        assert_eq!(skip_count, 1);
    }

    #[test]
    fn test_parse_profile_batch_response_empty_string_identity() {
        let input = r#"{"identity": "", "themes": ["test"]}"#;
        let profile = parse_profile_batch_response(input);
        assert!(profile.identity.is_none()); // empty string treated as None
    }

    #[test]
    fn test_chunks_to_summaries_empty_messages() {
        let chunks = vec![ConversationChunk {
            index: 0,
            title: Some("Empty".to_string()),
            messages: vec![],
            timestamp: None,
        }];
        let summaries = chunks_to_summaries(&chunks);
        assert_eq!(summaries[0].first_message, "");
        assert_eq!(summaries[0].last_message, "");
        assert_eq!(summaries[0].message_count, 0);
    }
}
