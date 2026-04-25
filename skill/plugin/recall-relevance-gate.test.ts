/**
 * Regression test for issue #116 — recall miss for single-token / short
 * query "favorite color" against a stored fact like
 * "User's favorite color is cobalt blue".
 *
 * Root cause: the recall tool's relevance gate filtered results based on
 * cosine similarity alone (`maxCosine < 0.15` → suppress all). Short
 * queries embedded by the local Harrier-OSS-270m model produce low
 * cosine similarity against longer fact embeddings even when every query
 * token literally appears in the candidate text — an extreme false-negative
 * with no semantic correlate. Hermes (Python client) has NO cosine gate
 * and recalled the same fact for the same Smart Account in rc.18 QA, which
 * pinned the bug to the OpenClaw plugin's gate logic.
 *
 * Fix: `passesRelevanceGate` accepts results when EITHER cosine clears the
 * threshold OR every meaningful query token (post stop-word removal) appears
 * as a stem-prefix substring in the top reranked result's text.
 *
 * This file documents both the failure mode (would-have-failed assertions
 * against the baseline cosine-only gate) and the fix's expected behaviour.
 *
 * Run with: npx tsx recall-relevance-gate.test.ts
 */

import {
  passesRelevanceGate,
  type RerankerResult,
} from './reranker.js';

let passed = 0;
let failed = 0;
let testNum = 0;

function assert(condition: boolean, message: string): void {
  testNum++;
  if (condition) {
    passed++;
    console.log(`ok ${testNum} - ${message}`);
  } else {
    failed++;
    console.log(`not ok ${testNum} - ${message}`);
  }
}

/**
 * Build a synthetic reranked result. Cosine similarity is set explicitly to
 * mimic the Harrier-OSS-270m output for short queries (typically below the
 * 0.15 default threshold even when topical match is unambiguous).
 */
function makeResult(text: string, cosine: number, id = 't1'): RerankerResult {
  return {
    id,
    text,
    rrfScore: 0.5,
    cosineSimilarity: cosine,
  };
}

const COSINE_THRESHOLD = 0.15;

// ---------------------------------------------------------------------------
// Baseline failure mode (issue #116)
// ---------------------------------------------------------------------------

console.log('# Baseline cosine-only gate (the bug)');

{
  // Mimic the rc.18 finding directly: short query "favorite color" against
  // a stored fact, with cosine 0.10 (below 0.15 threshold).
  const reranked = [
    makeResult("User's favorite color is cobalt blue", 0.10),
  ];
  // The pre-fix gate code would return false here (and the recall tool
  // would say "No relevant memories found for this query."). Document
  // that regression directly:
  const bareCosineGate = (
    Math.max(...reranked.map((r) => r.cosineSimilarity ?? 0)) >= COSINE_THRESHOLD
  );
  assert(
    bareCosineGate === false,
    'baseline cosine-only gate suppresses "favorite color" against the matching fact (the rc.18 bug)',
  );
}

// ---------------------------------------------------------------------------
// Lexical-override path (the fix)
// ---------------------------------------------------------------------------

console.log('# Fix — lexical override accepts on-target short queries');

{
  // Issue #116 reproduction.
  const reranked = [
    makeResult("User's favorite color is cobalt blue", 0.10),
  ];
  const ok = passesRelevanceGate('favorite color', reranked, COSINE_THRESHOLD);
  assert(ok === true, 'short query "favorite color" passes via lexical override (issue #116 regression)');
}

{
  // Single-token query: "color" alone, with the cosine still under the gate.
  const reranked = [
    makeResult("User's favorite color is cobalt blue", 0.08),
  ];
  const ok = passesRelevanceGate('color', reranked, COSINE_THRESHOLD);
  assert(ok === true, 'single-token query "color" passes via lexical override');
}

{
  // Longer natural-language query — same fact, but cosine higher because
  // the query has more semantic content. Should clear the cosine path.
  const reranked = [
    makeResult("User's favorite color is cobalt blue", 0.42),
  ];
  const ok = passesRelevanceGate("what's my favorite color?", reranked, COSINE_THRESHOLD);
  assert(ok === true, 'natural-language query passes via cosine path (no regression on the long-query case)');
}

