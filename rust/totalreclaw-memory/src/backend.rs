//! ZeroClaw Memory trait implementation.
//!
//! `TotalReclawMemory` is the main entry point for using TotalReclaw as a
//! ZeroClaw memory backend. It implements the `Memory` trait, providing:
//!
//! - E2E encrypted storage on-chain via The Graph subgraph
//! - Semantic search with BM25 + Cosine + RRF reranking
//! - LSH blind indexing for server-blind similarity search
//! - Portable: same recovery phrase works across ZeroClaw, OpenClaw, Claude Desktop, Hermes
//!
//! ## ZeroClaw Integration
//!
//! Register in ZeroClaw's factory (`src/memory/backend.rs`):
//! ```ignore
//! MemoryBackendKind::TotalReclaw => {
//!     Box::new(TotalReclawMemory::new(config).await?)
//! }
//! ```
//!
//! Configure in `~/.zeroclaw/config.toml`:
//! ```toml
//! [memory]
//! backend = "totalreclaw"
//!
//! [memory.totalreclaw]
//! recovery_phrase_path = "~/.totalreclaw/credentials.json"
//! embedding_config_path = "~/.totalreclaw/embedding-config.json"
//! relay_url = "https://api.totalreclaw.xyz"
//! ```
//!
//! ## Category Mapping (TotalReclaw -> ZeroClaw)
//!
//! ZeroClaw applies 7-day half-life decay to non-Core entries at retrieval time.
//! We map TotalReclaw memory types to ZeroClaw categories:
//!
//! | TotalReclaw type | ZeroClaw category | Decay |
//! |------------------|-------------------|-------|
//! | fact             | Core              | None  |
//! | preference       | Core              | None  |
//! | decision         | Core              | None  |
//! | goal             | Core              | None  |
//! | summary          | Core              | None  |
//! | episodic         | Conversation      | 7-day |
//! | context          | Daily             | 7-day |

use base64::Engine;

use crate::billing::{self, BillingCache};
use crate::crypto::{self, DerivedKeys};
use crate::embedding::{self, EmbeddingMode, EmbeddingProvider};
use crate::hotcache::HotCache;
use crate::lsh::LshHasher;
use crate::reranker::{self, Candidate, RerankerConfig};
use crate::relay::{RelayClient, RelayConfig};
use crate::search;
use crate::store;
use crate::wallet;
use crate::Result;
use totalreclaw_core::claims::{MemorySource, MemoryTypeV1};

/// Default relay URL.
const DEFAULT_RELAY_URL: &str = "https://api.totalreclaw.xyz";

/// Gnosis mainnet chain id (Pro tier).
const CHAIN_GNOSIS: u64 = 100;
/// Base Sepolia chain id (free-tier default).
const CHAIN_BASE_SEPOLIA: u64 = 84532;

/// Resolve the chain id to use before the relay handshake, from the on-disk
/// billing cache alone (no network). This is the FIRST of two resolution
/// steps in [`TotalReclawMemory::new`]:
///
///   1. **cache (offline, here):** Pro → Gnosis mainnet ([`CHAIN_GNOSIS`]),
///      otherwise the free-tier Base Sepolia default ([`CHAIN_BASE_SEPOLIA`]).
///      A cold/absent cache also yields the free-tier default so the very
///      first handshake still targets a valid chain.
///   2. **network (authoritative, in `new`):** after registering, a live
///      `billing_status()` call can still promote a Pro account to
///      [`CHAIN_GNOSIS`] if the cache was cold or stale.
///
/// Precedence is therefore cache-then-network, with the network check only
/// ever *upgrading* to Gnosis — never downgrading. There is no env override on
/// this path (the `TOTALRECLAW_CHAIN_ID` var was removed in v1).
fn initial_chain_id_from_cache() -> u64 {
    match billing::read_cache() {
        Some(cache) if cache.is_pro() => CHAIN_GNOSIS,
        _ => CHAIN_BASE_SEPOLIA,
    }
}

/// Result of parsing the decrypted fact envelope.
///
/// ZeroClaw reads both envelope shapes:
///  * **v0 (pre-2.0)**: `{"t": text, "a": agent_id, "s": source_tag}`
///    (source_tag is `zeroclaw_{category}` or `openclaw_extraction` etc.)
///  * **v1 (Memory Taxonomy v1)**: full v1 `ClaimPayload` with `text`,
///    `type`, `source` (one of `user|user-inferred|assistant|external|derived`),
///    `scope`, optional `reasoning`, `volatility`, `entities`, `importance`.
struct DecryptedEnvelope {
    text: String,
    category: MemoryCategory,
    /// Memory Taxonomy v1 provenance source if the envelope is v1; `None`
    /// for v0 envelopes (read-side back-compat). Used by `rerank_with_config`
    /// to apply Retrieval v2 Tier 1 source weights.
    v1_source: Option<MemorySource>,
}

