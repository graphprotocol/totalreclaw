import { describe, it, expect } from "vitest";
import { buildTimeline, sessionSlug } from "./timeline";
import type { VaultItem, MemoryClaimV1 } from "../types";

let seq = 0;
function item(
  text: string,
  createdAt: string,
  claimExtra: Partial<MemoryClaimV1> = {},
): VaultItem {
  const id = `f${seq++}`;
  const claim: MemoryClaimV1 = {
    id,
    text,
    type: "claim",
    source: "user",
    created_at: createdAt,
    schema_version: "1.0",
    ...claimExtra,
  };
  return {
    id,
    claim,
    type: claim.type,
    pinned: false,
    createdAt: new Date(createdAt),
    rawBlob: "",
    blindIndices: [],
    decayScore: 1,
  };
}

describe("buildTimeline", () => {
  it("groups a Crystal + its atomic facts by session_id", () => {
    const items = [
      item("Session summary", "2026-06-01T10:00:00Z", {
        metadata: {
          subtype: "session_crystal",
          session_id: "sid-1",
          open_threads: ["follow up on X", "decide Y"],
        },
      }),
      item("Prefers morning meetings", "2026-06-01T09:30:00Z", {
        metadata: { session_id: "sid-1" },
        entities: [{ name: "Alice" }],
      }),
      item("Uses TypeScript", "2026-06-01T09:31:00Z", {
        metadata: { session_id: "sid-1" },
        entities: [{ name: "TypeScript" }],
      }),
    ];
    const groups = buildTimeline(items);
    expect(groups).toHaveLength(1);
    const g = groups[0];
    expect(g.sessionId).toBe("sid-1");
    expect(g.crystal?.claim.text).toBe("Session summary");
    expect(g.facts).toHaveLength(2);
    expect(g.headline).toBe("Session summary");
    expect(g.openThreads).toBe(2);
    expect(g.entityNames.sort()).toEqual(["Alice", "TypeScript"]);
  });

  it("falls back to day-grouping for facts without a session_id", () => {
    const items = [
      item("loose fact A", "2026-06-02T08:00:00Z"),
      item("loose fact B", "2026-06-02T20:00:00Z"),
      item("other day", "2026-06-03T08:00:00Z"),
    ];
    const groups = buildTimeline(items);
    expect(groups).toHaveLength(2); // two distinct days
    const byDay = groups.map((g) => ({ id: g.sessionId, n: g.facts.length }));
    expect(byDay).toContainEqual({ id: null, n: 2 });
    expect(byDay).toContainEqual({ id: null, n: 1 });
  });

  it("orders groups by most-recent timestamp desc", () => {
    const items = [
      item("old", "2026-06-01T00:00:00Z", { metadata: { session_id: "a" } }),
      item("new", "2026-06-05T00:00:00Z", { metadata: { session_id: "b" } }),
    ];
    const groups = buildTimeline(items);
    expect(groups[0].sessionId).toBe("b");
    expect(groups[1].sessionId).toBe("a");
  });

  it("uses the first fact as headline when there's no Crystal", () => {
    const items = [item("just a fact", "2026-06-04T00:00:00Z", { metadata: { session_id: "z" } })];
    const groups = buildTimeline(items);
    expect(groups[0].crystal).toBeNull();
    expect(groups[0].headline).toBe("just a fact");
  });

  it("sessionSlug is deterministic + 8 hex chars", () => {
    const g = buildTimeline([
      item("x", "2026-06-04T00:00:00Z", { metadata: { session_id: "stable-id" } }),
    ])[0];
    const slug = sessionSlug(g);
    expect(slug).toMatch(/^[0-9a-f]{8}$/);
    expect(sessionSlug(g)).toBe(slug);
  });
});
