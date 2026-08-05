/**
 * curate-tool.test — unit tests for the four curation tool factories (#573):
 *   createMemoryPinTool / createMemoryUnpinTool / createMemoryRetypeTool /
 *   createMemorySetScopeTool.
 *
 * The bug (#563): the OpenClaw plugin had no curation tools. An agent asked to
 * "pin that" had no tool to call, so it shelled out — or worse, made one
 * memory_save call and reported "Done — pinned with importance 10", all
 * invented. Importance affects RANKING; pin protects against AUTO-SUPERSESSION.
 * Different guarantees, and the agent couldn't tell them apart without a real
 * tool. SKILL.md prose prohibiting the fake did not hold (§6 lesson); the same
 * rule in a tool description has held since 3.4.0.
 *
 * This test pins the four curation-tool siblings to memory_save. It captures a
 * `curate` closure (the dispatch path wired in index.ts's buildRecallDeps via
 * buildNativeCurate) and routes each op through it, returning a truthful
 * ok + new_fact_id + tx_hash the agent reports verbatim instead of fabricating.
 *
 * What this test asserts (the contract a reviewer should hold us to):
 *   1. Each factory is exported, returns a tool with the right name
 *      (memory_pin / memory_unpin / memory_retype / memory_set_scope — NOT the
 *      retired totalreclaw_* names, which manifest-shape.test.ts 1g guards out
 *      of contracts.tools).
 *   2. Each description names the supersession semantics (NEW fact_id, original
 *      id no longer active) AND the "importance is not a pin" rule — the §6
 *      control surface that must live in the description, not SKILL.md.
 *   3. `memory_id` is required: missing / empty / non-string -> ok:false AND
 *      curate is NOT called (no silent no-op the agent could misreport). The
 *      `fact_id` back-compat alias resolves identically.
 *   4. Happy path: each tool calls curate exactly once with the right op +
 *      factId + the op-specific extra field (reason for pin; newType for
 *      retype; newScope for set_scope; nothing extra for unpin).
 *   5. Truthful relay: curate ok:true surfaces new_fact_id + tx_hash (a REAL
 *      supersession, never a confabulated "pinned"); curate ok:false surfaces
 *      the error verbatim (the agent relays failure, never claims success).
 *
 * Run: `npx tsx memory/curate-tool.test.ts` — prints OK and exits 0 on pass.
 */

import { strict as assert } from 'node:assert';
import {
  createMemoryPinTool,
  createMemoryUnpinTool,
  createMemoryRetypeTool,
  createMemorySetScopeTool,
} from './tools.js';
import type { TrCurationInput, TrCurationResult } from './curation-runtime.js';

// ---------------------------------------------------------------------------
// Recording curate closure: captures every call so we can assert routing.
// ---------------------------------------------------------------------------

let curateCalls: TrCurationInput[] = [];
let nextCurateResult: TrCurationResult;

function resetCurate(): void {
  curateCalls = [];
  // Default happy-path result: a real supersession (new id + tx). Individual
  // sections override nextCurateResult to test failure / idempotent paths.
  nextCurateResult = {
    ok: true,
    op: 'pin',
    fact_id: 'fact-123',
    new_fact_id: 'fact-124',
    tx_hash: '0xabc123',
  };
}

const recordingCurate = async (input: TrCurationInput): Promise<TrCurationResult> => {
  curateCalls.push(input);
  // Echo the op back so the result matches what the tool dispatched.
  return { ...nextCurateResult, op: input.op, fact_id: input.factId };
};

// Helper: drive a tool's execute and parse the JSON payload it emits.
async function runTool(
  tool: { execute: (id: string, params: unknown) => Promise<{ content: Array<{ text: string }> }> },
  params: unknown,
): Promise<TrCurationResult & { message?: string }> {
  const res = await tool.execute('tcid', params);
  return JSON.parse(res.content[0]!.text);
}

// ---------------------------------------------------------------------------
// 1. Factory shape + name + description contract (the §6 control surface).
// ---------------------------------------------------------------------------

resetCurate();
const pinTool = createMemoryPinTool(recordingCurate);
const unpinTool = createMemoryUnpinTool(recordingCurate);
const retypeTool = createMemoryRetypeTool(recordingCurate);
const setScopeTool = createMemorySetScopeTool(recordingCurate);

const tools = [
  { tool: pinTool, name: 'memory_pin', label: 'Memory Pin' },
  { tool: unpinTool, name: 'memory_unpin', label: 'Memory Unpin' },
  { tool: retypeTool, name: 'memory_retype', label: 'Memory Retype' },
  { tool: setScopeTool, name: 'memory_set_scope', label: 'Memory Set Scope' },
];

