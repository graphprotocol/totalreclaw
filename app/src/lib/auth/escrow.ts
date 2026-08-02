/**
 * Phrase escrow — passkey-wrapped recovery phrase held by the relay as
 * ciphertext it cannot open. See docs/specs/web/phrase-escrow-relay.md
 * (internal repo) and docs/specs/web/spa-passkey-unlock.md §3.
 *
 * Pure functions, no I/O, fully unit-testable. No new dependency: `hkdf`/
 * `sha256` from `@noble/hashes` and `xchacha20poly1305` from `@noble/ciphers`
 * are already used by wrap.ts.
 *
 * INVARIANTS:
 *   - Never log, print, or transmit `prfSecret`, `escrow_handle`, or the mnemonic.
 *   - The handle is a live capability: body-only transport, never a URL.
 *   - Every function taking `prfSecret` treats it as BORROWED — it does not
 *     zero it. The caller owns its lifetime (matches wrap.ts / master.ts).
 */
import { xchacha20poly1305 } from "@noble/ciphers/chacha";
import { hkdf } from "@noble/hashes/hkdf";
import { sha256 } from "@noble/hashes/sha256";

const KEY_LEN = 32;
const NONCE_LEN = 24;
const MAX_CIPHERTEXT_BYTES = 512; // matches phrase-escrow-relay.md §2.1

function assertKeyLen(name: string, bytes: Uint8Array) {
  if (bytes.length !== KEY_LEN) throw new Error(`${name} must be ${KEY_LEN} bytes`);
}

/** KEK that encrypts the phrase. Domain-separated from the vault/master wraps. */
export function deriveEscrowWrapSecret(prfSecret: Uint8Array): Uint8Array {
  assertKeyLen("deriveEscrowWrapSecret: prfSecret", prfSecret);
  return hkdf(sha256, prfSecret, undefined, "tr-escrow-wrap-v1", KEY_LEN);
}

/** Unguessable capability that addresses this passkey's escrow record. */
export function deriveEscrowHandle(prfSecret: Uint8Array): Uint8Array {
  assertKeyLen("deriveEscrowHandle: prfSecret", prfSecret);
  return hkdf(sha256, prfSecret, undefined, "tr-escrow-lookup-v1", KEY_LEN);
}

/** What the relay stores. The handle itself never leaves the client on write. */
export function escrowLookupHash(handle: Uint8Array): Uint8Array {
  return sha256(handle);
}

export interface SealedEscrow {
  lookupHashHex: string;
  ciphertextHex: string;
  nonceHex: string;
  aadVersion: 1;
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function hexToBytes(hex: string): Uint8Array {
  if (hex.length % 2 !== 0) throw new Error("hexToBytes: odd-length hex string");
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < hex.length; i += 2) out[i / 2] = parseInt(hex.slice(i, i + 2), 16);
  return out;
}

/** aad = UTF-8("tr-escrow-v1|" + hex(handle)) — binds a ciphertext to its own handle. */
function buildAad(handle: Uint8Array): Uint8Array {
  return new TextEncoder().encode("tr-escrow-v1|" + bytesToHex(handle));
}

/** Encrypt the mnemonic under this passkey's escrow KEK. */
export function sealEscrow(mnemonic: string, prfSecret: Uint8Array): SealedEscrow {
  const kek = deriveEscrowWrapSecret(prfSecret);
  const handle = deriveEscrowHandle(prfSecret);
  const nonce = crypto.getRandomValues(new Uint8Array(NONCE_LEN));
  const aad = buildAad(handle);
  const ciphertext = xchacha20poly1305(kek, nonce, aad).encrypt(new TextEncoder().encode(mnemonic));
  return {
    lookupHashHex: bytesToHex(escrowLookupHash(handle)),
    ciphertextHex: bytesToHex(ciphertext),
    nonceHex: bytesToHex(nonce),
    aadVersion: 1,
  };
}

/** Thrown when a blob's `aadVersion` isn't one this client knows how to open. Distinct, non-retryable. */
export class EscrowVersionUnsupportedError extends Error {
  constructor(aadVersion: number) {
    super(`Escrow envelope aad_version ${aadVersion} is not supported by this app version.`);
    this.name = "EscrowVersionUnsupportedError";
  }
}

/**
 * Decrypt. Throws on AEAD failure (wrong passkey / tamper / corruption) —
 * see spa-passkey-unlock.md §6.2 for why that is a distinct signal from a
 * relay 404 ("no such handle").
 */
