/**
 * Unit tests for assertLocalOnlyLLMConfig.
 *
 * Run with: npx tsx local-only-guard.test.ts
 *
 * TAP-style output (same pattern as consolidation.test.ts).
 *
 * Note: plugin is ESM ("type": "module") and tsx runs these files as ESM.
 * We drive behavior exclusively via initLLMClient() — the legacy
 * resolveLLMConfig() fallback reads CONFIG which is frozen at module load,
 * so runtime env overrides don't flow through. initLLMClient() is the
 * production path, so this is the correct surface to test.
 */

import { assertLocalOnlyLLMConfig, initLLMClient } from './llm-client.js';

// ---------------------------------------------------------------------------
// Test scaffolding
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;

function test(name: string, fn: () => void) {
  try {
    fn();
    passed++;
    console.log(`ok - ${name}`);
  } catch (e) {
    failed++;
    const msg = e instanceof Error ? e.message : String(e);
    console.log(`not ok - ${name}`);
    console.log(`  ${msg}`);
  }
}

function assertThrows(fn: () => void, match?: RegExp | string): void {
  try {
    fn();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (match) {
      const ok = typeof match === 'string' ? msg.includes(match) : match.test(msg);
      if (!ok) throw new Error(`Expected error to match ${match}, got: ${msg}`);
    }
    return;
  }
  throw new Error('Expected fn to throw, but it did not');
}

function assertNotThrows(fn: () => void): void {
  try {
    fn();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    throw new Error(`Expected fn NOT to throw, but got: ${msg}`);
  }
}

function setEnv(key: string, value: string | undefined) {
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
}

/** Helper: initialize llm-client with a single OpenAI-compatible provider. */
function initWith(baseUrl: string) {
  initLLMClient({
    openclawProviders: {
      openai: {
        baseUrl,
        apiKey: 'test-key',
        api: 'openai',
        models: [{ id: 'test-model' }],
      },
    },
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test('guard is a no-op when TOTALRECLAW_IMPORT_LOCAL_ONLY is unset', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', undefined);
  initWith('https://api.openai.com/v1');
  assertNotThrows(() => assertLocalOnlyLLMConfig('test'));
});

test('guard is a no-op when TOTALRECLAW_IMPORT_LOCAL_ONLY="0"', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', '0');
  initWith('https://api.openai.com/v1');
  assertNotThrows(() => assertLocalOnlyLLMConfig('test'));
});

test('guard passes with http://127.0.0.1:8080/v1', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', '1');
  initWith('http://127.0.0.1:8080/v1');
  assertNotThrows(() => assertLocalOnlyLLMConfig('test'));
});

test('guard passes with http://localhost:8080/v1', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', '1');
  initWith('http://localhost:8080/v1');
  assertNotThrows(() => assertLocalOnlyLLMConfig('test'));
});

test('guard passes with http://[::1]:8080/v1 (IPv6 loopback)', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', '1');
  initWith('http://[::1]:8080/v1');
  assertNotThrows(() => assertLocalOnlyLLMConfig('test'));
});

test('guard throws for https://api.openai.com/v1', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', '1');
  initWith('https://api.openai.com/v1');
  assertThrows(
    () => assertLocalOnlyLLMConfig('sensitive import'),
    /refusing to sensitive import/,
  );
});

test('guard throws for https://api.z.ai/api/coding/paas/v4', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', '1');
  initWith('https://api.z.ai/api/coding/paas/v4');
  assertThrows(
    () => assertLocalOnlyLLMConfig('test'),
    /api\.z\.ai/,
  );
});

test('guard throws for any non-loopback hostname', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', '1');
  initWith('http://internal.corp:8080/v1');
  assertThrows(
    () => assertLocalOnlyLLMConfig('test'),
    /internal\.corp/,
  );
});

test('guard error message mentions the context argument', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', '1');
  initWith('https://api.anthropic.com/v1');
  assertThrows(
    () => assertLocalOnlyLLMConfig('chatgpt import'),
    /refusing to chatgpt import/,
  );
});

test('guard error message suggests remediation', () => {
  setEnv('TOTALRECLAW_IMPORT_LOCAL_ONLY', '1');
  initWith('https://api.openai.com/v1');
  assertThrows(
    () => assertLocalOnlyLLMConfig('test'),
    /OPENAI_BASE_URL/,
  );
});

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
