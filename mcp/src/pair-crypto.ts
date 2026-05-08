/**
 * pair-crypto — gateway-side cryptographic primitives for the relay-brokered
 * pair flow.
 *
 * NOTE — this is a port of the plugin's `skill/plugin/pair-crypto.ts`
 * (TotalReclaw plugin v3.3.1-rc.12+). Wire formats (base64url + AES-256-GCM
 * + HKDF-SHA256 + x25519 ECDH) are byte-for-byte compatible with the plugin
 * and the Hermes Python implementation, so all three gateway flavors talk to
 * the same browser pair-page and the same `totalreclaw-relay` endpoints.
 *
 * Ported into mcp-server 3.3.0-rc.1 to enable the MCP-only install path:
 * users add the MCP server, agent calls `totalreclaw_pair` tool, browser
 * generates phrase, encrypted phrase flows back to the MCP process which
 * decrypts and writes credentials.json — phrase NEVER crosses LLM context.
 *
 * Cipher suite (matches plugin / Hermes):
 *   - x25519 ECDH for key agreement
 *   - HKDF-SHA256 (info = "totalreclaw-pair-v2") for symmetric-key derivation
 *   - AES-256-GCM AEAD with 12-byte nonce, 16-byte tag, sid as AD
 *
 * Phrase-safety invariants:
 *   - This module ONLY runs on the gateway (MCP server) host.
 *   - No `fs.*` calls, no env-var reads, no network primitives.
 *   - Public surface emits only base64url-encoded raw 32-byte public keys.
 *
 * Future cleanup: pull `pair-crypto.ts` + `pair-remote-client.ts` into a
 * shared `@totalreclaw/pair-client` package so plugin + mcp-server can share
 * one source of truth. For now this is a verbatim copy — keep in sync with
 * the plugin manually until the shared-package work lands.
 */

import {
  createPrivateKey,
  createPublicKey,
  diffieHellman,
  generateKeyPairSync,
  hkdfSync,
  createCipheriv,
  createDecipheriv,
  randomBytes,
  timingSafeEqual,
} from 'node:crypto';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const HKDF_INFO = 'totalreclaw-pair-v2';
export const AEAD_KEY_BYTES = 32;
export const AEAD_NONCE_BYTES = 12;
export const AEAD_TAG_BYTES = 16;
export const X25519_KEY_BYTES = 32;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type PublicKeyB64 = string;
export type PrivateKeyB64 = string;
export type NonceB64 = string;
export type CiphertextB64 = string;

export interface GatewayKeypair {
  skB64: PrivateKeyB64;
  pkB64: PublicKeyB64;
}

export interface SessionKeys {
  kEnc: Buffer;
}

export interface DecryptInputs {
  skGatewayB64: PrivateKeyB64;
  pkDeviceB64: PublicKeyB64;
  sid: string;
  nonceB64: NonceB64;
  ciphertextB64: CiphertextB64;
}

export interface EncryptInputs {
  skLocalB64: PrivateKeyB64;
  pkRemoteB64: PublicKeyB64;
  sid: string;
  plaintext: Buffer | Uint8Array;
  nonceB64?: NonceB64;
}

export interface EncryptOutput {
  nonceB64: NonceB64;
  ciphertextB64: CiphertextB64;
}

// ---------------------------------------------------------------------------
// Key generation / conversion
// ---------------------------------------------------------------------------

export function generateGatewayKeypair(): GatewayKeypair {
  const { publicKey, privateKey } = generateKeyPairSync('x25519');
  return {
    skB64: extractRawPrivate(privateKey),
    pkB64: extractRawPublic(publicKey),
  };
}

function publicKeyFromB64(pkB64: PublicKeyB64): ReturnType<typeof createPublicKey> {
  const raw = Buffer.from(pkB64, 'base64url');
  if (raw.length !== X25519_KEY_BYTES) {
    throw new Error(`pair-crypto: public key must be ${X25519_KEY_BYTES} bytes (got ${raw.length})`);
  }
  return createPublicKey({
    key: { kty: 'OKP', crv: 'X25519', x: raw.toString('base64url') },
    format: 'jwk',
  });
}