/// Parse the decrypted fact envelope.
///
/// Tries v1 first (ClaimPayload JSON), falls back to v0 (`{t,a,s}` envelope)
/// on parse failure so pre-2.0 vault entries still decode.
fn parse_decrypted_envelope(decrypted: &str) -> DecryptedEnvelope {
    // Try v1 ClaimPayload JSON first. A v1 envelope has `text` and `type`
    // fields at top level; v0 envelopes use `t`/`a`/`s` short keys.
    if let Ok(obj) = serde_json::from_str::<serde_json::Value>(decrypted) {
        let is_v1 = obj.get("text").is_some() && obj.get("type").is_some();
        if is_v1 {
            let text = obj
                .get("text")
                .and_then(|v| v.as_str())
                .unwrap_or(decrypted)
                .to_string();
            // v1 source field: parse present strings via the core enum
            // (absent → None so the reranker falls back to the legacy weight).
            let v1_source = obj
                .get("source")
                .and_then(|v| v.as_str())
                .map(MemorySource::from_str_lossy);
            // v1 "type" → ZeroClaw category mapping
            let v1_type = obj
                .get("type")
                .and_then(|v| v.as_str())
                .unwrap_or("claim");
            let category = v1_type_to_category(v1_type);
            return DecryptedEnvelope {
                text,
                category,
                v1_source,
            };
        }

        // v0 envelope path (pre-2.0)
        let text = obj
            .get("t")
            .and_then(|v| v.as_str())
            .unwrap_or(decrypted)
            .to_string();
        let source = obj.get("s").and_then(|v| v.as_str()).unwrap_or("");
        let category = category_from_source(source);
        return DecryptedEnvelope {
            text,
            category,
            v1_source: None,
        };
    }
    // Fallback: treat entire decrypted content as text
    DecryptedEnvelope {
        text: decrypted.to_string(),
        category: MemoryCategory::Core,
        v1_source: None,
    }
}

/// Map a v1 memory type to a ZeroClaw memory category.
///
/// Parses the raw type token through the core [`MemoryTypeV1`] enum (rather
/// than re-matching taxonomy strings here) so the type vocabulary stays in
/// one place. Category rules:
///  * episode → Conversation (7-day decay)
///  * claim / preference / directive / commitment / summary → Core (durable)
///  * unknown → Core (via the enum's lossy fallback to `Claim`)
fn v1_type_to_category(v1_type: &str) -> MemoryCategory {
    match MemoryTypeV1::from_str_lossy(v1_type) {
        MemoryTypeV1::Episode => MemoryCategory::Conversation,
        MemoryTypeV1::Claim
        | MemoryTypeV1::Preference
        | MemoryTypeV1::Directive
        | MemoryTypeV1::Commitment
        | MemoryTypeV1::Summary => MemoryCategory::Core,
    }
}

/// Map a source string to a ZeroClaw memory category.
fn category_from_source(source: &str) -> MemoryCategory {
    let lower = source.to_lowercase();
    if lower.contains("conversation") || lower.contains("episodic") {
        MemoryCategory::Conversation
    } else if lower.contains("daily") || lower.contains("context") {
        MemoryCategory::Daily
    } else {
        // Core covers: fact, preference, decision, goal, summary, rule, debrief, and unknown
        MemoryCategory::Core
    }
}

/// Auto-recall top_k constant (after reranking). Matches all other clients.
const AUTO_RECALL_TOP_K: usize = 8;

// ---------------------------------------------------------------------------
// ZeroClaw-compatible types
// ---------------------------------------------------------------------------

/// Memory category (matches ZeroClaw's `MemoryCategory`).
#[derive(Debug, Clone, PartialEq)]
pub enum MemoryCategory {
    Core,
    Daily,
    Conversation,
    Custom(String),
}

impl std::fmt::Display for MemoryCategory {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MemoryCategory::Core => write!(f, "core"),
            MemoryCategory::Daily => write!(f, "daily"),
            MemoryCategory::Conversation => write!(f, "conversation"),
            MemoryCategory::Custom(s) => write!(f, "{}", s),
        }
    }
}

/// A single item in a batched store.
///
/// Mirrors the typed shape of the single-item [`TotalReclawMemory::store`]
/// path — plaintext content plus a typed [`MemoryCategory`] — rather than
/// positional `(content, source)` strings. The category is mapped to the
/// on-chain `zeroclaw_{category}` source tag internally, exactly as the
/// single-item path does.
#[derive(Debug, Clone)]
pub struct BatchStoreItem {
    pub content: String,
    pub category: MemoryCategory,
}

