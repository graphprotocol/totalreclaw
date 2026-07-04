//! Centroid-walk session segmentation for conversation imports.
//!
//! Pure computation — no I/O, no LLM calls, no network. Hoisted from the
//! Python `session_segmentation.py` (totalreclaw#368) so every client shares
//! byte-identical segmentation behaviour. Embedding stays client-side; only
//! the segmentation math lives here.
//!
//! # Algorithm (must match `session_segmentation.py` exactly)
//!
//! - Walk turns in chronological order.
//! - Maintain a running centroid = unnormalised sum of member embeddings.
//! - For each new turn `i` (`i >= 1`), compare its embedding to the
//!   *normalised* running centroid of the current session.
//! - Start a new session if EITHER:
//!     1. time gap `> gap_seconds` (hard boundary), OR
//!     2. `cosine(turn_emb, centroid_normalised) < sim_threshold` (semantic shift).
//! - When a turn joins the current session, accumulate `centroid += turn_emb`
//!   and re-normalise the centroid before the next comparison.
//!
//! Embeddings are assumed L2-normalised on input (Harrier 640d output already
//! is), so the dot product between an embedding and the normalised centroid
//! equals the cosine similarity.
//!
//! `None`/`null` timestamps are treated as a 0-gap to the previous turn (no
//! time split triggered); a `None` first entry is likewise treated as 0.
//!
//! Both thresholds are *strict*: gap uses `>` (not `>=`), similarity uses `<`
//! (a turn with `sim == sim_threshold` stays in the session). Default gap is
//! 1800 s (30 minutes); default similarity threshold is 0.55 (validated on
//! real Gemini Takeout data — 3942 turns).

/// Centroid-walk segmentation over time-ordered turns.
///
/// # Parameters
/// - `timestamps`: Unix seconds (`Some(f64)`) per turn, in chronological order.
///   `None` entries are treated as a 0-gap to the previous turn.
/// - `embeddings`: L2-normalised embedding vectors, same length as
///   `timestamps`. Each inner slice is one turn's vector.
/// - `gap_seconds`: minimum time gap (strict `>`) that forces a new session.
/// - `sim_threshold`: cosine-similarity threshold (strict `<` triggers a
///   split). Turns with `sim >= sim_threshold` join the current session.
///
/// # Returns
/// Ordered list of sessions; each session is a `Vec<usize>` of turn indices
/// (into the input), contiguous and ascending. Empty input returns `[]`.
///
/// # Note on length mismatch
/// If `embeddings` is shorter than `timestamps`, the algorithm walks only up
/// to `timestamps.len()` and indexes `embeddings` positionally; callers are
/// expected to pass equal-length inputs (the Python caller always does).
pub fn segment_sessions(
    timestamps: &[Option<f64>],
    embeddings: &[Vec<f64>],
    gap_seconds: f64,
    sim_threshold: f64,
) -> Vec<Vec<usize>> {
    let n = timestamps.len();
    if n == 0 {
        return Vec::new();
    }

    // Initialise first session with turn 0.
    let mut sessions: Vec<Vec<usize>> = vec![vec![0]];

    // Running centroid: unnormalised sum of member embeddings (copy so we
    // don't alias the input vector).
    let mut cen: Vec<f64> = embeddings[0].clone();
    // Precompute the normalised centroid (used for dot-product comparison).
    let mut cen_norm: Vec<f64> = normalise(&cen);

    for i in 1..n {
        // ── Time-gap check ──────────────────────────────────────────────
        let gap = match (timestamps[i - 1], timestamps[i]) {
            (Some(prev), Some(curr)) => curr - prev,
            _ => 0.0,
        };

        // ── Semantic similarity check ───────────────────────────────────
        let emb_i = &embeddings[i];
        let sim = dot(emb_i, &cen_norm);

        // ── Decision ────────────────────────────────────────────────────
        if gap > gap_seconds || sim < sim_threshold {
            // Start a new session; reset centroid.
            sessions.push(vec![i]);
            cen = emb_i.clone();
        } else {
            // Extend the current session; accumulate centroid.
            sessions.last_mut().unwrap().push(i);
            for (c, e) in cen.iter_mut().zip(emb_i.iter()) {
                *c += *e;
            }
        }

        // Always renormalise after each step (reset or accumulate).
        cen_norm = normalise(&cen);
    }

    sessions
}

