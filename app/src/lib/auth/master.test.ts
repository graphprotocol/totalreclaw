import { describe, it, expect, vi } from "vitest";
import { runWithMasterKey } from "./master";
import { wrapKey, deriveMasterWrapSecret } from "./wrap";

function bytes(fill: number): Uint8Array {
  return new Uint8Array(32).fill(fill);
}

const PRF = bytes(0x07); // stand-in PRF secret
// A stand-in "master wallet private key" (any 32 bytes — never a real key here).
const MASTER = new Uint8Array(32).map((_, i) => (i * 7 + 3) & 0xff);

describe("runWithMasterKey", () => {
  it("unwraps wrapped_master_key under the PRF-derived secret and passes it to fn", async () => {
    const wrapped = wrapKey(MASTER, deriveMasterWrapSecret(PRF));
    const seen = vi.fn(async (mp: Uint8Array) => {
      expect(Array.from(mp)).toEqual(Array.from(MASTER));
      return "signed";
    });
    const out = await runWithMasterKey(wrapped, PRF, seen);
    expect(out).toBe("signed");
    expect(seen).toHaveBeenCalledOnce();
  });

  it("zeroes the unwrapped master key after fn returns", async () => {
    const wrapped = wrapKey(MASTER, deriveMasterWrapSecret(PRF));
    let captured: Uint8Array | null = null;
    await runWithMasterKey(wrapped, PRF, async (mp) => {
      captured = mp;
      return null;
    });
    expect(captured).not.toBeNull();
    expect(Array.from(captured!)).toEqual(Array.from(new Uint8Array(32))); // all zero
  });

  it("zeroes the key even when fn throws", async () => {
    const wrapped = wrapKey(MASTER, deriveMasterWrapSecret(PRF));
    let captured: Uint8Array | null = null;
    await expect(
      runWithMasterKey(wrapped, PRF, async (mp) => {
        captured = mp;
        throw new Error("signer blew up");
      }),
    ).rejects.toThrow("signer blew up");
    expect(Array.from(captured!)).toEqual(Array.from(new Uint8Array(32)));
  });

  it("throws (AEAD failure) if the PRF secret is wrong — never yields a key", async () => {
    const wrapped = wrapKey(MASTER, deriveMasterWrapSecret(PRF));
    const fn = vi.fn();
    await expect(runWithMasterKey(wrapped, bytes(0x08), fn)).rejects.toThrow();
    expect(fn).not.toHaveBeenCalled();
  });
});