/// A memory entry (matches ZeroClaw's `MemoryEntry`).
#[derive(Debug, Clone)]
pub struct MemoryEntry {
    pub id: String,
    pub key: String,
    pub content: String,
    pub category: MemoryCategory,
    pub timestamp: String,
    pub session_id: Option<String>,
    pub score: Option<f64>,
}

// ---------------------------------------------------------------------------
// TotalReclawMemory
// ---------------------------------------------------------------------------

/// TotalReclaw memory backend for ZeroClaw.
///
/// Implements the ZeroClaw `Memory` trait with full E2E encryption.
pub struct TotalReclawMemory {
    keys: DerivedKeys,
    lsh_hasher: LshHasher,
    embedding_provider: Box<dyn EmbeddingProvider>,
    relay: RelayClient,
    private_key: [u8; 32],
    /// In-memory hot cache for recent recall results.
    hot_cache: std::sync::Mutex<HotCache>,
}

/// Configuration for creating a TotalReclawMemory instance.
pub struct TotalReclawConfig {
    pub mnemonic: String,
    pub embedding_mode: EmbeddingMode,
    pub embedding_dims: usize,
    pub relay_url: String,
    pub is_test: bool,
}

impl Default for TotalReclawConfig {
    fn default() -> Self {
        Self {
            mnemonic: String::new(),
            embedding_mode: EmbeddingMode::Ollama {
                base_url: "http://localhost:11434".into(),
                model: "nomic-embed-text".into(),
            },
            embedding_dims: 640,
            relay_url: DEFAULT_RELAY_URL.into(),
            is_test: false,
        }
    }
}

/// Merge broadened-search facts into the trapdoor-search hits, deduplicating
/// by fact id. Trapdoor hits keep their order and priority; a broadened fact is
/// appended only if its id was not already present.
///
/// Extracted from [`TotalReclawMemory::recall`] so the dedup rule is unit-
/// testable without a live relay.
fn merge_candidates_dedup(
    mut candidates: Vec<search::SubgraphFact>,
    broadened: Vec<search::SubgraphFact>,
) -> Vec<search::SubgraphFact> {
    let mut seen: std::collections::HashSet<String> =
        candidates.iter().map(|c| c.id.clone()).collect();
    for fact in broadened {
        if !seen.contains(&fact.id) {
            seen.insert(fact.id.clone());
            candidates.push(fact);
        }
    }
    candidates
}

/// Decrypt each candidate fact and build the reranker input. Facts whose blob
/// fails to decode or decrypt are skipped. A stored embedding whose dimension
/// no longer matches the current model is re-embedded from the decrypted text
/// (and dropped to empty if re-embedding fails).
///
/// Extracted from [`TotalReclawMemory::recall`] so the decrypt/dedup/re-embed
/// paths are unit-testable with a stub [`EmbeddingProvider`] and hand-built
/// facts — no live relay required.
async fn decrypt_and_prepare_candidates(
    candidates: &[search::SubgraphFact],
    keys: &DerivedKeys,
    embedding_provider: &dyn EmbeddingProvider,
) -> Vec<Candidate> {
    let mut rerank_candidates = Vec::new();
    for fact in candidates {
        // Decrypt content
        let blob_b64 = match search::hex_blob_to_base64(&fact.encrypted_blob) {
            Some(b) => b,
            None => continue,
        };
        let decrypted = match crypto::decrypt(&blob_b64, &keys.encryption_key) {
            Ok(t) => t,
            Err(_) => continue,
        };

        // Parse the envelope to extract the display text + v1 source.
        // v1 blobs carry `source` (Retrieval v2 Tier 1 weighting signal);
        // v0 blobs don't — reranker falls back to the legacy claim weight.
        let envelope = parse_decrypted_envelope(&decrypted);
        let text = envelope.text;

        // Decrypt embedding (if available)
        let mut emb = fact
            .encrypted_embedding
            .as_deref()
            .and_then(|e| crypto::decrypt(e, &keys.encryption_key).ok())
            .and_then(|b64| base64::engine::general_purpose::STANDARD.decode(&b64).ok())
            .map(|bytes| {
                bytes
                    .chunks_exact(4)
                    .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                    .collect::<Vec<f32>>()
            })
            .unwrap_or_default();

        // Re-embed if stored dimension differs from current model
        let expected_dims = embedding_provider.dimensions();
        if !emb.is_empty() && emb.len() != expected_dims {
            match embedding_provider.embed(&text).await {
                Ok(fresh) => emb = fresh,
                Err(_) => emb = Vec::new(),
            }
        }

        rerank_candidates.push(Candidate {
            id: fact.id.clone(),
            text: text.clone(),
            embedding: emb,
            timestamp: fact.timestamp.clone().unwrap_or_default(),
            source: envelope.v1_source,
        });
    }
    rerank_candidates
}

