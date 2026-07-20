/**
 * save-tool.test — unit test for the createMemorySaveTool factory (internal#499).
 *
 * The bug: the plugin registered only `memory_search` + `memory_get` — no write
 * tool. When a user said "remember X", the agent had no write tool, shelled out
 * to `tr remember "X"` (GNU coreutils `tr`, not a TotalReclaw CLI), got no
 * output, and reported "Saved" — silent data loss on an explicit remember.
 *
 * This test pins the write-side sibling: `memory_save`. It captures a `store`
 * closure (the same store path extraction/import use — `storeExtractedFacts` —
 * wired in index.ts's buildRecallDeps) and routes the fact through it, returning
 * a truthful ok/stored the agent can report verbatim instead of fabricating
 * success.
 *
 * What this test asserts (the contract a reviewer should hold us to):
 *   1. Factory is exported and returns a tool named `memory_save` (NOT
 *      `totalreclaw_remember` — that legacy name is retired + lockstep-guarded
 *      out of contracts.tools by manifest-shape.test.ts 1g).
 *   2. `text` is required: missing / empty / non-string -> the tool returns a
 *      structured ok:false error AND never calls `store` (no silent no-op).
 *   3. Happy path: the tool calls `store` exactly once with a TrMemorySaveInput
 *      carrying the text + only the optional fields the agent supplied
 *      (omitted optionals are absent, not undefined — defaults are the closure's
 *      job in index.ts, not the tool's).
 *   4. The tool forwards the store result truthfully: ok:true + stored count
 *      on success; ok:false + the store's error on failure (the agent must be
 *      able to tell "saved" from "not saved" so it stops saying "Saved" on a
 *      no-op).
 *
 * Run: `npx tsx memory/save-tool.test.ts` — prints OK and exits 0 on pass.
 */

import { strict as assert } from 'node:assert';
import { createMemorySaveTool } from './tools.js';
import type { TrMemorySaveFn, TrMemorySaveInput } from './memory-runtime.js';

// ---------------------------------------------------------------------------
// Recording store closure: captures every call so we can assert routing.
// ---------------------------------------------------------------------------

let storeCalls: TrMemorySaveInput[] = [];
let nextStoreResult: { ok: boolean; stored: number; error?: string } = { ok: true, stored: 1 };

function resetStore(): void {
  storeCalls = [];
  nextStoreResult = { ok: true, stored: 1 };
}

const recordingStore: TrMemorySaveFn = async (input) => {
  storeCalls.push(input);
  return nextStoreResult;
};

// Helper: drive the tool's execute and parse the JSON payload it emits.
async function runTool(
  tool: ReturnType<typeof createMemorySaveTool>,
  params: unknown,
): Promise<{ ok: boolean; stored: number; error?: string; message?: string }> {
  const res = await tool.execute('tcid', params);
  return JSON.parse(res.content[0]!.text);
}

// ---------------------------------------------------------------------------
// 1. Factory shape + name.
// ---------------------------------------------------------------------------

resetStore();
const tool = createMemorySaveTool(recordingStore);
assert.equal(tool.name, 'memory_save', 'tool name is memory_save');
assert.equal(typeof tool.execute, 'function', 'tool has an execute function');
assert.ok(
  tool.parameters && typeof tool.parameters === 'object',
  'tool declares a parameters schema',
);

// ---------------------------------------------------------------------------
// 2. `text` is required — missing / empty / wrong type -> ok:false, no store call.
//    This is the truthful-failure path: the agent must NOT be able to drive the
//    tool into a silent no-op that it then misreports as "Saved".
// ---------------------------------------------------------------------------

for (const [label, bad] of [
  ['missing text', {}],
  ['empty text', { text: '' }],
  ['whitespace-only text', { text: '   ' }],
  ['non-string text', { text: 42 }],
] as Array<[string, unknown]>) {
  resetStore();
  const out = await runTool(createMemorySaveTool(recordingStore), bad);
  assert.equal(out.ok, false, `required-text (${label}): ok must be false`);
  assert.ok(typeof out.error === 'string' && out.error.length > 0, `required-text (${label}): error string present`);
  assert.equal(storeCalls.length, 0, `required-text (${label}): store must NOT be called`);
}

