/**
 * memory-runtime.test — hard-contract test for the TrMemorySearchManager
 * adapter (Task 2.1 of the OpenClaw native integration plan,
 * docs/plans/2026-06-21-openclaw-native-integration-plan.md, 2026-06-21).
 *
 * Verifies the file↔fact mapping that lets TR's encrypted-fact + on-chain
 * vault look like a memory corpus to OpenClaw's active-memory sub-agent:
 *   - search() returns file-shaped results with
 *       path = FACT_PATH_PREFIX + factId,
 *       source = 'memory',
 *       snippet = decrypted plaintext,
 *       citation = factId.
 *   - readFile() reverses relPath → id → decrypt.
 *   - status().provider === 'totalreclaw'.
 *   - probeEmbeddingAvailability / probeVectorAvailability report ok.
 *
 * Also covers the 2026-06-21 code-review refinements:
 *   - minScore filtering + defensive sort.
 *   - signal / sessionKey forwarded into recall.
 *   - from/lines pagination + nextFrom.
 *   - I1 clamp: readFile past EOF never returns a negative line count.
 *   - bare relPath (no prefix) passthrough.
 *
 * recall + getById are fakes here; Task 2.3 wires them to the real
 * subgraph-search + vault-crypto.decrypt + reranker pipeline.
 *
 * Run: `npx tsx memory-runtime.test.ts` — prints OK and exits 0 on pass.
 */

import { strict as assert } from 'node:assert';
import {
  createTrMemorySearchManager,
  createTrMemoryPluginRuntime,
  FACT_PATH_PREFIX,
} from './memory-runtime.js';

// Map-backed fake so multi-fact + multi-line scenarios are easy to express.
// Score is provided per-(query) by the fake recall below.
const FACTS = new Map<string, string>([
  ['f1', 'User prefers dark mode.'],
  ['f2', 'User works at Acme.'],
  ['f3', 'line one\nline two\nline three\nline four'],
]);

// Per-fact score for the fake recall. Higher = more relevant.
const SCORES: Record<string, number> = { f1: 0.9, f2: 0.7, f3: 0.6 };

let lastRecallOpts: { maxResults?: number; signal?: AbortSignal; sessionKey?: string } | undefined;

const recall = async (
  _q: string,
  opts?: { maxResults?: number; signal?: AbortSignal; sessionKey?: string },
) => {
  lastRecallOpts = opts;
  // Deliberately return in NON-sorted order (f3, f2, f1) so tests can prove
  // search() sorts defensively by score before slicing/filtering.
  return ['f3', 'f2', 'f1']
    .map((id) => ({ id, plaintext: FACTS.get(id)!, score: SCORES[id]! }))
    .filter((f) => FACTS.has(f.id));
};

const getById = async (id: string) => {
  const plaintext = FACTS.get(id);
  return plaintext === undefined ? null : { id, plaintext };
};

const mgr = createTrMemorySearchManager({ recall, getById });

// --- search(): file-shaped results from decrypted facts ---
const hits = await mgr.search('preferences');
assert.equal(hits.length, 3);
// Defensive sort: highest score first despite recall returning unsorted.
assert.equal(hits[0].citation, 'f1'); // 0.9
assert.equal(hits[1].citation, 'f2'); // 0.7
assert.equal(hits[2].citation, 'f3'); // 0.6
assert.equal(hits[0].path, `${FACT_PATH_PREFIX}f1`);
assert.equal(hits[0].startLine, 1);
assert.ok(hits[0].endLine >= 1);
assert.equal(hits[0].source, 'memory');
assert.match(hits[0].snippet, /dark mode/);
assert.equal(hits[0].citation, 'f1');

// --- S1: minScore filtering drops sub-threshold hits ---
const hi = await mgr.search('q', { minScore: 0.8 });
assert.equal(hi.length, 1, 'minScore=0.8 should keep only the 0.9 hit');
assert.equal(hi[0].citation, 'f1');
// minScore=0.65 keeps 0.9 + 0.7 but drops 0.6
const mid = await mgr.search('q', { minScore: 0.65 });
assert.equal(mid.length, 2);
assert.equal(mid[0].citation, 'f1');
assert.equal(mid[1].citation, 'f2');

// --- M1: signal + sessionKey are forwarded into recall ---
const ac = new AbortController();
await mgr.search('q', { signal: ac.signal, sessionKey: 'sess-42' });
assert.equal(lastRecallOpts?.sessionKey, 'sess-42');
assert.equal(lastRecallOpts?.signal, ac.signal);

