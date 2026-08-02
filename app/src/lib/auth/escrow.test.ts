import { describe, it, expect } from "vitest";
import {
  deriveEscrowWrapSecret,
  deriveEscrowHandle,
  escrowLookupHash,
  sealEscrow,
  openEscrow,
  parseEscrowFile,
  sealedEscrowToFile,
  EscrowVersionUnsupportedError,
  ESCROW_FILE_FORMAT,
  ESCROW_FILE_VERSION,
} from "./escrow";
import { deriveMasterWrapSecret } from "./wrap";

function bytes(fill: number): Uint8Array {
  return new Uint8Array(32).fill(fill);
}

const PRF = bytes(0x00);
const MNEMONIC_12 =
  "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";
const MNEMONIC_24 =
  "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon " +
  "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art";

// Frozen golden vectors — HKDF-SHA256(ikm=32×0x00, salt=∅, info=..., L=32),
// computed independently and pinned here so an accidental info-string change
// (which would silently orphan every existing escrow record) fails loudly.
// See spa-passkey-unlock.md §10.1.
const GOLDEN_WRAP_SECRET_HEX = "b84d911af974ff057293fd2ddd4f65c679eeb9d2e776c183449af00b85d0ef41";
const GOLDEN_HANDLE_HEX = "3d23f08a3d9bb6236fb94a5b14289cb6fce937ade62afa8a5ee36f78a8eb2646";
const GOLDEN_LOOKUP_HASH_HEX = "ed1eb80ffb7ab5c9d7d535acdb3c8131d1b87f45a6fabda021cf710021f6583a";

function bytesToHex(b: Uint8Array): string {
  return Array.from(b)
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
}

describe("deriveEscrowWrapSecret", () => {
  it("matches the frozen golden vector for a zeroed PRF secret", () => {
    expect(bytesToHex(deriveEscrowWrapSecret(PRF))).toBe(GOLDEN_WRAP_SECRET_HEX);
  });

  it("is deterministic for a given PRF secret", () => {
    expect(deriveEscrowWrapSecret(PRF)).toEqual(deriveEscrowWrapSecret(PRF));
  });

  it("differs from deriveEscrowHandle and deriveMasterWrapSecret (domain separation)", () => {
    const wrap = deriveEscrowWrapSecret(PRF);
    const handle = deriveEscrowHandle(PRF);
    const master = deriveMasterWrapSecret(PRF);
    expect(wrap).not.toEqual(handle);
    expect(wrap).not.toEqual(master);
    expect(handle).not.toEqual(master);
  });

  it("rejects a non-32-byte PRF secret", () => {
    expect(() => deriveEscrowWrapSecret(new Uint8Array(16))).toThrow();
  });
});

describe("deriveEscrowHandle", () => {
  it("matches the frozen golden vector for a zeroed PRF secret", () => {
    expect(bytesToHex(deriveEscrowHandle(PRF))).toBe(GOLDEN_HANDLE_HEX);
  });

  it("is deterministic", () => {
    expect(deriveEscrowHandle(PRF)).toEqual(deriveEscrowHandle(PRF));
  });
});

describe("escrowLookupHash", () => {
  it("matches an independent SHA-256 of the handle (golden vector)", () => {
    const handle = deriveEscrowHandle(PRF);
    expect(bytesToHex(escrowLookupHash(handle))).toBe(GOLDEN_LOOKUP_HASH_HEX);
  });
});

