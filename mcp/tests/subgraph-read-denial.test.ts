/**
 * subgraph-read-denial.test.ts — internal #662 / D25.
 *
 * A relay read denial (429 read_quota_exceeded, 429 rate_limited, legacy 403
 * quota_exceeded) must surface as a typed error, pause reads until a
 * deadline (zero HTTP while paused, one log per episode), and never become
 * an empty result. Other non-2xx answers are plain errors with no pause.
 */

import {
  searchSubgraph,
  searchSubgraphBroadened,
  lookupFactById,
  getOwnerFactCount,
  fetchFactForForget,
  RelayReadBlocked,
  RelayReadError,
  RelayReadQuotaExceeded,
  RelayRateLimited,
} from '../src/subgraph/search';
import {
  classifySubgraphError,
  getReadBlock,
  readBlockPayload,
  resetReadBlock,
  setReadBlockLogger,
  readBlockToolResult,
  withReadPauseNotice,
} from '../src/subgraph/read-block';

const RELAY = 'https://relay.test';
const SA = '0xAbC0000000000000000000000000000000000001';
const OTHER = '0xAbC0000000000000000000000000000000000002';
const AUTH = 'deadbeef'.repeat(8);

const QUOTA_BODY = {
  success: false,
  error: 'read_quota_exceeded',
  error_code: 'read_quota_exceeded',
  message: 'Monthly read limit reached (1000/1000). Resets 2099-01-01T00:00:00.000Z.',
  tier: 'free',
  limit: 1000,
  used: 1000,
  resets_at: '2099-01-01T00:00:00.000Z',
  retry_after: 4_000_000,
  upgrade_url: 'https://totalreclaw.xyz/pricing',
};
const RATE_BODY = {
  success: false,
  error: 'Rate limit exceeded. Try again later.',
  error_code: 'rate_limited',
  limit_scope: 'wallet',
  retry_after: 120,
};

const realFetch = globalThis.fetch;
let calls = 0;
let logs: string[] = [];
let respond: () => Response;

beforeEach(() => {
  jest.useFakeTimers({ now: new Date('2026-11-25T12:00:00Z'), doNotFake: ['nextTick', 'setImmediate', 'queueMicrotask'] });
  delete process.env.TOTALRECLAW_SUBGRAPH_URL;
  delete process.env.TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS;
  resetReadBlock();
  logs = [];
  setReadBlockLogger((l) => logs.push(l));
  calls = 0;
  globalThis.fetch = (async () => {
    calls++;
    return respond();
  }) as typeof fetch;
  jest.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => {
  globalThis.fetch = realFetch;
  jest.useRealTimers();
  jest.restoreAllMocks();
  resetReadBlock();
});

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers });
const ok = () => json(200, { data: { blindIndexes: [{ id: 'b1', fact: { id: 'f1' } }], facts: [{ id: 'f1', isActive: true }] } });

describe('classifySubgraphError', () => {
  it('429 read_quota_exceeded carries the relay fields', () => {
    const e = classifySubgraphError(429, JSON.stringify(QUOTA_BODY), '4000000');
    expect(e).toBeInstanceOf(RelayReadQuotaExceeded);
    const q = e as RelayReadQuotaExceeded;
    expect(q.legacy).toBe(false);
    expect(q.upgradeUrl).toBe('https://totalreclaw.xyz/pricing');
    expect(q.tier).toBe('free');
    expect(q.limit).toBe(1000);
    expect(q.used).toBe(1000);
    expect(q.resetsAt).toBe('2099-01-01T00:00:00.000Z');
    expect(q.retryAfterS).toBe(4_000_000);
    expect(q.relayMessage).toContain('Monthly read limit reached');
  });

  it('429 rate_limited, and a legacy 429 text body', () => {
    const e = classifySubgraphError(429, JSON.stringify(RATE_BODY));
    expect(e).toBeInstanceOf(RelayRateLimited);
    expect((e as RelayRateLimited).limitScope).toBe('wallet');
    expect((e as RelayRateLimited).retryAfterS).toBe(120);
    const legacy = classifySubgraphError(429, 'Too Many Requests', '60');
    expect(legacy).toBeInstanceOf(RelayRateLimited);
    expect((legacy as RelayRateLimited).retryAfterS).toBe(60);
  });

  it('legacy 403 quota_exceeded is a quota denial; the same code on 500 is not', () => {
    const e = classifySubgraphError(403, JSON.stringify({ error: 'quota_exceeded', message: 'Read limit reached', upgrade_url: 'u' }));
    expect(e).toBeInstanceOf(RelayReadQuotaExceeded);
    expect((e as RelayReadQuotaExceeded).legacy).toBe(true);
    expect(classifySubgraphError(500, JSON.stringify({ error: 'quota_exceeded' }))).not.toBeInstanceOf(RelayReadBlocked);
  });

  it('5xx and non-JSON bodies are plain errors', () => {
    const e = classifySubgraphError(502, '<html>Bad gateway</html>');
    expect(e).toBeInstanceOf(RelayReadError);
    expect(e).not.toBeInstanceOf(RelayReadBlocked);
    expect(e.statusCode).toBe(502);
    expect(e.message).toBe('TotalReclaw relay read failed (HTTP 502): unknown error');
  });
});

