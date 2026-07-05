/**
 * Client-side conversation re-segmentation for the vault SPA.
 *
 * ## Why this exists
 *
 * The Hermes write-side keys a memory's `session_id` off a *chat-level* id, so
 * every conversation inside one messenger DM collapses into a single
 * `session_id` that persists across process restarts. A vault that groups by
 * `session_id` then shows one giant "session" that actually mixes many
 * unrelated conversations (a morning investing chat, an afternoon "local LLMs"
 * chat, an evening "self-hosting" chat — all under one id).
 *
 * The write-side fix + an on-chain backfill are separate, in-flight tracks.
 * This module is a **read-side mitigation**: it re-groups already-decrypted
 * facts into coherent conversations *on display*, so the vault is usable now.
 *
 * ## v1 — time-gap clustering (this file)
 *
 * Sort a collapsed session's facts by `createdAt`; whenever the gap between two
 * consecutive facts exceeds {@link DEFAULT_SESSION_GAP_MS}, a new display group
 * begins. No embeddings required — this cleanly separates the dominant collapse
 * case (conversations that are separated in time).
 *
 * ## Design notes
 *
 * - Pure and framework-free: takes items, returns groups. No React, no I/O.
 * - Session-aware but not session-dependent: pass `sessionKeyOf` to sub-split
 *   within each raw session (preserving the original id so no information is
 *   lost). Omit it and every fact is treated as one flat stream — which is the
 *   correct behaviour on `main` today, where the decrypted claim carries no
 *   `session_id` at all. When the write-side/redesign lands a `session_id`,
 *   wiring it in is a one-line accessor change; the algorithm is unchanged.
 * - Generic over `{ createdAt: Date }` so it is trivially unit-testable without
 *   constructing full `VaultItem`s.
 */

/**
 * Default idle gap that starts a new conversation group.
 *
 * 40 minutes sits in the recommended 30–45 min band: long enough that a brief
 * pause mid-conversation (thinking, a phone call, re-reading) does not fracture
 * one exchange into several, yet short enough that genuinely separate sittings
 * (morning vs. afternoon) land in distinct groups.
 */
export const DEFAULT_SESSION_GAP_MS = 40 * 60 * 1000;

/** Anything with a `createdAt` timestamp can be segmented. */
export interface Timestamped {
  createdAt: Date;
}

export interface SegmentationOptions<T extends Timestamped> {
  /**
   * Idle gap (ms) that begins a new group. Defaults to
   * {@link DEFAULT_SESSION_GAP_MS}.
   */
  gapMs?: number;
  /**
   * Optional accessor for the raw (write-side) session id. When provided,
   * items are first partitioned by this key and each partition is then
   * time-gap split independently — so a display group never merges facts from
   * two different raw sessions, and the original id is preserved on the group.
   *
   * Omit it (the case on `main` today) to treat all items as one stream.
   */
  sessionKeyOf?: (item: T) => string | null | undefined;
}

/** A coherent conversation as re-derived on the client. */
export interface ConversationGroup<T extends Timestamped> {
  /**
   * Stable key for this display group. Format:
   *   `<rawSessionId | "_">::<startTimestampMs>`
   * The raw-session prefix (or `_` when none) keeps groups from distinct raw
   * sessions distinct even if their time windows happen to touch; the start
   * timestamp disambiguates the sub-splits within one raw session. Stable
   * across re-renders as long as the underlying facts don't change.
   */
  key: string;
  /** The write-side session id these items came from, if any (else null). */
  sessionId: string | null;
  /** Items in ascending `createdAt` order. */
  items: T[];
  /** Earliest `createdAt` in the group. */
  start: Date;
  /** Latest `createdAt` in the group. */
  end: Date;
}

function ascByCreatedAt<T extends Timestamped>(a: T, b: T): number {
  const at = a.createdAt.getTime();
  const bt = b.createdAt.getTime();
  // NaN timestamps sort last so they can't corrupt a coherent run.
  if (Number.isNaN(at)) return Number.isNaN(bt) ? 0 : 1;
  if (Number.isNaN(bt)) return -1;
  return at - bt;
}

