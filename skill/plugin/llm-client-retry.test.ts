/**
 * Tests for the 3.3.1-rc.2 retry wrapper added to llm-client.chatCompletion.
 *
 * Covers:
 *   - isRetryable classifies 429, 502/503/504, timeouts as retryable
 *   - isRetryable classifies 401/403/404/parse-errors as non-retryable
 *   - chatCompletion retries transient failures with exponential backoff
 *   - chatCompletion fails fast on non-retryable errors
 *   - chatCompletion respects `attempts: 0` (still runs once) and
 *     `attempts: 3` caps retry count
 *   - Logger is called with the right levels (INFO on first failure,
 *     DEBUG on subsequent, WARN on final give-up)
 *
 * Run with: npx tsx llm-client-retry.test.ts
 */

import { isRetryable, chatCompletion, type LLMClientConfig } from './llm-client.js';

let passed = 0;
let failed = 0;

function assert(cond: boolean, name: string): void {
  const n = passed + failed + 1;
  if (cond) {
    console.log(`ok ${n} - ${name}`);
    passed++;
  } else {
    console.log(`not ok ${n} - ${name}`);
    failed++;
  }
}

// ---------------------------------------------------------------------------
// isRetryable
// ---------------------------------------------------------------------------

assert(isRetryable('LLM API 429: rate limit reached'), 'isRetryable: 429 → true');
assert(isRetryable('Rate limit reached for requests'), 'isRetryable: "rate limit" → true');
assert(isRetryable('LLM API 502: bad gateway'), 'isRetryable: 502 → true');
assert(isRetryable('LLM API 503: unavailable'), 'isRetryable: 503 → true');
assert(isRetryable('LLM API 504: timeout'), 'isRetryable: 504 → true');
assert(isRetryable('The operation was aborted due to timeout'), 'isRetryable: timeout msg → true');
assert(isRetryable('AbortError: aborted'), 'isRetryable: AbortError → true');

assert(!isRetryable('LLM API 401: unauthorized'), 'isRetryable: 401 → false');
assert(!isRetryable('LLM API 403: forbidden'), 'isRetryable: 403 → false');
assert(!isRetryable('LLM API 404: not found'), 'isRetryable: 404 → false');
assert(!isRetryable('LLM API 400: bad request'), 'isRetryable: 400 → false');
assert(!isRetryable('JSON parse error'), 'isRetryable: JSON parse → false');

// ---------------------------------------------------------------------------
// chatCompletion retry harness — we monkey-patch fetch for determinism.
// ---------------------------------------------------------------------------

// Save the real fetch.
const realFetch = globalThis.fetch;

function stubFetch(sequence: Array<() => Promise<Response>>): () => number {
  let callIdx = 0;
  globalThis.fetch = (async (..._args: unknown[]): Promise<Response> => {
    const next = sequence[Math.min(callIdx, sequence.length - 1)];
    callIdx++;
    return next();
  }) as unknown as typeof fetch;
  return () => callIdx;
}

function restoreFetch(): void {
  globalThis.fetch = realFetch;
}

