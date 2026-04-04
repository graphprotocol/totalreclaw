//! Subgraph search pipeline.
//!
//! Queries facts via GraphQL through the relay server.
//! Implements trapdoor batching, cursor-based pagination, and deduplication.
//!
//! Pure computation functions (trapdoor generation, response parsing,
//! hex_blob_to_base64, decrypt+rerank) are delegated to `totalreclaw_core::search`.
//! This module provides the async I/O wrappers that call the relay.

use std::collections::HashMap;

use serde::Deserialize;

use crate::relay::RelayClient;
use crate::Result;

// Re-export core search types and functions for backward compatibility.
pub use totalreclaw_core::search::{
    hex_blob_to_base64, SubgraphFact,
    // Constants
    TRAPDOOR_BATCH_SIZE, PAGE_SIZE,
    // Pure functions
    generate_search_trapdoors, parse_search_response, parse_broadened_response,
    decrypt_and_rerank, decrypt_and_rerank_with_key,
    // Query string accessors
    search_query, broadened_search_query, export_query, count_query,
};

/// GraphQL response types (internal, for relay deserialization).
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct BlindIndexEntry {
    #[allow(dead_code)]
    id: String,
    fact: Option<SubgraphFact>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct SearchData {
    blind_indexes: Option<Vec<BlindIndexEntry>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ExportData {
    facts: Option<Vec<SubgraphFact>>,
}

/// Search the subgraph for facts matching trapdoors.
///
/// Strategy: split trapdoors into small batches (5 each), fire in parallel,
/// deduplicate results by fact ID.
pub async fn search_candidates(
    relay: &RelayClient,
    owner: &str,
    trapdoors: &[String],
    max_candidates: usize,
) -> Result<Vec<SubgraphFact>> {
    let mut all_results: HashMap<String, SubgraphFact> = HashMap::new();

    // Split trapdoors into batches
    let chunks: Vec<&[String]> = trapdoors.chunks(TRAPDOOR_BATCH_SIZE).collect();

    // Fire all chunks (sequential for simplicity — could be parallel with join_all)
    for chunk in chunks {
        if all_results.len() >= max_candidates {
            break;
        }

        let variables = serde_json::json!({
            "trapdoors": chunk,
            "owner": owner,
            "first": PAGE_SIZE,
        });

        let data: SearchData = match relay.graphql(search_query(), variables).await {
            Ok(d) => d,
            Err(_) => continue,
        };

        if let Some(entries) = data.blind_indexes {
            for entry in entries {
                if let Some(fact) = entry.fact {
                    if fact.is_active != Some(false) && !all_results.contains_key(&fact.id) {
                        all_results.insert(fact.id.clone(), fact);
                    }
                }
            }
        }
    }

    Ok(all_results.into_values().collect())
}

/// Broadened search: fetch recent active facts by owner without trapdoor filtering.
/// Used as a fallback when trapdoor search returns 0 candidates (vague queries).
pub async fn search_broadened(
    relay: &RelayClient,
    owner: &str,
    max_candidates: usize,
) -> Result<Vec<SubgraphFact>> {
    let first = max_candidates.min(PAGE_SIZE);
    let variables = serde_json::json!({
        "owner": owner,
        "first": first,
    });

    let data: ExportData = relay.graphql(broadened_search_query(), variables).await?;
    Ok(data
        .facts
        .unwrap_or_default()
        .into_iter()
        .filter(|f| f.is_active != Some(false))
        .collect())
}

/// Fetch all facts for an owner (paginated export).
pub async fn fetch_all_facts(
    relay: &RelayClient,
    owner: &str,
) -> Result<Vec<SubgraphFact>> {
    let mut all_facts = Vec::new();
    let mut skip = 0;

    loop {
        let variables = serde_json::json!({
            "owner": owner,
            "first": PAGE_SIZE,
            "skip": skip,
        });

        let data: ExportData = relay.graphql(export_query(), variables).await?;

        let facts = data.facts.unwrap_or_default();
        let count = facts.len();
        all_facts.extend(facts);

        if count < PAGE_SIZE {
            break;
        }
        skip += count;
    }

    Ok(all_facts)
}

/// Count active facts for an owner.
pub async fn count_facts(relay: &RelayClient, owner: &str) -> Result<usize> {
    let variables = serde_json::json!({ "owner": owner });

    #[derive(Deserialize)]
    struct CountData {
        facts: Option<Vec<serde_json::Value>>,
    }

    let data: CountData = relay.graphql(count_query(), variables).await?;
    Ok(data.facts.map(|f| f.len()).unwrap_or(0))
}

/// Search for a fact by content fingerprint (exact dedup check).
pub async fn search_by_fingerprint(
    relay: &RelayClient,
    owner: &str,
    content_fp: &str,
) -> Result<Option<SubgraphFact>> {
    const FP_QUERY: &str = r#"
      query FactByFingerprint($owner: Bytes!, $contentFp: String!) {
        facts(
          where: { owner: $owner, contentFp: $contentFp, isActive: true }
          first: 1
        ) {
          id
          encryptedBlob
          isActive
        }
      }
    "#;

    let variables = serde_json::json!({
        "owner": owner,
        "contentFp": content_fp,
    });

    #[derive(Deserialize)]
    struct FpData {
        facts: Option<Vec<SubgraphFact>>,
    }

    let data: FpData = relay.graphql(FP_QUERY, variables).await?;
    Ok(data.facts.and_then(|f| f.into_iter().next()))
}