// ---------------------------------------------------------------------------
// 3. Happy path — routes the fact through `store`, forwarding only supplied
//    optional fields (defaults belong to the index.ts closure, not the tool).
// ---------------------------------------------------------------------------

resetStore();
nextStoreResult = { ok: true, stored: 1 };
let out = await runTool(createMemorySaveTool(recordingStore), { text: 'User prefers PostgreSQL over MySQL' });
assert.equal(out.ok, true, 'happy-path: ok is true when store succeeds');
assert.equal(out.stored, 1, 'happy-path: forwards stored count from store');
assert.equal(storeCalls.length, 1, 'happy-path: store called exactly once');
assert.deepEqual(
  storeCalls[0],
  { text: 'User prefers PostgreSQL over MySQL' },
  'happy-path: store receives only {text} when no optionals supplied',
);

// Optional fields are forwarded when supplied, absent when not.
resetStore();
nextStoreResult = { ok: true, stored: 1 };
out = await runTool(createMemorySaveTool(recordingStore), {
  text: 'Decided to ship 3.4.0 on Friday because the scanner is green',
  type: 'commitment',
  importance: 9,
  scope: 'work',
  reasoning: 'the scanner is green',
  entities: [{ name: '3.4.0', type: 'project' }],
});
assert.equal(storeCalls.length, 1, 'optionals: store called exactly once');
const sent = storeCalls[0]!;
assert.equal(sent.text, 'Decided to ship 3.4.0 on Friday because the scanner is green', 'optionals: text forwarded');
assert.equal(sent.type, 'commitment', 'optionals: type forwarded');
assert.equal(sent.importance, 9, 'optionals: importance forwarded');
assert.equal(sent.scope, 'work', 'optionals: scope forwarded');
assert.equal(sent.reasoning, 'the scanner is green', 'optionals: reasoning forwarded');
assert.deepEqual(sent.entities, [{ name: '3.4.0', type: 'project' }], 'optionals: entities forwarded');

// An invalid `importance` (non-number / out of range) is dropped, not forwarded
// as garbage — the closure applies a sane default rather than persisting junk.
resetStore();
out = await runTool(createMemorySaveTool(recordingStore), { text: 'x', importance: 'high' });
assert.ok(!('importance' in storeCalls[0]!), 'optionals: non-numeric importance is dropped (no garbage forwarded)');

// ---------------------------------------------------------------------------
// 4. Truthful failure — store returns ok:false and the tool surfaces it.
//    This is the direct fix for "agent reports Saved on a no-op": the agent
//    reads ok:false + error and must relay the failure instead of fabricating.
// ---------------------------------------------------------------------------

resetStore();
nextStoreResult = { ok: false, stored: 0, error: 'not paired — run setup first' };
out = await runTool(createMemorySaveTool(recordingStore), { text: 'User likes TypeScript' });
assert.equal(out.ok, false, 'failure: ok is false when store fails');
assert.equal(out.stored, 0, 'failure: stored is 0 when store fails');
assert.equal(out.error, 'not paired — run setup first', 'failure: store error surfaced verbatim');

// stored:0 (dedup / skip) is still ok:true but the agent can tell nothing was
// persisted — distinct from a real failure. The tool must not muddy the two.
resetStore();
nextStoreResult = { ok: true, stored: 0 };
out = await runTool(createMemorySaveTool(recordingStore), { text: 'duplicate of an existing memory' });
assert.equal(out.ok, true, 'dedup: ok stays true (store path ran)');
assert.equal(out.stored, 0, 'dedup: stored is 0 (nothing new persisted)');

console.log('save-tool.test — OK');