{
  // Stem-tolerance: query token "favorite" appears as "favorites" in fact text.
  const reranked = [
    makeResult("User has several favorites; the top one is mountains.", 0.05),
  ];
  const ok = passesRelevanceGate('favorite', reranked, COSINE_THRESHOLD);
  assert(ok === true, '4-char-prefix substring match tolerates light morphology (favorite ↔ favorites)');
}

// ---------------------------------------------------------------------------
// Precision — gate must still reject genuinely-irrelevant queries
// ---------------------------------------------------------------------------

console.log('# Fix — gate still rejects irrelevant queries');

{
  // Query terms have NO overlap with the top result text. Low cosine + no
  // lexical match → the gate must suppress. This is the precision case the
  // gate was originally introduced for; the fix must not break it.
  const reranked = [
    makeResult("User's favorite color is cobalt blue", 0.05),
  ];
  const ok = passesRelevanceGate('chess strategies', reranked, COSINE_THRESHOLD);
  assert(ok === false, 'irrelevant query is still suppressed (precision preserved)');
}

{
  // Partial-token match must NOT pass — the override requires ALL query
  // tokens to appear, not just one. "favorite cars" against a "favorite
  // color" fact: "favorite" matches but "cars" doesn't — should suppress.
  const reranked = [
    makeResult("User's favorite color is cobalt blue", 0.05),
  ];
  const ok = passesRelevanceGate('favorite cars', reranked, COSINE_THRESHOLD);
  assert(ok === false, 'partial token overlap does NOT trigger the lexical override');
}

{
  // Empty / all-stop-words query: no meaningful tokens to match. Falls back
  // to cosine path — must suppress when cosine is also below threshold.
  const reranked = [
    makeResult("User's favorite color is cobalt blue", 0.05),
  ];
  const ok = passesRelevanceGate('what is the', reranked, COSINE_THRESHOLD);
  assert(ok === false, 'all-stop-word query falls back to cosine path (suppress when low)');
}

{
  // Empty reranked list: must reject regardless of query.
  const ok = passesRelevanceGate('favorite color', [], COSINE_THRESHOLD);
  assert(ok === false, 'empty reranked list always returns false');
}

{
  // Cosine path still works in isolation — if the top result has high
  // cosine, lexical match is unnecessary.
  const reranked = [
    makeResult("Some unrelated text about chess.", 0.42),
  ];
  const ok = passesRelevanceGate('favorite color', reranked, COSINE_THRESHOLD);
  assert(ok === true, 'high cosine alone is sufficient (cosine path)');
}

// ---------------------------------------------------------------------------
// Edge: top result without text (defensive)
// ---------------------------------------------------------------------------

console.log('# Edge cases');

{
  const reranked: RerankerResult[] = [
    { id: 'x', text: '', rrfScore: 0.5, cosineSimilarity: 0.1 },
  ];
  const ok = passesRelevanceGate('favorite color', reranked, COSINE_THRESHOLD);
  assert(ok === false, 'empty top-result text cannot satisfy lexical override');
}

{
  // cosineSimilarity undefined (e.g. candidate had no embedding) — treated as
  // 0 by the gate. Lexical override should still rescue when applicable.
  const reranked: RerankerResult[] = [
    { id: 'x', text: "User's favorite color is cobalt blue", rrfScore: 0.5 },
  ];
  const ok = passesRelevanceGate('favorite color', reranked, COSINE_THRESHOLD);
  assert(ok === true, 'missing cosineSimilarity does not block lexical override');
}

console.log(`\n1..${testNum}`);
console.log(`# pass: ${passed}`);
console.log(`# fail: ${failed}`);

if (failed === 0) {
  console.log('\nALL TESTS PASSED');
  process.exit(0);
} else {
  console.log('\nFAILURES');
  process.exit(1);
}
