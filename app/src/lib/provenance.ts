/**
 * Agent-instance provenance rendering (#317 — SPA display half).
 *
 * The data layer (#473) has Hermes write an optional `agent_name` into the
 * encrypted memory blob. This module turns that raw signal into the human
 * provenance line the Keeper vault renders — e.g. "John (Hermes)".
 *
 * `agent_name` is user-controlled free text. Every value returned here is a
 * plain string rendered through normal JSX auto-escaping at the call site —
 * never via innerHTML/dangerouslySetInnerHTML.
 */
import type { MemoryClaimV1 } from "./types";

/**
 * The client that currently authors `agent_name`. Capture is Hermes-only today
 * (the encrypted blob carries no per-item client field, and #317's Known Gap
 * records that other clients only capture+write agent_name once un-parked). So
 * when a memory carries an agent_name, the writing client is Hermes. This is a
 * documented default, not an invented data source — revisit when another client
 * begins stamping agent_name (at which point it would also stamp its client).
 */
export const AGENT_PROVENANCE_CLIENT = "Hermes";

/** Raw client token (X-TotalReclaw-Client-style) → friendly display name. */
const CLIENT_LABELS: Record<string, string> = {
  hermes: "Hermes",
  "python-client": "Hermes",
  python: "Hermes",
  openclaw: "OpenClaw",
  nanoclaw: "NanoClaw",
  mcp: "MCP",
  "mcp-server": "MCP",
  zeroclaw: "ZeroClaw",
  "rust-client": "ZeroClaw",
  "rust-client:zeroclaw": "ZeroClaw",
};

/** Map a raw client token to a friendly name. Unknown tokens pass through
 *  trimmed (callers may already hand us a friendly value like "Hermes"). */
export function humanizeClient(raw: string): string {
  const trimmed = raw.trim();
  return CLIENT_LABELS[trimmed.toLowerCase()] ?? trimmed;
}

/**
 * Render a human provenance label from a client type + optional agent name.
 * Mirrors the Python `compose_provenance_label` semantics:
 *   composeProvenanceLabel("hermes", "John") === "John (Hermes)"
 *   composeProvenanceLabel("hermes", undefined) === "Hermes"
 * An empty/whitespace-only agentName is treated as absent (client only), so a
 * blob with a blank agent_name reads exactly as today.
 */
export function composeProvenanceLabel(client: string, agentName?: string): string {
  const label = humanizeClient(client);
  const name = agentName?.trim();
  return name ? `${name} (${label})` : label;
}

/** The trimmed agent_name for a claim, or undefined when absent/blank.
 *  Canonical wire location is the top-level `agent_name` key (Hermes write
 *  path); `metadata.agent_name` is checked as a fallback for paths that nest
 *  it (e.g. the Python recall reconstruction). */
export function agentNameOf(claim: MemoryClaimV1): string | undefined {
  const raw = claim.agent_name ?? claim.metadata?.agent_name;
  const name = raw?.trim();
  return name ? name : undefined;
}

/** Full provenance label for a claim when it carries an agent_name, else
 *  undefined so callers fall back to their existing client/source display with
 *  zero visual change. */
export function agentProvenanceLabel(claim: MemoryClaimV1): string | undefined {
  const name = agentNameOf(claim);
  return name ? composeProvenanceLabel(AGENT_PROVENANCE_CLIENT, name) : undefined;
}