describe('read denial through the search path', () => {
  it('429 read_quota_exceeded throws a distinct error with the relay fields, never []', async () => {
    respond = () => json(429, QUOTA_BODY, { 'Retry-After': '4000000' });
    const err = await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH).catch((e) => e);
    expect(err).toBeInstanceOf(RelayReadQuotaExceeded);
    const payload = readBlockPayload(err, getReadBlock(SA));
    expect(payload).not.toHaveProperty('memories');
    expect(payload.error_code).toBe('read_quota_exceeded');
    expect(payload.error).toContain('monthly memory-read allowance');
    expect(payload.error).toContain('it resets on 2099-01-01 (UTC)');
    expect(payload.relay_message).toContain('Monthly read limit reached');
    expect(payload.relay_retry_after_seconds).toBe(4_000_000);
    expect(payload.upgrade_url).toBe('https://totalreclaw.xyz/pricing');
    expect(payload.resets_at).toBe('2099-01-01T00:00:00.000Z');
    expect(payload.retry_after_seconds).toBe(900);
  });

  it('pauses reads: no HTTP until the deadline, then one probe, then resumes on success', async () => {
    respond = () => json(429, QUOTA_BODY);
    await expect(searchSubgraph(SA, ['t1'], 100, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadQuotaExceeded);
    expect(calls).toBe(1);

    // Paused: every read path fails without touching the network.
    await expect(searchSubgraph(SA, ['t1'], 100, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadQuotaExceeded);
    await expect(searchSubgraphBroadened(SA, 10, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadQuotaExceeded);
    await expect(lookupFactById(SA, 'f1', RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadQuotaExceeded);
    await expect(getOwnerFactCount(SA, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadQuotaExceeded);
    await expect(fetchFactForForget(SA, 'f1', RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadQuotaExceeded);
    expect(calls).toBe(1);

    // Just before the 15 minute re-probe: still paused.
    jest.setSystemTime(Date.now() + 899_000);
    await expect(searchSubgraph(SA, ['t1'], 100, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadQuotaExceeded);
    expect(calls).toBe(1);

    // Deadline passed: one probe goes out; a success resumes normal reads.
    jest.setSystemTime(Date.now() + 2_000);
    respond = ok;
    const rows = await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH);
    expect(rows.map((r) => r.id)).toEqual(['f1']);
    expect(calls).toBeGreaterThan(1);
    expect(getReadBlock(SA)).toBeNull();
    const before = calls;
    await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH);
    expect(calls).toBeGreaterThan(before);
  });

  it('a probe that is denied again re-pauses without a second log line', async () => {
    respond = () => json(429, QUOTA_BODY);
    await expect(searchSubgraph(SA, ['t1'], 100, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadBlocked);
    jest.setSystemTime(Date.now() + 901_000);
    await expect(searchSubgraph(SA, ['t1'], 100, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadBlocked);
    expect(calls).toBe(2);
    expect(getReadBlock(SA)).not.toBeNull();
    expect(logs).toHaveLength(1);
  });

  it('logs once per episode even when parallel queries are all denied', async () => {
    respond = () => json(429, QUOTA_BODY);
    const trapdoors = Array.from({ length: 40 }, (_, i) => `t${i}`); // 8 chunks
    await expect(searchSubgraph(SA, trapdoors, 100, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadBlocked);
    expect(logs).toHaveLength(1);
    expect(logs[0]).toContain('read_quota');
    // Fan-out stops: at most the in-flight batch, far fewer than all chunks plus retries.
    expect(calls).toBeLessThanOrEqual(6);
    // A new episode after a success logs again.
    jest.setSystemTime(Date.now() + 901_000);
    respond = ok;
    await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH);
    respond = () => json(429, RATE_BODY);
    await expect(searchSubgraph(SA, ['t1'], 100, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadBlocked);
    expect(logs).toHaveLength(2);
  });

  it('429 rate_limited pauses for the relay retry_after (30 s floor) and the text names minutes', async () => {
    respond = () => json(429, RATE_BODY);
    const err = await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH).catch((e) => e);
    expect(err).toBeInstanceOf(RelayRateLimited);
    const payload = readBlockPayload(err, getReadBlock(SA));
    expect(payload.error_code).toBe('rate_limited');
    expect(payload.error).toBe('Memory lookups are briefly paused (too many requests). They resume automatically in about 2 minutes.');
    expect(payload.retry_after_seconds).toBe(120);
    expect(payload).not.toHaveProperty('memories');

    jest.setSystemTime(Date.now() + 119_000);
    await expect(searchSubgraph(SA, ['t1'], 100, RELAY, AUTH)).rejects.toBeInstanceOf(RelayRateLimited);
    expect(calls).toBe(1);
    jest.setSystemTime(Date.now() + 2_000);
    respond = ok;
    await expect(searchSubgraph(SA, ['t1'], 100, RELAY, AUTH)).resolves.toHaveLength(1);
  });

  it('5xx throws a plain read error and does not pause', async () => {
    respond = () => json(502, { error: 'bad gateway', message: 'upstream down' });
    const err = await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH).catch((e) => e);
    expect(err).toBeInstanceOf(RelayReadError);
    expect(err).not.toBeInstanceOf(RelayReadBlocked);
    expect(err.message).toBe('TotalReclaw relay read failed (HTTP 502): upstream down');
    expect(getReadBlock(SA)).toBeNull();
    expect(logs).toHaveLength(0);
    await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH).catch(() => {});
    expect(calls).toBe(2); // not short-circuited
  });

  it('lookups and counts keep treating a plain 5xx as no data', async () => {
    respond = () => json(500, { error: 'boom' });
    await expect(lookupFactById(SA, 'f1', RELAY, AUTH)).resolves.toEqual({ state: 'absent' });
    await expect(getOwnerFactCount(SA, RELAY, AUTH)).resolves.toBe(0);
  });

  it('the pause is per wallet', async () => {
    respond = () => json(429, QUOTA_BODY);
    await expect(searchSubgraph(SA, ['t1'], 100, RELAY, AUTH)).rejects.toBeInstanceOf(RelayReadBlocked);
    respond = ok;
    await expect(searchSubgraph(OTHER, ['t1'], 100, RELAY, AUTH)).resolves.toHaveLength(1);
  });

  it('the re-probe cadence honors TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS and the reset date', async () => {
    process.env.TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS = '60';
    respond = () => json(429, QUOTA_BODY);
    await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH).catch(() => {});
    expect(readBlockPayload(classifySubgraphError(429, JSON.stringify(QUOTA_BODY)) as RelayReadBlocked, getReadBlock(SA)).retry_after_seconds).toBe(60);
  });

  it('success path is unchanged', async () => {
    respond = ok;
    const rows = await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH);
    expect(rows.map((r) => r.id)).toEqual(['f1']);
    expect(logs).toHaveLength(0);
  });
});

describe('tool results', () => {
  it('recall result for a denial has the error and relay fields, no memories key', async () => {
    respond = () => json(429, QUOTA_BODY);
    const err = await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH).catch((e) => e);
    const res = readBlockToolResult(err, SA, { latency_ms: 5, mode: 'subgraph' });
    const body = JSON.parse(res.content[0].text);
    expect(body).not.toHaveProperty('memories');
    expect(body.error_code).toBe('read_quota_exceeded');
    expect(body.upgrade_url).toBe('https://totalreclaw.xyz/pricing');
    expect(body.mode).toBe('subgraph');
    expect(body.instruction).toContain('Do NOT say they have no memories');
  });

  it('status gains memory_reads only while reads are paused', async () => {
    const status = () => ({ content: [{ type: 'text', text: JSON.stringify({ tier: 'free', formatted: 'Tier: Free' }) }] });
    expect(JSON.parse(withReadPauseNotice(status(), SA).content[0].text)).not.toHaveProperty('memory_reads');

    respond = () => json(429, RATE_BODY);
    await searchSubgraph(SA, ['t1'], 100, RELAY, AUTH).catch(() => {});
    const body = JSON.parse(withReadPauseNotice(status(), SA).content[0].text);
    expect(body.tier).toBe('free');
    expect(body.memory_reads.paused).toBe(true);
    expect(body.memory_reads.error_code).toBe('rate_limited');
    expect(body.formatted).toContain('Memory lookups: Memory lookups are briefly paused');
    // Another wallet is unaffected.
    expect(JSON.parse(withReadPauseNotice(status(), OTHER).content[0].text)).not.toHaveProperty('memory_reads');
  });
});
