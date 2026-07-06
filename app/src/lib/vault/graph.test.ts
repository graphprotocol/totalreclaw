import { describe, it, expect } from "vitest";
import { buildGraph } from "./graph";
import type { SessionGroup } from "./timeline";
import type { VaultItem } from "../types";

let seq = 0;
function group(entityNames: string[], topics?: string[]): SessionGroup {
  const crystal = topics
    ? ({ claim: { metadata: { topics_discussed: topics } } } as unknown as VaultItem)
    : null;
  return {
    key: `g${seq++}`,
    sessionId: null,
    date: new Date(0),
    headline: "",
    crystal,
    facts: [],
    entityNames,
    openThreads: 0,
    importance: 8,
  };
}

const ent = (name: string) => `e:${name.toLowerCase()}`;
const top = (name: string) => `t:${name.toLowerCase()}`;
const hasEdge = (links: { source: string; target: string }[], a: string, b: string) =>
  links.some((l) => (l.source === a && l.target === b) || (l.source === b && l.target === a));

describe("buildGraph", () => {
  it("links entities that co-occur in a session", () => {
    const g = buildGraph([group(["Hermes", "Vault SPA"])]);
    expect(g.entityCount).toBe(2);
    expect(hasEdge(g.links, ent("Hermes"), ent("Vault SPA"))).toBe(true);
    expect(g.neighborsOf(ent("Hermes")).has(ent("Vault SPA"))).toBe(true);
  });

  it("does NOT link entities from different sessions", () => {
    const g = buildGraph([group(["Hermes"]), group(["Gnosis"])]);
    expect(hasEdge(g.links, ent("Hermes"), ent("Gnosis"))).toBe(false);
  });

  it("derives topic↔entity edges from Crystal topics", () => {
    const g = buildGraph([group(["relay"], ["TotalReclaw"])]);
    expect(g.topicCount).toBe(1);
    expect(hasEdge(g.links, top("TotalReclaw"), ent("relay"))).toBe(true);
  });

  it("case-folds duplicate entity names", () => {
    const g = buildGraph([group(["Gnosis"]), group(["gnosis"])]);
    expect(g.entityCount).toBe(1);
    // appears in two sessions → weight 2
    expect(g.nodes.find((n) => n.id === ent("Gnosis"))?.weight).toBe(2);
  });

  it("caps entities for layout and reports how many were held back", () => {
    const many = Array.from({ length: 61 }, (_, i) => `entity-${i}`);
    const g = buildGraph([group(many)]);
    expect(g.entityCount).toBe(61);
    expect(g.cappedEntities).toBe(1);
    expect(g.nodes.filter((n) => n.kind === "entity")).toHaveLength(60);
  });
});
