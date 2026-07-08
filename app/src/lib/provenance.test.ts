import { describe, it, expect } from "vitest";
import {
  AGENT_PROVENANCE_CLIENT,
  agentNameOf,
  agentProvenanceLabel,
  composeProvenanceLabel,
  humanizeClient,
} from "./provenance";
import type { MemoryClaimV1 } from "./types";

function claim(extra: Partial<MemoryClaimV1> = {}): MemoryClaimV1 {
  return {
    id: "c1",
    text: "example",
    type: "claim",
    source: "user",
    created_at: "2026-07-08T00:00:00Z",
    schema_version: "1.0",
    ...extra,
  };
}

describe("humanizeClient", () => {
  it("maps known raw client tokens to friendly names", () => {
    expect(humanizeClient("hermes")).toBe("Hermes");
    expect(humanizeClient("python-client")).toBe("Hermes");
    expect(humanizeClient("openclaw")).toBe("OpenClaw");
    expect(humanizeClient("mcp-server")).toBe("MCP");
    expect(humanizeClient("rust-client:zeroclaw")).toBe("ZeroClaw");
  });

  it("is case-insensitive and trims", () => {
    expect(humanizeClient("  HERMES ")).toBe("Hermes");
  });

  it("passes unknown tokens through trimmed", () => {
    expect(humanizeClient("  Some Future Client ")).toBe("Some Future Client");
  });
});

describe("composeProvenanceLabel", () => {
  it("renders 'Name (Client)' when an agent name is present", () => {
    expect(composeProvenanceLabel("hermes", "John")).toBe("John (Hermes)");
    expect(composeProvenanceLabel("Hermes", "John")).toBe("John (Hermes)");
  });

  it("renders just the humanized client when no agent name", () => {
    expect(composeProvenanceLabel("hermes", undefined)).toBe("Hermes");
    expect(composeProvenanceLabel("hermes")).toBe("Hermes");
  });

  it("treats empty / whitespace-only names as absent", () => {
    expect(composeProvenanceLabel("hermes", "")).toBe("Hermes");
    expect(composeProvenanceLabel("hermes", "   ")).toBe("Hermes");
  });

  it("trims the agent name", () => {
    expect(composeProvenanceLabel("hermes", "  John  ")).toBe("John (Hermes)");
  });
});

describe("agentNameOf", () => {
  it("reads the top-level agent_name (canonical wire location)", () => {
    expect(agentNameOf(claim({ agent_name: "John" }))).toBe("John");
  });

  it("falls back to metadata.agent_name", () => {
    expect(agentNameOf(claim({ metadata: { agent_name: "Jane" } }))).toBe("Jane");
  });

  it("prefers the top-level key over metadata", () => {
    expect(
      agentNameOf(claim({ agent_name: "Top", metadata: { agent_name: "Meta" } })),
    ).toBe("Top");
  });

  it("returns undefined when absent or blank", () => {
    expect(agentNameOf(claim())).toBeUndefined();
    expect(agentNameOf(claim({ agent_name: "   " }))).toBeUndefined();
  });
});

describe("agentProvenanceLabel", () => {
  it("composes with the Hermes default client when an agent name is present", () => {
    expect(agentProvenanceLabel(claim({ agent_name: "John" }))).toBe("John (Hermes)");
    expect(AGENT_PROVENANCE_CLIENT).toBe("Hermes");
  });

  it("returns undefined when the claim has no agent name (zero-regression fallback)", () => {
    expect(agentProvenanceLabel(claim())).toBeUndefined();
  });
});