impl TotalReclawMemory {
    /// Create a new TotalReclaw memory backend.
    ///
    /// This initializes all crypto keys, derives the EOA and Smart Account,
    /// sets up the LSH hasher, the embedding provider, and registers with the relay.
    pub async fn new(config: TotalReclawConfig) -> Result<Self> {
        // Derive keys from mnemonic
        let keys = crypto::derive_keys_from_mnemonic(&config.mnemonic)?;
        let lsh_seed = crypto::derive_lsh_seed(&config.mnemonic, &keys.salt)?;
        let lsh_hasher = LshHasher::new(&lsh_seed, config.embedding_dims)?;

        // Create embedding provider
        let embedding_provider =
            embedding::create_provider(config.embedding_mode, config.embedding_dims)?;

        // Derive EOA + private key natively (BIP-44)
        let eth_wallet = wallet::derive_eoa(&config.mnemonic)?;
        let private_key = eth_wallet.private_key;

        // Resolve Smart Account address via CREATE2 factory
        let wallet_address =
            wallet::resolve_smart_account_address(&eth_wallet.address, "https://sepolia.base.org")
                .await?;

        // Compute auth key hash and hex
        let auth_key_hex = hex::encode(keys.auth_key);
        let auth_key_hash = crypto::compute_auth_key_hash(&keys.auth_key);
        let salt_hex = hex::encode(keys.salt);

        // Chain resolution step 1 (offline): guess from the billing cache so the
        // first handshake targets the right chain without a round-trip.
        // See `initial_chain_id_from_cache` for the full cache→network precedence.
        let chain_id = initial_chain_id_from_cache();

        // Create relay client with wallet address
        let relay_config = RelayConfig {
            relay_url: config.relay_url.clone(),
            auth_key_hex: auth_key_hex.clone(),
            wallet_address: wallet_address.clone(),
            is_test: config.is_test,
            chain_id,
        };
        let mut relay = RelayClient::new(relay_config);

        // Register with relay (idempotent)
        let _user_id = relay
            .register(&auth_key_hash, &salt_hex)
            .await
            .ok(); // Non-fatal if registration fails (may already be registered)

        // Chain resolution step 2 (network, authoritative): a live billing check
        // can promote a Pro account to Gnosis mainnet when the cache was cold or
        // stale. This only ever upgrades to `CHAIN_GNOSIS`, never downgrades.
        if let Ok(status) = relay.billing_status().await {
            if status.tier.as_deref() == Some("pro") {
                relay.set_chain_id(CHAIN_GNOSIS);
            }
        }

        Ok(Self {
            keys,
            lsh_hasher,
            embedding_provider,
            relay,
            private_key,
            hot_cache: std::sync::Mutex::new(HotCache::new()),
        })
    }

    /// Get the wallet address.
    pub fn wallet_address(&self) -> &str {
        self.relay.wallet_address()
    }

    /// Get a reference to the relay client.
    pub fn relay(&self) -> &RelayClient {
        &self.relay
    }

    /// Get a reference to the derived keys.
    pub fn keys(&self) -> &DerivedKeys {
        &self.keys
    }

    /// Get a reference to the private key.
    pub fn private_key(&self) -> &[u8; 32] {
        &self.private_key
    }

    // -----------------------------------------------------------------------
    // Memory trait methods
    // -----------------------------------------------------------------------

    /// Backend name.
    pub fn name(&self) -> &str {
        "totalreclaw"
    }

    /// Store a memory entry using native UserOp.
    ///
    /// Importance defaults to 10 (max) if not specified. Per the client-consistency
    /// spec, `decayScore = importance / 10`, so importance=10 -> decayScore=1.0.
    ///
    /// Clears the hot cache after storing to avoid stale results.
    pub async fn store(
        &self,
        _key: &str,
        content: &str,
        category: MemoryCategory,
        _session_id: Option<&str>,
    ) -> Result<()> {
        self.store_with_importance(_key, content, category, _session_id, 10.0)
            .await
    }

    /// Store a memory entry with explicit importance (1-10 scale).
    ///
    /// Per the client-consistency spec, `decayScore = importance / 10`.
    pub async fn store_with_importance(
        &self,
        _key: &str,
        content: &str,
        category: MemoryCategory,
        _session_id: Option<&str>,
        importance: f64,
    ) -> Result<()> {
        let source = format!("zeroclaw_{}", category);
        store::store_fact_with_importance(
            content,
            &source,
            importance,
            &self.keys,
            &self.lsh_hasher,
            self.embedding_provider.as_ref(),
            &self.relay,
            Some(&self.private_key),
        )
        .await?;

        // Invalidate hot cache after store (new data available)
        if let Ok(mut cache) = self.hot_cache.lock() {
            cache.clear();
        }
        Ok(())
    }

