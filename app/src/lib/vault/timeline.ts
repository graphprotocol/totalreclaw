/**
 * Session-grouped view of the decrypted vault — the Keeper Memory timeline.
 *
 * Groups decrypted VaultItems by `metadata.session_id` (written by the
 * session-end Crystal pipeline). The Crystal (subtype === "session_crystal")
 * heads its session; the rest are its atomic facts. Items WITHOUT a session_id
 * (sparse / pre-batching entries) fall back to day-grouping so nothing is lost.
 */
import type { VaultItem } from "../types";

const SESSION_CRYSTAL = "session_crystal";

export interface SessionGroup {
  /** Stable group key: "s:<session_id>" or "day:<YYYY-MM-DD>". */
  key: string;
  /** session_id when this is a real session; null for day-bucketed loose facts. */
  sessionId: string | null;
  /** Most recent timestamp in the group (used for ordering). */
  date: Date;
  /** Crystal headline (or first fact text), truncated for the card. */
  headline: string;
  /** The session Crystal, if one exists. */
  crystal: VaultItem | null;
  /** Atomic facts (excludes the Crystal). */
  facts: VaultItem[];
  /** Union of entity names across the group. */
  entityNames: string[];
  /** Count of unresolved open threads (from the Crystal). */
  openThreads: number;
  /** Group importance (Crystal's, else max fact importance, else 8). */
  importance: number;
}

function ymd(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function truncate(s: string, n: number): string {
  const t = s.trim();
  return t.length <= n ? t : t.slice(0, n - 1).trimEnd() + "…";
}

function isCrystal(item: VaultItem): boolean {
  return item.claim.metadata?.subtype === SESSION_CRYSTAL;
}

/** Build the recency-ordered session timeline from decrypted vault items. */
export function buildTimeline(items: VaultItem[]): SessionGroup[] {
  const buckets = new Map<string, VaultItem[]>();

  for (const item of items) {
    const sid = item.claim.metadata?.session_id;
    const key = sid ? `s:${sid}` : `day:${ymd(item.createdAt)}`;
    const arr = buckets.get(key);
    if (arr) arr.push(item);
    else buckets.set(key, [item]);
  }

  const groups: SessionGroup[] = [];

  for (const [key, members] of buckets) {
    const sessionId = key.startsWith("s:") ? key.slice(2) : null;
    const crystal = members.find(isCrystal) ?? null;
    const facts = crystal ? members.filter((m) => m !== crystal) : members.slice();

    const date = members.reduce(
      (max, m) => (m.createdAt > max ? m.createdAt : max),
      members[0].createdAt,
    );

    const entityNames = Array.from(
      new Set(
        members.flatMap((m) => (m.claim.entities ?? []).map((e) => e.name).filter(Boolean)),
      ),
    );

    const headlineSource = crystal?.claim.text ?? facts[0]?.claim.text ?? members[0].claim.text;
    const maxFactImportance = members.reduce(
      (max, m) => Math.max(max, m.claim.importance ?? 0),
      0,
    );
    const importance = crystal?.claim.importance ?? (maxFactImportance || 8);

    groups.push({
      key,
      sessionId,
      date,
      headline: truncate(headlineSource ?? "Untitled", 120),
      crystal,
      facts,
      entityNames,
      openThreads: crystal?.claim.metadata?.open_threads?.length ?? 0,
      importance,
    });
  }

  groups.sort((a, b) => b.date.getTime() - a.date.getTime());
  return groups;
}

/** 8-char session-hash (sha256-free quick id) for URL routing. Uses session_id
 *  when present, else the day key. Deterministic + stable per group. */
export function sessionSlug(group: SessionGroup): string {
  const basis = group.sessionId ?? group.key;
  // Lightweight FNV-1a → 8 hex chars (URL-friendly; not security-sensitive).
  let h = 0x811c9dc5;
  for (let i = 0; i < basis.length; i++) {
    h ^= basis.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}