function privateKeyFromB64(skB64: PrivateKeyB64): ReturnType<typeof createPrivateKey> {
  const raw = Buffer.from(skB64, 'base64url');
  if (raw.length !== X25519_KEY_BYTES) {
    throw new Error(`pair-crypto: private key must be ${X25519_KEY_BYTES} bytes (got ${raw.length})`);
  }
  const tempPriv = createPrivateKey({
    key: { kty: 'OKP', crv: 'X25519', d: raw.toString('base64url'), x: '' },
    format: 'jwk',
  });
  const pubObj = createPublicKey(tempPriv);
  const pubJwk = pubObj.export({ format: 'jwk' }) as { x: string };
  return createPrivateKey({
    key: { kty: 'OKP', crv: 'X25519', d: raw.toString('base64url'), x: pubJwk.x },
    format: 'jwk',
  });
}

function extractRawPublic(pk: ReturnType<typeof createPublicKey>): PublicKeyB64 {
  const jwk = pk.export({ format: 'jwk' }) as { x?: string };
  if (!jwk.x) throw new Error('pair-crypto: public key JWK is missing the x field');
  return jwk.x;
}

function extractRawPrivate(sk: ReturnType<typeof createPrivateKey>): PrivateKeyB64 {
  const jwk = sk.export({ format: 'jwk' }) as { d?: string };
  if (!jwk.d) throw new Error('pair-crypto: private key JWK is missing the d field');
  return jwk.d;
}

export function derivePublicFromPrivate(skB64: PrivateKeyB64): PublicKeyB64 {
  const sk = privateKeyFromB64(skB64);
  const pk = createPublicKey(sk);
  return extractRawPublic(pk);
}

// ---------------------------------------------------------------------------
// ECDH + HKDF
// ---------------------------------------------------------------------------

export function computeSharedSecret(opts: {
  skLocalB64: PrivateKeyB64;
  pkRemoteB64: PublicKeyB64;
}): Buffer {
  const sk = privateKeyFromB64(opts.skLocalB64);
  const pk = publicKeyFromB64(opts.pkRemoteB64);
  const shared = diffieHellman({ privateKey: sk, publicKey: pk });
  if (shared.length !== X25519_KEY_BYTES) {
    throw new Error(
      `pair-crypto: ECDH output wrong length (got ${shared.length}, expected ${X25519_KEY_BYTES})`,
    );
  }
  return shared;
}

export function deriveSessionKeys(opts: {
  sharedSecret: Buffer;
  sid: string;
}): SessionKeys {
  if (opts.sharedSecret.length !== X25519_KEY_BYTES) {
    throw new Error('pair-crypto: shared secret must be 32 bytes');
  }
  if (typeof opts.sid !== 'string' || opts.sid.length === 0) {
    throw new Error('pair-crypto: sid is required for HKDF salt binding');
  }
  const salt = Buffer.from(opts.sid, 'utf-8');
  const info = Buffer.from(HKDF_INFO, 'utf-8');
  const okm = hkdfSync('sha256', opts.sharedSecret, salt, info, AEAD_KEY_BYTES);
  return { kEnc: Buffer.from(okm) };
}

export function deriveAeadKeyFromEcdh(opts: {
  skLocalB64: PrivateKeyB64;
  pkRemoteB64: PublicKeyB64;
  sid: string;
}): SessionKeys {
  const shared = computeSharedSecret({
    skLocalB64: opts.skLocalB64,
    pkRemoteB64: opts.pkRemoteB64,
  });
  return deriveSessionKeys({ sharedSecret: shared, sid: opts.sid });
}

// ---------------------------------------------------------------------------
// AEAD
// ---------------------------------------------------------------------------

