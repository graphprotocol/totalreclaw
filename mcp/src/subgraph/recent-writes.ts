/**
 * In-memory record of what THIS process wrote, so id-based tools (pin,
 * unpin, retype, set_scope, forget) work on a fact saved seconds ago, before
 * the subgraph indexes it (#701).
 *
 * Holds ciphertext and ids only: no plaintext, no keys. Never persisted
 * (this module must not import fs), bounded by TTL and entry count, keyed by
 * owner, and cleared on account swap by the caller.
 */

import type { SubgraphSearchFact } from './search.js';

export const RECENT_WRITES_TTL_MS = 900_000; // 15 min, same as Hermes write_gate
export const RECENT_WRITES_MAX_ENTRIES = 256;
/** The blob pin / retype / forget / remember write for a tombstone. */
export const TOMBSTONE_BLOB_HEX = Buffer.from('tombstone').toString('hex');

export interface RecentWriteInput {
  id: string;
  encryptedBlob: string;
  decayScore: number | string;
  timestamp: string;
}

interface Stamped<T> {
  at: number;
  value: T;
}

export class RecentWrites {
  private readonly ttlMs: number;
  private readonly maxEntries: number;
  private readonly now: () => number;
  private entries = new Map<string, Stamped<SubgraphSearchFact>>();
  private tombstones = new Map<string, Stamped<true>>();

  constructor(opts: { ttlMs?: number; maxEntries?: number; now?: () => number } = {}) {
    this.ttlMs = opts.ttlMs ?? RECENT_WRITES_TTL_MS;
    this.maxEntries = opts.maxEntries ?? RECENT_WRITES_MAX_ENTRIES;
    this.now = opts.now ?? Date.now;
  }

  private key(owner: string, factId: string): string {
    return `${owner.toLowerCase()}|${factId}`;
  }

  private prune(): void {
    const cutoff = this.now() - this.ttlMs;
    for (const m of [this.entries, this.tombstones] as Array<Map<string, Stamped<unknown>>>) {
      for (const [k, v] of m) {
        if (v.at < cutoff) m.delete(k);
        else break; // insertion order == age order
      }
      while (m.size > this.maxEntries) {
        const oldest = m.keys().next().value as string;
        m.delete(oldest);
      }
    }
  }

  recordWrite(owner: string, input: RecentWriteInput): void {
    this.prune();
    const k = this.key(owner, input.id);
    this.tombstones.delete(k);
    const parsed = Math.floor(Date.parse(input.timestamp) / 1000);
    const seconds = Number.isNaN(parsed) ? Math.floor(this.now() / 1000) : parsed;
    this.entries.delete(k);
    this.entries.set(k, {
      at: this.now(),
      value: {
        id: input.id,
        encryptedBlob: input.encryptedBlob.startsWith('0x') ? input.encryptedBlob.slice(2) : input.encryptedBlob,
        encryptedEmbedding: null,
        decayScore: String(input.decayScore),
        timestamp: String(seconds),
        isActive: true,
      },
    });
    this.prune();
  }

  recordTombstone(owner: string, factId: string): void {
    this.prune();
    const k = this.key(owner, factId);
    this.entries.delete(k);
    this.tombstones.delete(k);
    this.tombstones.set(k, { at: this.now(), value: true });
    this.prune();
  }

  get(owner: string, factId: string): SubgraphSearchFact | null {
    this.prune();
    const hit = this.entries.get(this.key(owner, factId));
    return hit ? { ...hit.value } : null;
  }

  isTombstoned(owner: string, factId: string): boolean {
    this.prune();
    return this.tombstones.has(this.key(owner, factId));
  }

  clear(): void {
    this.entries.clear();
    this.tombstones.clear();
  }
}

/** Record a submitted batch; does nothing unless the submit succeeded. */
export function recordSubmittedPayloads(
  cache: RecentWrites,
  owner: string,
  payloads: RecentWriteInput[],
  submit: { success: boolean },
): void {
  if (submit.success !== true) return;
  for (const p of payloads) {
    if (p.encryptedBlob === TOMBSTONE_BLOB_HEX) cache.recordTombstone(owner, p.id);
    else cache.recordWrite(owner, p);
  }
}
