/**
 * #323 — vault export serializers.
 *
 * The contract under test is MCP round-trip compatibility: the JSON shape
 * must satisfy `parseJsonContent` in mcp/src/tools/import.ts (a `facts`
 * array whose items carry `text` + optional `importance`/`type`/`id`), and
 * the markdown must satisfy `parseMarkdownContent` (`## <text>` sections
 * split by `---`, `**Importance:**` / `**Type:**` bullets, `ID: \`…\``).
 * The markdown assertions below literally apply the importer's regexes.
 */
import { describe, it, expect } from "vitest";
import { toExportJson, toExportMarkdown, exportFilename, EXPORT_VERSION } from "./export";
import type { VaultItem, MemoryClaimV1 } from "./types";

function item(overrides: Partial<MemoryClaimV1> = {}, extra: Partial<VaultItem> = {}): VaultItem {
  const claim: MemoryClaimV1 = {
    id: "fact-1",
    text: "Pedro lives in Porto",
    type: "claim",
    source: "user",
    created_at: "2026-07-01T10:00:00.000Z",
    schema_version: "1.0",
    importance: 7,
    ...overrides,
  };
  return {
    id: claim.id,
    claim,
    type: claim.type,
    pinned: false,
    createdAt: new Date("2026-07-01T10:00:00.000Z"),
    rawBlob: "00",
    blindIndices: [],
    decayScore: 100,
    isActive: true,
    ...extra,
  };
}

const NOW = new Date("2026-07-16T00:00:00.000Z");

describe("toExportJson", () => {
  it("emits the MCP export envelope: version + exported_at + facts[]", () => {
    const parsed = JSON.parse(toExportJson([item()], NOW));
    expect(parsed.version).toBe(EXPORT_VERSION);
    expect(parsed.exported_at).toBe(NOW.toISOString());
    expect(Array.isArray(parsed.facts)).toBe(true);
    expect(parsed.facts).toHaveLength(1);
  });

  it("each fact carries the importer-required keys with exact values", () => {
    const f = JSON.parse(toExportJson([item()], NOW)).facts[0];
    expect(f.id).toBe("fact-1");
    expect(f.text).toBe("Pedro lives in Porto");
    expect(f.type).toBe("claim");
    expect(f.importance).toBe(7);
    expect(f.created_at).toBe("2026-07-01T10:00:00.000Z");
    expect(f.source).toBe("user");
    expect(f.metadata).toEqual({ tags: [], source: "user" });
  });

  it("surfaces optional v1 fields only when present", () => {
    const rich = JSON.parse(
      toExportJson(
        [
          item({
            scope: "work",
            volatility: "stable",
            reasoning: "chose X because Y",
            entities: [{ name: "Porto", type: "place" }] as MemoryClaimV1["entities"],
            agent_name: "John",
            metadata: { session_id: "s-42" },
          }),
        ],
        NOW,
      ),
    ).facts[0];
    expect(rich.scope).toBe("work");
    expect(rich.volatility).toBe("stable");
    expect(rich.reasoning).toBe("chose X because Y");
    expect(rich.entities).toHaveLength(1);
    expect(rich.agent_name).toBe("John");
    expect(rich.metadata.session_id).toBe("s-42");

    const bare = JSON.parse(toExportJson([item()], NOW)).facts[0];
    for (const k of ["scope", "volatility", "reasoning", "entities", "agent_name", "pin_status"]) {
      expect(bare).not.toHaveProperty(k);
    }
  });

  it("normalizes legacy 0–1 importance to the 1–10 scale and defaults to 5", () => {
    expect(JSON.parse(toExportJson([item({ importance: 0.9 })], NOW)).facts[0].importance).toBe(9);
    expect(JSON.parse(toExportJson([item({ importance: undefined })], NOW)).facts[0].importance).toBe(5);
  });
});

describe("toExportMarkdown (importer-regex round-trip)", () => {
  const md = toExportMarkdown(
    [item(), item({ id: "fact-2", text: "Prefers dark mode", type: "preference", importance: 4 })],
    NOW,
  );

  it("parses back with the exact mcp parseMarkdownContent logic", () => {
    // Verbatim port of mcp/src/tools/import.ts parseMarkdownContent.
    const sections = md.split(/^---$/m);
    const facts: Array<{ text: string; importance?: number; type?: string; id?: string }> = [];
    for (const section of sections) {
      const headingMatch = section.match(/^##\s+(.+)$/m);
      if (!headingMatch) continue;
      const text = headingMatch[1].trim();
      const impMatch = section.match(/\*\*Importance:\*\*\s*(\d+)/);
      const typeMatch = section.match(/\*\*Type:\*\*\s*(\w+)/);
      const idMatch = section.match(/ID:\s*`([^`]+)`/);
      facts.push({
        text,
        importance: impMatch ? parseInt(impMatch[1], 10) : undefined,
        type: typeMatch?.[1],
        id: idMatch?.[1],
      });
    }
    expect(facts).toEqual([
      { text: "Pedro lives in Porto", importance: 7, type: "claim", id: "fact-1" },
      { text: "Prefers dark mode", importance: 4, type: "preference", id: "fact-2" },
    ]);
  });

  it("renders every item plus the export header", () => {
    expect(md).toContain("# TotalReclaw Export");
    expect(md).toContain(`**Exported:** ${NOW.toISOString()}`);
    expect(md).toContain("**Total Facts:** 2");
    expect(md).toContain("- **Source:** user");
  });
});

describe("exportFilename", () => {
  it("stamps the UTC date", () => {
    expect(exportFilename("json", NOW)).toBe("totalreclaw-export-20260716.json");
    expect(exportFilename("md", NOW)).toBe("totalreclaw-export-20260716.md");
  });
});