for (const { tool, name, label } of tools) {
  assert.equal(tool.name, name, `tool name is ${name}`);
  assert.equal(tool.label, label, `tool label is ${label}`);
  assert.equal(typeof tool.execute, 'function', `${name} has an execute function`);
  assert.ok(
    tool.parameters && typeof tool.parameters === 'object',
    `${name} declares a parameters schema`,
  );
  // §6 control surface: the description MUST name the supersession semantics
  // AND the "importance is not a pin" rule. These are the constraints that
  // failed to hold as SKILL.md prose; they must live in the description.
  const desc = tool.description.toLowerCase();
  assert.ok(
    desc.includes('supersession'),
    `${name} description names the on-chain SUPERSESSION semantics`,
  );
  assert.ok(
    desc.includes('new') && desc.includes('fact_id'),
    `${name} description states the response carries a NEW fact_id`,
  );
  assert.ok(
    desc.includes('importance') && desc.includes('not a substitute'),
    `${name} description states importance is NOT a substitute (the §6 rule)`,
  );
  assert.ok(
    desc.includes('memory_search'),
    `${name} description tells the agent to look up the id via memory_search first`,
  );
}

// ---------------------------------------------------------------------------
// 2. `memory_id` is required — missing / empty / wrong type -> ok:false, no
//    curate call. The `fact_id` back-compat alias resolves identically.
// ---------------------------------------------------------------------------

for (const [label, bad] of [
  ['missing memory_id', {}],
  ['empty memory_id', { memory_id: '' }],
  ['whitespace-only memory_id', { memory_id: '   ' }],
  ['non-string memory_id', { memory_id: 42 }],
] as Array<[string, unknown]>) {
  for (const { tool, name } of tools) {
    resetCurate();
    const out = await runTool(tool, bad);
    assert.equal(out.ok, false, `required-memory_id (${label}) on ${name}: ok must be false`);
    assert.ok(
      typeof out.error === 'string' && out.error.length > 0,
      `required-memory_id (${label}) on ${name}: error string present`,
    );
    assert.equal(curateCalls.length, 0, `required-memory_id (${label}) on ${name}: curate NOT called`);
  }
}

// `fact_id` back-compat alias resolves to the same id as `memory_id`.
for (const { tool, name } of tools) {
  resetCurate();
  const out = await runTool(tool, { fact_id: 'alias-1' });
  assert.equal(out.ok, true, `${name}: fact_id alias resolves and succeeds`);
  assert.equal(curateCalls.length, 1, `${name}: fact_id alias -> curate called once`);
  assert.equal(curateCalls[0]!.factId, 'alias-1', `${name}: fact_id alias forwarded as factId`);
}

// `memory_id` is preferred over `fact_id` when both present (MCP parity).
resetCurate();
await runTool(pinTool, { memory_id: 'canonical-1', fact_id: 'alias-1' });
assert.equal(
  curateCalls[0]!.factId,
  'canonical-1',
  'memory_id preferred over fact_id when both present (MCP parity)',
);

// ---------------------------------------------------------------------------
// 3. Happy path — each tool dispatches with the right op + extra field.
// ---------------------------------------------------------------------------

// pin: op='pin', forwards reason when supplied.
resetCurate();
let out = await runTool(pinTool, { memory_id: 'fact-1', reason: 'user asked to lock it' });
assert.equal(out.ok, true, 'pin happy-path: ok is true');
assert.equal(curateCalls.length, 1, 'pin happy-path: curate called once');
assert.equal(curateCalls[0]!.op, 'pin', 'pin happy-path: op is pin');
assert.equal(curateCalls[0]!.factId, 'fact-1', 'pin happy-path: factId forwarded');
assert.equal(curateCalls[0]!.reason, 'user asked to lock it', 'pin happy-path: reason forwarded');

// pin without reason: reason field absent (not undefined-as-garbage).
resetCurate();
await runTool(pinTool, { memory_id: 'fact-1' });
assert.ok(!('reason' in curateCalls[0]!), 'pin without reason: reason absent, not garbage');

// unpin: op='unpin', no extra field.
resetCurate();
out = await runTool(unpinTool, { memory_id: 'fact-2' });
assert.equal(out.ok, true, 'unpin happy-path: ok is true');
assert.equal(curateCalls[0]!.op, 'unpin', 'unpin happy-path: op is unpin');
assert.equal(curateCalls[0]!.factId, 'fact-2', 'unpin happy-path: factId forwarded');

