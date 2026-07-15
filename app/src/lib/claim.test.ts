/**
 * A.2 Phase 2 — supersession claim rebuilder tests.
 *
 * Exercises the REAL web-target `@totalreclaw/core` WASM validator (initSync
 * from disk bytes, like userop.golden.test.ts) — the exact validate-then-
 * reattach path the browser runs on pin/unpin. The Crystal round-trip test is
 * the metadata-preservation proof from the plan: pin a Crystal → re-encrypt →
 * decryptFacts → buildTimeline still buckets under `s:<session_id>` with
 * `.crystal` set and the item pinned.
 */
import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { rebuildClaimJson } from "./claim";
import { decryptFacts } from "./api";
import { buildTimeline } from "./vault/timeline";
import { encryptBlob } from "./crypto";
import type { RawFact, SessionKeys } from "./types";

type Core = typeof import("@totalreclaw/core/web");
let core: Core;

beforeAll(async () => {
  const require = createRequire(import.meta.url);
  const dir = dirname(require.resolve("@totalreclaw/core/web"));
  core = (await import("@totalreclaw/core/web")) as unknown as Core;
  (core as unknown as { initSync: (m: { module: Uint8Array }) => void }).initSync({
    module: readFileSync(join(dir, "totalreclaw_core_bg.wasm")),
  });
});

const OVERRIDES = {
  newId: "11111111-2222-4333-8444-555555555555",
  supersededBy: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  pinStatus: "pinned" as const,
};

const FULL_SOURCE_CLAIM = {
  id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  text: "Chose Gnosis over Base because gas is sponsored end-to-end",
  type: "claim",
  source: "user",
  created_at: "2026-06-01T10:20:30.000Z",
  schema_version: "1.0",
  scope: "work",
  volatility: "stable",
  entities: [{ name: "Gnosis", type: "company" }],
  reasoning: "gas is sponsored end-to-end",
  expires_at: "2027-01-01T00:00:00.000Z",
  importance: 8,
  confidence: 0.9,
  tags: ["legacy-tag"],
  supersedes: ["00000000-0000-4000-8000-000000000000"],
  agent_name: "John",
  metadata: {
    subtype: "session_crystal",
    session_id: "0197f000-aaaa-7000-8000-abcdefabcdef",
    key_outcomes: ["shipped the thing"],
    open_threads: ["follow up on QA"],
    topics_discussed: ["gnosis", "gas"],
    custom_future_metadata_key: "must survive",
  },
  custom_future_top_level: { keep: true },
};

describe("rebuildClaimJson — full carry-forward + overrides", () => {
  it("overrides only id / pin_status / superseded_by; everything else survives", () => {
    const out = JSON.parse(
      rebuildClaimJson(core, JSON.stringify(FULL_SOURCE_CLAIM), OVERRIDES),
    ) as Record<string, unknown>;

    expect(out.id).toBe(OVERRIDES.newId);
    expect(out.pin_status).toBe("pinned");
    expect(out.superseded_by).toBe(OVERRIDES.supersededBy);

    // Modeled fields carried forward (created_at preserved, not reset).
    expect(out.text).toBe(FULL_SOURCE_CLAIM.text);
    expect(out.type).toBe("claim");
    expect(out.source).toBe("user");
    expect(out.created_at).toBe("2026-06-01T10:20:30.000Z");
    expect(out.schema_version).toBe("1.0");
    expect(out.scope).toBe("work");
    expect(out.volatility).toBe("stable");
    expect(out.entities).toEqual([{ name: "Gnosis", type: "company" }]);
    expect(out.reasoning).toBe(FULL_SOURCE_CLAIM.reasoning);
    expect(out.expires_at).toBe(FULL_SOURCE_CLAIM.expires_at);
    expect(out.importance).toBe(8);
    expect(out.confidence).toBe(0.9);

    // Unmodeled / future fields survive core's serialize-strip (forward-compat).
    expect(out.tags).toEqual(["legacy-tag"]);
    expect(out.supersedes).toEqual(["00000000-0000-4000-8000-000000000000"]);
    expect(out.agent_name).toBe("John");
    expect(out.custom_future_top_level).toEqual({ keep: true });
  });

  it("re-attaches metadata VERBATIM after validation (unknown metadata keys survive)", () => {
    const out = JSON.parse(
      rebuildClaimJson(core, JSON.stringify(FULL_SOURCE_CLAIM), OVERRIDES),
    ) as Record<string, unknown>;
    // Byte-verbatim: core's typed MemoryMetadataV1 would drop the unknown key.
    expect(out.metadata).toEqual(FULL_SOURCE_CLAIM.metadata);
  });

  it("emits pin_status 'unpinned' on the unpin path", () => {
    const out = JSON.parse(
      rebuildClaimJson(core, JSON.stringify(FULL_SOURCE_CLAIM), {
        ...OVERRIDES,
        pinStatus: "unpinned",
      }),
    ) as Record<string, unknown>;
    expect(out.pin_status).toBe("unpinned");
  });

  it("applies the canonical omission rules (scope unspecified / volatility updatable / empty optionals)", () => {
    const src = {
      id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
      text: "Prefers dark mode in every editor",
      type: "preference",
      source: "user-inferred",
      created_at: "2026-06-01T10:20:30.000Z",
      schema_version: "1.0",
      scope: "unspecified",
      volatility: "updatable",
      entities: [],
      reasoning: "",
    };
    const out = JSON.parse(
      rebuildClaimJson(core, JSON.stringify(src), OVERRIDES),
    ) as Record<string, unknown>;
    expect("scope" in out).toBe(false);
    expect("volatility" in out).toBe(false);
    expect("entities" in out).toBe(false);
    expect("reasoning" in out).toBe(false);
    expect(out.pin_status).toBe("pinned");
  });

  it("rejects a pre-v1 (short-key) blob instead of inventing fields", () => {
    const v0 = { t: "legacy short-key text", c: "pref", i: 7 };
    expect(() =>
      rebuildClaimJson(core, JSON.stringify(v0), OVERRIDES),
    ).toThrow(/pre-v1/);
  });

  it("rejects non-JSON plaintext", () => {
    expect(() => rebuildClaimJson(core, "not json", OVERRIDES)).toThrow(
      /not a JSON claim/,
    );
  });

  it("rejects invalid enum members (core validation is live, not bypassed)", () => {
    const bad = { ...FULL_SOURCE_CLAIM, type: "not-a-type" };
    expect(() =>
      rebuildClaimJson(core, JSON.stringify(bad), OVERRIDES),
    ).toThrow();
  });
});