describe("sealEscrow / openEscrow", () => {
  it("round-trips a 12-word mnemonic", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const handle = deriveEscrowHandle(PRF);
    expect(openEscrow(sealed, handle, PRF)).toBe(MNEMONIC_12);
  });

  it("round-trips a 24-word mnemonic", () => {
    const sealed = sealEscrow(MNEMONIC_24, PRF);
    const handle = deriveEscrowHandle(PRF);
    expect(openEscrow(sealed, handle, PRF)).toBe(MNEMONIC_24);
  });

  it("sealEscrow's lookupHashHex matches the golden vector", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    expect(sealed.lookupHashHex).toBe(GOLDEN_LOOKUP_HASH_HEX);
  });

  it("nonce is never reused across two seals of identical inputs", () => {
    const a = sealEscrow(MNEMONIC_12, PRF);
    const b = sealEscrow(MNEMONIC_12, PRF);
    expect(a.nonceHex).not.toBe(b.nonceHex);
    expect(a.ciphertextHex).not.toBe(b.ciphertextHex);
    // ...but both still decrypt to the same phrase.
    const handle = deriveEscrowHandle(PRF);
    expect(openEscrow(a, handle, PRF)).toBe(MNEMONIC_12);
    expect(openEscrow(b, handle, PRF)).toBe(MNEMONIC_12);
  });

  it("throws AEAD failure with the wrong prfSecret", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const wrongPrf = bytes(0x01);
    const wrongHandle = deriveEscrowHandle(wrongPrf);
    expect(() => openEscrow(sealed, wrongHandle, wrongPrf)).toThrow();
  });

  it("throws AEAD failure on tampered ciphertext", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const handle = deriveEscrowHandle(PRF);
    const tampered = { ...sealed, ciphertextHex: "00" + sealed.ciphertextHex.slice(2) };
    expect(() => openEscrow(tampered, handle, PRF)).toThrow();
  });

  it("throws AEAD failure on tampered nonce", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const handle = deriveEscrowHandle(PRF);
    const tampered = { ...sealed, nonceHex: "00" + sealed.nonceHex.slice(2) };
    expect(() => openEscrow(tampered, handle, PRF)).toThrow();
  });

  it("throws AEAD failure when the handle comes from a different PRF (mismatched AAD)", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const foreignHandle = deriveEscrowHandle(bytes(0x99));
    expect(() => openEscrow(sealed, foreignHandle, PRF)).toThrow();
  });

  it("rejects aadVersion: 2 with a distinct, non-retryable error — no decryption attempted", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const handle = deriveEscrowHandle(PRF);
    const futureBlob = { ...sealed, aadVersion: 2 };
    expect(() => openEscrow(futureBlob, handle, PRF)).toThrow(EscrowVersionUnsupportedError);
  });
});

describe("file export/restore (D-1 amendment)", () => {
  it("sealedEscrowToFile → parseEscrowFile round-trips the exact ciphertext/nonce", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const file = sealedEscrowToFile(sealed, new Date("2026-08-02T10:00:00.000Z"));
    const parsed = parseEscrowFile(JSON.stringify(file));
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) throw new Error("unreachable");
    // Byte-identical to what putEscrow would send for the same seal — the
    // single-envelope property (relay spec §7.1) must not silently fork.
    expect(parsed.blob.ciphertextHex).toBe(sealed.ciphertextHex);
    expect(parsed.blob.nonceHex).toBe(sealed.nonceHex);
    expect(parsed.blob.aadVersion).toBe(sealed.aadVersion);

    const handle = deriveEscrowHandle(PRF);
    expect(openEscrow(parsed.blob, handle, PRF)).toBe(MNEMONIC_12);
  });

  it("file contains no vault identifier — no smart_account, address, or lookup_hash key", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const file = sealedEscrowToFile(sealed) as unknown as Record<string, unknown>;
    expect(file).not.toHaveProperty("smart_account");
    expect(file).not.toHaveProperty("address");
    expect(file).not.toHaveProperty("lookup_hash");
    expect(file).not.toHaveProperty("lookupHashHex");
  });

  it("file carries the required format/version/note fields", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const file = sealedEscrowToFile(sealed);
    expect(file.format).toBe(ESCROW_FILE_FORMAT);
    expect(file.v).toBe(ESCROW_FILE_VERSION);
    expect(file.note.length).toBeGreaterThan(0);
  });

  it("rejects a malformed file without throwing", () => {
    expect(parseEscrowFile("not json").ok).toBe(false);
    expect(parseEscrowFile("{}").ok).toBe(false);
    expect(parseEscrowFile(JSON.stringify({ format: "something-else" })).ok).toBe(false);
  });

  it("flags an unsupported aad_version distinctly, without attempting decryption", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const file = { ...sealedEscrowToFile(sealed), aad_version: 2 };
    const parsed = parseEscrowFile(JSON.stringify(file));
    expect(parsed.ok).toBe(false);
    if (parsed.ok) throw new Error("unreachable");
    expect(parsed.error).toBe("unsupported-version");
  });

  it("a wrong-passkey file produces an AEAD failure from openEscrow, not a parse error", () => {
    const sealed = sealEscrow(MNEMONIC_12, PRF);
    const file = sealedEscrowToFile(sealed);
    const parsed = parseEscrowFile(JSON.stringify(file));
    if (!parsed.ok) throw new Error("unreachable");
    const wrongPrf = bytes(0x42);
    const wrongHandle = deriveEscrowHandle(wrongPrf);
    expect(() => openEscrow(parsed.blob, wrongHandle, wrongPrf)).toThrow();
  });
});