    /// Store multiple memory entries as a single batched UserOp.
    ///
    /// Each item carries a typed [`MemoryCategory`], matching the single-item
    /// `store` path; the category is mapped to the `zeroclaw_{category}` source
    /// tag before submission.
    ///
    /// Gas savings: ~64% vs individual submissions for batch of 5.
    pub async fn store_batch(&self, items: &[BatchStoreItem]) -> Result<Vec<String>> {
        // Map each typed category to its on-chain source tag (same rule as the
        // single-item path), then hand (content, source) pairs to the pipeline.
        let sources: Vec<String> = items
            .iter()
            .map(|it| format!("zeroclaw_{}", it.category))
            .collect();
        let facts: Vec<(&str, &str)> = items
            .iter()
            .zip(sources.iter())
            .map(|(it, src)| (it.content.as_str(), src.as_str()))
            .collect();

        let result = store::store_fact_batch(
            &facts,
            &self.keys,
            &self.lsh_hasher,
            self.embedding_provider.as_ref(),
            &self.relay,
            &self.private_key,
        )
        .await?;

        // Invalidate hot cache after batch store
        if let Ok(mut cache) = self.hot_cache.lock() {
            cache.clear();
        }
        Ok(result)
    }

    /// Recall memories matching a query.
    ///
    /// Uses a hot cache to skip remote queries when a semantically similar
    /// query (cosine >= 0.85) was recently answered.
    pub async fn recall(
        &self,
        query: &str,
        limit: usize,
        _session_id: Option<&str>,
    ) -> Result<Vec<MemoryEntry>> {
        // 1. Generate query embedding first (needed for both cache check and search)
        let query_embedding = self.embedding_provider.embed(query).await?;

        // 2. Hot cache check: skip remote query if similar query was recently answered
        if let Ok(cache) = self.hot_cache.lock() {
            if let Some(cached_results) = cache.lookup(&query_embedding) {
                return Ok(cached_results);
            }
        }

        // 3. Generate query trapdoors (word hashes + stems)
        let word_trapdoors = crate::blind::generate_blind_indices(query);

        // 4. Generate LSH trapdoors from embedding
        let embedding_f64: Vec<f64> = query_embedding.iter().map(|&f| f as f64).collect();
        let lsh_trapdoors = self.lsh_hasher.hash(&embedding_f64)?;

        // 5. Combine all trapdoors
        let mut all_trapdoors = word_trapdoors;
        all_trapdoors.extend(lsh_trapdoors.into_iter());

        // 6. Dynamic candidate pool sizing from billing cache
        let billing_cache = billing::read_cache();
        let max_candidates = billing::get_max_candidate_pool(billing_cache.as_ref());

        // 7. Search subgraph
        let candidates = search::search_candidates(
            &self.relay,
            &all_trapdoors,
            max_candidates,
        )
        .await?;

        // Always run broadened search and merge — ensures vocabulary mismatches
        // (e.g., "preferences" vs "prefer") don't cause recall failures.
        // The reranker handles scoring; extra cost is ~1 GraphQL query per recall.
        let broadened = search::search_broadened(&self.relay, max_candidates)
            .await
            .unwrap_or_default();

        // Merge broadened results into the trapdoor hits (deduplicate by ID).
        let candidates = merge_candidates_dedup(candidates, broadened);

        // 8. Decrypt candidates and build reranker input (with v1 provenance),
        // re-embedding any fact whose stored embedding dimension no longer
        // matches the current model.
        let rerank_candidates = decrypt_and_prepare_candidates(
            &candidates,
            &self.keys,
            self.embedding_provider.as_ref(),
        )
        .await;

        // 9. Rerank with Retrieval v2 Tier 1 source weights enabled.
        // v1 blobs carry a MemorySource; v0 blobs fall through to the legacy
        // claim weight so pre-2.0 vaults aren't penalized.
        let ranked = reranker::rerank_with_config(
            query,
            &query_embedding,
            &rerank_candidates,
            limit,
            RerankerConfig {
                // source-weights off (recall alignment 2026-06-08; tie-or-worse on shipped path)
                apply_source_weights: false,
                ..RerankerConfig::default()
            },
        )?;

        // 10. Convert to MemoryEntry
        let results: Vec<MemoryEntry> = ranked
            .into_iter()
            .map(|r| MemoryEntry {
                id: r.id.clone(),
                key: r.id,
                content: r.text,
                category: MemoryCategory::Core, // Category not stored in subgraph; default to Core
                timestamp: r.timestamp,
                session_id: None,
                score: Some(r.score),
            })
            .collect();

        // 11. Update hot cache
        if let Ok(mut cache) = self.hot_cache.lock() {
            cache.insert(query_embedding, results.clone());
        }

        Ok(results)
    }

    /// Auto-recall: search with the spec-mandated top_k=8.
    ///
    /// Per the client-consistency spec, auto-recall at session start
    /// uses the raw user message as query and returns top 8 after reranking.
    pub async fn auto_recall(&self, query: &str) -> Result<Vec<MemoryEntry>> {
        self.recall(query, AUTO_RECALL_TOP_K, None).await
    }

