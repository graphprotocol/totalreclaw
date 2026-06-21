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
 * recall + getById are fakes here; Task 2.3 wires them to the real
 * subgraph-search + vault-crypto.decrypt + reranker pipeline.
 *
 * Run: `npx tsx memory-runtime.test.ts` — prints OK and exits 0 on pass.
 */

import { strict as assert } from 'node:assert';
import { createTrMemorySearchManager, FACT_PATH_PREFIX } from './memory-runtime.js';

const recall = async (_q: string) => [
  { id: 'f1', plaintext: 'User prefers dark mode.', score: 0.9 },
  { id: 'f2', plaintext: 'User works at Acme.', score: 0.7 },
];
const getById = async (id: string) => ({
  id,
  plaintext: id === 'f1' ? 'User prefers dark mode.' : 'User works at Acme.',
});

const mgr = createTrMemorySearchManager({ recall, getById });

// --- search(): file-shaped results from decrypted facts ---
const hits = await mgr.search('preferences');
assert.equal(hits.length, 2);
assert.equal(hits[0].path, `${FACT_PATH_PREFIX}f1`);
assert.equal(hits[0].startLine, 1);
assert.ok(hits[0].endLine >= 1);
assert.equal(hits[0].source, 'memory');
assert.match(hits[0].snippet, /dark mode/);
assert.equal(hits[0].citation, 'f1');

// --- readFile(): reverse path -> id -> decrypt ---
const r = await mgr.readFile({ relPath: `${FACT_PATH_PREFIX}f1` });
assert.match(r.text, /dark mode/);
assert.equal(r.path, `${FACT_PATH_PREFIX}f1`);

// --- status + probes ---
assert.equal(mgr.status().provider, 'totalreclaw');
assert.ok((await mgr.probeEmbeddingAvailability()).ok);

console.log('memory-runtime.test OK');
