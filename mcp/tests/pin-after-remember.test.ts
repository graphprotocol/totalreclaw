import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { executePinOperation, type PinOpDeps } from '../src/tools/pin';
import { executeRetype, type MetadataOpDeps } from '../src/tools/retype';
import { executeSetScope } from '../src/tools/set-scope';
import { buildV1ClaimBlob, blobIsPinned } from '../src/claims-helper';
import { RecentWrites, recordSubmittedPayloads } from '../src/subgraph/recent-writes';
import { createRewriteLookup } from '../src/subgraph/fact-lookup';

const TEST_STATE_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-pin-after-remember-'));
process.env.TOTALRECLAW_STATE_DIR = TEST_STATE_DIR;
afterAll(() => {
  try { fs.rmSync(TEST_STATE_DIR, { recursive: true, force: true }); } catch { /* ignore */ }
  delete process.env.TOTALRECLAW_STATE_DIR;
});

const OWNER = '0x1234567890abcdef1234567890abcdef12345678';
const hex = (s: string) => Buffer.from(s, 'utf-8').toString('hex');
const UNHEX = (h: string) => Buffer.from(h, 'hex').toString('utf-8');

function harness(withRecord = true) {
  const cache = new RecentWrites();
  const submitted: Buffer[][] = [];
  const subgraphCalls: string[] = [];
  const fetchFactById = createRewriteLookup({
    owner: OWNER, cache, retry: { totalMs: 0 }, log: () => {},
    lookupSubgraph: async (id) => { subgraphCalls.push(id); return { state: 'absent' }; }, // not indexed yet
  });
  const deps: PinOpDeps & MetadataOpDeps = {
    owner: OWNER, sourceAgent: 'mcp-server', fetchFactById,
    decryptBlob: UNHEX, encryptBlob: hex,
    submitBatch: async (p) => { submitted.push(p); return { txHash: '0xabc', success: true }; },
    generateIndices: async () => ({ blindIndices: [] }),
    recordWrites: withRecord ? (r) => recordSubmittedPayloads(cache, OWNER, r, { success: true }) : undefined,
  };
  return { cache, deps, submitted, subgraphCalls };
}