    /// Get a specific memory entry by key/ID.
    pub async fn get(&self, key: &str) -> Result<Option<MemoryEntry>> {
        let results = self.recall(key, 1, None).await?;
        Ok(results.into_iter().next())
    }

    /// List all memories (paginated export).
    ///
    /// Parses each decrypted envelope to extract the correct category
    /// (previously hardcoded to Core).
    pub async fn list(
        &self,
        _category: Option<&MemoryCategory>,
        _session_id: Option<&str>,
    ) -> Result<Vec<MemoryEntry>> {
        let facts = search::fetch_all_facts(&self.relay).await?;

        let mut entries = Vec::new();
        for fact in facts {
            let blob_b64 = match search::hex_blob_to_base64(&fact.encrypted_blob) {
                Some(b) => b,
                None => continue,
            };
            let decrypted = match crypto::decrypt(&blob_b64, &self.keys.encryption_key) {
                Ok(t) => t,
                Err(_) => continue,
            };

            let envelope = parse_decrypted_envelope(&decrypted);

            entries.push(MemoryEntry {
                id: fact.id.clone(),
                key: fact.id,
                content: envelope.text,
                category: envelope.category,
                timestamp: fact.timestamp.unwrap_or_default(),
                session_id: None,
                score: None,
            });
        }

        Ok(entries)
    }

    /// Forget (soft-delete) a memory entry using native UserOp.
    ///
    /// Emits a Memory Taxonomy v1 tombstone (outer protobuf `version = 4`)
    /// so the subgraph can distinguish v1 deletes from legacy v3 deletes.
    pub async fn forget(&self, key: &str) -> Result<bool> {
        store::store_tombstone_v1(key, &self.relay, Some(&self.private_key)).await?;
        Ok(true)
    }

    // -----------------------------------------------------------------------
    // Memory Taxonomy v1 extensions
    // -----------------------------------------------------------------------

    /// Store a Memory Taxonomy v1 claim (explicit v1 write path).
    ///
    /// Use this when the caller has full v1 context (type, source, scope,
    /// volatility, optional reasoning). Produces a v4 protobuf envelope
    /// with the canonical `MemoryClaimV1` JSON inner blob.
    ///
    /// For the ZeroClaw `Memory` trait's simpler `(key, content, category)`
    /// shape, `store_with_importance()` remains the primary entry point
    /// and continues to emit v3 envelopes during the v0→v1 migration
    /// window. Switch your agent to `store_v1()` when you want v1
    /// provenance-weighted reranking at recall time.
    pub async fn store_v1(&self, input: &store::V1StoreInput) -> Result<String> {
        let fact_id = store::store_fact_v1(
            input,
            &self.keys,
            &self.lsh_hasher,
            self.embedding_provider.as_ref(),
            &self.relay,
            Some(&self.private_key),
        )
        .await?;

        // Invalidate hot cache after v1 store
        if let Ok(mut cache) = self.hot_cache.lock() {
            cache.clear();
        }
        Ok(fact_id)
    }

    /// Pin a memory claim — supersede it with a new v1 blob whose
    /// `volatility: stable` signals "never auto-supersede".
    ///
    /// ZeroClaw implements pin as a supersede operation: fetch the fact,
    /// re-store it as a v1 claim with `volatility: stable`, then tombstone
    /// the prior version. The new claim's `superseded_by` field tracks the
    /// previous id so a future unpin can reverse the chain.
    ///
    /// Not-yet-implemented: this method returns an error advising the
    /// caller to run the MCP server's `totalreclaw_pin` tool instead.
    /// See ZeroClaw 2.0 Known Gaps in CLAUDE.md.
    pub async fn pin(&self, _memory_id: &str) -> Result<()> {
        Err(crate::Error::NotImplemented(
            "pin: not yet implemented in ZeroClaw — use the MCP totalreclaw_pin tool \
             from your agent. See CLAUDE.md Known Gaps."
                .into(),
        ))
    }

    /// Retype a memory claim — change its v1 `type` (claim → directive, etc.)
    /// via supersede.
    ///
    /// Not-yet-implemented in ZeroClaw's native trait. The MCP server's
    /// `totalreclaw_retype` tool is the canonical path.
    pub async fn retype(
        &self,
        _memory_id: &str,
        _new_type: MemoryTypeV1,
    ) -> Result<()> {
        Err(crate::Error::NotImplemented(
            "retype: not yet implemented in ZeroClaw — use the MCP \
             totalreclaw_retype tool. See CLAUDE.md Known Gaps."
                .into(),
        ))
    }

