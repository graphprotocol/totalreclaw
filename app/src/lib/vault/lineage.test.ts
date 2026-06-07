import { describe, it, expect } from "vitest";
import { buildClaimLineage, listChangedChains } from "./lineage";
import type { VaultItem, MemoryClaimV1 } from "../types";

function it_(claimId: string, createdAt: string, supersededBy?: string, active = true): VaultItem {
  const claim: MemoryClaimV1 = {
    id: claimId,
    text: `claim ${claimId}`,
    type: "claim",
    source: "user",
    created_at: createdAt,
    schema_version: "1.0",
    superseded_by: supersededBy,
  };
  return {
    id: `fact-${claimId}`,
    claim,
    type: "claim",
    pinned: false,
    createdAt: new Date(createdAt),
    rawBlob: "",
    blindIndices: [],
    decayScore: active ? 1 : 0,
    isActive: active,
  };
}

describe("buildClaimLineage", () => {
  // A -> B -> C  (A,B superseded; C active)
  const items = [
    it_("A", "2026-06-01T00:00:00Z", "B", false),
    it_("B", "2026-06-02T00:00:00Z", "C", false),
    it_("C", "2026-06-03T00:00:00Z", undefined, true),
  ];

  it("returns the full chain oldest→newest from any member", () => {
    expect(buildClaimLineage(items, "B").map((i) => i.claim.id)).toEqual(["A", "B", "C"]);
    expect(buildClaimLineage(items, "A").map((i) => i.claim.id)).toEqual(["A", "B", "C"]);
    expect(buildClaimLineage(items, "C").map((i) => i.claim.id)).toEqual(["A", "B", "C"]);
  });

  it("returns [] for an unknown claim", () => {
    expect(buildClaimLineage(items, "Z")).toEqual([]);
  });

  it("handles a single-version belief", () => {
    const solo = [it_("X", "2026-06-01T00:00:00Z")];
    expect(buildClaimLineage(solo, "X").map((i) => i.claim.id)).toEqual(["X"]);
  });
});

describe("listChangedChains", () => {
  it("returns only multi-version chains, newest first", () => {
    const items = [
      it_("A", "2026-06-01T00:00:00Z", "B", false),
      it_("B", "2026-06-02T00:00:00Z", undefined, true),
      it_("solo", "2026-06-05T00:00:00Z"),
      it_("P", "2026-06-03T00:00:00Z", "Q", false),
      it_("Q", "2026-06-10T00:00:00Z", undefined, true),
    ];
    const chains = listChangedChains(items);
    expect(chains).toHaveLength(2);
    // Q-chain is newer than B-chain
    expect(chains[0].map((i) => i.claim.id)).toEqual(["P", "Q"]);
    expect(chains[1].map((i) => i.claim.id)).toEqual(["A", "B"]);
  });
});
