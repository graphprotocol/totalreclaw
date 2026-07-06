import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  deriveSessionKeys,
  encryptBlob,
  decryptBlob,
  isMnemonicValid,
  generateRecoveryPhrase,
  deriveEoaPrivateKey,
} from "./crypto";
import { secp256k1 } from "@noble/curves/secp256k1";
import { keccak_256 } from "@noble/hashes/sha3";

// BIP-39 test mnemonic (all-zeros entropy, BIP-39 spec vector)
const TEST_MNEMONIC =
  "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";

// Golden vectors for TEST_MNEMONIC (canonical derivation):
//   seed  = BIP-39 PBKDF2 512-bit output
//   salt  = seed[0:32]
//   keys  = HKDF-SHA256(seed, salt, <info>, 32)
const GOLDEN_AUTH_KEY_HEX =
  "5580b82ac0a8763328600dd335b139d0915de9a591970207d6820306a2a36ae7";
const GOLDEN_ENC_KEY_HEX =
  "a58fdc56e1d768461d95cd46b49e03727b2eb342ac558b9f3ebf1255b871f703";

// EOA for TEST_MNEMONIC at BIP-32 m/44'/60'/0'/0/0 — well-known test vector.
const GOLDEN_EOA = "0x9858effd232b4033e47d90003d41ec34ecaeda94";

const SERVER_URL = "https://relay.test";
const MOCK_SMART_ACCOUNT = "0xcafef00dcafef00dcafef00dcafef00dcafef00d";

// Fixture ciphertext: encryptBlob('{"test":"golden"}', GOLDEN_ENC_KEY, nonce=zeros[24])
// wire format: nonce[24] || tag[16] || ciphertext
const FIXTURE_CIPHERTEXT_HEX =
  "00000000000000000000000000000000000000000000000040445878d8a28677a8bc521153f0e771c33dad4a6764bf46768a56e76b8f670968";
const FIXTURE_PLAINTEXT = '{"test":"golden"}';

function hexToBytes(hex: string): Uint8Array {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < hex.length; i += 2)
    out[i / 2] = parseInt(hex.slice(i, i + 2), 16);
  return out;
}

beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/v1/smart-account")) {
      return new Response(
        JSON.stringify({ smart_account: MOCK_SMART_ACCOUNT, chain_id: 84532 }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response("not mocked: " + url, { status: 404 });
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("deriveSessionKeys", () => {
  it("produces canonical authKey for known mnemonic (golden vector)", async () => {
    const keys = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    expect(keys.authKeyHex).toBe(GOLDEN_AUTH_KEY_HEX);
  });

  it("produces canonical encryptionKey for known mnemonic (golden vector)", async () => {
    const keys = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    const encHex = Array.from(keys.encryptionKey)
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
    expect(encHex).toBe(GOLDEN_ENC_KEY_HEX);
  });

  it("derives canonical EOA for TEST_MNEMONIC", async () => {
    const keys = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    expect(keys.eoaAddress).toBe(GOLDEN_EOA);
  });

  it("fetches walletAddress from /v1/smart-account using the EOA", async () => {
    const keys = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    expect(keys.walletAddress).toBe(MOCK_SMART_ACCOUNT);

    const fetchMock = vi.mocked(fetch);
    const calledUrl = String(fetchMock.mock.calls[0][0]);
    expect(calledUrl).toContain("/v1/smart-account");
    expect(calledUrl).toContain(`eoa=${GOLDEN_EOA}`);
    expect(calledUrl).toContain("chain=84532");
  });

  it("authKey and encryptionKey are distinct", async () => {
    const keys = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    expect(keys.authKeyHex).not.toBe(GOLDEN_ENC_KEY_HEX);
  });

  it("is deterministic", async () => {
    const k1 = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    const k2 = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    expect(k1.authKeyHex).toBe(k2.authKeyHex);
    expect(Array.from(k1.encryptionKey).join(",")).toBe(
      Array.from(k2.encryptionKey).join(","),
    );
    expect(k1.eoaAddress).toBe(k2.eoaAddress);
  });

  it("produces different keys for different mnemonics", async () => {
    const k1 = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    const k2 = await deriveSessionKeys(
      "legal winner thank year wave sausage worth useful legal winner thank yellow",
      SERVER_URL,
    );
    expect(k1.authKeyHex).not.toBe(
      Array.from(k2.authKey)
        .map((b) => b.toString(16).padStart(2, "0"))
        .join(""),
    );
    expect(k1.eoaAddress).not.toBe(k2.eoaAddress);
  });

  it("rejects invalid mnemonic", async () => {
    await expect(
      deriveSessionKeys("not valid mnemonic", SERVER_URL),
    ).rejects.toThrow("Invalid 12-word recovery phrase");
  });

  it("surfaces relay errors with status code", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("rate-limited", { status: 429 }),
    );
    await expect(
      deriveSessionKeys(TEST_MNEMONIC, SERVER_URL),
    ).rejects.toThrow(/smart-account derivation failed \(429\)/);
  });
});

