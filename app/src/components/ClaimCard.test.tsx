import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ClaimCard } from "./ClaimCard";
import type { VaultItem, MemoryClaimV1 } from "../lib/types";

function item(claimExtra: Partial<MemoryClaimV1> = {}): VaultItem {
  const claim: MemoryClaimV1 = {
    id: "f1",
    text: "Prefers dark mode",
    type: "preference",
    source: "user",
    created_at: "2026-07-08T00:00:00Z",
    schema_version: "1.0",
    ...claimExtra,
  };
  return {
    id: "f1",
    claim,
    type: claim.type,
    pinned: false,
    createdAt: new Date("2026-07-08T00:00:00Z"),
    rawBlob: "",
    blindIndices: [],
    decayScore: 1,
    isActive: true,
  };
}

describe("ClaimCard agent provenance (#317)", () => {
  it("renders 'via John (Hermes)' when the claim carries a top-level agent_name", () => {
    const html = renderToStaticMarkup(<ClaimCard item={item({ agent_name: "John" })} />);
    expect(html).toContain("via John (Hermes)");
  });

  it("reads agent_name nested in metadata as a fallback", () => {
    const html = renderToStaticMarkup(
      <ClaimCard item={item({ metadata: { agent_name: "Jane" } })} />,
    );
    expect(html).toContain("via Jane (Hermes)");
  });

  it("escapes agent_name (no raw markup injection)", () => {
    const html = renderToStaticMarkup(
      <ClaimCard item={item({ agent_name: "<img src=x onerror=alert(1)>" })} />,
    );
    expect(html).not.toContain("<img src=x");
    expect(html).toContain("&lt;img");
  });

  it("renders the unchanged source line and no provenance when agent_name is absent", () => {
    const html = renderToStaticMarkup(<ClaimCard item={item()} />);
    expect(html).not.toContain("via ");
    expect(html).toContain("from you");
  });
});
