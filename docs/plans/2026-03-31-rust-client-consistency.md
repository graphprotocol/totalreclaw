# Rust Client Consistency — Close All Gaps Against Spec

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bring the Rust `totalreclaw-memory` crate to full compliance with `docs/specs/totalreclaw/client-consistency.md` — closing every gap identified in the feature comparison against OpenClaw (the gold standard implementation).

**Architecture:** Add billing cache, store-time cosine dedup, dynamic candidate pool, hot cache, quota warnings, 403 handling, feature flags, and correct client identification — all within the existing crate structure. No new external dependencies needed (reranker already has cosine similarity).

**Tech Stack:** Rust, existing crate dependencies (reqwest, serde, chrono, uuid). No new crates.

---

## Prerequisites

Read these files before starting:

| File | Why |
|------|-----|
| `docs/specs/totalreclaw/client-consistency.md` | The spec — every parameter must match |
| `rust/totalreclaw-memory/src/backend.rs` | Current Memory trait implementation |
| `rust/totalreclaw-memory/src/store.rs` | Current store pipeline |
| `rust/totalreclaw-memory/src/search.rs` | Current search pipeline |
| `rust/totalreclaw-memory/src/relay.rs` | Current relay client |
| `rust/totalreclaw-memory/src/reranker.rs` | Has cosine_similarity() we can reuse |
| `skill/plugin/index.ts` | OpenClaw reference (search for PluginHotCache, SEMANTIC_SKIP_THRESHOLD, billingCache) |

**ALL tests MUST hit staging (`api-staging.totalreclaw.xyz`), NEVER production.**

---

## Gap Matrix (spec requirement → current state → task)

| Spec Requirement | Current State | Task |
|-----------------|---------------|------|
| Client ID: `rust-client:zeroclaw` | Hardcoded `zeroclaw-memory` | Task 1 |
| Billing cache (2h TTL) | No caching | Task 2 |
| Feature flags from billing | Not fetched | Task 2 |
| Quota warnings (>80%) | Not implemented | Task 3 |
| 403 handling + cache invalidation | Not implemented | Task 3 |
| Dynamic candidate pool sizing | Static 100 | Task 4 |
| Store-time cosine dedup (>= 0.85) | Fingerprint only | Task 5 |
| Fetch 50 existing memories for dedup | Not implemented | Task 5 |
| Hot cache (encrypted in-memory) | No caching | Task 6 |
| Importance normalization (importance/10 → decayScore) | Hardcoded 1.0 | Task 7 |
| decayScore from importance | Hardcoded 1.0 | Task 7 |
| top_k = 8 for auto-recall | Passed as parameter | Task 8 |

