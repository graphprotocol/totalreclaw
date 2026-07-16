/**
 * Vault export serializers (#323 — one-click plain-text portability).
 *
 * Both formats mirror the MCP server's `totalreclaw_export` output
 * (mcp/src/tools/export.ts) so an SPA export round-trips through the MCP
 * `totalreclaw_import` tool:
 *   - JSON: `{ version, exported_at, facts: [...] }` — the importer's
 *     `parseJsonContent` reads `facts[].text/importance/type/id` and
 *     tolerates every additional field.
 *   - Markdown: `## <text>` sections separated by `---` with
 *     `**Importance:**` / `**Type:**` bullets and an `ID: \`…\`` line —
 *     exactly what the importer's `parseMarkdownContent` matches.
 *
 * Everything here is pure serialization of already-decrypted items; the
 * plaintext never leaves the browser.
 */
import type { VaultItem } from "./types";

export const EXPORT_VERSION = "1.0.0";

/** `totalreclaw-export-YYYYMMDD.<ext>` (UTC date). */
export function exportFilename(ext: "json" | "md", now: Date = new Date()): string {
  const ymd = now.toISOString().slice(0, 10).replace(/-/g, "");
  return `totalreclaw-export-${ymd}.${ext}`;
}

/** v1 importance is a 1–10 integer; default to the rubric midpoint. */
function importanceOf(item: VaultItem): number {
  const raw = item.claim.importance;
  if (typeof raw !== "number" || Number.isNaN(raw)) return 5;
  // Legacy blobs occasionally carry the 0–1 normalized scale.
  const scaled = raw > 0 && raw <= 1 ? raw * 10 : raw;
  return Math.min(10, Math.max(1, Math.round(scaled)));
}

function exportedFact(item: VaultItem): Record<string, unknown> {
  const c = item.claim;
  const base: Record<string, unknown> = {
    id: item.id,
    text: c.text,
    importance: importanceOf(item),
    created_at: item.createdAt.toISOString(),
    type: item.type,
    source: c.source,
  };
  if (c.scope) base.scope = c.scope;
  if (c.volatility) base.volatility = c.volatility;
  if (c.reasoning) base.reasoning = c.reasoning;
  if (c.expires_at) base.expires_at = c.expires_at;
  if (c.confidence != null) base.confidence = c.confidence;
  if (c.superseded_by) base.superseded_by = c.superseded_by;
  if (c.entities?.length) base.entities = c.entities;
  // Additive fields the MCP export doesn't carry (importers ignore them).
  if (c.agent_name) base.agent_name = c.agent_name;
  if (item.pinned) base.pin_status = c.pin_status ?? "pinned";
  base.metadata = {
    tags: c.tags ?? [],
    source: c.source,
    ...(c.metadata?.session_id ? { session_id: c.metadata.session_id } : {}),
  };
  return base;
}

export function toExportJson(items: VaultItem[], now: Date = new Date()): string {
  return JSON.stringify(
    {
      version: EXPORT_VERSION,
      exported_at: now.toISOString(),
      facts: items.map(exportedFact),
    },
    null,
    2,
  );
}

export function toExportMarkdown(items: VaultItem[], now: Date = new Date()): string {
  const lines: string[] = [
    `# TotalReclaw Export`,
    ``,
    `**Exported:** ${now.toISOString()}`,
    `**Total Facts:** ${items.length}`,
    ``,
    `---`,
    ``,
  ];

  for (const item of items) {
    const c = item.claim;
    lines.push(`## ${c.text}`);
    lines.push(``);
    lines.push(`- **Importance:** ${importanceOf(item)}/10`);
    lines.push(`- **Created:** ${item.createdAt.toISOString()}`);
    lines.push(`- **Type:** ${item.type}`);
    lines.push(`- **Source:** ${c.source}`);
    if (c.scope) lines.push(`- **Scope:** ${c.scope}`);
    if (c.reasoning) lines.push(`- **Reasoning:** ${c.reasoning}`);
    if (c.volatility) lines.push(`- **Volatility:** ${c.volatility}`);
    if (c.tags?.length) lines.push(`- **Tags:** ${c.tags.join(", ")}`);
    lines.push(``);
    lines.push(`ID: \`${item.id}\``);
    lines.push(``);
    lines.push(`---`);
    lines.push(``);
  }

  return lines.join("\n");
}
