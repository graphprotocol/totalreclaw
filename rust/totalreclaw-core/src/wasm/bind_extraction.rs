//! `bind_extraction` wasm-bindgen bindings (split out of the former monolithic wasm.rs).
//!
//! Shared imports + helpers (`to_js_error`, `parse_key_hex`, `keys_to_js`)
//! come from the parent module via `use super::*;`. Each `#[wasm_bindgen]`
//! export keeps its exact `js_name`, so the JS-visible surface is unchanged.

use super::*;

// ---------------------------------------------------------------------------
// Debrief
// ---------------------------------------------------------------------------

/// Parse a debrief LLM response into validated items.
///
/// Returns a JSON array of `{ text, type, importance }` objects.
#[wasm_bindgen(js_name = "parseDebriefResponse")]
pub fn wasm_parse_debrief_response(response: &str) -> Result<JsValue, JsError> {
    let items = debrief::parse_debrief_response(response);
    serde_wasm_bindgen::to_value(&items).map_err(|e| JsError::new(&e.to_string()))
}

/// Get the canonical debrief system prompt template.
///
/// Contains `{already_stored_facts}` placeholder.
#[wasm_bindgen(js_name = "getDebriefSystemPrompt")]
pub fn wasm_get_debrief_system_prompt() -> String {
    debrief::DEBRIEF_SYSTEM_PROMPT.to_string()
}

/// Get the canonical v1 merged-topic extraction system prompt.
///
/// Single source of truth across all TotalReclaw clients — TS/WASM
/// callers get the same bytes the Python `totalreclaw_core` module
/// returns from `get_extraction_system_prompt()`. Includes the Rule 6
/// meta-request filter (see the docstring on `prompts.rs`).
#[wasm_bindgen(js_name = "getExtractionSystemPrompt")]
pub fn wasm_get_extraction_system_prompt() -> String {
    crate::prompts::get_extraction_system_prompt().to_string()
}

/// Get the canonical v1 compaction system prompt.
///
/// Used on end-of-context surfaces where the importance floor is 5 rather
/// than the default 6.
#[wasm_bindgen(js_name = "getCompactionSystemPrompt")]
pub fn wasm_get_compaction_system_prompt() -> String {
    crate::prompts::get_compaction_system_prompt().to_string()
}

/// Build the debrief prompt with already-stored facts filled in.
///
/// `stored_facts_json`: JSON array of strings (fact texts already stored).
#[wasm_bindgen(js_name = "buildDebriefPrompt")]
pub fn wasm_build_debrief_prompt(stored_facts_json: &str) -> Result<String, JsError> {
    let facts: Vec<String> = serde_json::from_str(stored_facts_json)
        .map_err(|e| JsError::new(&format!("invalid JSON array: {}", e)))?;
    let refs: Vec<&str> = facts.iter().map(|s| s.as_str()).collect();
    Ok(debrief::build_debrief_prompt(&refs))
}

// ---------------------------------------------------------------------------
// Constants (exposed as getter functions since wasm_bindgen doesn't support statics)
// ---------------------------------------------------------------------------

/// Minimum messages for debrief (8 = 4 turns).
#[wasm_bindgen(js_name = "getMinDebriefMessages")]
pub fn wasm_min_debrief_messages() -> usize {
    debrief::MIN_DEBRIEF_MESSAGES
}

/// Maximum debrief items (5).
#[wasm_bindgen(js_name = "getMaxDebriefItems")]
pub fn wasm_max_debrief_items() -> usize {
    debrief::MAX_DEBRIEF_ITEMS
}

/// Source tag for debrief items.
#[wasm_bindgen(js_name = "getDebriefSource")]
pub fn wasm_debrief_source() -> String {
    debrief::DEBRIEF_SOURCE.to_string()
}

