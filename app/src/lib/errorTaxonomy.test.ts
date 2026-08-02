import { describe, it, expect } from "vitest";
import { mapUnlockError } from "./errorTaxonomy";
import { PrfUnsupportedError } from "./auth/passkey";
import { EscrowVersionUnsupportedError } from "./auth/escrow";

describe("mapUnlockError", () => {
  it("maps PrfUnsupportedError to prf-unsupported, keeping its own message", () => {
    const e = new PrfUnsupportedError();
    const mapped = mapUnlockError(e);
    expect(mapped.kind).toBe("prf-unsupported");
    expect(mapped.message).toBe(e.message);
  });

  it("maps EscrowVersionUnsupportedError to escrow-version-unsupported", () => {
    const mapped = mapUnlockError(new EscrowVersionUnsupportedError(2));
    expect(mapped.kind).toBe("escrow-version-unsupported");
    expect(mapped.message).toMatch(/newer version/i);
  });

  it("maps a NotAllowedError DOMException to cancelled", () => {
    const e = new DOMException("The operation either timed out or was not allowed", "NotAllowedError");
    const mapped = mapUnlockError(e);
    expect(mapped.kind).toBe("cancelled");
    expect(mapped.message).toBe("Unlock cancelled.");
  });

  it("maps the passkey.ts null-credential Error (message contains 'cancelled') to cancelled", () => {
    const mapped = mapUnlockError(new Error("Passkey unlock was cancelled."));
    expect(mapped.kind).toBe("cancelled");
  });

  it("maps a TypeError (fetch network failure) to offline", () => {
    const mapped = mapUnlockError(new TypeError("Failed to fetch"));
    expect(mapped.kind).toBe("offline");
    expect(mapped.message).toMatch(/can.t reach/i);
  });

  it('defaults to "unwrap-failed" with the LOCAL message for context "local"', () => {
    const mapped = mapUnlockError(new Error("bad decrypt"), "local");
    expect(mapped.kind).toBe("unwrap-failed");
    expect(mapped.message).toMatch(/try your recovery phrase/i);
  });

  it('defaults to "unwrap-failed" with the ESCROW message for context "escrow" — distinct from the local message', () => {
    const mapped = mapUnlockError(new Error("bad decrypt"), "escrow");
    expect(mapped.kind).toBe("unwrap-failed");
    expect(mapped.message).toMatch(/damaged/i);
  });

  it('defaults to "unwrap-failed" with the FILE message for context "file" — distinct from local and escrow', () => {
    const mapped = mapUnlockError(new Error("bad decrypt"), "file");
    expect(mapped.kind).toBe("unwrap-failed");
    expect(mapped.message).toMatch(/different passkey/i);
  });

  it("context defaults to local when omitted", () => {
    const mapped = mapUnlockError(new Error("bad decrypt"));
    expect(mapped.message).toMatch(/try your recovery phrase/i);
  });

  it("every mapped kind has a non-empty message (no state leaves the user without a forward path)", () => {
    const cases: unknown[] = [
      new PrfUnsupportedError(),
      new EscrowVersionUnsupportedError(2),
      new DOMException("x", "NotAllowedError"),
      new Error("cancelled"),
      new TypeError("Failed to fetch"),
      new Error("anything else"),
    ];
    for (const e of cases) {
      expect(mapUnlockError(e).message.length).toBeGreaterThan(0);
    }
  });
});