// retype: op='retype', forwards new_type -> newType.
resetCurate();
out = await runTool(retypeTool, { memory_id: 'fact-3', new_type: 'directive' });
assert.equal(out.ok, true, 'retype happy-path: ok is true');
assert.equal(curateCalls[0]!.op, 'retype', 'retype happy-path: op is retype');
assert.equal(curateCalls[0]!.newType, 'directive', 'retype happy-path: new_type -> newType forwarded');

// retype with INVALID new_type: dropped (not forwarded as garbage). The tool
// still calls curate, but without newType — runCurationOp then surfaces the
// missing-field error truthfully. (An alternative would be to reject at the
// tool boundary; we mirror memory_save's "drop invalid optionals" behaviour
// for consistency, and let the dispatch own validation.)
resetCurate();
await runTool(retypeTool, { memory_id: 'fact-3', new_type: 'not-a-real-type' });
assert.ok(
  !('newType' in curateCalls[0]!),
  'retype invalid new_type: dropped, not forwarded as garbage',
);

// set_scope: op='set_scope', forwards scope -> newScope.
resetCurate();
out = await runTool(setScopeTool, { memory_id: 'fact-4', scope: 'finance' });
assert.equal(out.ok, true, 'set_scope happy-path: ok is true');
assert.equal(curateCalls[0]!.op, 'set_scope', 'set_scope happy-path: op is set_scope');
assert.equal(curateCalls[0]!.newScope, 'finance', 'set_scope happy-path: scope -> newScope forwarded');

// ---------------------------------------------------------------------------
// 4. Truthful relay — the new_fact_id + tx_hash surface on success (a REAL
//    supersession), and the message names the NEW id so the agent never
//    confabulates. This is the direct fix for the #563 confabulation.
// ---------------------------------------------------------------------------

resetCurate();
nextCurateResult = {
  ok: true,
  op: 'pin',
  fact_id: 'old-1',
  new_fact_id: 'new-1',
  tx_hash: '0xdeadbeef',
};
out = await runTool(pinTool, { memory_id: 'old-1' });
assert.equal(out.ok, true, 'success relay: ok is true');
assert.equal(out.new_fact_id, 'new-1', 'success relay: new_fact_id surfaced (the supersession)');
assert.equal(out.tx_hash, '0xdeadbeef', 'success relay: tx_hash surfaced');
assert.ok(
  out.message!.includes('new-1'),
  'success relay: message names the NEW id (agent reports the supersession, not a confabulated pin)',
);
assert.ok(out.message!.includes('Pinned'), 'success relay: message says Pinned');

// Idempotent (unpinning an already-unpinned memory) -> ok:true, no new id, a
// "no change" message. The agent must NOT claim a supersession happened.
resetCurate();
nextCurateResult = { ok: true, op: 'unpin', fact_id: 'x', idempotent: true };
out = await runTool(unpinTool, { memory_id: 'x' });
assert.equal(out.ok, true, 'idempotent: ok stays true');
assert.equal(out.idempotent, true, 'idempotent: idempotent flag surfaced');
assert.ok(out.message!.includes('No change'), 'idempotent: message says No change (not a supersession)');

// ---------------------------------------------------------------------------
// 5. Truthful failure — curate returns ok:false and the tool surfaces it.
//    This is the direct counter to "agent reports a successful pin that never
//    happened": the agent reads ok:false + error and must relay the failure
//    instead of fabricating.
// ---------------------------------------------------------------------------

resetCurate();
nextCurateResult = { ok: false, op: 'pin', fact_id: 'bad-1', error: 'fact not found' };
out = await runTool(pinTool, { memory_id: 'bad-1' });
assert.equal(out.ok, false, 'failure relay: ok is false when curate fails');
assert.equal(out.error, 'fact not found', 'failure relay: error surfaced verbatim');
assert.ok(
  out.message!.includes('Could not') && out.message!.includes('fact not found'),
  'failure relay: message names the failure (agent relays it, never claims success)',
);

// Closure-thrown error (e.g. not paired) is also surfaced truthfully — the
// executeCurate helper wraps runCurationOp's no-throw contract with its own
// try/catch so a thrown dep-assembly error becomes an ok:false result.
resetCurate();
const throwingCurate = async (_input: TrCurationInput): Promise<TrCurationResult> => {
  throw new Error('not paired — run setup first');
};
out = await runTool(createMemoryPinTool(throwingCurate), { memory_id: 'z' });
assert.equal(out.ok, false, 'thrown-error relay: ok is false');
assert.ok(
  out.message!.includes('not paired'),
  'thrown-error relay: message surfaces the thrown error verbatim',
);

console.log('curate-tool.test — OK');