/// Return an L2-normalised copy of `v`. Returns a copy unchanged if the norm
/// is below `1e-9` (matches the Python `_normalise` epsilon behaviour).
fn normalise(v: &[f64]) -> Vec<f64> {
    let norm = v.iter().map(|x| x * x).sum::<f64>().sqrt();
    if norm < 1e-9 {
        return v.to_vec();
    }
    let inv = 1.0 / norm;
    v.iter().map(|x| x * inv).collect()
}

/// Dot product of two vectors, truncated to the shorter length (matches
/// Python `zip` semantics).
fn dot(a: &[f64], b: &[f64]) -> f64 {
    a.iter().zip(b.iter()).map(|(x, y)| x * y).sum()
}

// ---------------------------------------------------------------------------
// Tests — mirror python/tests/test_session_segmentation.py case-for-case.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn unit(v: &[f64]) -> Vec<f64> {
        let norm = v.iter().map(|x| x * x).sum::<f64>().sqrt();
        if norm < 1e-9 {
            return v.to_vec();
        }
        v.iter().map(|x| x / norm).collect()
    }

    fn topic_a() -> Vec<f64> {
        unit(&[1.0, 0.0, 0.0, 0.0])
    }
    fn topic_b() -> Vec<f64> {
        unit(&[0.0, 1.0, 0.0, 0.0])
    }
    fn topic_c() -> Vec<f64> {
        unit(&[0.0, 0.0, 1.0, 0.0])
    }

    // ── basic API ──────────────────────────────────────────────────────

    #[test]
    fn empty_input() {
        let sessions = segment_sessions(&[], &[], 1800.0, 0.55);
        assert_eq!(sessions, Vec::<Vec<usize>>::new());
    }

    #[test]
    fn single_turn() {
        let sessions = segment_sessions(&[Some(0.0)], &[topic_a()], 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0]]);
    }

    #[test]
    fn two_turns_same_topic_no_gap() {
        let ts = [Some(0.0), Some(100.0)];
        let embs = [topic_a(), unit(&[0.9, 0.1, 0.0, 0.0])];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0, 1]]);
    }

    // ── time-gap split ─────────────────────────────────────────────────

    #[test]
    fn time_gap_splits_session() {
        let ts = [Some(0.0), Some(2000.0)];
        let embs = [topic_a(), topic_a()];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0], vec![1]]);
    }

    #[test]
    fn time_gap_exactly_at_boundary_stays() {
        let ts = [Some(0.0), Some(1800.0)];
        let embs = [topic_a(), topic_a()];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0, 1]]);
    }

    #[test]
    fn time_gap_just_above_boundary_splits() {
        let ts = [Some(0.0), Some(1801.0)];
        let embs = [topic_a(), topic_a()];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions.len(), 2);
    }

    #[test]
    fn multiple_time_gaps() {
        let ts = [Some(0.0), Some(2000.0), Some(4000.0)];
        let embs = [topic_a(), topic_a(), topic_a()];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0], vec![1], vec![2]]);
    }

    // ── semantic split ─────────────────────────────────────────────────

    #[test]
    fn semantic_split_perpendicular_topics() {
        let ts = [Some(0.0), Some(100.0)];
        let embs = [topic_a(), topic_b()];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0], vec![1]]);
    }

    #[test]
    fn semantic_threshold_stays_in_session() {
        let x = 0.55_f64;
        let y = (1.0 - x * x).sqrt();
        let ts = [Some(0.0), Some(100.0)];
        let embs = [unit(&[1.0, 0.0, 0.0, 0.0]), unit(&[x, y, 0.0, 0.0])];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0, 1]]);
    }

    #[test]
    fn semantic_just_below_threshold_splits() {
        let x = 0.549_f64;
        let y = (1.0_f64 - x * x).max(0.0).sqrt();
        let ts = [Some(0.0), Some(100.0)];
        let embs = [unit(&[1.0, 0.0, 0.0, 0.0]), unit(&[x, y, 0.0, 0.0])];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions.len(), 2);
    }

    // ── centroid accumulation ──────────────────────────────────────────

    #[test]
    fn referential_followup_stays_via_centroid() {
        let e0 = unit(&[1.0, 0.0, 0.0, 0.0]);
        let e1 = unit(&[0.9, 0.1, 0.0, 0.0]);
        let e2 = unit(&[0.6, 0.4, 0.0, 0.0]); // cos vs e0 = 0.6 > 0.55
        let ts = [Some(0.0), Some(100.0), Some(200.0)];
        let embs = [e0, e1, e2];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0, 1, 2]]);
    }

    #[test]
    fn centroid_based_not_prev_turn() {
        let e_a = unit(&[1.0, 0.0, 0.0, 0.0]);
        let e_drift = unit(&[0.99, 0.14, 0.0, 0.0]);
        let ts = [Some(0.0), Some(10.0), Some(20.0), Some(30.0)];
        let embs = [e_a.clone(), e_a.clone(), e_drift, e_a];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0, 1, 2, 3]]);
    }

    #[test]
    fn multi_topic_window_splits() {
        let ts = [Some(0.0), Some(60.0), Some(120.0), Some(600.0), Some(660.0)];
        let embs = [topic_a(), topic_a(), topic_b(), topic_c(), topic_c()];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0, 1], vec![2], vec![3, 4]]);
    }

    // ── None timestamps ────────────────────────────────────────────────

    #[test]
    fn none_timestamps_treated_as_no_gap() {
        let ts = [None, None, None];
        let embs = [topic_a(), topic_a(), topic_a()];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions, vec![vec![0, 1, 2]]);
    }

    #[test]
    fn mixed_none_and_real_timestamps() {
        // ts[1]=None -> gap(0->1)=0; ts[2]=5000 but ts[1]=None -> gap(1->2)=0.
        // All same topic, so all stay in one session.
        let ts = [Some(0.0), None, Some(5000.0)];
        let embs = [topic_a(), topic_a(), topic_a()];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        let total: usize = sessions.iter().map(|s| s.len()).sum();
        assert_eq!(total, 3);
        assert_eq!(sessions, vec![vec![0, 1, 2]]);
    }

    // ── return-type invariants ─────────────────────────────────────────

    #[test]
    fn all_turns_covered() {
        let ts = [
            Some(0.0),
            Some(100.0),
            Some(2000.0),
            Some(2100.0),
            Some(5000.0),
        ];
        let embs = [topic_a(), topic_b(), topic_a(), topic_c(), topic_a()];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        let mut all: Vec<usize> = sessions.iter().flatten().copied().collect();
        all.sort_unstable();
        assert_eq!(all, vec![0, 1, 2, 3, 4]);
    }

    #[test]
    fn sessions_are_contiguous_ordered() {
        // Deterministic perpendicular-ish vectors — every session contiguous.
        let ts: Vec<Option<f64>> = (0..10).map(|i| Some(i as f64 * 30.0)).collect();
        let embs: Vec<Vec<f64>> = (0..10)
            .map(|i| {
                let mut v = vec![0.0; 4];
                v[i % 4] = 1.0;
                unit(&v)
            })
            .collect();
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        let mut prev_last: i64 = -1;
        for s in &sessions {
            let expected: Vec<usize> = (s[0]..=s[s.len() - 1]).collect();
            assert_eq!(*s, expected, "session indices must be contiguous");
            assert!(s[0] as i64 > prev_last, "sessions must not overlap");
            prev_last = s[s.len() - 1] as i64;
        }
    }

    // ── singletons ─────────────────────────────────────────────────────

    #[test]
    fn all_perpendicular_embeddings_all_singletons() {
        let ts = [Some(0.0), Some(100.0), Some(200.0), Some(300.0)];
        let embs = [
            vec![1.0, 0.0, 0.0, 0.0],
            vec![0.0, 1.0, 0.0, 0.0],
            vec![0.0, 0.0, 1.0, 0.0],
            vec![0.0, 0.0, 0.0, 1.0],
        ];
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions.len(), 4);
        assert!(sessions.iter().all(|s| s.len() == 1));
    }

    // ── long intact conversation (matches Python test_long_conversation) ─

    #[test]
    fn long_conversation_stays_in_one_session() {
        // Deterministic tight cluster around [0.8, 0, 0, 0]; all turns should
        // stay in a single session. (Rust can't reproduce Python's random
        // module exactly, so we use a fixed deterministic set close to the
        // Python fixture's characteristics.)
        let n = 56;
        let ts: Vec<Option<f64>> = (0..n).map(|i| Some(i as f64 * 30.0)).collect();
        let embs: Vec<Vec<f64>> = (0..n)
            .map(|i| {
                // Tiny deterministic wobble, dominant first axis.
                let wobble = ((i as f64) * 0.017).sin() * 0.05;
                unit(&[0.82, wobble, wobble * 0.5, -wobble * 0.5])
            })
            .collect();
        let sessions = segment_sessions(&ts, &embs, 1800.0, 0.55);
        assert_eq!(sessions.len(), 1);
        assert_eq!(sessions[0], (0..n).collect::<Vec<usize>>());
    }
}