describe("encryptBlob / decryptBlob round-trip", () => {
  it("encrypt then decrypt returns original plaintext", async () => {
    const { encryptionKey } = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    const original = JSON.stringify({ type: "fact", content: "hello world" });
    const hex = encryptBlob(original, encryptionKey);
    const decrypted = decryptBlob(hex, encryptionKey);
    expect(decrypted).toBe(original);
  });

  it("decrypts known fixture ciphertext against canonical enc key", () => {
    const encKey = hexToBytes(GOLDEN_ENC_KEY_HEX);
    const decrypted = decryptBlob(FIXTURE_CIPHERTEXT_HEX, encKey);
    expect(decrypted).toBe(FIXTURE_PLAINTEXT);
  });

  it("decryptBlob rejects tampered ciphertext", async () => {
    const { encryptionKey } = await deriveSessionKeys(TEST_MNEMONIC, SERVER_URL);
    const hex = encryptBlob("secret", encryptionKey);
    // Flip last byte
    const tampered =
      hex.slice(0, -2) +
      ((parseInt(hex.slice(-2), 16) ^ 0xff).toString(16).padStart(2, "0"));
    expect(() => decryptBlob(tampered, encryptionKey)).toThrow();
  });
});

describe("isMnemonicValid", () => {
  it("accepts valid 12-word BIP-39 mnemonic", () => {
    expect(isMnemonicValid(TEST_MNEMONIC)).toBe(true);
  });

  it("rejects invalid phrase", () => {
    expect(isMnemonicValid("not a valid mnemonic phrase")).toBe(false);
  });
});

describe("generateRecoveryPhrase", () => {
  it("produces a valid 12-word BIP-39 phrase", () => {
    const phrase = generateRecoveryPhrase();
    expect(phrase.trim().split(/\s+/)).toHaveLength(12);
    expect(isMnemonicValid(phrase)).toBe(true);
  });

  it("produces a different phrase each call", () => {
    expect(generateRecoveryPhrase()).not.toBe(generateRecoveryPhrase());
  });
});

describe("deriveEoaPrivateKey (L3)", () => {
  it("derives a 32-byte private key whose address matches the golden EOA", async () => {
    const priv = await deriveEoaPrivateKey(TEST_MNEMONIC);
    expect(priv).toHaveLength(32);
    // Derive the address from the private key and confirm it matches GOLDEN_EOA.
    // (We assert via the derived address, never by hardcoding the private key.)
    const pub = secp256k1.getPublicKey(priv, false); // 65 bytes, uncompressed
    const addr = "0x" + Array.from(keccak_256(pub.slice(1)).slice(-20))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
    expect(addr).toBe(GOLDEN_EOA);
  });

  it("rejects an invalid mnemonic", async () => {
    await expect(deriveEoaPrivateKey("not valid")).rejects.toThrow("Invalid recovery phrase");
  });
});