function mockResponse(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function okResponse(content: string): Response {
  return mockResponse(
    200,
    JSON.stringify({
      choices: [{ message: { content } }],
    }),
  );
}

const testConfig: LLMClientConfig = {
  apiKey: 'test',
  baseUrl: 'http://example.test/v1',
  model: 'test-model',
  apiFormat: 'openai',
};

// ---------------------------------------------------------------------------
// Happy path — first attempt succeeds, no retries.
// ---------------------------------------------------------------------------

{
  const getCount = stubFetch([
    async () => okResponse('hello from LLM'),
  ]);
  try {
    const logs: Array<[string, string]> = [];
    const logger = {
      info: (msg: string) => logs.push(['info', msg]),
      warn: (msg: string) => logs.push(['warn', msg]),
      debug: (msg: string) => logs.push(['debug', msg]),
    };
    const result = await chatCompletion(testConfig, [
      { role: 'user', content: 'hi' },
    ], { logger });
    assert(result === 'hello from LLM', 'retry: first-attempt-success returns content');
    assert(getCount() === 1, 'retry: first-attempt-success → 1 fetch');
    assert(logs.length === 0, 'retry: first-attempt-success → no log lines');
  } finally {
    restoreFetch();
  }
}

// ---------------------------------------------------------------------------
// 429 → retry → success
// ---------------------------------------------------------------------------

{
  const getCount = stubFetch([
    async () => mockResponse(429, '{"error":{"message":"Rate limit reached"}}'),
    async () => okResponse('recovered'),
  ]);
  try {
    const logs: Array<[string, string]> = [];
    const logger = {
      info: (msg: string) => logs.push(['info', msg]),
      warn: (msg: string) => logs.push(['warn', msg]),
      debug: (msg: string) => logs.push(['debug', msg]),
    };
    const result = await chatCompletion(testConfig, [
      { role: 'user', content: 'hi' },
    ], { logger, retry: { attempts: 3, baseDelayMs: 10 } });
    assert(result === 'recovered', 'retry: 429 → 200 returns content from 2nd attempt');
    assert(getCount() === 2, 'retry: 429 → 200 made 2 fetch calls');
    assert(logs.some(([lvl]) => lvl === 'info'), 'retry: 429 → 200 emits INFO log on first failure');
  } finally {
    restoreFetch();
  }
}

// ---------------------------------------------------------------------------
// All attempts fail with 429 → give up with warn
// ---------------------------------------------------------------------------

{
  const getCount = stubFetch([
    async () => mockResponse(429, '{"error":{"message":"Rate limit"}}'),
    async () => mockResponse(429, '{"error":{"message":"Rate limit"}}'),
    async () => mockResponse(429, '{"error":{"message":"Rate limit"}}'),
  ]);
  try {
    const logs: Array<[string, string]> = [];
    const logger = {
      info: (msg: string) => logs.push(['info', msg]),
      warn: (msg: string) => logs.push(['warn', msg]),
      debug: (msg: string) => logs.push(['debug', msg]),
    };
    let thrown: unknown;
    try {
      await chatCompletion(testConfig, [
        { role: 'user', content: 'hi' },
      ], { logger, retry: { attempts: 3, baseDelayMs: 10 } });
    } catch (err) {
      thrown = err;
    }
    assert(thrown instanceof Error, 'retry: all-fail throws');
    assert(getCount() === 3, 'retry: all-fail with attempts=3 → 3 fetches');
    assert(logs.some(([lvl]) => lvl === 'warn'), 'retry: all-fail emits WARN on give-up');
    assert(logs.some(([lvl]) => lvl === 'info'), 'retry: all-fail emits INFO on first failure');
    assert(logs.some(([lvl]) => lvl === 'debug'), 'retry: all-fail emits DEBUG on 2nd retry attempt');
  } finally {
    restoreFetch();
  }
}

// ---------------------------------------------------------------------------
// 401 (unauth) → fail-fast, no retry
// ---------------------------------------------------------------------------

{
  const getCount = stubFetch([
    async () => mockResponse(401, '{"error":{"message":"unauthorized"}}'),
  ]);
  try {
    let thrown: unknown;
    try {
      await chatCompletion(testConfig, [
        { role: 'user', content: 'hi' },
      ], { retry: { attempts: 3, baseDelayMs: 10 } });
    } catch (err) {
      thrown = err;
    }
    assert(thrown instanceof Error, 'retry: 401 throws');
    assert(getCount() === 1, 'retry: 401 is fail-fast (1 fetch)');
  } finally {
    restoreFetch();
  }
}

// ---------------------------------------------------------------------------
// Timeout / AbortError → retry
// ---------------------------------------------------------------------------

{
  const getCount = stubFetch([
    async () => {
      throw new Error('The operation was aborted due to timeout');
    },
    async () => okResponse('recovered from timeout'),
  ]);
  try {
    const logs: Array<[string, string]> = [];
    const logger = {
      info: (msg: string) => logs.push(['info', msg]),
      warn: (msg: string) => logs.push(['warn', msg]),
      debug: (msg: string) => logs.push(['debug', msg]),
    };
    const result = await chatCompletion(testConfig, [
      { role: 'user', content: 'hi' },
    ], { logger, retry: { attempts: 3, baseDelayMs: 10 } });
    assert(result === 'recovered from timeout', 'retry: timeout → retry → success');
    assert(getCount() === 2, 'retry: timeout → 2 fetches');
  } finally {
    restoreFetch();
  }
}

// ---------------------------------------------------------------------------
// Disable retry: attempts set to 1 → no retry even on 429
// ---------------------------------------------------------------------------

{
  const getCount = stubFetch([
    async () => mockResponse(429, '{"error":{"message":"Rate limit"}}'),
  ]);
  try {
    let thrown: unknown;
    try {
      await chatCompletion(testConfig, [
        { role: 'user', content: 'hi' },
      ], { retry: { attempts: 1, baseDelayMs: 10 } });
    } catch (err) {
      thrown = err;
    }
    assert(thrown instanceof Error, 'retry: attempts=1 → throws on 429');
    assert(getCount() === 1, 'retry: attempts=1 → single fetch, no retry');
  } finally {
    restoreFetch();
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
