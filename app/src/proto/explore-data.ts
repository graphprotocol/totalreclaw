// Cross-link index for the Explore prototype: ties graph nodes ↔ sessions ↔ facts.
import { KG_NODES, KG_LINKS, neighborsOf } from "./graph-data";
import { SEED_SESSIONS, type SeedSession, type SeedFact } from "./seed";

const NODE_BY_ID = new Map(KG_NODES.map((n) => [n.id, n]));

// topic id → its child entity labels (via topic→entity links)
const TOPIC_CHILDREN = (() => {
  const m = new Map<string, string[]>();
  for (const n of KG_NODES) if (n.kind === "topic") m.set(n.id, []);
  for (const l of KG_LINKS) {
    const s = NODE_BY_ID.get(l.source);
    const t = NODE_BY_ID.get(l.target);
    if (s?.kind === "topic" && t?.kind === "entity") m.get(s.id)?.push(t.label);
    if (t?.kind === "topic" && s?.kind === "entity") m.get(t.id)?.push(s.label);
  }
  return m;
})();

export function labelOf(id: string): string {
  return NODE_BY_ID.get(id)?.label ?? id;
}

export function kindOf(id: string): "topic" | "entity" {
  return NODE_BY_ID.get(id)?.kind ?? "entity";
}

/** Sessions touching a node: entity → sessions listing its label; topic → sessions touching any child entity. */
export function sessionsForNode(id: string): SeedSession[] {
  const node = NODE_BY_ID.get(id);
  if (!node) return [];
  if (node.kind === "entity") {
    return SEED_SESSIONS.filter((s) => s.entities.includes(node.label));
  }
  const children = TOPIC_CHILDREN.get(id) ?? [];
  return SEED_SESSIONS.filter((s) => s.entities.some((e) => children.includes(e)));
}

export function factsForNode(id: string): SeedFact[] {
  return sessionsForNode(id).flatMap((s) => s.facts);
}

export interface NeighborRef {
  id: string;
  label: string;
  kind: "topic" | "entity";
}

export function neighborRefs(id: string): NeighborRef[] {
  return [...neighborsOf(id)].map((nid) => ({ id: nid, label: labelOf(nid), kind: kindOf(nid) }));
}