// What handleRememberSubgraph records after a successful submit.
function rememberHere(cache: RecentWrites, id: string, extra: Partial<Parameters<typeof buildV1ClaimBlob>[0]> = {}) {
  const blob = buildV1ClaimBlob({ id, text: 'Tidewater analytics moves to ClickHouse', type: 'claim', source: 'user', scope: 'work', importance: 8, ...extra });
  recordSubmittedPayloads(cache, OWNER, [{ id, timestamp: new Date().toISOString(), encryptedBlob: hex(blob), decayScore: 0.8 }], { success: true });
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

describe('pin right after remember (#701)', () => {
  test('pin right after remember succeeds while the subgraph has not indexed the fact', async () => {
    const h = harness();
    const id = crypto.randomUUID();
    rememberHere(h.cache, id);
    const r = await executePinOperation(id, 'pinned', h.deps);
    expect(r.success).toBe(true);
    expect(r.error).toBeUndefined();
    expect(r.previous_status).toBe('active');
    expect(r.new_status).toBe('pinned');
    expect(r.new_fact_id).toMatch(UUID_RE);
    expect(h.submitted).toHaveLength(1);
    expect(h.submitted[0]).toHaveLength(2);
    expect(h.subgraphCalls).toEqual([id]);
  });

  test('set_scope then retype on the new id work before indexing', async () => {
    const h = harness();
    const id = crypto.randomUUID();
    rememberHere(h.cache, id);
    const pin = await executePinOperation(id, 'pinned', h.deps);
    const scope = await executeSetScope(pin.new_fact_id!, 'personal', h.deps);
    expect(scope.success).toBe(true);
    expect(scope.previous_value).toBe('work');
    expect(scope.new_value).toBe('personal');
    const retype = await executeRetype(scope.new_memory_id!, 'preference', h.deps);
    expect(retype.success).toBe(true);
    expect(retype.previous_value).toBe('claim');
  });

  test('unpin right after pin works before indexing', async () => {
    const h = harness();
    const id = crypto.randomUUID();
    rememberHere(h.cache, id);
    const pin = await executePinOperation(id, 'pinned', h.deps);
    const unpin = await executePinOperation(pin.new_fact_id!, 'active', h.deps);
    expect(unpin.success).toBe(true);
    expect(unpin.previous_status).toBe('pinned');
  });

  test('the replaced id is not found again', async () => {
    const h = harness();
    const id = crypto.randomUUID();
    rememberHere(h.cache, id);
    await executePinOperation(id, 'pinned', h.deps);
    const again = await executePinOperation(id, 'pinned', h.deps);
    expect(again.success).toBe(false);
    expect(again.error).toBe(`Fact not found: ${id}`);
    expect(h.submitted).toHaveLength(1);
    expect(h.subgraphCalls).toEqual([id]);
  });

  test('without the local record the pin fails as it does today (control)', async () => {
    const h = harness(false);
    const id = crypto.randomUUID();
    const r = await executePinOperation(id, 'pinned', h.deps);
    expect(r.error).toBe(`Fact not found: ${id}`);
  });
});

describe('retype / set_scope keep the pin (#779)', () => {
  test('set_scope on a pinned fact writes a pinned successor', async () => {
    const h = harness();
    const id = crypto.randomUUID();
    rememberHere(h.cache, id, { pinStatus: 'pinned' });
    const r = await executeSetScope(id, 'personal', h.deps);
    expect(r.success).toBe(true);
    const successor = h.cache.get(OWNER, r.new_memory_id!);
    expect(successor).not.toBeNull();
    const plain = UNHEX(successor!.encryptedBlob);
    expect(blobIsPinned(plain)).toBe(true);
    expect(JSON.parse(plain).scope).toBe('personal');
  });

  test('retype on a pinned fact writes a pinned successor', async () => {
    const h = harness();
    const id = crypto.randomUUID();
    rememberHere(h.cache, id, { pinStatus: 'pinned' });
    const r = await executeRetype(id, 'preference', h.deps);
    expect(r.success).toBe(true);
    const plain = UNHEX(h.cache.get(OWNER, r.new_memory_id!)!.encryptedBlob);
    expect(blobIsPinned(plain)).toBe(true);
    expect(JSON.parse(plain).type).toBe('preference');
  });

  test('an unpinned fact stays unpinned, and entities / expiry / metadata are carried', async () => {
    const h = harness();
    const id = crypto.randomUUID();
    rememberHere(h.cache, id, {
      entities: [{ name: 'ClickHouse', type: 'tool' }],
      expiresAt: '2030-01-01T00:00:00Z',
      metadata: { session_id: 's-1' },
      agentName: 'John (Hermes)',
    });
    const r = await executeRetype(id, 'preference', h.deps);
    const parsed = JSON.parse(UNHEX(h.cache.get(OWNER, r.new_memory_id!)!.encryptedBlob));
    expect(blobIsPinned(JSON.stringify(parsed))).toBe(false);
    expect(parsed.entities).toEqual([{ name: 'ClickHouse', type: 'tool' }]);
    expect(parsed.expires_at).toBe('2030-01-01T00:00:00Z');
    expect(parsed.metadata).toEqual({ session_id: 's-1' });
    expect(parsed.agent_name).toBe('John (Hermes)');
  });

  test('set_scope carries agent_name', async () => {
    const h = harness();
    const id = crypto.randomUUID();
    rememberHere(h.cache, id, { agentName: 'John (Hermes)' });
    const r = await executeSetScope(id, 'personal', h.deps);
    expect(JSON.parse(UNHEX(h.cache.get(OWNER, r.new_memory_id!)!.encryptedBlob)).agent_name).toBe('John (Hermes)');
  });

  test('pin and unpin keep expiry, metadata and agent_name', async () => {
    const h = harness();
    const id = crypto.randomUUID();
    rememberHere(h.cache, id, { expiresAt: '2030-01-01T00:00:00Z', metadata: { session_id: 's-1' }, agentName: 'John (Hermes)' });
    const pin = await executePinOperation(id, 'pinned', h.deps);
    const pinned = JSON.parse(UNHEX(h.cache.get(OWNER, pin.new_fact_id!)!.encryptedBlob));
    expect(pinned.pin_status).toBe('pinned');
    expect(pinned.expires_at).toBe('2030-01-01T00:00:00Z');
    expect(pinned.metadata).toEqual({ session_id: 's-1' });
    expect(pinned.agent_name).toBe('John (Hermes)');
    const unpin = await executePinOperation(pin.new_fact_id!, 'active', h.deps);
    const back = JSON.parse(UNHEX(h.cache.get(OWNER, unpin.new_fact_id!)!.encryptedBlob));
    expect(back.expires_at).toBe('2030-01-01T00:00:00Z');
    expect(back.metadata).toEqual({ session_id: 's-1' });
    expect(back.agent_name).toBe('John (Hermes)');
  });
});