    /// Set or change the v1 `scope` of a memory claim via supersede.
    ///
    /// Not-yet-implemented in ZeroClaw's native trait. The MCP server's
    /// `totalreclaw_set_scope` tool is the canonical path.
    pub async fn set_scope(
        &self,
        _memory_id: &str,
        _new_scope: totalreclaw_core::claims::MemoryScope,
    ) -> Result<()> {
        Err(crate::Error::NotImplemented(
            "set_scope: not yet implemented in ZeroClaw — use the MCP \
             totalreclaw_set_scope tool. See CLAUDE.md Known Gaps."
                .into(),
        ))
    }

    /// Count active memories.
    pub async fn count(&self) -> Result<usize> {
        search::count_facts(&self.relay).await
    }

    /// Health check.
    pub async fn health_check(&self) -> bool {
        self.relay.health_check().await.unwrap_or(false)
    }

    /// Billing status -- tier, usage, limits. Also updates the billing cache.
    pub async fn status(&self) -> Result<crate::relay::BillingStatus> {
        self.relay.billing_status().await
    }

    /// Fetch billing cache (from disk or relay, with 2h TTL).
    ///
    /// Returns a cached billing status with parsed feature flags.
    pub async fn billing_cache(&self) -> Result<BillingCache> {
        billing::fetch_billing_status(&self.relay).await
    }

    /// Check for quota warnings (>80% usage).
    ///
    /// Returns a human-readable warning message or None if usage is below 80%.
    /// Call at session start (before_agent_start equivalent).
    pub async fn quota_warning(&self) -> Option<String> {
        let cache = billing::fetch_billing_status(&self.relay).await.ok()?;
        cache.quota_warning_message()
    }

    /// Store debrief items from a session.
    ///
    /// ZeroClaw calls this from its consolidation phase or session-end handler.
    /// The caller is responsible for running the LLM and passing parsed results.
    ///
    /// Each item is stored via the existing `store` pipeline with
    /// `source: "zeroclaw_debrief"`.
    pub async fn debrief(&self, items: &[crate::debrief::DebriefItem]) -> Result<usize> {
        let mut stored = 0;
        for item in items.iter().take(crate::debrief::MAX_DEBRIEF_ITEMS) {
            let importance = item.importance as f64;
            store::store_fact_with_importance(
                &item.text,
                crate::debrief::DEBRIEF_SOURCE,
                importance,
                &self.keys,
                &self.lsh_hasher,
                self.embedding_provider.as_ref(),
                &self.relay,
                Some(&self.private_key),
            )
            .await?;
            stored += 1;
        }

        // Invalidate hot cache after debrief store
        if let Ok(mut cache) = self.hot_cache.lock() {
            cache.clear();
        }
        Ok(stored)
    }

    /// Export all memories as plaintext (decrypted).
    pub async fn export(&self) -> Result<Vec<MemoryEntry>> {
        self.list(None, None).await
    }

    /// Upgrade to Pro tier -- returns Stripe checkout URL.
    pub async fn upgrade(&self) -> Result<String> {
        self.relay.create_checkout().await
    }
}

#[cfg(test)]
mod recall_tests {
    use super::*;
    use std::future::Future;
    use std::pin::Pin;

    /// Deterministic key material — no mnemonic derivation needed for the
    /// pure decrypt/re-embed paths under test.
    fn test_keys() -> DerivedKeys {
        DerivedKeys {
            auth_key: [1u8; 32],
            encryption_key: [7u8; 32],
            dedup_key: [3u8; 32],
            salt: [2u8; 32],
        }
    }

    /// Build an `encrypted_blob` the way the store path does: encrypt → base64
    /// → decode → hex (recall reverses this via `hex_blob_to_base64`).
    fn encrypt_blob(keys: &DerivedKeys, plaintext: &str) -> String {
        let b64 = crypto::encrypt(plaintext, &keys.encryption_key).unwrap();
        let bytes = base64::engine::general_purpose::STANDARD.decode(&b64).unwrap();
        hex::encode(bytes)
    }

    /// Build an `encrypted_embedding`: little-endian f32 bytes → base64 → encrypt
    /// (recall decrypts then base64-decodes then reads f32 chunks).
    fn encrypt_embedding(keys: &DerivedKeys, emb: &[f32]) -> String {
        let mut bytes = Vec::with_capacity(emb.len() * 4);
        for f in emb {
            bytes.extend_from_slice(&f.to_le_bytes());
        }
        let inner_b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
        crypto::encrypt(&inner_b64, &keys.encryption_key).unwrap()
    }

    fn fact(id: &str, encrypted_blob: &str, encrypted_embedding: Option<String>) -> search::SubgraphFact {
        search::SubgraphFact {
            id: id.to_string(),
            encrypted_blob: encrypted_blob.to_string(),
            encrypted_embedding,
            decay_score: None,
            timestamp: Some("2026-01-01T00:00:00Z".to_string()),
            created_at: None,
            is_active: Some(true),
            content_fp: None,
        }
    }

