//! In-memory hot cache for recently recalled facts.
//!
//! Wraps the generic `totalreclaw_core::hotcache::HotCache<T>` with
//! `MemoryEntry`-specific types for the ZeroClaw memory backend.
//!
//! Matches the TypeScript plugin's hot cache behavior:
//! - Caches up to 30 recent query results in memory
//! - Skips remote subgraph query if a semantically similar query was recently answered
//! - Similarity threshold: cosine >= 0.85 between query embeddings
//! - Cache is per-session (lives on the TotalReclawMemory struct)

use crate::backend::MemoryEntry;

/// In-memory hot cache for semantic query dedup.
///
/// Thin wrapper around `totalreclaw_core::hotcache::HotCache<Vec<MemoryEntry>>`.
pub struct HotCache {
    inner: totalreclaw_core::hotcache::HotCache<Vec<MemoryEntry>>,
}

impl HotCache {
    /// Create an empty hot cache with default settings (30 entries, cosine >= 0.85).
    pub fn new() -> Self {
        Self {
            inner: totalreclaw_core::hotcache::HotCache::new(),
        }
    }

    /// Check if a semantically similar query has already been answered.
    ///
    /// Returns cached results if a query with cosine >= 0.85 exists.
    pub fn lookup(&self, query_embedding: &[f32]) -> Option<Vec<MemoryEntry>> {
        self.inner.lookup(query_embedding).cloned()
    }

    /// Insert a query result into the cache.
    ///
    /// Evicts the oldest entry if the cache is full.
    pub fn insert(&mut self, query_embedding: Vec<f32>, results: Vec<MemoryEntry>) {
        self.inner.insert(query_embedding, results);
    }

    /// Clear the cache (e.g., after a store operation).
    pub fn clear(&mut self) {
        self.inner.clear();
    }

    /// Number of cached entries.
    pub fn len(&self) -> usize {
        self.inner.len()
    }

    /// Whether the cache is empty.
    pub fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }
}

impl Default for HotCache {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backend::MemoryCategory;

    fn make_entry(id: &str, content: &str) -> MemoryEntry {
        MemoryEntry {
            id: id.into(),
            key: id.into(),
            content: content.into(),
            category: MemoryCategory::Core,
            timestamp: String::new(),
            session_id: None,
            score: Some(0.9),
        }
    }

    #[test]
    fn test_hot_cache_miss_then_hit() {
        let mut cache = HotCache::new();

        let embedding = vec![1.0f32, 0.0, 0.0, 0.0];
        assert!(cache.lookup(&embedding).is_none());

        let results = vec![make_entry("1", "test fact")];
        cache.insert(embedding.clone(), results.clone());

        // Exact same embedding -> hit
        let hit = cache.lookup(&embedding);
        assert!(hit.is_some());
        assert_eq!(hit.unwrap().len(), 1);
    }

    #[test]
    fn test_hot_cache_similar_query_hit() {
        let mut cache = HotCache::new();

        let emb1 = vec![1.0f32, 0.0, 0.0, 0.0];
        let results = vec![make_entry("1", "test fact")];
        cache.insert(emb1, results);

        // Very similar embedding (cosine > 0.85) -> hit
        let emb2 = vec![0.99f32, 0.1, 0.0, 0.0];
        assert!(cache.lookup(&emb2).is_some());
    }

    #[test]
    fn test_hot_cache_dissimilar_query_miss() {
        let mut cache = HotCache::new();

        let emb1 = vec![1.0f32, 0.0, 0.0, 0.0];
        let results = vec![make_entry("1", "test fact")];
        cache.insert(emb1, results);

        // Orthogonal embedding (cosine = 0) -> miss
        let emb2 = vec![0.0f32, 1.0, 0.0, 0.0];
        assert!(cache.lookup(&emb2).is_none());
    }

    #[test]
    fn test_hot_cache_eviction() {
        let mut cache = HotCache::new();

        // Fill cache beyond max
        for i in 0..35 {
            let emb = vec![i as f32, 0.0, 0.0, 0.0];
            cache.insert(emb, vec![make_entry(&i.to_string(), "fact")]);
        }

        assert_eq!(cache.len(), 30);
    }

    #[test]
    fn test_hot_cache_clear() {
        let mut cache = HotCache::new();
        cache.insert(vec![1.0f32], vec![make_entry("1", "fact")]);
        assert_eq!(cache.len(), 1);

        cache.clear();
        assert!(cache.is_empty());
    }
}
