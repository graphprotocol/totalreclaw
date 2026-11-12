/**
 * Pairing crypto (X25519 + HKDF-SHA256 + AES-256-GCM): RFC 7748 vectors,
 * round trip and negative cases.
 *
 * Regression guard for graphprotocol/totalreclaw-internal#708: building the
 * X25519 private key as a JWK with an empty `x` throws ERR_CRYPTO_INVALID_JWK
 * on Node 26, which broke every pairing. CI runs this file on Node 22, 24
 * and 26.
 *
 * Plaintexts are dummy bytes, never anything shaped like a recovery phrase.
 */

import {
  generateGatewayKeypair,
  derivePublicFromPrivate,
  computeSharedSecret,
  encryptPairingPayload,
  decryptPairingPayload,
} from '../src/pair-crypto';

// RFC 7748 section 6.1 (X25519 key agreement).
const ALICE_PRIV = '77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a';
const ALICE_PUB = '8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a';
const BOB_PRIV = '5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb';
const BOB_PUB = 'de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f';
const SHARED = '4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742';

const b64 = (hex: string) => Buffer.from(hex, 'hex').toString('base64url');
const hex = (b64u: string) => Buffer.from(b64u, 'base64url').toString('hex');

describe('pair-crypto: RFC 7748 vectors', () => {
  it('derivePublicFromPrivate gives Alice and Bob public keys', () => {
    expect(hex(derivePublicFromPrivate(b64(ALICE_PRIV)))).toBe(ALICE_PUB);
    expect(hex(derivePublicFromPrivate(b64(BOB_PRIV)))).toBe(BOB_PUB);
  });

  it('computeSharedSecret gives the known secret in both directions', () => {
    const ab = computeSharedSecret({ skLocalB64: b64(ALICE_PRIV), pkRemoteB64: b64(BOB_PUB) });
    const ba = computeSharedSecret({ skLocalB64: b64(BOB_PRIV), pkRemoteB64: b64(ALICE_PUB) });
    expect(ab.toString('hex')).toBe(SHARED);
    expect(ba.toString('hex')).toBe(SHARED);
  });
});

describe('pair-crypto: keypair', () => {
  it('derivePublicFromPrivate(sk) equals the generated public key', () => {
    const kp = generateGatewayKeypair();
    expect(derivePublicFromPrivate(kp.skB64)).toBe(kp.pkB64);
  });
});

describe('pair-crypto: encrypt/decrypt', () => {
  const sid = 'test-session-0001';
  const plaintext = Buffer.from(Array.from({ length: 48 }, (_, i) => (i * 7 + 3) & 0xff));

  function encrypted() {
    const gateway = generateGatewayKeypair();
    const device = generateGatewayKeypair();
    const out = encryptPairingPayload({
      skLocalB64: device.skB64,
      pkRemoteB64: gateway.pkB64,
      sid,
      plaintext,
    });
    return { gateway, device, out };
  }

  it('round-trips', () => {
    const { gateway, device, out } = encrypted();
    const pt = decryptPairingPayload({
      skGatewayB64: gateway.skB64,
      pkDeviceB64: device.pkB64,
      sid,
      nonceB64: out.nonceB64,
      ciphertextB64: out.ciphertextB64,
    });
    expect(pt.equals(plaintext)).toBe(true);
  });

  it('throws on a wrong sid', () => {
    const { gateway, device, out } = encrypted();
    expect(() =>
      decryptPairingPayload({
        skGatewayB64: gateway.skB64,
        pkDeviceB64: device.pkB64,
        sid: 'another-session',
        nonceB64: out.nonceB64,
        ciphertextB64: out.ciphertextB64,
      }),
    ).toThrow();
  });

  it('throws on tampered ciphertext', () => {
    const { gateway, device, out } = encrypted();
    const ct = Buffer.from(out.ciphertextB64, 'base64url');
    ct[0] ^= 0x01;
    expect(() =>
      decryptPairingPayload({
        skGatewayB64: gateway.skB64,
        pkDeviceB64: device.pkB64,
        sid,
        nonceB64: out.nonceB64,
        ciphertextB64: ct.toString('base64url'),
      }),
    ).toThrow();
  });

  it('throws on a wrong-length key', () => {
    const kp = generateGatewayKeypair();
    const short = Buffer.alloc(31, 1).toString('base64url');
    expect(() => derivePublicFromPrivate(short)).toThrow(/private key must be 32 bytes/);
    expect(() => computeSharedSecret({ skLocalB64: short, pkRemoteB64: kp.pkB64 })).toThrow();
    expect(() => computeSharedSecret({ skLocalB64: kp.skB64, pkRemoteB64: short })).toThrow(
      /public key must be 32 bytes/,
    );
  });
});
