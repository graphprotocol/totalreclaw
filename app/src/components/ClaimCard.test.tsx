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

describe("ClaimCard curation affordances (A.2 Phase 3)", () => {
  it("stays read-only (no action row) with no handlers", () => {
    const html = renderToStaticMarkup(<ClaimCard item={item()} />);
    expect(html).not.toContain("Retype");
    expect(html).not.toContain("Scope");
    expect(html).not.toContain("Forget");
  });

  it("shows Retype and Scope affordances when handlers are supplied", () => {
    const html = renderToStaticMarkup(
      <ClaimCard item={item()} onRetype={() => {}} onSetScope={() => {}} />,
    );
    expect(html).toContain("Retype");
    expect(html).toContain("Scope");
  });

  it("shows pending copy while a retype / rescope is in flight", () => {
    const retyping = renderToStaticMarkup(
      <ClaimCard item={item()} onRetype={() => {}} retypePending />,
    );
    expect(retyping).toContain("Retyping…");
    const rescoping = renderToStaticMarkup(
      <ClaimCard item={item()} onSetScope={() => {}} scopePending />,
    );
    expect(rescoping).toContain("Rescoping…");
  });
});
