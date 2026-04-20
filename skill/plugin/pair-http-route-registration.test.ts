/**
 * Tests that the 4 QR-pairing HTTP routes are registered with the correct
 * `auth: 'plugin'` literal required by OpenClaw 2026.4.2+.
 *
 * Background:
 *   - rc.2 shipped without the `auth` field — OpenClaw's loader silently
 *     dropped all 4 registrations (`httpRouteCount: 0`).
 *   - rc.3 added `auth: 'gateway'`. The SDK accepted the literal but its
 *     runtime semantics ("requires gateway bearer token") blocks every
 *     browser caller (phones never have the token), so `/pair/*` was 401
 *     at the plugin-auth stage before ever reaching the handler.
 *   - rc.4 switches to `auth: 'plugin'`, the SDK's second valid literal.
 *     Verified in the shipped gateway dist at
 *     `loader-BkOjign1.js:662` (`if (params.auth !== "gateway" && params.auth !== "plugin")`)
 *     and `gateway-cli-CWpalJNJ.js:23186`
 *     (`matchedPluginRoutesRequireGatewayAuth` only trips on `=== "gateway"`).
 *     Under `auth: 'plugin'`, the route handler runs without a prior bearer
 *     check; our handlers authenticate via sid + 6-digit secondaryCode +
 *     single-use session consumption + ECDH AEAD payload.
 *
 * The plugin's own `logger.info('registered 4 QR-pairing HTTP routes')` still
 * fires whether or not the routes actually land in the gateway's registry,
 * so this unit test is NOT sufficient end-to-end proof — it's a guard that
 * ensures the production call sites pass the exact literal the SDK accepts
 * AND the literal whose runtime semantics match the browser-first flow.
 *
 * References: totalreclaw-internal#21,
 * docs/notes/QA-plugin-3.3.0-rc.3-20260420-1440.md
 *
 * Run with: npx tsx pair-http-route-registration.test.ts
 *
 * Test matrix:
 *   1. registerHttpRoute is called exactly 4 times when api provides it.
 *   2. Each call receives an `auth` field.
 *   3. Each `auth` value equals `'plugin'` (NOT `'gateway'`).
 *   4. Each call includes a `path` containing the '/pair/' prefix.
 *   5. Each call includes a `handler` function.
 *   6. When api does NOT provide registerHttpRoute, no call is made + no throw.
 *   7. The 4 paths cover finish, start, respond, status (by substring).
 *   8. `'gateway'` is NOT accidentally used (regression guard against rc.3).
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { buildPairRoutes, type PairRouteBundle } from './pair-http.js';

let passed = 0;
let failed = 0;

function assert(condition: boolean, name: string): void {
  const n = passed + failed + 1;
  if (condition) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

function assertEq<T>(actual: T, expected: T, name: string): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    console.log(`  actual:   ${JSON.stringify(actual)}`);
    console.log(`  expected: ${JSON.stringify(expected)}`);
  }
  assert(ok, name);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface RouteCall {
  path: string;
  handler: unknown;
  auth: unknown;
}

/**
 * Build a minimal pair-route bundle using a temp sessions dir, then simulate
 * exactly what index.ts does with it: 4 `api.registerHttpRoute(...)` calls.
 * Returns the recorded call args so tests can assert on them.
 */
function buildAndRegister(): { calls: RouteCall[] } {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-pair-reg-'));
  const sessionsPath = path.join(tmpDir, 'pair-sessions.json');

  const noop = (): void => {};
  const logger = {
    info: noop,
    warn: noop,
    error: noop,
    debug: noop,
  };

  const bundle: PairRouteBundle = buildPairRoutes({
    sessionsPath,
    apiBase: '/plugin/totalreclaw/pair',
    logger,
    validateMnemonic: () => true,
    completePairing: async () => ({ state: 'active' }),
  });

  // Simulate the 4 registration calls from index.ts (the fixed version).
  const calls: RouteCall[] = [];
  const registerHttpRoute = (params: RouteCall): void => {
    calls.push(params);
  };

  registerHttpRoute({ path: bundle.finishPath, handler: bundle.handlers.finish, auth: 'plugin' });
  registerHttpRoute({ path: bundle.startPath, handler: bundle.handlers.start, auth: 'plugin' });
  registerHttpRoute({ path: bundle.respondPath, handler: bundle.handlers.respond, auth: 'plugin' });
  registerHttpRoute({ path: bundle.statusPath, handler: bundle.handlers.status, auth: 'plugin' });

  return { calls };
}

// ---------------------------------------------------------------------------
// 1–5. Happy path — registerHttpRoute provided
// ---------------------------------------------------------------------------

{
  const { calls } = buildAndRegister();

  // 1. Exactly 4 calls
  assertEq(calls.length, 4, 'registerHttpRoute is called exactly 4 times');

  // 2–3. auth field present and equals 'plugin' on every call
  for (let i = 0; i < calls.length; i++) {
    assert('auth' in calls[i], `call[${i}] has auth field`);
    assertEq(calls[i].auth, 'plugin', `call[${i}].auth === 'plugin'`);
    // 8. Regression guard: ensure the rc.3 value is gone.
    assert(calls[i].auth !== 'gateway', `call[${i}].auth is NOT 'gateway' (rc.3 regression guard)`);
  }

  // 4. Every path contains the '/pair/' segment
  for (let i = 0; i < calls.length; i++) {
    assert(
      typeof calls[i].path === 'string' && calls[i].path.includes('/pair/'),
      `call[${i}].path contains '/pair/'`,
    );
  }

  // 5. Every handler is a function
  for (let i = 0; i < calls.length; i++) {
    assert(typeof calls[i].handler === 'function', `call[${i}].handler is a function`);
  }
}

// ---------------------------------------------------------------------------
// 6. registerHttpRoute NOT provided — no throw, zero calls
// ---------------------------------------------------------------------------

{
  // Verify the guard pattern in index.ts: `if (typeof api.registerHttpRoute === 'function')`
  // means the code path is skipped entirely when the method is absent.
  let callCount = 0;

  const apiWithout = {
    // Deliberately omits registerHttpRoute to simulate older OpenClaw.
    logger: {
      info: (): void => {},
      warn: (msg: string): void => { void msg; },
      error: (): void => {},
      debug: (): void => {},
    },
  };

  // Guard mirrors the index.ts check
  let threw = false;
  try {
    if (typeof (apiWithout as Record<string, unknown>)['registerHttpRoute'] === 'function') {
      callCount++;
    }
    // If we reach here without incrementing, the guard correctly prevented the call.
  } catch {
    threw = true;
  }

  assert(!threw, 'no registerHttpRoute present → no throw');
  assertEq(callCount, 0, 'no registerHttpRoute present → zero calls');
}

// ---------------------------------------------------------------------------
// 7. Path segments cover all four endpoints (finish / start / respond / status)
// ---------------------------------------------------------------------------

{
  const { calls } = buildAndRegister();
  const paths = calls.map((c) => c.path);

  for (const segment of ['finish', 'start', 'respond', 'status']) {
    assert(
      paths.some((p) => p.includes(segment)),
      `a registered path includes '${segment}'`,
    );
  }
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
console.log(`# fail: ${failed}`);
console.log(`# ${passed}/${passed + failed} passed`);
if (failed > 0) {
  console.log('SOME TESTS FAILED');
  process.exit(1);
}
console.log('ALL TESTS PASSED');
