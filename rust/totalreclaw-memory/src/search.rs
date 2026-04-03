//! Subgraph search pipeline.
//!
//! Queries facts via GraphQL through the relay server.
//! Implements trapdoor batching, cursor-based pagination, and deduplication.

use std::collections::HashMap;

use serde::Deserialize;

use crate::relay::RelayClient;
use crate::Result;

/// Default number of trapdoors per GraphQL query batch.
const TRAPDOOR_BATCH_SIZE: usize = 5;

/// Default page size for GraphQL queries (Graph Studio limit = 1000).
const PAGE_SIZE: usize = 1000;

/// A raw fact from the subgraph.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SubgraphFact {
    pub id: String,
    pub encrypted_blob: String,
    pub encrypted_embedding: Option<String>,
    pub decay_score: Option<String>,
    pub timestamp: Option<String>,
    pub is_active: Option<bool>,
    pub content_fp: Option<String>,
}

/// GraphQL response types.
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

/// GraphQL query for blind index lookup.
const SEARCH_QUERY: &str = r#"
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
        encryptedBlob
        encryptedEmbedding
        decayScore
        timestamp
        isActive
        contentFp
      }
    }
  }
"#;

/// Broadened search query: fetch recent active facts by owner without trapdoor filtering.
/// Used as fallback when trapdoor search returns 0 candidates (e.g., vague queries).
const BROADENED_SEARCH_QUERY: &str = r#"
  query BroadenedSearch($owner: Bytes!, $first: Int!) {
    facts(
      where: { owner: $owner, isActive: true }
      first: $first
      orderBy: timestamp
      orderDirection: desc
    ) {
      id
      encryptedBlob
      encryptedEmbedding
      decayScore
      timestamp
      isActive
      contentFp
    }
  }
"#;

/// Export all facts query.
const EXPORT_QUERY: &str = r#"
  query ExportFacts($owner: Bytes!, $first: Int!, $skip: Int!) {
    facts(
      where: { owner: $owner, isActive: true }
      first: $first
      skip: $skip
      orderBy: timestamp
      orderDirection: desc
    ) {
      id
      encryptedBlob
      encryptedEmbedding
      decayScore
      timestamp
      isActive
    }
  }
"#;

/// Fact count query.
const COUNT_QUERY: &str = r#"
  query FactCount($owner: Bytes!) {
    facts(where: { owner: $owner, isActive: true }, first: 1000) {
      id
    }
  }
"#;

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

        let data: SearchData = match relay.graphql(SEARCH_QUERY, variables).await {
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

    let data: ExportData = relay.graphql(BROADENED_SEARCH_QUERY, variables).await?;
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

        let data: ExportData = relay.graphql(EXPORT_QUERY, variables).await?;

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

    let data: CountData = relay.graphql(COUNT_QUERY, variables).await?;
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

/// Decode an encrypted blob from subgraph hex format to base64 for decryption.
///
/// Subgraph returns `0x`-prefixed hex. Strip prefix, decode hex to bytes, base64-encode.
pub fn hex_blob_to_base64(hex_blob: &str) -> Option<String> {
    let hex_str = hex_blob.strip_prefix("0x").unwrap_or(hex_blob);
    let bytes = hex::decode(hex_str).ok()?;
    Some(base64::Engine::encode(
        &base64::engine::general_purpose::STANDARD,
        &bytes,
    ))
}
