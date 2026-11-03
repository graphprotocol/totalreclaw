//! Subgraph search pipeline.
//!
//! Queries facts via GraphQL through the relay server.
//! Implements trapdoor batching, deduplication, and ids-first payload loading.
//!
//! Pure computation functions (trapdoor generation, response parsing,
//! hex_blob_to_base64, decrypt+rerank) are delegated to `totalreclaw_core::search`.
//! This module provides the async I/O wrappers that call the relay.

use std::collections::{HashMap, HashSet};

use futures_util::future::join_all;
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

/// Max subgraph queries in flight per search. Bounds the burst only.
pub const SEARCH_QUERY_CONCURRENCY: usize = 6;

/// Fact payloads fetched per `FACTS_BY_IDS_QUERY` (~3.5 KB each).
pub const FACT_FETCH_BATCH_SIZE: usize = 100;

/// Blind-index lookup that returns fact ids only (internal #666, ported from
/// the Python client, public #703). A trapdoor batch can match most of the
/// vault, so carrying each fact's blob + embedding in every batch downloaded
/// the same fact many times. Payloads come from `FACTS_BY_IDS_QUERY`.
const SEARCH_IDS_QUERY: &str = r#"
  query SearchByBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!) {
    blindIndexes(
      where: { hash_in: $trapdoors, owner: $owner, fact_: { isActive: true } }
      first: $first
      orderBy: id
      orderDirection: desc
    ) {
      id
      fact {
        id
      }
    }
  }
"#;

/// Payload query for the ids collected by [`search_candidates`]. Selects the
/// same fields `totalreclaw_core::search::search_query()` carried.
const FACTS_BY_IDS_QUERY: &str = r#"
  query FactsByIds($ids: [ID!]!, $owner: Bytes!, $first: Int!) {
    facts(
      where: { id_in: $ids, owner: $owner, isActive: true }
      first: $first
    ) {
      id
      encryptedBlob
      encryptedEmbedding
      decayScore
      timestamp
      createdAt
      isActive
      contentFp
    }
  }
"#;

/// GraphQL response types (internal, for relay deserialization).
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct FactRef {
    id: String,
    is_active: Option<bool>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct BlindIndexIdEntry {
    #[allow(dead_code)]
    id: String,
    fact: Option<FactRef>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct SearchIdsData {
    blind_indexes: Option<Vec<BlindIndexIdEntry>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ExportData {
    facts: Option<Vec<SubgraphFact>>,
}

/// Search the subgraph for facts matching trapdoors.
///
/// Strategy: split trapdoors into small batches (5 each) and query them for
/// fact ids, up to [`SEARCH_QUERY_CONCURRENCY`] at a time. Batches are merged
/// in order with the same stop rule as the old sequential loop (stop before a
/// batch once `max_candidates` ids are in hand), so the candidate set is
/// unchanged. Then each unique fact's payload is fetched once, in batches of
/// [`FACT_FETCH_BATCH_SIZE`]. Results are in first-hit order.
pub async fn search_candidates(
    relay: &RelayClient,
    trapdoors: &[String],
    max_candidates: usize,
) -> Result<Vec<SubgraphFact>> {
    let owner = relay.wallet_address();
    let mut hit_ids: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    let chunks: Vec<&[String]> = trapdoors.chunks(TRAPDOOR_BATCH_SIZE).collect();

    'waves: for wave in chunks.chunks(SEARCH_QUERY_CONCURRENCY) {
        if hit_ids.len() >= max_candidates {
            break;
        }
        let pages = join_all(wave.iter().map(|chunk| {
            relay.graphql::<SearchIdsData>(
                SEARCH_IDS_QUERY,
                serde_json::json!({
                    "trapdoors": chunk,
                    "owner": owner,
                    "first": PAGE_SIZE,
                }),
            )
        }))
        .await;

        for page in pages {
            if hit_ids.len() >= max_candidates {
                break 'waves;
            }
            // A failed batch is skipped, as before.
            let Ok(data) = page else { continue };
            for entry in data.blind_indexes.unwrap_or_default() {
                if let Some(fact) = entry.fact {
                    if fact.is_active != Some(false) && seen.insert(fact.id.clone()) {
                        hit_ids.push(fact.id);
                    }
                }
            }
        }
    }

    let mut payloads: HashMap<String, SubgraphFact> = HashMap::new();
    let batches: Vec<&[String]> = hit_ids.chunks(FACT_FETCH_BATCH_SIZE).collect();
    for wave in batches.chunks(SEARCH_QUERY_CONCURRENCY) {
        let pages = join_all(wave.iter().map(|ids| {
            relay.graphql::<ExportData>(
                FACTS_BY_IDS_QUERY,
                serde_json::json!({
                    "ids": ids,
                    "owner": owner,
                    "first": ids.len(),
                }),
            )
        }))
        .await;
        for page in pages {
            match page {
                Ok(data) => {
                    for fact in data.facts.unwrap_or_default() {
                        if fact.is_active != Some(false) {
                            payloads.insert(fact.id.clone(), fact);
                        }
                    }
                }
                // Those candidates drop out; the rest still rank.
                Err(e) => log::warn!("fact payload fetch failed: {e}"),
            }
        }
    }

    Ok(hit_ids
        .into_iter()
        .filter_map(|id| payloads.remove(&id))
        .collect())
}

/// Broadened search: fetch recent active facts by owner without trapdoor filtering.
/// Used as a fallback when trapdoor search returns 0 candidates (vague queries).
pub async fn search_broadened(
    relay: &RelayClient,
    max_candidates: usize,
) -> Result<Vec<SubgraphFact>> {
    let first = max_candidates.min(PAGE_SIZE);
    let variables = serde_json::json!({
        "owner": relay.wallet_address(),
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
pub async fn fetch_all_facts(relay: &RelayClient) -> Result<Vec<SubgraphFact>> {
    let owner = relay.wallet_address();
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
pub async fn count_facts(relay: &RelayClient) -> Result<usize> {
    let variables = serde_json::json!({ "owner": relay.wallet_address() });

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
        "owner": relay.wallet_address(),
        "contentFp": content_fp,
    });

    #[derive(Deserialize)]
    struct FpData {
        facts: Option<Vec<SubgraphFact>>,
    }

    let data: FpData = relay.graphql(FP_QUERY, variables).await?;
    Ok(data.facts.and_then(|f| f.into_iter().next()))
}