**NOT applicable to ZeroClaw (by design):**
- Auto-extraction hooks (ZeroClaw orchestrates extraction, not us)
- LLM-guided dedup (requires LLM API key — ZeroClaw's consolidation handles this)
- Extraction prompts (ZeroClaw's consolidation pipeline provides these)
- MEMORY.md header injection (OpenClaw-specific)
- Pre-compaction/pre-reset hooks (OpenClaw-specific)

---

## Task 1: Fix client identification header

**Files:**
- Modify: `rust/totalreclaw-memory/src/relay.rs`
- Modify: `rust/totalreclaw-memory/src/userop.rs`

Per spec: `X-TotalReclaw-Client` must be `rust-client:zeroclaw`.

**Step 1: Update relay.rs**

Find `"zeroclaw-memory"` and replace with `"rust-client:zeroclaw"` in the `headers()` method and any other occurrences.

**Step 2: Update userop.rs**

Find `"zeroclaw-memory"` in `build_headers()` and replace with `"rust-client:zeroclaw"`.

**Step 3: Run tests**

```bash
cargo test
```

**Step 4: Commit**

```bash
git commit -m "fix(rust): client ID header to rust-client:zeroclaw per spec"
```

---

## Task 2: Billing cache with feature flags (2h TTL)

**Files:**
- Create: `rust/totalreclaw-memory/src/billing.rs`
- Modify: `rust/totalreclaw-memory/src/lib.rs` (add `pub mod billing;`)
- Modify: `rust/totalreclaw-memory/src/backend.rs` (use billing cache)

**Step 1: Implement billing cache**

```rust
// rust/totalreclaw-memory/src/billing.rs
//! Billing cache with 2-hour TTL.
//!
//! Fetches billing status + feature flags from relay, caches in memory.
//! Per spec: TTL = 7200 seconds (2 hours).

use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::relay::RelayClient;
use crate::Result;

const CACHE_TTL: Duration = Duration::from_secs(7200); // 2 hours

/// Feature flags from the billing endpoint.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct FeatureFlags {
    /// Max candidate pool size for search.
    #[serde(default)]
    pub max_candidate_pool: Option<usize>,
    /// Extraction interval in turns.
    #[serde(default)]
    pub extraction_interval: Option<usize>,
    /// Max facts per extraction cycle.
    #[serde(default)]
    pub max_facts_per_extraction: Option<usize>,
    /// Whether LLM-guided dedup is enabled.
    #[serde(default)]
    pub llm_dedup: Option<bool>,
}

/// Cached billing status.
#[derive(Debug, Clone)]
pub struct BillingInfo {
    pub tier: String,
    pub facts_used: u64,
    pub facts_limit: u64,
    pub features: FeatureFlags,
}

/// Thread-safe billing cache.
pub struct BillingCache {
    inner: Mutex<Option<CacheEntry>>,
}

struct CacheEntry {
    info: BillingInfo,
    fetched_at: Instant,
}

impl BillingCache {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(None),
        }
    }

    /// Get billing info, fetching from relay if cache is stale or empty.
    pub async fn get(&self, relay: &RelayClient) -> Result<BillingInfo> {
        // Check cache
        {
            let guard = self.inner.lock().unwrap();
            if let Some(ref entry) = *guard {
                if entry.fetched_at.elapsed() < CACHE_TTL {
                    return Ok(entry.info.clone());
                }
            }
        }

        // Fetch fresh
        let status = relay.billing_status().await?;

        let features: FeatureFlags = status
            .features
            .as_ref()
            .and_then(|v| serde_json::from_value(v.clone()).ok())
            .unwrap_or_default();

        let info = BillingInfo {
            tier: status.tier.unwrap_or_else(|| "free".into()),
            facts_used: status.facts_used.unwrap_or(0),
            facts_limit: status.facts_limit.unwrap_or(0),
            features,
        };

        // Update cache
        {
            let mut guard = self.inner.lock().unwrap();
            *guard = Some(CacheEntry {
                info: info.clone(),
                fetched_at: Instant::now(),
            });
        }

        Ok(info)
    }

    /// Invalidate the cache (e.g., on 403).
    pub fn invalidate(&self) {
        let mut guard = self.inner.lock().unwrap();
        *guard = None;
    }

    /// Check if quota is above warning threshold (80%).
    pub fn quota_warning(&self) -> Option<String> {
        let guard = self.inner.lock().unwrap();
        if let Some(ref entry) = *guard {
            let info = &entry.info;
            if info.facts_limit > 0 {
                let pct = (info.facts_used as f64 / info.facts_limit as f64) * 100.0;
                if pct >= 80.0 {
                    return Some(format!(
                        "TotalReclaw quota warning: {:.0}% used ({}/{} writes). Consider upgrading to Pro.",
                        pct, info.facts_used, info.facts_limit
                    ));
                }
            }
        }
        None
    }
}
```

**Step 2: Wire into backend.rs**

Add `billing_cache: BillingCache` to `TotalReclawMemory`. Initialize in `new()`. Call `billing_cache.get()` in `store()` to get feature flags. Call `billing_cache.quota_warning()` in `recall()` to check if we should warn.

**Step 3: Test**

```bash
cargo test
```

**Step 4: Commit**

```bash
git commit -m "feat(rust): billing cache with 2h TTL + feature flags per spec"
```

---

## Task 3: Quota warnings + 403 handling

**Files:**
- Modify: `rust/totalreclaw-memory/src/backend.rs`
- Modify: `rust/totalreclaw-memory/src/relay.rs` (detect 403 status)

**Step 1: 403 detection in relay**

In `RelayClient::submit_fact_native()` and `graphql()`, check for HTTP 403. If 403, invalidate billing cache and return a specific error variant.

Add to `Error` enum in `lib.rs`:
```rust
#[error("quota exceeded: {0}")]
QuotaExceeded(String),
```

**Step 2: Handle in backend.rs**

In `store()`, catch `Error::QuotaExceeded` → invalidate billing cache → return error with upgrade message.

In `recall()`, prepend quota warning to results if `billing_cache.quota_warning()` returns Some.

**Step 3: Test + commit**

---

## Task 4: Dynamic candidate pool sizing

**Files:**
- Modify: `rust/totalreclaw-memory/src/backend.rs`
- Modify: `rust/totalreclaw-memory/src/search.rs`

**Step 1: Read max_candidate_pool from billing features**

Replace hardcoded `DEFAULT_MAX_CANDIDATES = 100` with:
```rust
let max_candidates = self.billing_cache
    .get(&self.relay).await
    .ok()
    .and_then(|b| b.features.max_candidate_pool)
    .unwrap_or(100);
```

**Step 2: Pass to search_candidates()**

The `search_candidates()` function already accepts `max_candidates` as a parameter.

**Step 3: Test + commit**

---

## Task 5: Store-time cosine dedup (>= 0.85 threshold)

**Files:**
- Modify: `rust/totalreclaw-memory/src/store.rs`

Per spec: before storing, fetch up to 50 existing memories via recall, compute cosine similarity against each, skip store if any match >= 0.85.

**Step 1: Add cosine dedup to store pipeline**

After the content fingerprint check (which handles exact dupes), add:

```rust
// Fetch up to 50 existing memories for near-duplicate detection
let existing = search::search_candidates(
    relay, relay.wallet_address(),
    &blind::generate_blind_indices(content),
    50,
).await.unwrap_or_default();

// Decrypt and compute cosine similarity against new embedding
for fact in &existing {
    if let Some(b64) = search::hex_blob_to_base64(&fact.encrypted_blob) {
        // Skip — we only need the embedding, not the full text
    }
    if let Some(ref enc_emb) = fact.encrypted_embedding {
        if let Ok(emb_b64) = crypto::decrypt(enc_emb, &keys.encryption_key) {
            if let Ok(emb_bytes) = base64::engine::general_purpose::STANDARD.decode(&emb_b64) {
                let existing_emb: Vec<f32> = emb_bytes.chunks_exact(4)
                    .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                    .collect();
                let sim = reranker::cosine_similarity_f32(&embedding, &existing_emb);
                if sim >= 0.85 {
                    // Near-duplicate found — tombstone and replace
                    let _ = store_tombstone(&fact.id, relay, private_key).await;
                    break;
                }
            }
        }
    }
}
```

Note: `cosine_similarity_f32` may need to be added as a public function in reranker.rs (the existing one works on `&[f32]` slices — just make it `pub`).

**Step 2: Test + commit**

---

## Task 6: Hot cache (encrypted in-memory recent facts)

**Files:**
- Create: `rust/totalreclaw-memory/src/hot_cache.rs`
- Modify: `rust/totalreclaw-memory/src/lib.rs`
- Modify: `rust/totalreclaw-memory/src/backend.rs`

Per OpenClaw reference: keep the most recent 30 recalled facts in memory (encrypted). On recall(), check hot cache first before hitting subgraph.

```rust
// rust/totalreclaw-memory/src/hot_cache.rs
//! In-memory hot cache for recently recalled facts.
//!
//! Stores the last 30 decrypted results keyed by query.
//! If a new query has cosine similarity >= 0.85 to a cached query,
//! return cached results (two-tier search skip).

use std::collections::VecDeque;
use std::sync::Mutex;

use crate::backend::MemoryEntry;

const MAX_CACHE_SIZE: usize = 30;
const SEMANTIC_SKIP_THRESHOLD: f32 = 0.85;

pub struct HotCache {
    entries: Mutex<VecDeque<CacheEntry>>,
}

struct CacheEntry {
    query_embedding: Vec<f32>,
    results: Vec<MemoryEntry>,
}

impl HotCache {
    pub fn new() -> Self {
        Self {
            entries: Mutex::new(VecDeque::with_capacity(MAX_CACHE_SIZE)),
        }
    }

    /// Check if a semantically similar query was recently answered.
    pub fn check(&self, query_embedding: &[f32]) -> Option<Vec<MemoryEntry>> {
        let guard = self.entries.lock().unwrap();
        for entry in guard.iter().rev() {
            let sim = cosine_sim(query_embedding, &entry.query_embedding);
            if sim >= SEMANTIC_SKIP_THRESHOLD {
                return Some(entry.results.clone());
            }
        }
        None
    }

    /// Store results for a query embedding.
    pub fn store(&self, query_embedding: Vec<f32>, results: Vec<MemoryEntry>) {
        let mut guard = self.entries.lock().unwrap();
        if guard.len() >= MAX_CACHE_SIZE {
            guard.pop_front();
        }
        guard.push_back(CacheEntry {
            query_embedding,
            results,
        });
    }

    /// Invalidate the entire cache (e.g., after a store operation).
    pub fn invalidate(&self) {
        let mut guard = self.entries.lock().unwrap();
        guard.clear();
    }
}

fn cosine_sim(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let mut dot = 0.0f32;
    let mut norm_a = 0.0f32;
    let mut norm_b = 0.0f32;
    for i in 0..a.len() {
        dot += a[i] * b[i];
        norm_a += a[i] * a[i];
        norm_b += b[i] * b[i];
    }
    let denom = norm_a.sqrt() * norm_b.sqrt();
    if denom < 1e-10 { 0.0 } else { dot / denom }
}
```

Wire into `backend.rs`:
- On `recall()`: check hot cache before hitting subgraph. If hit, return cached results.
- On `recall()` miss: query subgraph, store results in hot cache.
- On `store()`: invalidate hot cache (new fact may change recall results).

**Step 2: Test + commit**

---

## Task 7: Importance normalization (decayScore)

**Files:**
- Modify: `rust/totalreclaw-memory/src/store.rs`
- Modify: `rust/totalreclaw-memory/src/backend.rs`

Per spec: `decayScore = importance / 10` (0.0-1.0 scale). Currently hardcoded to 1.0.

**Step 1: Add importance parameter to store pipeline**

```rust
// In store.rs, change store_fact() signature:
pub async fn store_fact(
    content: &str,
    source: &str,
    importance: f64,  // 0.0-1.0 (already normalized)
    keys: &crypto::DerivedKeys,
    ...
```

Set `decay_score: importance` in the FactPayload.

**Step 2: In backend.rs**

Map ZeroClaw categories to default importance:
- Core (fact/preference/decision/goal/summary): importance = 0.8
- Conversation (episodic): importance = 0.5
- Daily (context): importance = 0.3

**Step 3: Test + commit**

---

## Task 8: Auto-recall top_k = 8

**Files:**
- Modify: `rust/totalreclaw-memory/src/backend.rs`

Per spec: auto-recall returns top 8 after reranking.

Currently the `recall()` method takes `limit` as a parameter (passed through from ZeroClaw). Add a constant:

```rust
/// Default top_k for auto-recall per client-consistency spec.
pub const AUTO_RECALL_TOP_K: usize = 8;
```

Document in the backend that when ZeroClaw calls `recall()` for auto-search, `limit` should be 8.

**Step 1: Add constant + test + commit**

---

## Task 9: Integration tests against spec

**Files:**
- Create: `rust/totalreclaw-memory/tests/client_consistency.rs`

Write a test that verifies each spec parameter:

```rust
#[test]
fn test_client_id_format() {
    // Verify header is "rust-client:zeroclaw"
}

#[test]
fn test_auto_recall_top_k() {
    assert_eq!(backend::AUTO_RECALL_TOP_K, 8);
}

#[test]
fn test_billing_cache_ttl() {
    // Verify 7200 seconds
}

#[test]
fn test_importance_normalization() {
    // Verify Core → 0.8, Conversation → 0.5, Daily → 0.3
}

#[tokio::test]
#[ignore] // Requires staging
async fn test_quota_warning_at_80_percent() {
    // Verify warning fires above 80%
}
```

**Step: Test + commit**

---

## Task 10: Re-run all E2E tests

**Step 1: Run full suite**

```bash
# Unit tests
cargo test

# Native UserOp E2E
cargo test --test native_userop_e2e -- --ignored --nocapture

# Three-way cross-client
cargo test --test three_way_cross_client -- --ignored --nocapture

# Client consistency
cargo test --test client_consistency -- --nocapture
```

All must pass.

**Step 2: Commit any fixes**

**Step 3: Final commit**

```bash
git commit -m "chore(rust): all spec gaps closed, full E2E validation passes"
```

---

## Execution Order

```
Task 1  — Client ID fix (5 min, no deps)
Task 2  — Billing cache + feature flags (30 min)
Task 3  — Quota warnings + 403 handling (15 min, depends on Task 2)
Task 4  — Dynamic candidate pool (10 min, depends on Task 2)
Task 5  — Store-time cosine dedup (20 min)
Task 6  — Hot cache (20 min)
Task 7  — Importance normalization (10 min)
Task 8  — Auto-recall top_k (5 min)
Task 9  — Integration tests (15 min)
Task 10 — Full E2E validation (5 min)
```

Tasks 1, 5, 6, 7, 8 are independent. Tasks 3-4 depend on Task 2. Task 10 depends on all.

## Validation Checklist

- [ ] `X-TotalReclaw-Client: rust-client:zeroclaw` in all requests
- [ ] Billing cache with 2h TTL, feature flags parsed
- [ ] Quota warning at >80% usage
- [ ] 403 → cache invalidation + QuotaExceeded error
- [ ] Dynamic candidate pool from billing features
- [ ] Store-time cosine dedup >= 0.85 (fetch 50, compare embeddings)
- [ ] Hot cache (30 facts, semantic skip >= 0.85)
- [ ] Importance normalization (Core=0.8, Conversation=0.5, Daily=0.3)
- [ ] Auto-recall top_k = 8
- [ ] All unit tests pass
- [ ] Native UserOp E2E passes
- [ ] Three-way cross-client E2E passes