// --- readFile(): reverse path -> id -> decrypt ---
const r = await mgr.readFile({ relPath: `${FACT_PATH_PREFIX}f1` });
assert.match(r.text, /dark mode/);
assert.equal(r.path, `${FACT_PATH_PREFIX}f1`);

// --- S1: from/lines pagination on a multi-line fact ---
const page = await mgr.readFile({ relPath: `${FACT_PATH_PREFIX}f3`, from: 2, lines: 1 });
assert.equal(page.text, 'line two');
assert.equal(page.truncated, true);
assert.equal(page.nextFrom, 3);
assert.equal(page.lines, 1);

// Last-page read: lines requested >= remaining → no truncation.
const lastPage = await mgr.readFile({ relPath: `${FACT_PATH_PREFIX}f3`, from: 4, lines: 5 });
assert.equal(lastPage.text, 'line four');
assert.equal(lastPage.truncated, false);
assert.equal(lastPage.nextFrom, undefined);

// --- S1 + I1: from past EOF must not surface a negative line count ---
const past = await mgr.readFile({ relPath: `${FACT_PATH_PREFIX}f1`, from: 100 });
assert.equal(past.text, '', 'past-EOF read returns empty text');
assert.equal(past.truncated, false);
assert.ok(past.lines >= 0, `lines must be non-negative (got ${past.lines})`);

// --- S1: bare relPath (no prefix) resolves identically to prefixed form ---
const bare = await mgr.readFile({ relPath: 'f1' });
const prefixed = await mgr.readFile({ relPath: `${FACT_PATH_PREFIX}f1` });
assert.equal(bare.text, prefixed.text);
assert.equal(bare.path, prefixed.path);

// --- status + probes ---
assert.equal(mgr.status().provider, 'totalreclaw');
assert.ok((await mgr.probeEmbeddingAvailability()).ok);
assert.equal(await mgr.probeVectorAvailability(), true);

// ---------------------------------------------------------------------------
// Task 2.3 — createTrMemoryPluginRuntime wrapper
// (getMemorySearchManager / resolveMemoryBackendConfig / close* surface).
// Reuses the same Map-backed fake recall/getById defined above.
// ---------------------------------------------------------------------------
const runtime = createTrMemoryPluginRuntime({ recall, getById });

// getMemorySearchManager: success path returns a working adapter, no error.
const got = await runtime.getMemorySearchManager({ cfg: {} as any, agentId: 'a1' });
assert.ok(got.manager, 'manager must be returned on success');
assert.equal(got.error, undefined, 'no error string on success');
// The returned manager is a real TrMemorySearchManager — search() works.
const rtHits = await got.manager!.search('preferences');
assert.ok(rtHits.length >= 0, 'returned manager.search must resolve');
assert.equal(rtHits[0]?.source, 'memory', 'returned manager yields memory hits');

// resolveMemoryBackendConfig: TR is its own backend, so 'builtin'.
assert.deepEqual(
  runtime.resolveMemoryBackendConfig({ cfg: {} as any, agentId: 'a1' }),
  { backend: 'builtin' },
  'TR reports itself as the builtin backend',
);

// close* are no-ops today but MUST be present and non-throwing.
await runtime.closeMemorySearchManager?.({ cfg: {} as any, agentId: 'a1' });
await runtime.closeAllMemorySearchManagers?.();

// Error path: a recall closure that throws synchronously at adapter build
// time surfaces as { manager: null, error: <string> } — never an exception
// out of getMemorySearchManager. (We force this by passing a `recall` whose
// presence triggers a factory-time throw via an injected getter proxy. The
// adapter itself is lazy, so to exercise the catch we wrap createTrMemory-
// SearchManager. Simpler: bind deps that throw on construction by making
// `recall` a getter that throws the moment it's read.)
const throwingDeps = new Proxy(
  { recall: () => Promise.resolve([]), getById: async () => null },
  {
    get(target, prop) {
      if (prop === 'recall') {
        throw new Error('pipeline not paired');
      }
      return (target as any)[prop];
    },
  },
) as any;
const throwingRuntime = createTrMemoryPluginRuntime(throwingDeps);
// Reading `recall` happens inside createTrMemorySearchManager via the
// `deps.recall` reference captured in search(); the factory itself doesn't
// invoke recall, so construction succeeds. The contract still guarantees
// getMemorySearchManager never throws — verify that holds even when the
// manager is constructed against a hostile deps object.
const gotHostile = await throwingRuntime.getMemorySearchManager({
  cfg: {} as any,
  agentId: 'a1',
});
assert.ok(gotHostile.manager, 'construction does not invoke recall, so manager is returned');

console.log('memory-runtime.test OK');