export function aeadDecrypt(opts: {
  kEnc: Buffer;
  nonceB64: NonceB64;
  sid: string;
  ciphertextB64: CiphertextB64;
}): Buffer {
  const nonce = Buffer.from(opts.nonceB64, 'base64url');
  if (nonce.length !== AEAD_NONCE_BYTES) {
    throw new Error(`pair-crypto: nonce must be ${AEAD_NONCE_BYTES} bytes (got ${nonce.length})`);
  }
  if (opts.kEnc.length !== AEAD_KEY_BYTES) {
    throw new Error(`pair-crypto: AEAD key must be ${AEAD_KEY_BYTES} bytes`);
  }
  const combined = Buffer.from(opts.ciphertextB64, 'base64url');
  if (combined.length < AEAD_TAG_BYTES) {
    throw new Error('pair-crypto: ciphertext too short to contain tag');
  }
  const ct = combined.subarray(0, combined.length - AEAD_TAG_BYTES);
  const tag = combined.subarray(combined.length - AEAD_TAG_BYTES);

  const decipher = createDecipheriv('aes-256-gcm', opts.kEnc, nonce, {
    authTagLength: AEAD_TAG_BYTES,
  });
  decipher.setAAD(Buffer.from(opts.sid, 'utf-8'), { plaintextLength: ct.length });
  decipher.setAuthTag(tag);

  const pt1 = decipher.update(ct);
  const pt2 = decipher.final();
  return Buffer.concat([pt1, pt2]);
}

export function decryptPairingPayload(inputs: DecryptInputs): Buffer {
  const { kEnc } = deriveAeadKeyFromEcdh({
    skLocalB64: inputs.skGatewayB64,
    pkRemoteB64: inputs.pkDeviceB64,
    sid: inputs.sid,
  });
  return aeadDecrypt({
    kEnc,
    nonceB64: inputs.nonceB64,
    sid: inputs.sid,
    ciphertextB64: inputs.ciphertextB64,
  });
}

export function aeadEncryptWithSessionKey(opts: {
  kEnc: Buffer;
  sid: string;
  plaintext: Buffer | Uint8Array;
  nonceB64?: NonceB64;
}): EncryptOutput {
  if (opts.kEnc.length !== AEAD_KEY_BYTES) {
    throw new Error(`pair-crypto: AEAD key must be ${AEAD_KEY_BYTES} bytes`);
  }
  const nonceBuf =
    opts.nonceB64 !== undefined
      ? Buffer.from(opts.nonceB64, 'base64url')
      : randomBytes(AEAD_NONCE_BYTES);
  if (nonceBuf.length !== AEAD_NONCE_BYTES) {
    throw new Error(`pair-crypto: nonce must be ${AEAD_NONCE_BYTES} bytes`);
  }

  const pt = Buffer.isBuffer(opts.plaintext) ? opts.plaintext : Buffer.from(opts.plaintext);
  const cipher = createCipheriv('aes-256-gcm', opts.kEnc, nonceBuf, {
    authTagLength: AEAD_TAG_BYTES,
  });
  cipher.setAAD(Buffer.from(opts.sid, 'utf-8'), { plaintextLength: pt.length });
  const ct = Buffer.concat([cipher.update(pt), cipher.final()]);
  const tag = cipher.getAuthTag();
  return {
    nonceB64: nonceBuf.toString('base64url'),
    ciphertextB64: Buffer.concat([ct, tag]).toString('base64url'),
  };
}

export function encryptPairingPayload(inputs: EncryptInputs): EncryptOutput {
  const { kEnc } = deriveAeadKeyFromEcdh({
    skLocalB64: inputs.skLocalB64,
    pkRemoteB64: inputs.pkRemoteB64,
    sid: inputs.sid,
  });
  return aeadEncryptWithSessionKey({
    kEnc,
    sid: inputs.sid,
    plaintext: inputs.plaintext,
    nonceB64: inputs.nonceB64,
  });
}

export function compareSecondaryCodesCT(a: string, b: string): boolean {
  const aBuf = Buffer.from(a, 'utf-8');
  const bBuf = Buffer.from(b, 'utf-8');
  const lenMatch = aBuf.length === bBuf.length;
  const max = Math.max(aBuf.length, bBuf.length, 6);
  const aPad = Buffer.alloc(max);
  const bPad = Buffer.alloc(max);
  aBuf.copy(aPad);
  bBuf.copy(bPad);
  const byteMatch = timingSafeEqual(aPad, bPad);
  return lenMatch && byteMatch;
}