export function openEscrow(
  blob: { ciphertextHex: string; nonceHex: string; aadVersion: number },
  handle: Uint8Array,
  prfSecret: Uint8Array,
): string {
  if (blob.aadVersion !== 1) throw new EscrowVersionUnsupportedError(blob.aadVersion);
  const kek = deriveEscrowWrapSecret(prfSecret);
  const nonce = hexToBytes(blob.nonceHex);
  if (nonce.length !== NONCE_LEN) throw new Error("openEscrow: bad nonce length");
  const ciphertext = hexToBytes(blob.ciphertextHex);
  if (ciphertext.length === 0 || ciphertext.length > MAX_CIPHERTEXT_BYTES) {
    throw new Error("openEscrow: ciphertext out of bounds");
  }
  const aad = buildAad(handle);
  const plaintext = xchacha20poly1305(kek, nonce, aad).decrypt(ciphertext);
  return new TextDecoder().decode(plaintext);
}

// ---------------------------------------------------------------------------
// File export (D-1 amendment) — phrase-escrow-relay.md §7. The relay is not
// involved on this path: one sealEscrow() call produces the bytes for both
// destinations (relay record AND file), so there is exactly one seal path
// and one open path to test and review.
// ---------------------------------------------------------------------------

export const ESCROW_FILE_FORMAT = "totalreclaw-escrow";
export const ESCROW_FILE_VERSION = 1;

export interface EscrowFile {
  format: typeof ESCROW_FILE_FORMAT;
  v: typeof ESCROW_FILE_VERSION;
  aad_version: number;
  nonce: string;
  ciphertext: string;
  created_at: string;
  note: string;
}

const FILE_NOTE =
  "Encrypted TotalReclaw vault backup. Open it at app.totalreclaw.xyz with the passkey from the device that created it. Without that passkey this file cannot be decrypted by anyone, including TotalReclaw.";

/**
 * Serialise a SealedEscrow into the downloadable file shape (§7.3).
 * Deliberately carries NO smart_account, address, or lookup_hash — see the
 * spec's metadata-leak rationale. `lookupHashHex` on `sealed` is simply not
 * read here.
 */
export function sealedEscrowToFile(sealed: SealedEscrow, createdAt: Date = new Date()): EscrowFile {
  return {
    format: ESCROW_FILE_FORMAT,
    v: ESCROW_FILE_VERSION,
    aad_version: sealed.aadVersion,
    nonce: sealed.nonceHex,
    ciphertext: sealed.ciphertextHex,
    created_at: createdAt.toISOString(),
    note: FILE_NOTE,
  };
}

/** Suggested filename per phrase-escrow-relay.md §7.3: `totalreclaw-backup-YYYY-MM-DD.json`. */
export function escrowFilename(date: Date = new Date()): string {
  const iso = date.toISOString().slice(0, 10); // YYYY-MM-DD
  return `totalreclaw-backup-${iso}.json`;
}

export type EscrowFileParseError = "bad-file" | "unsupported-version";

/**
 * Parse + validate an uploaded file's text per §7.4 step 2, BEFORE any
 * WebAuthn call — an obviously-malformed file should never prompt a
 * biometric. Returns the blob shape `openEscrow` accepts, or a tagged error.
 */
export function parseEscrowFile(
  text: string,
): { ok: true; blob: { ciphertextHex: string; nonceHex: string; aadVersion: number } } | { ok: false; error: EscrowFileParseError } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false, error: "bad-file" };
  }
  if (typeof parsed !== "object" || parsed === null) return { ok: false, error: "bad-file" };
  const p = parsed as Record<string, unknown>;
  if (p.format !== ESCROW_FILE_FORMAT || p.v !== ESCROW_FILE_VERSION) return { ok: false, error: "bad-file" };
  if (typeof p.nonce !== "string" || p.nonce.length !== NONCE_LEN * 2 || !/^[0-9a-fA-F]+$/.test(p.nonce)) {
    return { ok: false, error: "bad-file" };
  }
  if (
    typeof p.ciphertext !== "string" ||
    p.ciphertext.length === 0 ||
    p.ciphertext.length > MAX_CIPHERTEXT_BYTES * 2 ||
    !/^[0-9a-fA-F]+$/.test(p.ciphertext)
  ) {
    return { ok: false, error: "bad-file" };
  }
  if (typeof p.aad_version !== "number" || !Number.isInteger(p.aad_version) || p.aad_version < 1) {
    return { ok: false, error: "bad-file" };
  }
  if (p.aad_version !== 1) return { ok: false, error: "unsupported-version" };
  return {
    ok: true,
    blob: { ciphertextHex: p.ciphertext, nonceHex: p.nonce, aadVersion: p.aad_version },
  };
}
