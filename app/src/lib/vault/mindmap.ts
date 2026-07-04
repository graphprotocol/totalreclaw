/**
 * "Map of your mind" graph, derived client-side from the decrypted vault.
 *
 * Produces a planetarium-shaped graph: a central "you", one node per life-domain
 * (scope), and entity nodes clustered under their dominant scope. Entities +
 * scope live inside the encrypted blob (claim.entities / claim.scope), so this is
 * computed after full decryption — the subgraph is server-blind.
 *
 * Consumed by components/memory/MindMap.tsx.
 */
import type { VaultItem } from "../types";

export type Scope = "work" | "personal" | "health" | "family" | "creative" | "finance" | "misc";
export type MindKind = "you" | "scope" | "entity";

export interface MindNode {
  id: string;
  label: string;
  kind: MindKind;
  scope: Scope | null;
  weight: number;
}
export interface MindLink {
  source: string;
  target: string;
}
export interface DerivedMind {
  nodes: MindNode[];
  links: MindLink[];
  neighborsOf: (id: string) => Set<string>;
  entityCount: number;
  cappedEntities: number;
}

/** Muted earth-jewel tones — distinct enough to cluster by, warm enough for the
 *  Keeper palette; glow well on the dark planetarium canvas. */
export const SCOPES: { id: Scope; label: string; color: string }[] = [
  { id: "work", label: "Work", color: "#C16240" },
  { id: "creative", label: "Creative", color: "#9B72B0" },
  { id: "health", label: "Health", color: "#5B8A6B" },
  { id: "family", label: "Family", color: "#C89A3C" },
  { id: "personal", label: "Personal", color: "#4E8B9C" },
  { id: "finance", label: "Finance", color: "#8A8450" },
  { id: "misc", label: "Other", color: "#8A7F76" },
];
export const SCOPE_COLOR: Record<Scope, string> = Object.fromEntries(
  SCOPES.map((s) => [s.id, s.color]),
) as Record<Scope, string>;

export const YOU_ID = "you";

const KNOWN = new Set<Scope>(["work", "personal", "health", "family", "creative", "finance"]);
function coerceScope(s: string | undefined): Scope {
  const v = (s ?? "").toLowerCase() as Scope;
  return KNOWN.has(v) ? v : "misc";
}

const MAX_ENTITY_NODES = 50;
const entId = (lc: string) => `e:${lc}`;
const scopeNodeId = (s: Scope) => `scope:${s}`;

/** Build the planetarium graph from the decrypted vault. */
export function buildMindGraph(items: VaultItem[]): DerivedMind {
  // entity → { label, weight, scope votes }
  const ent = new Map<string, { label: string; weight: number; votes: Map<Scope, number> }>();
  // session grouping for co-occurrence
  const sessions = new Map<string, Set<string>>(); // key → set of entity lc

  for (const it of items) {
    const scope = coerceScope(it.claim.scope);
    const sid = it.claim.metadata?.session_id ?? `day:${it.createdAt.toISOString().slice(0, 10)}`;
    const names = (it.claim.entities ?? []).map((e) => e.name.trim()).filter(Boolean);
    const seenThisItem = new Set<string>();
    for (const raw of names) {
      const lc = raw.toLowerCase();
      if (seenThisItem.has(lc)) continue;
      seenThisItem.add(lc);
      const rec = ent.get(lc);
      if (rec) {
        rec.weight += 1;
        rec.votes.set(scope, (rec.votes.get(scope) ?? 0) + 1);
      } else {
        ent.set(lc, { label: raw, weight: 1, votes: new Map([[scope, 1]]) });
      }
      let set = sessions.get(sid);
      if (!set) sessions.set(sid, (set = new Set()));
      set.add(lc);
    }
  }

  const entityCount = ent.size;
  const kept = new Set(
    [...ent.entries()].sort((a, b) => b[1].weight - a[1].weight).slice(0, MAX_ENTITY_NODES).map(([lc]) => lc),
  );
  const cappedEntities = Math.max(0, entityCount - kept.size);

  const domScope = (lc: string): Scope => {
    const votes = ent.get(lc)!.votes;
    let best: Scope = "misc";
    let bestN = -1;
    for (const [s, n] of votes) if (n > bestN) ((bestN = n), (best = s));
    return best;
  };

  const scopesUsed = new Set<Scope>();
  for (const lc of kept) scopesUsed.add(domScope(lc));

  const nodes: MindNode[] = [{ id: YOU_ID, label: "You", kind: "you", scope: null, weight: 14 }];
  for (const s of SCOPES) {
    if (scopesUsed.has(s.id)) nodes.push({ id: scopeNodeId(s.id), label: s.label, kind: "scope", scope: s.id, weight: 10 });
  }
  for (const lc of kept) {
    const rec = ent.get(lc)!;
    nodes.push({ id: entId(lc), label: rec.label, kind: "entity", scope: domScope(lc), weight: rec.weight });
  }

  const seen = new Set<string>();
  const links: MindLink[] = [];
  const addEdge = (a: string, b: string) => {
    if (a === b) return;
    const k = a < b ? `${a}|${b}` : `${b}|${a}`;
    if (seen.has(k)) return;
    seen.add(k);
    links.push({ source: a, target: b });
  };
  for (const s of scopesUsed) addEdge(YOU_ID, scopeNodeId(s));
  for (const lc of kept) addEdge(scopeNodeId(domScope(lc)), entId(lc));
  for (const set of sessions.values()) {
    const arr = [...set].filter((lc) => kept.has(lc)).map(entId);
    for (let i = 0; i < arr.length; i++) for (let j = i + 1; j < arr.length; j++) addEdge(arr[i], arr[j]);
  }

  const adj = new Map<string, Set<string>>();
  for (const n of nodes) adj.set(n.id, new Set());
  for (const l of links) {
    adj.get(l.source)?.add(l.target);
    adj.get(l.target)?.add(l.source);
  }

  return { nodes, links, neighborsOf: (id) => adj.get(id) ?? new Set(), entityCount, cappedEntities };
}
