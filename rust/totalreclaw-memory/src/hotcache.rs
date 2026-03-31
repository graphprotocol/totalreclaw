//! In-memory hot cache for recently recalled facts.
//!
//! Matches the TypeScript plugin's hot cache behavior:
//! - Caches up to 30 recent query results in memory
//! - Skips remote subgraph query if a semantically similar query was recently answered
//! - Similarity threshold: cosine >= 0.85 between query embeddings
//! - Cache is per-session (lives on the TotalReclawMemory struct)

use crate::backend::MemoryEntry;
use crate::reranker;

/// Maximum number of cached queries.
const HOT_CACHE_MAX_ENTRIES: usize = 30;

/// Cosine similarity threshold for cache hit (skip remote query).
const HOT_CACHE_SIMILARITY_THRESHOLD: f64 = 0.85;

/// A cached query result.
#[derive(Clone)]
struct CacheEntry {
    /// The query embedding.
    query_embedding: Vec<f32>,
    /// The cached results.
    results: Vec<MemoryEntry>,
}

/// In-memory hot cache for semantic query dedup.
pub struct HotCache {
    entries: Vec<CacheEntry>,
}

impl HotCache {
    /// Create an empty hot cache.
    pub fn new() -> Self {
        Self {
            entries: Vec::with_capacity(HOT_CACHE_MAX_ENTRIES),
        }
    }

    /// Check if a semantically similar query has already been answered.
    ///
    /// Returns cached results if a query with cosine >= 0.85 exists.
    pub fn lookup(&self, query_embedding: &[f32]) -> Option<Vec<MemoryEntry>> {
        for entry in &self.entries {
            let similarity =
                reranker::cosine_similarity_f32(query_embedding, &entry.query_embedding);
            if similarity >= HOT_CACHE_SIMILARITY_THRESHOLD {
                return Some(entry.results.clone());
            }
        }
        None
    }

    /// Insert a query result into the cache.
    ///
    /// Evicts the oldest entry if the cache is full.
    pub fn insert(&mut self, query_embedding: Vec<f32>, results: Vec<MemoryEntry>) {
        if self.entries.len() >= HOT_CACHE_MAX_ENTRIES {
            self.entries.remove(0); // FIFO eviction
        }
        self.entries.push(CacheEntry {
            query_embedding,
            results,
        });
    }

    /// Clear the cache (e.g., after a store operation).
    pub fn clear(&mut self) {
        self.entries.clear();
    }

    /// Number of cached entries.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Whether the cache is empty.
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
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
    use crate::backend::{MemoryCategory, MemoryEntry};

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

        assert_eq!(cache.len(), HOT_CACHE_MAX_ENTRIES);
    }

    #[test]
    fn test_hot_cache_clear() {
        let mut cache = HotCache::new();
        cache.insert(vec![1.0f32], vec![make_entry("1", "fact")]);
        assert_eq!(cache.len(), 1);

        cache.clear();
        assert!(cache.is_empty());
    }

    #[test]
    fn test_hot_cache_constants() {
        assert_eq!(HOT_CACHE_MAX_ENTRIES, 30);
        assert!((HOT_CACHE_SIMILARITY_THRESHOLD - 0.85).abs() < 1e-10);
    }
}
