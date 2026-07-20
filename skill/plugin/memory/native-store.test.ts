// scanner-sim: allow
/**
 * Direct unit coverage of the `memory_save` store closure's TRUTHFULNESS
 * contract (#499 review Finding 2) — the real branch logic, not a mock that
 * does what the test tells it. Untested production glue is exactly what let
 * the original silent-data-loss bug reach QA, so every non-persist path is
 * pinned to `ok:false` (never a fabricated "Saved").
 *
 * Run: npx tsx native-store.test.ts
 */
import { strict as assert } from 'node:assert';
import { buildNativeStore, type NativeStoreCtx } from './native-store.js';

let passed = 0;
let failed = 0;
function check(name: string, fn: () => void | Promise<void>): Promise<void> {
  return Promise.resolve()
    .then(fn)
    .then(() => { console.log(`ok ${passed + failed + 1} - ${name}`); passed++; })
    .catch((e) => { console.error(`not ok ${passed + failed + 1} - ${name}\n  ${e}`); failed++; process.exitCode = 1; });
}

function ctx(over: Partial<NativeStoreCtx>): NativeStoreCtx {
  return {
    ensureInit: async () => {},
    isPaired: () => true,
    storeFacts: async () => 1,
    ...over,
  };
}

await check('init throws → ok:false, setup-incomplete error, storeFacts NOT called', async () => {
  let stored = false;
  const save = buildNativeStore(ctx({
    ensureInit: async () => { throw new Error('WS handshake 502'); },
    storeFacts: async () => { stored = true; return 1; },
  }));
  const r = await save({ text: 'x' });
  assert.equal(r.ok, false);
  assert.equal(r.stored, 0);
  assert.match(r.error ?? '', /setup incomplete: WS handshake 502/);
  assert.equal(stored, false, 'must not reach the store path when init fails');
});

await check('not paired → ok:false, no store call (the key silent-loss guard)', async () => {
  let stored = false;
  const save = buildNativeStore(ctx({
    isPaired: () => false,
    storeFacts: async () => { stored = true; return 1; },
  }));
  const r = await save({ text: 'remember my birthday' });
  assert.equal(r.ok, false);
  assert.equal(r.stored, 0);
  assert.match(r.error ?? '', /not paired/);
  assert.equal(stored, false);
});

await check('storeFacts throws (on-chain/quota fail) → ok:false, error surfaced', async () => {
  const save = buildNativeStore(ctx({
    storeFacts: async () => { throw new Error('paymaster rejected UserOp AA33'); },
  }));
  const r = await save({ text: 'x' });
  assert.equal(r.ok, false);
  assert.equal(r.stored, 0);
  assert.match(r.error ?? '', /paymaster rejected/);
});

await check('storeFacts returns 0 (dedup/skip) → ok:TRUE, stored:0 (never "Saved")', async () => {
  // A dedup no-op is a SUCCESSFUL run that stored nothing — ok stays true so
  // the tool says "duplicate, not stored", but stored:0 means the agent must
  // NOT claim it saved a new fact.
  const save = buildNativeStore(ctx({ storeFacts: async () => 0 }));
  const r = await save({ text: 'x' });
  assert.equal(r.ok, true);
  assert.equal(r.stored, 0);
  assert.equal(r.error, undefined);
});

await check('happy path → ok:true, stored:1', async () => {
  const save = buildNativeStore(ctx({ storeFacts: async () => 1 }));
  const r = await save({ text: 'Pedro lives in Porto' });
  assert.equal(r.ok, true);
  assert.equal(r.stored, 1);
});

await check('isPaired evaluated AFTER ensureInit (hot-reload pairing honored)', async () => {
  // Simulate a fresh install whose pairing completes DURING ensureInit: isPaired
  // reads live state, so it must be checked after init resolves, not before.
  let inited = false;
  const save = buildNativeStore(ctx({
    ensureInit: async () => { inited = true; },
    isPaired: () => inited, // only "paired" once init ran
    storeFacts: async () => 1,
  }));
  const r = await save({ text: 'x' });
  assert.equal(r.ok, true, 'a pair that completes in ensureInit must count');
});

await check('defaults applied: type=claim, importance=8, action=ADD, confidence=1, source=user', async () => {
  let seen: Record<string, unknown> | undefined;
  const save = buildNativeStore(ctx({
    storeFacts: async (facts) => { seen = facts[0] as unknown as Record<string, unknown>; return 1; },
  }));
  await save({ text: 'x' });
  assert.equal(seen?.type, 'claim');
  assert.equal(seen?.importance, 8);
  assert.equal(seen?.action, 'ADD');
  assert.equal(seen?.confidence, 1.0);
  assert.equal(seen?.source, 'user');
});

await check('explicit fields override defaults; optional fields only when supplied', async () => {
  let seen: Record<string, unknown> | undefined;
  const save = buildNativeStore(ctx({
    storeFacts: async (facts) => { seen = facts[0] as unknown as Record<string, unknown>; return 1; },
  }));
  await save({ text: 'x', type: 'commitment', importance: 3, reasoning: 'because Y' });
  assert.equal(seen?.type, 'commitment');
  assert.equal(seen?.importance, 3);
  assert.equal(seen?.reasoning, 'because Y');
  assert.equal('scope' in (seen ?? {}), false, 'scope omitted when not supplied');
  assert.equal('entities' in (seen ?? {}), false, 'entities omitted when not supplied');
});

console.log(`\n# ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