    /// Stub embedding provider: reports a fixed dimensionality and returns a
    /// fixed vector on `embed`. Used to drive the re-embed branch offline.
    struct StubEmbedder {
        dims: usize,
        out: Vec<f32>,
    }

    impl EmbeddingProvider for StubEmbedder {
        fn embed(&self, _text: &str) -> Pin<Box<dyn Future<Output = Result<Vec<f32>>> + Send + '_>> {
            let out = self.out.clone();
            Box::pin(async move { Ok(out) })
        }
        fn dimensions(&self) -> usize {
            self.dims
        }
    }

    const V1_ENVELOPE: &str = r#"{"text":"cats are great","type":"claim","source":"user"}"#;

    #[test]
    fn merge_candidates_dedup_appends_only_unseen() {
        let trapdoor = vec![fact("a", "", None), fact("b", "", None)];
        let broadened = vec![fact("b", "", None), fact("c", "", None)];
        let out = merge_candidates_dedup(trapdoor, broadened);
        let ids: Vec<_> = out.iter().map(|f| f.id.clone()).collect();
        // Trapdoor hits keep their order; only the unseen broadened id is added.
        assert_eq!(ids, vec!["a", "b", "c"]);
    }

    #[test]
    fn merge_candidates_dedup_preserves_all_when_disjoint() {
        let trapdoor = vec![fact("a", "", None)];
        let broadened = vec![fact("b", "", None)];
        let out = merge_candidates_dedup(trapdoor, broadened);
        assert_eq!(out.len(), 2);
    }

    #[tokio::test]
    async fn recall_reembeds_on_dimension_mismatch() {
        let keys = test_keys();
        let blob = encrypt_blob(&keys, V1_ENVELOPE);
        // Stored embedding is 3-dim; current model expects 4-dim.
        let emb_blob = encrypt_embedding(&keys, &[0.1, 0.2, 0.3]);
        let stub = StubEmbedder { dims: 4, out: vec![9.0, 9.0, 9.0, 9.0] };

        let out =
            decrypt_and_prepare_candidates(&[fact("x", &blob, Some(emb_blob))], &keys, &stub).await;

        assert_eq!(out.len(), 1);
        assert_eq!(out[0].text, "cats are great");
        // Dimension mismatch triggers a fresh embed from the decrypted text.
        assert_eq!(out[0].embedding, vec![9.0, 9.0, 9.0, 9.0]);
    }

    #[tokio::test]
    async fn recall_keeps_embedding_when_dimension_matches() {
        let keys = test_keys();
        let blob = encrypt_blob(&keys, V1_ENVELOPE);
        let stored = vec![0.5f32, 0.6, 0.7, 0.8];
        let emb_blob = encrypt_embedding(&keys, &stored);
        // Provider would return a different vector; it must NOT be consulted.
        let stub = StubEmbedder { dims: 4, out: vec![9.0, 9.0, 9.0, 9.0] };

        let out =
            decrypt_and_prepare_candidates(&[fact("y", &blob, Some(emb_blob))], &keys, &stub).await;

        assert_eq!(out[0].embedding, stored);
    }

    #[tokio::test]
    async fn recall_leaves_missing_embedding_empty() {
        let keys = test_keys();
        let blob = encrypt_blob(&keys, V1_ENVELOPE);
        let stub = StubEmbedder { dims: 4, out: vec![9.0, 9.0, 9.0, 9.0] };

        let out = decrypt_and_prepare_candidates(&[fact("z", &blob, None)], &keys, &stub).await;

        // No stored embedding: the re-embed guard (`!emb.is_empty()`) skips, so
        // the candidate carries an empty vector (reranker falls back to BM25).
        assert!(out[0].embedding.is_empty());
    }

    #[tokio::test]
    async fn recall_skips_undecryptable_and_wrong_key_blobs() {
        let keys = test_keys();
        let good = encrypt_blob(&keys, V1_ENVELOPE);
        // "nothex" fails hex decode; a blob encrypted under a different key
        // decodes as hex/base64 but fails authenticated decryption.
        let wrong_key = DerivedKeys { encryption_key: [42u8; 32], ..test_keys() };
        let wrong = encrypt_blob(&wrong_key, V1_ENVELOPE);
        let stub = StubEmbedder { dims: 4, out: vec![] };

        let out = decrypt_and_prepare_candidates(
            &[
                fact("nothex", "nothex", None),
                fact("wrongkey", &wrong, None),
                fact("good", &good, None),
            ],
            &keys,
            &stub,
        )
        .await;

        let ids: Vec<_> = out.iter().map(|c| c.id.clone()).collect();
        assert_eq!(ids, vec!["good"]);
    }
}
