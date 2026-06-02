// Graph fixture for the KG explorer A/B (React Flow vs Reagraph). Seed only.
export type GraphKind = "topic" | "entity";

export interface KGNode {
  id: string;
  label: string;
  kind: GraphKind;
}

export interface KGLink {
  source: string;
  target: string;
}

export const KG_NODES: KGNode[] = [
  { id: "t-tr", label: "TotalReclaw", kind: "topic" },
  { id: "t-graph", label: "The Graph", kind: "topic" },
  { id: "t-health", label: "Health", kind: "topic" },
  { id: "t-family", label: "Family", kind: "topic" },
  { id: "t-read", label: "Reading", kind: "topic" },

  { id: "e-spa", label: "Vault SPA", kind: "entity" },
  { id: "e-crystal", label: "Crystal", kind: "entity" },
  { id: "e-mind", label: "mind-map", kind: "entity" },
  { id: "e-fraunces", label: "Fraunces", kind: "entity" },
  { id: "e-relay", label: "relay", kind: "entity" },

  { id: "e-subgraph", label: "subgraph", kind: "entity" },
  { id: "e-gnosis", label: "Gnosis", kind: "entity" },
  { id: "e-hermes", label: "Hermes", kind: "entity" },
  { id: "e-foundation", label: "foundation", kind: "entity" },

  { id: "e-running", label: "running", kind: "entity" },
  { id: "e-knee", label: "knee", kind: "entity" },
  { id: "e-sleep", label: "sleep", kind: "entity" },

  { id: "e-lisbon", label: "Lisbon", kind: "entity" },
  { id: "e-july", label: "July trip", kind: "entity" },
  { id: "e-grand", label: "grandparents", kind: "entity" },

  { id: "e-borges", label: "Borges", kind: "entity" },
  { id: "e-essay", label: "essay", kind: "entity" },
  { id: "e-mem", label: "memory", kind: "entity" },
];

export const KG_LINKS: KGLink[] = [
  // topic → entity
  { source: "t-tr", target: "e-spa" },
  { source: "t-tr", target: "e-crystal" },
  { source: "t-tr", target: "e-mind" },
  { source: "t-tr", target: "e-fraunces" },
  { source: "t-tr", target: "e-relay" },
  { source: "t-graph", target: "e-subgraph" },
  { source: "t-graph", target: "e-gnosis" },
  { source: "t-graph", target: "e-hermes" },
  { source: "t-graph", target: "e-foundation" },
  { source: "t-health", target: "e-running" },
  { source: "t-health", target: "e-knee" },
  { source: "t-health", target: "e-sleep" },
  { source: "t-family", target: "e-lisbon" },
  { source: "t-family", target: "e-july" },
  { source: "t-family", target: "e-grand" },
  { source: "t-read", target: "e-borges" },
  { source: "t-read", target: "e-essay" },
  { source: "t-read", target: "e-mem" },
  // cross-links (what makes the graph interesting + keeps it one connected map)
  { source: "t-tr", target: "t-graph" },
  { source: "t-tr", target: "t-read" },
  { source: "t-tr", target: "t-health" },
  { source: "t-tr", target: "t-family" },
  { source: "e-hermes", target: "e-spa" },
  { source: "e-relay", target: "e-hermes" },
  { source: "e-subgraph", target: "e-gnosis" },
  { source: "e-crystal", target: "e-mem" },
  { source: "e-running", target: "e-sleep" },
  { source: "e-lisbon", target: "e-july" },
];

const adjacency = (() => {
  const m = new Map<string, Set<string>>();
  for (const n of KG_NODES) m.set(n.id, new Set<string>());
  for (const l of KG_LINKS) {
    m.get(l.source)?.add(l.target);
    m.get(l.target)?.add(l.source);
  }
  return m;
})();

export function neighborsOf(id: string): Set<string> {
  return adjacency.get(id) ?? new Set<string>();
}
