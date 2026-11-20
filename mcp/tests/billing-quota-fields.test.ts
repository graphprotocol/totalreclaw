/**
 * @jest-environment node
 *
 * billing-quota-fields.test.ts
 *
 * Spec: docs/specs/ops/write-quota-source-of-truth.md §4.1 (public repo plan
 * WQ-B Task 1). The relay's /v1/billing/status now sends:
 *   - free_writes_limit  = the cap for the wallet's CURRENT tier (not always
 *     the Free 250 figure),
 *   - upgrade_writes_limit (optional, Free only) = the Pro cap, so clients
 *     never hardcode 3,000,
 *   - write_grace (optional) = the Pro overshoot allowance (R-E).
 *
 * totalreclaw_status and totalreclaw_account must pass those through, clamp
 * `remaining_writes` at 0 (a Pro user inside the grace band used to see
 * negative numbers), and render the upgrade figure with an en-US thousands
 * separator when present — never a hardcoded one.
 */

import { handleStatus } from '../src/tools/status';
import { handleAccount } from '../src/tools/account';

const PRO_GRACE_BODY = {
  tier: 'pro',
  free_writes_used: 3400,
  free_writes_limit: 3000,
  write_grace: 1000,
  expires_at: null,
};

const FREE_UPGRADE_BODY = {
  tier: 'free',
  free_writes_used: 10,
  free_writes_limit: 250,
  upgrade_writes_limit: 3000,
  expires_at: null,
};

const OLDER_RELAY_BODY = {
  tier: 'free',
  free_writes_used: 10,
  free_writes_limit: 250,
  expires_at: null,
};

let body: Record<string, unknown>;
let originalFetch: typeof fetch;

beforeEach(() => {
  originalFetch = global.fetch;
  global.fetch = jest.fn(async () => ({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  global.fetch = originalFetch;
});

async function statusJson(): Promise<Record<string, unknown>> {
  const res = await handleStatus(
    { serverUrl: 'http://x', authKeyHex: 'aa' },
    { wallet_address: '0xabc' },
  );
  return JSON.parse(res.content[0].text);
}

async function accountJson(): Promise<Record<string, unknown>> {
  const res = await handleAccount({
    serverUrl: 'http://x',
    authKeyHex: 'aa',
    walletAddress: '0xabc',
    getFactCount: async () => 5,
  });
  return JSON.parse(res.content[0].text);
}

describe('billing quota fields (write-quota source of truth)', () => {
  test('pro in grace band clamps remaining and passes grace', async () => {
    body = PRO_GRACE_BODY;
    const out = await statusJson();
    expect(out.remaining_writes).toBe(0);
    expect(out.write_grace).toBe(1000);
    expect(out.writes_limit).toBe(3000);
  });

  test('free passes the upgrade figure and prints it', async () => {
    body = FREE_UPGRADE_BODY;
    const out = await statusJson();
    expect(out.upgrade_writes_limit).toBe(3000);
    expect(String(out.formatted)).toContain('Pro: 3,000 memories/month');
  });

  test('older relay without the new fields', async () => {
    body = OLDER_RELAY_BODY;
    const out = await statusJson();
    expect(out.upgrade_writes_limit).toBeUndefined();
    expect(String(out.formatted)).not.toContain('Pro:');
    expect(out.remaining_writes).toBe(240);
  });

  test('account tool clamps too', async () => {
    body = PRO_GRACE_BODY;
    const out = await accountJson();
    expect(out.remaining_writes).toBe(0);
  });
});
