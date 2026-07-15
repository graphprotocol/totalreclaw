/**
 * Claim rebuilder for 2-call supersession writes (A.2 Phase 2 — pin/unpin).
 *
 * Mirrors `mcp/src/claims-helper.ts:buildV1ClaimBlob` byte-compatibly: the new
 * claim is the old decrypted claim with ONLY `id` / `pin_status` /
 * `superseded_by` overridden, canonical omission rules applied (omit `scope`
 * when `unspecified`, `volatility` when `updatable`), validated through core
 * `validateMemoryClaimV1`, then `schema_version` + `metadata` re-attached after
 * validation (core's serde drops default schema_version and round-trips
 * `metadata` through a TYPED struct that strips unknown keys — the known trap;
 * re-attaching verbatim is what keeps `session_id` / `subtype:
 * "session_crystal"` / Crystal lists alive through supersession).
 *
 * Beyond the MCP builder, this carries EVERY top-level source field forward —
 * including ones the SPA's MemoryClaimV1 type doesn't name (`agent_name`,
 * `tags`, `supersedes`, unknown future fields) — so a pin from the web app
 * never silently drops another client's data (forward-compat).
 *
 * WRITE-path module: reached only via dynamic `import()` from `api.ts`. The
 * core WASM instance is injected (see `wasm.ts:loadCore`) so node-side tests
 * can pass an `initSync`-loaded core.
 */
import type { PinStatus } from "./types";

/** The single core export this module needs (injected for testability). */
export interface ClaimValidator {
  validateMemoryClaimV1(claimJson: string): string;
}

export interface SupersessionOverrides {
  /** Fresh UUID for the superseding claim (protobuf field 1 / Fact.id). */
  newId: string;
  /** The old fact id being tombstoned — becomes `superseded_by`. */
  supersededBy: string;
  /** 'pinned' (pin) or 'unpinned' (unpin) — always emitted explicitly. */
  pinStatus: PinStatus;
}

/**
 * Rebuild the decrypted source claim JSON into the superseding claim JSON.
 *
 * `rawClaimJson` is the plaintext of `VaultItem.rawBlob` (NOT the normalized
 * `VaultItem.claim` — normalization clamps enums, which must not leak into
 * what we re-encrypt). Returns canonical JSON ready for `encryptBlob`.
 *
 * Throws when the source is not a v1-shaped claim (pre-v1 vault entries are
 * not pin-supported from the web app — same as they aren't readable here).
 */
export function rebuildClaimJson(
  core: ClaimValidator,
  rawClaimJson: string,
  overrides: SupersessionOverrides,
): string {
  let raw: unknown;
  try {
    raw = JSON.parse(rawClaimJson);
  } catch {
    throw new Error("Source memory is not a JSON claim.");
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error("Source memory is not a JSON claim.");
  }
  const source = raw as Record<string, unknown>;
  if (
    typeof source.text !== "string" ||
    typeof source.type !== "string" ||
    typeof source.source !== "string"
  ) {
    throw new Error(
      "This memory was written in a pre-v1 format the web app can't rewrite. " +
        "Use a TotalReclaw agent to pin it.",
    );
  }

  // Full carry-forward: spread the decrypted source, override only the three
  // supersession fields. `created_at` is preserved (the memory's original
  // creation time — keeps timeline ordering stable through a pin).
  const input: Record<string, unknown> = { ...source };
  input.id = overrides.newId;
  input.superseded_by = overrides.supersededBy;
  input.pin_status = overrides.pinStatus;
  input.schema_version = "1.0";
  if (typeof input.created_at !== "string" || input.created_at === "") {
    input.created_at = new Date().toISOString();
  }

  // Canonical omission rules (claims-helper.ts:buildV1ClaimBlob) — defaults
  // and empty optionals are omitted from the wire, not serialized.
  if (input.scope === "unspecified") delete input.scope;
  if (input.volatility === "updatable") delete input.volatility;
  if (Array.isArray(input.entities) && input.entities.length === 0) delete input.entities;
  if (!input.reasoning) delete input.reasoning;
  if (!input.expires_at) delete input.expires_at;

  // Canonicalise + validate via core (throws on schema violations — bad enum
  // members, text length, etc.). MemoryClaimV1 tolerates unknown fields on
  // parse but STRIPS them on serialize, so everything unmodeled is re-attached
  // below from `input`.
  const validated = core.validateMemoryClaimV1(JSON.stringify(input));
  const parsed = JSON.parse(validated) as Record<string, unknown>;

  // Re-attach schema_version (core drops it when it equals the default) so
  // the output matches the plugin/MCP wire byte-for-byte.
  parsed.schema_version = "1.0";

  // Re-attach `metadata` VERBATIM after validation (claims-helper.ts:418).
  // Core round-trips metadata through the typed MemoryMetadataV1 struct which
  // silently drops unknown keys and all-default payloads.
  if (input.metadata !== undefined && input.metadata !== null) {
    parsed.metadata = input.metadata;
  }

  // Forward-compat: restore every top-level field core stripped (agent_name,
  // tags, supersedes, unknown future fields). Fields core KEPT stay canonical.
  for (const [key, value] of Object.entries(input)) {
    if (!(key in parsed) && value !== undefined) {
      parsed[key] = value;
    }
  }

  return JSON.stringify(parsed);
}
