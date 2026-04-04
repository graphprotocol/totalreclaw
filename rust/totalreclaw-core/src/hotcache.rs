//! Generic in-memory hot cache for semantic query dedup.
//!
//! Caches recent query results keyed by embedding similarity. When a new query
//! arrives whose embedding has cosine similarity >= threshold to a cached query,
//! the cached result is returned instead of performing a remote lookup.
//!
//! This is a pure-computation module (no I/O, no WASM bindings). Disk
//! persistence, if needed, stays in the consuming crate.
//!
//! Defaults match the TypeScript plugin: 30 entries, cosine >= 0.85.

use crate::reranker;

/// Default maximum number of cached entries.
const DEFAULT_MAX_ENTRIES: usize = 30;

/// Default cosine similarity threshold for a cache hit.
const DEFAULT_SIMILARITY_THRESHOLD: f64 = 0.85;

/// Generic in-memory hot cache for semantic query dedup.
///
/// `T` is the cached result type (e.g. `Vec<MemoryEntry>` in the memory crate,
/// a JSON string in WASM callers, etc.). It must be `Clone` so that cache hits
/// can return owned copies without consuming the cache entry.
pub struct HotCache<T: Clone> {
    entries: Vec<(Vec<f32>, T)>, // (query_embedding, cached_result)
    max_entries: usize,
    similarity_threshold: f64,
}

impl<T: Clone> HotCache<T> {
    /// Create a hot cache with default settings (30 entries, cosine >= 0.85).
    pub fn new() -> Self {
        Self {
            entries: Vec::with_capacity(DEFAULT_MAX_ENTRIES),
            max_entries: DEFAULT_MAX_ENTRIES,
            similarity_threshold: DEFAULT_SIMILARITY_THRESHOLD,
        }
    }

    /// Create a hot cache with custom capacity and similarity threshold.
    pub fn with_config(max_entries: usize, similarity_threshold: f64) -> Self {
        Self {
            entries: Vec::with_capacity(max_entries),
            max_entries,
            similarity_threshold,
        }
    }

    /// Look up a cached result by embedding similarity.
    ///
    /// Returns a reference to the cached result if any cached query embedding
    /// has cosine similarity >= threshold to `query_embedding`.
    pub fn lookup(&self, query_embedding: &[f32]) -> Option<&T> {
        for (cached_embedding, result) in &self.entries {
            let similarity = reranker::cosine_similarity_f32(query_embedding, cached_embedding);
            if similarity >= self.similarity_threshold {
                return Some(result);
            }
        }
        None
    }

    /// Insert a query result into the cache.
    ///
    /// Evicts the oldest entry (FIFO) if the cache is full.
    pub fn insert(&mut self, query_embedding: Vec<f32>, result: T) {
        if self.entries.len() >= self.max_entries {
            self.entries.remove(0); // FIFO eviction
        }
        self.entries.push((query_embedding, result));
    }

    /// Clear all cached entries (e.g. after a store operation invalidates results).
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

impl<T: Clone> Default for HotCache<T> {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_miss_then_hit() {
        let mut cache: HotCache<Vec<String>> = HotCache::new();

        let embedding = vec![1.0f32, 0.0, 0.0, 0.0];
        assert!(cache.lookup(&embedding).is_none());

        let results = vec!["test fact".to_string()];
        cache.insert(embedding.clone(), results.clone());

        // Exact same embedding -> hit
        let hit = cache.lookup(&embedding);
        assert!(hit.is_some());
        assert_eq!(hit.unwrap().len(), 1);
        assert_eq!(hit.unwrap()[0], "test fact");
    }

    #[test]
    fn test_similar_query_hit() {
        let mut cache: HotCache<String> = HotCache::new();

        let emb1 = vec![1.0f32, 0.0, 0.0, 0.0];
        cache.insert(emb1, "cached result".to_string());

        // Very similar embedding (cosine > 0.85) -> hit
        let emb2 = vec![0.99f32, 0.1, 0.0, 0.0];
        assert!(cache.lookup(&emb2).is_some());
        assert_eq!(cache.lookup(&emb2).unwrap(), "cached result");
    }

    #[test]
    fn test_dissimilar_query_miss() {
        let mut cache: HotCache<String> = HotCache::new();

        let emb1 = vec![1.0f32, 0.0, 0.0, 0.0];
        cache.insert(emb1, "cached result".to_string());

        // Orthogonal embedding (cosine = 0) -> miss
        let emb2 = vec![0.0f32, 1.0, 0.0, 0.0];
        assert!(cache.lookup(&emb2).is_none());
    }

    #[test]
    fn test_eviction() {
        let mut cache: HotCache<i32> = HotCache::new();

        // Fill cache beyond max (30)
        for i in 0..35 {
            let emb = vec![i as f32, 0.0, 0.0, 0.0];
            cache.insert(emb, i);
        }

        assert_eq!(cache.len(), DEFAULT_MAX_ENTRIES);
    }

    #[test]
    fn test_clear() {
        let mut cache: HotCache<i32> = HotCache::new();
        cache.insert(vec![1.0f32], 42);
        assert_eq!(cache.len(), 1);

        cache.clear();
        assert!(cache.is_empty());
    }

    #[test]
    fn test_custom_config() {
        let mut cache: HotCache<i32> = HotCache::with_config(5, 0.95);

        for i in 0..10 {
            cache.insert(vec![i as f32, 0.0], i);
        }
        assert_eq!(cache.len(), 5);
    }

    #[test]
    fn test_custom_threshold() {
        // High threshold -> similar queries miss
        let mut cache: HotCache<String> = HotCache::with_config(30, 0.999);

        let emb1 = vec![1.0f32, 0.0, 0.0, 0.0];
        cache.insert(emb1, "result".to_string());

        // This would match at 0.85 but not at 0.999
        let emb2 = vec![0.99f32, 0.1, 0.0, 0.0];
        assert!(cache.lookup(&emb2).is_none());
    }

    #[test]
    fn test_constants() {
        assert_eq!(DEFAULT_MAX_ENTRIES, 30);
        assert!((DEFAULT_SIMILARITY_THRESHOLD - 0.85).abs() < 1e-10);
    }
}
