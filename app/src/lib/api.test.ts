import { describe, it, expect } from "vitest";
import { decryptFacts } from "./api";
import { encryptBlob } from "./crypto";
import type { MemoryClaimV1, RawFact, SessionKeys } from "./types";

// A fixed 32-byte encryption key is all decryptFacts needs; the other
// SessionKeys fields are unused on the read path.
const ENC_KEY = new Uint8Array(32).fill(7);
const KEYS = { encryptionKey: ENC_KEY } as unknown as SessionKeys;

function baseClaim(overrides: Partial<MemoryClaimV1> = {}): MemoryClaimV1 {
  return {
    id: "claim-1",
    text: "I fly out of Lisbon on Tuesday",
    type: "claim",
    source: "user",
    created_at: "2026-07-01T09:00:00.000Z",
    schema_version: "1.0",
    ...overrides,
  };
}

function rawFactFor(claim: MemoryClaimV1, id = "fact-1"): RawFact {
  return {
    id,
    encrypted_blob: encryptBlob(JSON.stringify(claim), ENC_KEY),
    blind_indices: [],
    decay_score: 1,
    version: 4,
    source: claim.source,
    created_at: claim.created_at,
    updated_at: claim.created_at,
  };
}

describe("decryptFacts — sessionId surfacing", () => {
  it("surfaces claim.metadata.session_id as VaultItem.sessionId", () => {
    const claim = baseClaim({
      metadata: { session_id: "01902d40-7a2b-7f12-9c44-1c5e7d2af6a1" },
    });
    const [item] = decryptFacts([rawFactFor(claim)], KEYS);
    expect(item!.sessionId).toBe("01902d40-7a2b-7f12-9c44-1c5e7d2af6a1");
  });

  it("falls back to null when the blob carries no metadata", () => {
    const [item] = decryptFacts([rawFactFor(baseClaim())], KEYS);
    expect(item!.sessionId).toBeNull();
  });

  it("falls back to null when metadata exists without a session_id", () => {
    const claim = baseClaim({ metadata: { subtype: "session_crystal" } });
    const [item] = decryptFacts([rawFactFor(claim)], KEYS);
    expect(item!.sessionId).toBeNull();
  });

  it("keeps each fact's own session id across a mixed batch", () => {
    const a = baseClaim({ id: "a", metadata: { session_id: "sess-a" } });
    const b = baseClaim({ id: "b", text: "unrelated", metadata: { session_id: "sess-b" } });
    const c = baseClaim({ id: "c", text: "legacy" }); // no metadata
    const items = decryptFacts(
      [rawFactFor(a, "fa"), rawFactFor(b, "fb"), rawFactFor(c, "fc")],
      KEYS,
    );
    expect(items.map((i) => i.sessionId)).toEqual(["sess-a", "sess-b", null]);
  });
});