// ---------------------------------------------------------------------------
// Crystal round-trip — the metadata-preservation proof from the plan.
// ---------------------------------------------------------------------------

const GOLDEN_ENC_KEY_HEX =
  "a58fdc56e1d768461d95cd46b49e03727b2eb342ac558b9f3ebf1255b871f703";
const ENC_KEY = new Uint8Array(
  GOLDEN_ENC_KEY_HEX.match(/../g)!.map((b) => parseInt(b, 16)),
);
const KEYS: SessionKeys = {
  authKey: new Uint8Array(),
  encryptionKey: ENC_KEY,
  authKeyHex: "",
  eoaAddress: "0x0",
  walletAddress: "0x0",
  chainId: 100,
};

function rawFact(id: string, blobHex: string): RawFact {
  return {
    id,
    encrypted_blob: blobHex,
    blind_indices: [],
    decay_score: 1,
    version: 4,
    source: "",
    created_at: "2026-07-01T00:00:00.000Z",
    updated_at: "2026-07-01T00:00:00.000Z",
    is_active: true,
  };
}

describe("Crystal pin round-trip (decrypt → rebuild → re-encrypt → timeline)", () => {
  it("keeps the session bucket, the crystal flag, and flips pinned:true", () => {
    const sessionId = "0197f000-aaaa-7000-8000-abcdefabcdef";
    const crystalClaim = {
      id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
      text: "Session crystal: shipped A.2 Phase 2 and validated on staging",
      type: "summary",
      source: "derived",
      created_at: "2026-07-01T00:00:00.000Z",
      schema_version: "1.0",
      importance: 9,
      metadata: {
        subtype: "session_crystal",
        session_id: sessionId,
        key_outcomes: ["pin/unpin shipped"],
        open_threads: ["Phase 3 retype"],
        topics_discussed: ["supersession"],
      },
    };

    // 1. The Crystal as it sits on-chain today.
    const sourceBlobHex = encryptBlob(JSON.stringify(crystalClaim), ENC_KEY);
    const [sourceItem] = decryptFacts([rawFact(crystalClaim.id, sourceBlobHex)], KEYS);
    expect(sourceItem.claim.metadata?.session_id).toBe(sessionId);
    expect(sourceItem.pinned).toBe(false);

    // 2. Pin it: rebuild from the decrypted rawBlob plaintext + re-encrypt —
    //    exactly what setPinStatus does.
    const rebuilt = rebuildClaimJson(
      core,
      JSON.stringify(crystalClaim),
      { newId: "11111111-2222-4333-8444-555555555555", supersededBy: crystalClaim.id, pinStatus: "pinned" },
    );
    const newBlobHex = encryptBlob(rebuilt, ENC_KEY);

    // 3. Read the superseding fact back like a fresh vault load.
    const [pinnedItem] = decryptFacts(
      [rawFact("11111111-2222-4333-8444-555555555555", newBlobHex)],
      KEYS,
    );
    expect(pinnedItem.pinned).toBe(true);
    expect(pinnedItem.claim.superseded_by).toBe(crystalClaim.id);
    expect(pinnedItem.claim.metadata).toEqual(crystalClaim.metadata);

    // 4. The timeline still buckets it as the session's Crystal.
    const groups = buildTimeline([pinnedItem]);
    expect(groups).toHaveLength(1);
    expect(groups[0].key).toBe(`s:${sessionId}`);
    expect(groups[0].crystal).not.toBeNull();
    expect(groups[0].crystal!.pinned).toBe(true);
    expect(groups[0].openThreads).toBe(1);
  });
});