function makeGroup<T extends Timestamped>(
  items: T[],
  sessionId: string | null,
): ConversationGroup<T> {
  const start = items[0]!.createdAt;
  const end = items[items.length - 1]!.createdAt;
  const startMs = start.getTime();
  const prefix = sessionId ?? "_";
  // A NaN start would make an unstable key; fall back to the first item's id-ish
  // ordinal via 0 so the key stays deterministic for a given item order.
  const keyStamp = Number.isNaN(startMs) ? "nan" : String(startMs);
  return {
    key: `${prefix}::${keyStamp}`,
    sessionId,
    items,
    start,
    end,
  };
}

/**
 * Split one already-partitioned, time-sorted run of items into conversation
 * groups on idle gaps. Assumes `sortedItems` is non-empty and ascending.
 * A NaN gap (invalid timestamp) is treated as an infinite gap, so bad rows
 * land in their own trailing group.
 */
function splitRunByGap<T extends Timestamped>(
  sortedItems: T[],
  sessionId: string | null,
  gapMs: number,
): ConversationGroup<T>[] {
  const groups: ConversationGroup<T>[] = [];
  let current: T[] = [sortedItems[0]!];

  for (let i = 1; i < sortedItems.length; i++) {
    const prev = sortedItems[i - 1]!;
    const item = sortedItems[i]!;
    const delta = item.createdAt.getTime() - prev.createdAt.getTime();
    const isNewGroup = Number.isNaN(delta) || delta > gapMs;
    if (isNewGroup) {
      groups.push(makeGroup(current, sessionId));
      current = [item];
    } else {
      current.push(item);
    }
  }
  groups.push(makeGroup(current, sessionId));
  return groups;
}

/**
 * Re-segment a flat list of timestamped items into coherent conversation
 * groups using idle-gap clustering.
 *
 * Behaviour:
 * - With no `sessionKeyOf`: sorts everything by `createdAt` and splits on gaps
 *   larger than `gapMs`. This is the shipping default on `main`.
 * - With a `sessionKeyOf`: partitions by raw session first, then gap-splits
 *   within each — never merging across raw sessions, and preserving the raw id.
 *
 * Returned groups are ordered by their `start` timestamp (ascending). The
 * caller can reverse for a newest-first view. Items inside each group are
 * always ascending by `createdAt`.
 */
export function segmentByTimeGap<T extends Timestamped>(
  items: readonly T[],
  options: SegmentationOptions<T> = {},
): ConversationGroup<T>[] {
  const gapMs = options.gapMs ?? DEFAULT_SESSION_GAP_MS;
  const { sessionKeyOf } = options;

  if (items.length === 0) return [];

  // Partition by raw session id (or a single null bucket when no accessor).
  // First-appearance order is preserved by the Map.
  const partitions = new Map<string | null, T[]>();
  for (const item of items) {
    const raw = sessionKeyOf ? sessionKeyOf(item) : null;
    const sessionId = raw ?? null;
    const existing = partitions.get(sessionId);
    if (existing) existing.push(item);
    else partitions.set(sessionId, [item]);
  }

  const groups: ConversationGroup<T>[] = [];
  for (const [sessionId, bucket] of partitions) {
    const sorted = [...bucket].sort(ascByCreatedAt);
    groups.push(...splitRunByGap(sorted, sessionId, gapMs));
  }

  groups.sort((a, b) => {
    const at = a.start.getTime();
    const bt = b.start.getTime();
    if (Number.isNaN(at)) return Number.isNaN(bt) ? 0 : 1;
    if (Number.isNaN(bt)) return -1;
    return at - bt;
  });
  return groups;
}

/** Milliseconds spanned by a group (0 for a single-item group). */
export function groupDurationMs<T extends Timestamped>(
  group: ConversationGroup<T>,
): number {
  return group.end.getTime() - group.start.getTime();
}

/**
 * True when a run of facts is "suspiciously collapsed" and worth
 * re-segmenting for the user: it either spans a wide time range or holds many
 * facts. The view uses this to decide whether to surface the re-grouped view
 * by default. Pure; thresholds are explicit args so the caller owns policy.
 */
export function isCollapsedRun(
  itemCount: number,
  spanMs: number,
  opts: { maxSpanMs?: number; maxItems?: number } = {},
): boolean {
  const maxSpanMs = opts.maxSpanMs ?? 6 * 60 * 60 * 1000; // 6h
  const maxItems = opts.maxItems ?? 30;
  return itemCount > maxItems || spanMs > maxSpanMs;
}
