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
use crate::reranker::{self, Candidate};
use crate::relay::{RelayClient, RelayConfig};
use crate::search;
use crate::store;
use crate::wallet;
use crate::Result;

/// Default relay URL.
const DEFAULT_RELAY_URL: &str = "https://api.totalreclaw.xyz";

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
            embedding_dims: 1024,
            relay_url: DEFAULT_RELAY_URL.into(),
            is_test: false,
        }
    }
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

        // Auto-detect Pro tier from billing cache for chain routing
        // Free tier = Base Sepolia (84532), Pro tier = Gnosis mainnet (100)
        let chain_id = if let Some(cache) = billing::read_cache() {
            if cache.is_pro() { 100 } else { 84532 }
        } else {
            84532
        };

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

        // Re-check billing to potentially update chain_id for Pro users
        if let Ok(status) = relay.billing_status().await {
            if status.tier.as_deref() == Some("pro") {
                relay.set_chain_id(100);
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
    /// Gas savings: ~64% vs individual submissions for batch of 5.
    pub async fn store_batch(
        &self,
        facts: &[(&str, &str)], // (content, source) pairs
    ) -> Result<Vec<String>> {
        let result = store::store_fact_batch(
            facts,
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
        let mut candidates = search::search_candidates(
            &self.relay,
            self.relay.wallet_address(),
            &all_trapdoors,
            max_candidates,
        )
        .await?;

        // Always run broadened search and merge — ensures vocabulary mismatches
        // (e.g., "preferences" vs "prefer") don't cause recall failures.
        // The reranker handles scoring; extra cost is ~1 GraphQL query per recall.
        let broadened = search::search_broadened(
            &self.relay,
            self.relay.wallet_address(),
            max_candidates,
        )
        .await
        .unwrap_or_default();

        // Merge broadened results with existing candidates (deduplicate by ID)
        let mut seen: std::collections::HashSet<String> =
            candidates.iter().map(|c| c.id.clone()).collect();
        for fact in broadened {
            if !seen.contains(&fact.id) {
                seen.insert(fact.id.clone());
                candidates.push(fact);
            }
        }

        // 8. Decrypt candidates and build reranker input
        let mut rerank_candidates = Vec::new();
        for fact in &candidates {
            // Decrypt content
            let blob_b64 = match search::hex_blob_to_base64(&fact.encrypted_blob) {
                Some(b) => b,
                None => continue,
            };
            let text = match crypto::decrypt(&blob_b64, &self.keys.encryption_key) {
                Ok(t) => t,
                Err(_) => continue,
            };

            // Decrypt embedding (if available)
            let emb = fact
                .encrypted_embedding
                .as_deref()
                .and_then(|e| crypto::decrypt(e, &self.keys.encryption_key).ok())
                .and_then(|b64| {
                    base64::engine::general_purpose::STANDARD
                        .decode(&b64)
                        .ok()
                })
                .map(|bytes| {
                    bytes
                        .chunks_exact(4)
                        .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                        .collect::<Vec<f32>>()
                })
                .unwrap_or_default();

            rerank_candidates.push(Candidate {
                id: fact.id.clone(),
                text: text.clone(),
                embedding: emb,
                timestamp: fact.timestamp.clone().unwrap_or_default(),
            });
        }

        // 9. Rerank
        let ranked = reranker::rerank(query, &query_embedding, &rerank_candidates, limit)?;

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
    pub async fn list(
        &self,
        _category: Option<&MemoryCategory>,
        _session_id: Option<&str>,
    ) -> Result<Vec<MemoryEntry>> {
        let facts = search::fetch_all_facts(&self.relay, self.relay.wallet_address()).await?;

        let mut entries = Vec::new();
        for fact in facts {
            let blob_b64 = match search::hex_blob_to_base64(&fact.encrypted_blob) {
                Some(b) => b,
                None => continue,
            };
            let text = match crypto::decrypt(&blob_b64, &self.keys.encryption_key) {
                Ok(t) => t,
                Err(_) => continue,
            };

            entries.push(MemoryEntry {
                id: fact.id.clone(),
                key: fact.id,
                content: text,
                category: MemoryCategory::Core,
                timestamp: fact.timestamp.unwrap_or_default(),
                session_id: None,
                score: None,
            });
        }

        Ok(entries)
    }

    /// Forget (soft-delete) a memory entry using native UserOp.
    pub async fn forget(&self, key: &str) -> Result<bool> {
        store::store_tombstone(key, &self.relay, Some(&self.private_key)).await?;
        Ok(true)
    }

    /// Count active memories.
    pub async fn count(&self) -> Result<usize> {
        search::count_facts(&self.relay, self.relay.wallet_address()).await
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
