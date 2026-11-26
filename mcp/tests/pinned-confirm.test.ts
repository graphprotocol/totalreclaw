/**
 * internal#778: forget / unpin / retype / set_scope on a pinned memory return
 * confirmation_required with a confirm_token and change nothing. The change
 * happens only on a re-call with the token once it is CONFIRM_TOKEN_MIN_AGE_MS
 * old (MCP has no turn signal; Known Gap, same as the PII override).
 */

import { buildV1ClaimBlob } from '../src/claims-helper';
import { CONFIRM_TOKEN_MIN_AGE_MS, CONFIRM_TOKEN_TTL_MS, ConfirmationStore } from '../src/pii';
import { pinConfirmation } from '../src/pin-confirm';
import { pinnedForgetResponse } from '../src/tools/forget';
import { handleUnpinSubgraphWithDeps, type PinOpDeps } from '../src/tools/pin';
import { handleRetypeWithDeps, type MetadataOpDeps } from '../src/tools/retype';
import { handleSetScopeWithDeps } from '../src/tools/set-scope';

type Resp = { content: Array<{ type: string; text: string }> };
const parse = (r: Resp) => JSON.parse(r.content[0].text);

let now = 1_700_000_000_000;
let realNow: () => number;
beforeEach(() => {
  realNow = Date.now;
  Date.now = () => now;
});
afterEach(() => {
  Date.now = realNow;
});
const advance = (ms: number) => {
  now += ms;
};

function blob(pinned: boolean): string {
  return buildV1ClaimBlob({
    text: 'Migration is shelved',
    type: 'claim',
    source: 'user',
    id: 'pin-1',
    ...(pinned ? { pinStatus: 'pinned' as const } : {}),
  });
}

function makeDeps(pinned: boolean): PinOpDeps & MetadataOpDeps & { submitted: Buffer[][] } {
  const submitted: Buffer[][] = [];
  const b = blob(pinned);
  return {
    submitted,
    owner: '0x1234567890abcdef1234567890abcdef12345678',
    sourceAgent: 'mcp-server',
    async fetchFactById(id: string) {
      return { id, encryptedBlob: b, encryptedEmbedding: null, decayScore: '0.8', timestamp: '1700000000', isActive: true };
    },
    decryptBlob: (x: string) => x,
    encryptBlob: (p: string) => Buffer.from(p, 'utf-8').toString('hex'),
    async submitBatch(payloads: Buffer[]) {
      submitted.push(payloads);
      return { txHash: '0xdeadbeef', success: true };
    },
    async generateIndices() {
      return { blindIndices: ['a', 'b'], encryptedEmbedding: 'e' };
    },
  } as unknown as PinOpDeps & MetadataOpDeps & { submitted: Buffer[][] };
}

const OPS = {
  unpin: {
    run: (args: Record<string, unknown>, deps: PinOpDeps) => handleUnpinSubgraphWithDeps(args, deps),
    args: { memory_id: 'pin-1' },
  },
  retype: {
    run: (args: Record<string, unknown>, deps: MetadataOpDeps) => handleRetypeWithDeps(args, deps),
    args: { memory_id: 'pin-1', new_type: 'preference' },
  },
  set_scope: {
    run: (args: Record<string, unknown>, deps: MetadataOpDeps) => handleSetScopeWithDeps(args, deps),
    args: { memory_id: 'pin-1', scope: 'work' },
  },
} as const;

describe.each(Object.keys(OPS) as Array<keyof typeof OPS>)('%s on a pinned memory', (op) => {
  const { run, args } = OPS[op];

  test('asks first and writes nothing', async () => {
    const deps = makeDeps(true);
    const out = parse(await run({ ...args }, deps as never));
    expect(out.success).toBe(false);
    expect(out.confirmation_required).toBe(`pinned_${op}`);
    expect(out.confirm_token).toMatch(/^[0-9a-f]{16}$/);
    expect(out.message).toMatch(/pinned/);
    expect(out.next_step).toMatch(/confirm_token/);
    expect(deps.submitted).toHaveLength(0);
  });

  test('a token younger than the minimum age, unknown and wrong tokens are refused', async () => {
    const deps = makeDeps(true);
    const first = parse(await run({ ...args }, deps as never));
    const tooSoon = parse(await run({ ...args, confirm_token: first.confirm_token }, deps as never));
    expect(tooSoon.confirmation_required).toBe(`pinned_${op}`);
    advance(CONFIRM_TOKEN_MIN_AGE_MS);
    for (const bad of ['deadbeefdeadbeef', 'x']) {
      const out = parse(await run({ ...args, confirm_token: bad }, deps as never));
      expect(out.confirmation_required).toBe(`pinned_${op}`);
    }
    expect(deps.submitted).toHaveLength(0);
  });

  test('the right token after the minimum age works once; reuse is refused', async () => {
    const deps = makeDeps(true);
    const first = parse(await run({ ...args }, deps as never));
    advance(CONFIRM_TOKEN_MIN_AGE_MS);
    const ok = parse(await run({ ...args, confirm_token: first.confirm_token }, deps as never));
    expect(ok.confirmation_required).toBeUndefined();
    expect(ok.success).toBe(true);
    expect(deps.submitted).toHaveLength(1);
    const again = parse(await run({ ...args, confirm_token: first.confirm_token }, deps as never));
    expect(again.confirmation_required).toBe(`pinned_${op}`);
    expect(deps.submitted).toHaveLength(1);
  });

  test('an expired token is refused', async () => {
    const deps = makeDeps(true);
    const first = parse(await run({ ...args }, deps as never));
    advance(CONFIRM_TOKEN_TTL_MS + 1);
    const out = parse(await run({ ...args, confirm_token: first.confirm_token }, deps as never));
    expect(out.confirmation_required).toBe(`pinned_${op}`);
    expect(deps.submitted).toHaveLength(0);
  });

  test('an unpinned memory is changed without a prompt', async () => {
    const deps = makeDeps(false);
    const out = parse(await run({ ...args }, deps as never));
    expect(out.confirmation_required).toBeUndefined();
    expect(out.success).toBe(true);
    // unpinning an unpinned memory is an idempotent no-op, the others write
    expect(deps.submitted).toHaveLength(op === 'unpin' ? 0 : 1);
  });
});

