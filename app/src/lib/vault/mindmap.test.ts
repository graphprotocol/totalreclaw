import { describe, it, expect } from "vitest";
import { buildMindGraph, YOU_ID } from "./mindmap";
import type { VaultItem } from "../types";

let seq = 0;
function item(scope: string, entities: string[], sessionId?: string): VaultItem {
  return {
    id: `i${seq++}`,
    claim: {
      id: `c${seq}`,
      text: "x",
      type: "claim",
      source: "user",
      created_at: "2026-01-01T00:00:00Z",
      schema_version: "1.0",
      scope,
      entities: entities.map((name) => ({ name })),
      metadata: sessionId ? { session_id: sessionId } : undefined,
    },
    type: "claim",
    pinned: false,
    createdAt: new Date("2026-01-01"),
    rawBlob: "",
    blindIndices: [],
    decayScore: 1,
    isActive: true,
  };
}

const eId = (lc: string) => `e:${lc}`;
const hasEdge = (links: { source: string; target: string }[], a: string, b: string) =>
  links.some((l) => (l.source === a && l.target === b) || (l.source === b && l.target === a));

describe("buildMindGraph", () => {
  it("builds you + scope + entity nodes", () => {
    const g = buildMindGraph([item("work", ["Hermes", "Gnosis"])]);
    expect(g.nodes.find((n) => n.id === YOU_ID)).toBeTruthy();
    expect(g.nodes.some((n) => n.kind === "scope" && n.scope === "work")).toBe(true);
    expect(g.entityCount).toBe(2);
    // you → scope → entity chain
    expect(hasEdge(g.links, YOU_ID, "scope:work")).toBe(true);
    expect(hasEdge(g.links, "scope:work", eId("hermes"))).toBe(true);
  });

  it("links entities co-occurring in the same session", () => {
    const g = buildMindGraph([item("work", ["Hermes", "Vault SPA"], "s1")]);
    expect(hasEdge(g.links, eId("hermes"), eId("vault spa"))).toBe(true);
  });

  it("assigns an entity to its most common scope", () => {
    const g = buildMindGraph([
      item("work", ["Gnosis"]),
      item("work", ["Gnosis"]),
      item("personal", ["Gnosis"]),
    ]);
    expect(g.nodes.find((n) => n.id === eId("gnosis"))?.scope).toBe("work");
  });

  it("coerces unknown/unspecified scope to misc", () => {
    const g = buildMindGraph([item("unspecified", ["thing"])]);
    expect(g.nodes.find((n) => n.id === eId("thing"))?.scope).toBe("misc");
  });

  it("caps entities at 50 and reports the remainder", () => {
    const many = Array.from({ length: 55 }, (_, i) => `e${i}`);
    const g = buildMindGraph([item("work", many)]);
    expect(g.entityCount).toBe(55);
    expect(g.cappedEntities).toBe(5);
    expect(g.nodes.filter((n) => n.kind === "entity")).toHaveLength(50);
  });
});
