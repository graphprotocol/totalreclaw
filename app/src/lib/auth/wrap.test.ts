import { describe, it, expect } from "vitest";
import { wrapKey, unwrapKey, deriveMasterWrapSecret } from "./wrap";

function bytes(fill: number): Uint8Array {
  return new Uint8Array(32).fill(fill);
}

const SECRET = bytes(0x00); // stand-in PRF secret
const KEY = bytes(0x11); // stand-in vault key

describe("wrapKey / unwrapKey", () => {
  it("round-trips a 32-byte key", () => {
    const blob = wrapKey(KEY, SECRET);
    expect(unwrapKey(blob, SECRET)).toEqual(KEY);
  });

  it("uses a fresh 24-byte nonce per wrap (non-deterministic ciphertext)", () => {
    const a = wrapKey(KEY, SECRET);
    const b = wrapKey(KEY, SECRET);
    expect(a.nonce.length).toBe(24);
    expect(a.nonce).not.toEqual(b.nonce);
    expect(a.ciphertext).not.toEqual(b.ciphertext);
    // ...but both unwrap to the same key
    expect(unwrapKey(a, SECRET)).toEqual(unwrapKey(b, SECRET));
  });

  it("fails AEAD on tampered ciphertext", () => {
    const blob = wrapKey(KEY, SECRET);
    blob.ciphertext[0] ^= 0xff;
    expect(() => unwrapKey(blob, SECRET)).toThrow();
  });

  it("fails with the wrong secret", () => {
    const blob = wrapKey(KEY, SECRET);
    expect(() => unwrapKey(blob, bytes(0x22))).toThrow();
  });

  it("rejects non-32-byte keys and secrets", () => {
    expect(() => wrapKey(new Uint8Array(16), SECRET)).toThrow();
    expect(() => wrapKey(KEY, new Uint8Array(16))).toThrow();
  });
});

describe("deriveMasterWrapSecret", () => {
  it("is deterministic for a given PRF secret", () => {
    expect(deriveMasterWrapSecret(SECRET)).toEqual(deriveMasterWrapSecret(SECRET));
  });

  it("is domain-separated from the raw PRF secret (vault vs master)", () => {
    const m = deriveMasterWrapSecret(SECRET);
    expect(m).not.toEqual(SECRET);
    expect(m.length).toBe(32);
  });

  it("wrapping the same key under vault vs master secret yields distinct blobs that each unwrap correctly", () => {
    const masterSecret = deriveMasterWrapSecret(SECRET);
    const vaultBlob = wrapKey(KEY, SECRET);
    const masterBlob = wrapKey(KEY, masterSecret);
    expect(unwrapKey(vaultBlob, SECRET)).toEqual(KEY);
    expect(unwrapKey(masterBlob, masterSecret)).toEqual(KEY);
    // cross-unwrap must fail (independent wrap entries)
    expect(() => unwrapKey(masterBlob, SECRET)).toThrow();
  });
});
