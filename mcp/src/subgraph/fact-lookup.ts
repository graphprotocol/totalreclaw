/**
 * Fact lookup for the id-based write tools (#701). Order:
 *   1. id tombstoned by this process            -> not found (no network)
 *   2. subgraph: active -> use it; inactive -> remember as tombstoned, not found
 *   3. subgraph absent -> this process's own write record (ciphertext)
 *   4. still absent and UUID-shaped -> short bounded re-poll of the subgraph
 * Pure logic over injected functions; nothing here touches disk.
 */

import type { FactLookup, SubgraphSearchFact } from './search.js';
import type { RecentWrites } from './recent-writes.js';

export const LOOKUP_RETRY_TOTAL_MS = 10_000;
export const LOOKUP_RETRY_POLL_MS = 2_000;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export interface RetryOptions {
  totalMs?: number;
  pollMs?: number;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

function resolveRetry(r: RetryOptions | undefined) {
  return {
    totalMs: r?.totalMs ?? LOOKUP_RETRY_TOTAL_MS,
    pollMs: r?.pollMs ?? LOOKUP_RETRY_POLL_MS,
    sleep: r?.sleep ?? ((ms: number) => new Promise<void>((res) => setTimeout(res, ms))),
    now: r?.now ?? Date.now,
  };
}

const defaultLog = (line: string) => console.error(line);

export function createRewriteLookup(opts: {
  owner: string;
  cache: RecentWrites;
  lookupSubgraph: (factId: string) => Promise<FactLookup>;
  retry?: RetryOptions;
  log?: (line: string) => void;
}): (factId: string) => Promise<SubgraphSearchFact | null> {
  const { owner, cache, lookupSubgraph } = opts;
  const log = opts.log ?? defaultLog;
  const { totalMs, pollMs, sleep, now } = resolveRetry(opts.retry);

  return async (id: string) => {
    if (cache.isTombstoned(owner, id)) return null;
    let r = await lookupSubgraph(id);
    if (r.state === 'active') return r.fact;
    if (r.state === 'inactive') {
      cache.recordTombstone(owner, id);
      return null;
    }
    const cached = cache.get(owner, id);
    if (cached) {
      log(`recent-writes: ${id.slice(0, 8)} served from this process's write record (subgraph has not indexed it yet)`);
      return cached;
    }
    if (!UUID_RE.test(id) || totalMs <= 0) return null;
    const start = now();
    const deadline = start + totalMs;
    while (now() < deadline) {
      await sleep(Math.min(pollMs, deadline - now()));
      r = await lookupSubgraph(id);
      if (r.state === 'active') {
        log(`recent-writes: ${id.slice(0, 8)} appeared on the subgraph after ${now() - start} ms`);
        return r.fact;
      }
      if (r.state === 'inactive') {
        cache.recordTombstone(owner, id);
        return null;
      }
    }
    log(`recent-writes: ${id.slice(0, 8)} not on the subgraph after ${now() - start} ms`);
    return null;
  };
}

/** Same shape for forget's existence check; lookup errors propagate at once (no retry). */
export function createForgetLookup<F extends { isActive?: boolean }>(opts: {
  owner: string;
  cache: RecentWrites;
  fetchForForget: (factId: string) => Promise<F | null>; // throws on lookup failure
  retry?: RetryOptions;
  log?: (line: string) => void;
}): (factId: string) => Promise<F | { isActive?: boolean; encryptedBlob?: string | null } | null> {
  const { owner, cache, fetchForForget } = opts;
  const log = opts.log ?? defaultLog;
  const { totalMs, pollMs, sleep, now } = resolveRetry(opts.retry);

  return async (id: string) => {
    if (cache.isTombstoned(owner, id)) return { isActive: false };
    let fact = await fetchForForget(id);
    if (fact) {
      if (fact.isActive === false) cache.recordTombstone(owner, id);
      return fact;
    }
    const cached = cache.get(owner, id);
    if (cached) {
      log(`recent-writes: ${id.slice(0, 8)} served from this process's write record (subgraph has not indexed it yet)`);
      return { isActive: true, encryptedBlob: cached.encryptedBlob };
    }
    if (!UUID_RE.test(id) || totalMs <= 0) return null;
    const start = now();
    const deadline = start + totalMs;
    while (now() < deadline) {
      await sleep(Math.min(pollMs, deadline - now()));
      fact = await fetchForForget(id);
      if (fact) {
        if (fact.isActive === false) cache.recordTombstone(owner, id);
        return fact;
      }
    }
    log(`recent-writes: ${id.slice(0, 8)} not on the subgraph after ${now() - start} ms`);
    return null;
  };
}
