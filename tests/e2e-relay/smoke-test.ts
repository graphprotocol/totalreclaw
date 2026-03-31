/**
 * E2E smoke test for the relay service running in Docker.
 *
 * Prerequisites:
 *   docker compose up -d  (from this directory)
 *   Wait for relay healthcheck to pass.
 *
 * Tests:
 *   1. Health check (GET /health -> 200)
 *   2. Register user (POST /v1/register)
 *   3. Check billing status (GET /v1/billing/status -> free tier)
 *   4. Verify features dict returned
 *
 * Run:
 *   npx tsx smoke-test.ts
 */
import { createHash, randomBytes } from 'crypto';

const RELAY_URL = process.env.RELAY_URL || 'http://localhost:28080';

async function request(
  method: string,
  path: string,
  body?: any,
  headers?: Record<string, string>,
): Promise<{ status: number; data: any }> {
  const url = `${RELAY_URL}${path}`;
  const options: RequestInit = {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...headers,
    },
  };
  if (body) {
    options.body = JSON.stringify(body);
  }

  const res = await fetch(url, options);
  const data = await res.json();
  return { status: res.status, data };
}

async function assert(condition: boolean, message: string): Promise<void> {
  if (!condition) {
    throw new Error(`ASSERTION FAILED: ${message}`);
  }
}

async function main() {
  console.log(`Running E2E smoke tests against ${RELAY_URL}\n`);
  let passed = 0;
  let failed = 0;

  // Generate test credentials
  const authKey = randomBytes(32);
  const authKeyHash = createHash('sha256').update(authKey).digest();
  const salt = randomBytes(32);
  const tokenHex = authKey.toString('hex');
  // Valid-format Ethereum address for billing tests (deterministic from test key)
  const testWalletAddress = '0x' + createHash('sha256').update('smoke-test-wallet').digest('hex').slice(0, 40);

  // ============================================================
  // Test 1: Health check
  // ============================================================
  try {
    const res = await request('GET', '/health');
    await assert(res.status === 200, `Expected 200, got ${res.status}`);
    await assert(res.data.status === 'ok', `Expected status=ok, got ${res.data.status}`);
    await assert(res.data.service === 'totalreclaw-relay', `Expected service=totalreclaw-relay`);
    console.log('  [PASS] T1: Health check');
    passed++;
  } catch (err: any) {
    console.log(`  [FAIL] T1: Health check - ${err.message}`);
    failed++;
  }

  // ============================================================
  // Test 2: Register user
  // ============================================================
  let userId: string = '';
  try {
    const res = await request('POST', '/v1/register', {
      auth_key_hash: authKeyHash.toString('hex'),
      salt: salt.toString('hex'),
    });
    await assert(res.status === 200, `Expected 200, got ${res.status}`);
    await assert(res.data.success === true, `Expected success=true`);
    await assert(typeof res.data.user_id === 'string', `Expected user_id to be a string`);
    userId = res.data.user_id;
    console.log(`  [PASS] T2: Register user (user_id=${userId})`);
    passed++;
  } catch (err: any) {
    console.log(`  [FAIL] T2: Register user - ${err.message}`);
    failed++;
  }

  // ============================================================
  // Test 3: Registration is idempotent
  // ============================================================
  try {
    const res = await request('POST', '/v1/register', {
      auth_key_hash: authKeyHash.toString('hex'),
      salt: salt.toString('hex'),
    });
    await assert(res.status === 200, `Expected 200, got ${res.status}`);
    await assert(res.data.success === true, `Expected success=true`);
    await assert(res.data.user_id === userId, `Expected same user_id`);
    console.log('  [PASS] T3: Registration idempotency');
    passed++;
  } catch (err: any) {
    console.log(`  [FAIL] T3: Registration idempotency - ${err.message}`);
    failed++;
  }

  // ============================================================
  // Test 4: Billing status returns free tier
  // ============================================================
  try {
    const res = await request(
      'GET',
      `/v1/billing/status?wallet_address=${testWalletAddress}`,
      undefined,
      { Authorization: `Bearer ${tokenHex}` },
    );
    await assert(res.status === 200, `Expected 200, got ${res.status}`);
    await assert(res.data.success === true, `Expected success=true`);
    await assert(res.data.tier === 'free', `Expected tier=free, got ${res.data.tier}`);
    await assert(res.data.free_writes_used === 0, `Expected 0 writes used`);
    await assert(
      typeof res.data.free_writes_limit === 'number' && res.data.free_writes_limit > 0,
      `Expected positive free_writes_limit, got ${res.data.free_writes_limit}`,
    );
    console.log('  [PASS] T4: Billing status (free tier)');
    passed++;
  } catch (err: any) {
    console.log(`  [FAIL] T4: Billing status - ${err.message}`);
    failed++;
  }

  // ============================================================
  // Test 5: Features dict is correct for free tier
  // ============================================================
  try {
    const res = await request(
      'GET',
      `/v1/billing/status?wallet_address=${testWalletAddress}`,
      undefined,
      { Authorization: `Bearer ${tokenHex}` },
    );
    await assert(res.data.features !== undefined, 'Expected features dict');
    await assert(res.data.features.llm_dedup === true, 'Expected llm_dedup=true (enabled for all tiers)');
    await assert(res.data.features.custom_extract_interval === false, 'Expected custom_extract_interval=false');
    await assert(res.data.features.min_extract_interval === 5, `Expected min_extract_interval=5, got ${res.data.features.min_extract_interval}`);
    console.log('  [PASS] T5: Feature flags (free tier)');
    passed++;
  } catch (err: any) {
    console.log(`  [FAIL] T5: Feature flags - ${err.message}`);
    failed++;
  }

  // ============================================================
  // Test 6: Auth-gated endpoints reject unauthenticated requests
  // ============================================================
  try {
    const res1 = await request('GET', '/v1/billing/status?wallet_address=0xTest');
    await assert(res1.status === 401, `Expected 401 for billing/status, got ${res1.status}`);

    const res2 = await request('POST', '/v1/bundler', { jsonrpc: '2.0', method: 'test', params: [] });
    await assert(res2.status === 401, `Expected 401 for bundler, got ${res2.status}`);

    const res3 = await request('POST', '/v1/subgraph', { query: '{ test }' });
    await assert(res3.status === 401, `Expected 401 for subgraph, got ${res3.status}`);

    console.log('  [PASS] T6: Auth enforcement');
    passed++;
  } catch (err: any) {
    console.log(`  [FAIL] T6: Auth enforcement - ${err.message}`);
    failed++;
  }

  // ============================================================
  // Summary
  // ============================================================
  console.log(`\nResults: ${passed} passed, ${failed} failed out of ${passed + failed} tests`);

  if (failed > 0) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('Smoke test crashed:', err);
  process.exit(2);
});
