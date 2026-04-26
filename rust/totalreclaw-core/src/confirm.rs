//! Read-after-write primitive — confirm a fact id has been indexed by the subgraph.
//!
//! Ships in 2.2.x to fix a class of bugs where on-chain mutation tools
//! (`set_scope`, `retype`, `pin`, `unpin`, `forget`) returned success based on
//! the bundler ack alone — but a follow-up `export` / `recall` then surfaced
//! stale state because the subgraph indexer hadn't yet observed the L1 inclusion
//! (typical lag 5-30s on production Gnosis). User-visible symptom: agent says
//! "scope set to health", user runs export, sees `unspecified`.
//!
//! This module provides the pure-compute halves of the read-after-write
//! sequence: a GraphQL query string and a response-parser that returns whether
//! the new fact id is now visible AND active. Host languages (TypeScript,
//! Python) wrap a polling loop around these helpers — they own HTTP I/O,
//! sleep / timeout primitives, and the caller's tx hash bookkeeping.
//!
//! # Mnemonic isolation
//!
//! By design this module never touches the mnemonic, the encryption key, or
//! any decrypted blob. It only reads the *existence + active flag* of a fact
//! by its UUID — information that is already public in the subgraph. The
//! phrase-safety CI guard (`scripts/check-phrase-safety.sh`) enforces this
//! at the repository level.

/// GraphQL query that resolves a fact by its UUID. The host wraps this in
/// a polling loop after submitting an on-chain mutation: poll until the fact
/// is found AND `isActive == true`, OR the host's timeout elapses.
///
/// The query intentionally also fetches `blockNumber` so callers that DO have
/// access to a block-number lower bound (e.g. from `eth_getTransactionReceipt`)
/// can additionally enforce `block_number >= tx.block`. The default
/// `parse_indexed_response()` parser ignores the block number — confirming
/// presence + active is sufficient for the user-visible read-after-write
/// guarantee.
pub const FACT_BY_ID_INDEXED_QUERY: &str = r#"
  query ConfirmIndexed($id: ID!) {
    fact(id: $id) {
      id
      isActive
      blockNumber
    }
  }
"#;

/// Default polling interval (ms) — 1s per attempt.
pub const DEFAULT_POLL_INTERVAL_MS: u64 = 1_000;

/// Default total timeout (ms) — 30s. Indexer lag on Gnosis production runs
/// 5-30s under normal load; the timeout sits at the high end.
pub const DEFAULT_TIMEOUT_MS: u64 = 30_000;

/// Get the GraphQL query string used by `confirm_indexed`. Provided as a
/// function so host bindings can re-export it through their normal calling
/// convention rather than as a raw constant.
pub fn confirm_indexed_query() -> &'static str {
    FACT_BY_ID_INDEXED_QUERY
}

/// Parse a subgraph GraphQL response and return whether the fact is indexed
/// AND active.
///
/// Accepts either the wrapped `{"data": {"fact": ...}}` shape or the inner
/// `{"fact": ...}` shape — different host adapters strip the `data`
/// envelope at different layers, so we accept both for robustness.
///
/// Returns:
/// - `Ok(true)` — fact present, `isActive == true`. Mutation is fully indexed.
/// - `Ok(false)` — fact not present, OR present but `isActive == false`
///   (still propagating). Caller should poll again.
/// - `Err(...)` — response was not valid JSON or did not match either shape.
pub fn parse_indexed_response(response_json: &str) -> Result<bool, String> {
    // Both shapes deserialize through serde_json::Value first so we don't
    // double-decode and so `serde(rename_all = "camelCase")` propagates
    // consistently regardless of which wrapper landed at the top.
    let value: serde_json::Value = serde_json::from_str(response_json).map_err(|e| {
        format!(
            "confirm_indexed: response is not valid JSON ({}). Body: {}",
            e,
            response_json.chars().take(200).collect::<String>()
        )
    })?;

    // Wrapped: `{"data":{"fact":...}}`
    if let Some(data) = value.get("data") {
        if data.is_null() {
            return Ok(false);
        }
        if let Some(fact_field) = data.get("fact") {
            return Ok(parse_fact_value(fact_field));
        }
        return Err(
            "confirm_indexed: wrapped response missing `fact` field under `data`".to_string(),
        );
    }

    // Unwrapped: `{"fact":...}`
    if let Some(fact_field) = value.get("fact") {
        return Ok(parse_fact_value(fact_field));
    }

    Err(format!(
        "confirm_indexed: response did not match either {{data:{{fact}}}} or {{fact}} shape: {}",
        response_json.chars().take(200).collect::<String>()
    ))
}

/// Helper: read a JSON value at the `fact` slot and return whether it
/// corresponds to an indexed-and-active fact.
fn parse_fact_value(fact: &serde_json::Value) -> bool {
    if fact.is_null() {
        return false;
    }
    fact.get("isActive")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_wrapped_active_true() {
        let r = r#"{"data":{"fact":{"id":"abc","isActive":true,"blockNumber":"123"}}}"#;
        assert_eq!(parse_indexed_response(r).unwrap(), true);
    }

    #[test]
    fn parses_wrapped_active_false() {
        let r = r#"{"data":{"fact":{"id":"abc","isActive":false,"blockNumber":"123"}}}"#;
        assert_eq!(parse_indexed_response(r).unwrap(), false);
    }

    #[test]
    fn parses_wrapped_null_fact() {
        let r = r#"{"data":{"fact":null}}"#;
        assert_eq!(parse_indexed_response(r).unwrap(), false);
    }

    #[test]
    fn parses_wrapped_null_data() {
        let r = r#"{"data":null}"#;
        assert_eq!(parse_indexed_response(r).unwrap(), false);
    }

    #[test]
    fn parses_unwrapped_shape() {
        let r = r#"{"fact":{"id":"abc","isActive":true,"blockNumber":"123"}}"#;
        assert_eq!(parse_indexed_response(r).unwrap(), true);
    }

    #[test]
    fn parses_missing_block_number() {
        // `blockNumber` is optional in our parser — a subgraph that omits it
        // (older schemas, partial response) must still resolve to true.
        let r = r#"{"data":{"fact":{"id":"abc","isActive":true}}}"#;
        assert_eq!(parse_indexed_response(r).unwrap(), true);
    }

    #[test]
    fn rejects_garbage_input() {
        assert!(parse_indexed_response("{not json").is_err());
    }

    #[test]
    fn query_string_contains_fact_isactive() {
        let q = confirm_indexed_query();
        assert!(q.contains("fact(id: $id)"));
        assert!(q.contains("isActive"));
    }
}
