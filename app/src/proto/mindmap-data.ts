// Rich graph fixture for the "Map of your mind" redesign A/B (seed only, throwaway).
// Hand-authored to feel full so the three layouts (Atlas / Radial / Constellation)
// each read clearly. In the real app this is DERIVED client-side from decrypted
// claim.entities + scope by session co-occurrence — see app/src/lib/vault/graph.ts.

export type Scope = "work" | "health" | "family" | "creative" | "personal" | "finance";
export type MindKind = "you" | "scope" | "entity";

export interface MindNode {
  id: string;
  label: string;
  kind: MindKind;
  scope: Scope | null;
  /** How often this comes up — drives node size + glow. */
  weight: number;
}

export interface MindLink {
  source: string;
  target: string;
}

/** Muted earth-jewel tones — distinct enough to cluster by, warm enough to stay
 *  on the Keeper palette. Glow beautifully on the dark planetarium canvas. */
export const SCOPES: { id: Scope; label: string; color: string }[] = [
  { id: "work", label: "Work", color: "#C16240" }, // clay
  { id: "creative", label: "Creative", color: "#9B72B0" }, // muted plum
  { id: "health", label: "Health", color: "#5B8A6B" }, // sage
  { id: "family", label: "Family", color: "#C89A3C" }, // amber
  { id: "personal", label: "Personal", color: "#4E8B9C" }, // muted teal
  { id: "finance", label: "Finance", color: "#8A8450" }, // olive
];

export const SCOPE_COLOR: Record<Scope, string> = Object.fromEntries(
  SCOPES.map((s) => [s.id, s.color]),
) as Record<Scope, string>;

// ── Entities (the leaves) ────────────────────────────────────────────
const ENTITIES: { label: string; scope: Scope; weight: number }[] = [
  // work
  { label: "The Graph", scope: "work", weight: 9 },
  { label: "Hermes", scope: "work", weight: 8 },
  { label: "Vault SPA", scope: "work", weight: 8 },
  { label: "subgraph", scope: "work", weight: 6 },
  { label: "Gnosis", scope: "work", weight: 6 },
  { label: "relay", scope: "work", weight: 5 },
  { label: "Crystal", scope: "work", weight: 5 },
  { label: "impeccable", scope: "work", weight: 4 },
  { label: "Foundation", scope: "work", weight: 4 },
  // creative
  { label: "essay", scope: "creative", weight: 5 },
  { label: "Borges", scope: "creative", weight: 4 },
  { label: "memory", scope: "creative", weight: 6 },
  { label: "writing", scope: "creative", weight: 4 },
  // health
  { label: "running", scope: "health", weight: 6 },
  { label: "knee", scope: "health", weight: 4 },
  { label: "sleep", scope: "health", weight: 5 },
  { label: "marathon", scope: "health", weight: 3 },
  // family
  { label: "Lisbon", scope: "family", weight: 6 },
  { label: "July trip", scope: "family", weight: 5 },
  { label: "grandparents", scope: "family", weight: 4 },
  { label: "kids", scope: "family", weight: 5 },
  { label: "passports", scope: "family", weight: 3 },
  // personal
  { label: "flat white", scope: "personal", weight: 5 },
  { label: "standing desk", scope: "personal", weight: 4 },
  { label: "split keyboard", scope: "personal", weight: 3 },
  { label: "aisle seat", scope: "personal", weight: 3 },
  // finance
  { label: "Stripe", scope: "finance", weight: 4 },
  { label: "budget", scope: "finance", weight: 3 },
];

const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-");

export const YOU_ID = "you";

export const MIND_NODES: MindNode[] = [
  { id: YOU_ID, label: "You", kind: "you", scope: null, weight: 14 },
  ...SCOPES.map((s) => ({
    id: `scope:${s.id}`,
    label: s.label,
    kind: "scope" as const,
    scope: s.id,
    weight: 10,
  })),
  ...ENTITIES.map((e) => ({
    id: `e:${slug(e.label)}`,
    label: e.label,
    kind: "entity" as const,
    scope: e.scope,
    weight: e.weight,
  })),
];

// you → scope, scope → its entities
const treeLinks: MindLink[] = [
  ...SCOPES.map((s) => ({ source: YOU_ID, target: `scope:${s.id}` })),
  ...ENTITIES.map((e) => ({ source: `scope:${e.scope}`, target: `e:${slug(e.label)}` })),
];

// cross-links — the interesting connections that make the graph worth exploring
const crossLinks: MindLink[] = [
  ["Hermes", "Vault SPA"],
  ["Hermes", "relay"],
  ["relay", "subgraph"],
  ["subgraph", "Gnosis"],
  ["Vault SPA", "Crystal"],
  ["Crystal", "memory"], // work ↔ creative bridge
  ["memory", "essay"],
  ["Borges", "memory"],
  ["running", "sleep"],
  ["running", "knee"],
  ["Lisbon", "July trip"],
  ["July trip", "kids"],
  ["kids", "passports"],
  ["flat white", "standing desk"],
  ["standing desk", "split keyboard"],
  ["The Graph", "Foundation"],
  ["The Graph", "Gnosis"],
  ["Stripe", "budget"],
  ["Vault SPA", "Stripe"], // work ↔ finance bridge
].map(([a, b]) => ({ source: `e:${slug(a)}`, target: `e:${slug(b)}` }));

export const MIND_LINKS: MindLink[] = [...treeLinks, ...crossLinks];

/** Neighbor adjacency for hover-highlight. */
const adjacency = (() => {
  const m = new Map<string, Set<string>>();
  for (const n of MIND_NODES) m.set(n.id, new Set());
  for (const l of MIND_LINKS) {
    m.get(l.source)?.add(l.target);
    m.get(l.target)?.add(l.source);
  }
  return m;
})();

export function mindNeighbors(id: string): Set<string> {
  return adjacency.get(id) ?? new Set();
}