test('a token is bound to the action and the new value', async () => {
  const deps = makeDeps(true);
  const first = parse(await handleRetypeWithDeps({ memory_id: 'pin-1', new_type: 'preference' }, deps));
  advance(CONFIRM_TOKEN_MIN_AGE_MS);
  // another new_type
  const other = parse(
    await handleRetypeWithDeps({ memory_id: 'pin-1', new_type: 'directive', confirm_token: first.confirm_token }, deps),
  );
  expect(other.confirmation_required).toBe('pinned_retype');
  // another action
  const unpin = parse(await handleUnpinSubgraphWithDeps({ memory_id: 'pin-1', confirm_token: first.confirm_token }, deps));
  expect(unpin.confirmation_required).toBe('pinned_unpin');
  expect(deps.submitted).toHaveLength(0);
});

describe('forget on a pinned memory', () => {
  const fact = (pinned: boolean) => ({ encryptedBlob: '0x' + Buffer.from(blob(pinned)).toString('hex') });
  const decrypt = (hex: string) => Buffer.from(hex.replace(/^0x/, ''), 'hex').toString('utf-8');

  test('asks first and forgets nothing', () => {
    const r = pinnedForgetResponse(fact(true), decrypt, 'pin-1', {});
    const out = parse(r as Resp);
    expect(out.deleted_count).toBe(0);
    expect(out.fact_ids).toEqual([]);
    expect(out.confirmation_required).toBe('pinned_forget');
    expect(out.confirm_token).toBeTruthy();
  });

  test('too-young, wrong and reused tokens are refused; the right one proceeds once', () => {
    const first = parse(pinnedForgetResponse(fact(true), decrypt, 'pin-1', {}) as Resp);
    expect(pinnedForgetResponse(fact(true), decrypt, 'pin-1', { token: first.confirm_token })).not.toBeNull();
    advance(CONFIRM_TOKEN_MIN_AGE_MS);
    expect(pinnedForgetResponse(fact(true), decrypt, 'pin-1', { token: 'deadbeefdeadbeef' })).not.toBeNull();
    expect(pinnedForgetResponse(fact(true), decrypt, 'other-id', { token: first.confirm_token })).not.toBeNull();
    expect(pinnedForgetResponse(fact(true), decrypt, 'pin-1', { token: first.confirm_token })).toBeNull();
    expect(pinnedForgetResponse(fact(true), decrypt, 'pin-1', { token: first.confirm_token })).not.toBeNull();
  });

  test('unreadable pin state fails closed: nothing is forgotten, no token', () => {
    const boom = () => {
      throw new Error('cannot decrypt');
    };
    const out = parse(pinnedForgetResponse(fact(true), boom, 'pin-1', {}) as Resp);
    expect(out.deleted_count).toBe(0);
    expect(out.error).toMatch(/Could not check/);
    expect(out.confirm_token).toBeUndefined();
    // a (even valid-looking) token does not bypass it
    expect(pinnedForgetResponse(fact(true), boom, 'pin-1', { token: 'deadbeefdeadbeef' })).not.toBeNull();
  });

  test('pin tokens do not use the sensitive-data store', () => {
    const first = parse(pinnedForgetResponse(fact(true), decrypt, 'pin-1', {}) as Resp);
    const { confirmations } = jest.requireActual('../src/pii');
    advance(CONFIRM_TOKEN_MIN_AGE_MS);
    expect(confirmations.redeem(first.confirm_token, 'pinned_forget', 'pin-1')).toBe('unknown_token');
  });

  test('an unpinned memory is forgotten without a prompt', () => {
    expect(pinnedForgetResponse(fact(false), decrypt, 'x', {})).toBeNull();
    expect(pinnedForgetResponse(null, decrypt, 'x', {})).toBeNull();
  });
});

test('pinConfirmation uses an injected store and its clock', () => {
  let t = 0;
  const store = new ConfirmationStore(() => t);
  const ask = pinConfirmation('unpin', 'f1', { store });
  expect(ask?.confirmation_required).toBe('pinned_unpin');
  t += CONFIRM_TOKEN_MIN_AGE_MS;
  expect(pinConfirmation('unpin', 'f1', { store, token: ask!.confirm_token })).toBeNull();
});
