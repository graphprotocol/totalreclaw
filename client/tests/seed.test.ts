import {
  generateMnemonic,
  mnemonicToKeys,
  mnemonicToSmartAccountAddress,
  validateMnemonic,
  deriveKeysFromMnemonic,
  deriveLshSeed,
  computeAuthKeyHash,
  DERIVATION_PATH,
  DEFAULT_CHAIN_ID,
} from "../src/crypto/seed";

// Mock the getSmartAccountAddress function to avoid RPC calls in tests.
jest.mock("../src/userop/builder", () => ({
  ...jest.requireActual("../src/userop/builder"),
  getSmartAccountAddress: jest.fn(
    async (ownerAddress: string, _chainId: number) => {
      const crypto = require("crypto");
      const hash = crypto
        .createHash("sha256")
        .update(ownerAddress)
        .digest("hex");
      return ("0x" + hash.slice(0, 40)) as `0x${string}`;
    }
  ),
}));

describe("Seed Module", () => {
  const TEST_MNEMONIC =
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about";

  describe("generateMnemonic", () => {
    it("should generate a 12-word mnemonic", () => {
      const mnemonic = generateMnemonic();
      const words = mnemonic.split(" ");
      expect(words).toHaveLength(12);
    });

    it("should generate valid BIP-39 mnemonics", () => {
      const mnemonic = generateMnemonic();
      expect(validateMnemonic(mnemonic)).toBe(true);
    });

    it("should generate unique mnemonics each time", () => {
      const m1 = generateMnemonic();
      const m2 = generateMnemonic();
      expect(m1).not.toEqual(m2);
    });
  });

  describe("validateMnemonic", () => {
    it("should accept valid 12-word mnemonic", () => {
      expect(validateMnemonic(TEST_MNEMONIC)).toBe(true);
    });

    it("should reject invalid mnemonic", () => {
      expect(validateMnemonic("not a valid mnemonic phrase at all")).toBe(false);
    });

    it("should reject empty string", () => {
      expect(validateMnemonic("")).toBe(false);
    });

    it("should reject null-ish values", () => {
      expect(validateMnemonic("   ")).toBe(false);
    });
  });

  describe("deriveKeysFromMnemonic (HKDF from BIP-39 seed)", () => {
    it("should derive 32-byte keys", () => {
      const keys = deriveKeysFromMnemonic(TEST_MNEMONIC);
      expect(keys.authKey.length).toBe(32);
      expect(keys.encryptionKey.length).toBe(32);
      expect(keys.dedupKey.length).toBe(32);
      expect(keys.salt.length).toBe(32);
    });

    it("should be deterministic", () => {
      const k1 = deriveKeysFromMnemonic(TEST_MNEMONIC);
      const k2 = deriveKeysFromMnemonic(TEST_MNEMONIC);
      expect(k1.authKey.equals(k2.authKey)).toBe(true);
      expect(k1.encryptionKey.equals(k2.encryptionKey)).toBe(true);
      expect(k1.dedupKey.equals(k2.dedupKey)).toBe(true);
      expect(k1.salt.equals(k2.salt)).toBe(true);
    });

    it("should derive different keys for different mnemonics", () => {
      const k1 = deriveKeysFromMnemonic(TEST_MNEMONIC);
      const k2 = deriveKeysFromMnemonic("zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong");
      expect(k1.encryptionKey.equals(k2.encryptionKey)).toBe(false);
    });

    it("should derive distinct auth, encryption, and dedup keys", () => {
      const keys = deriveKeysFromMnemonic(TEST_MNEMONIC);
      expect(keys.authKey.equals(keys.encryptionKey)).toBe(false);
      expect(keys.authKey.equals(keys.dedupKey)).toBe(false);
      expect(keys.encryptionKey.equals(keys.dedupKey)).toBe(false);
    });

    it("salt should be first 32 bytes of BIP-39 seed", () => {
      const { mnemonicToSeedSync } = require("@scure/bip39");
      const seed = mnemonicToSeedSync(TEST_MNEMONIC);
      const keys = deriveKeysFromMnemonic(TEST_MNEMONIC);
      expect(keys.salt).toEqual(Buffer.from(seed.slice(0, 32)));
    });
  });

  describe("deriveLshSeed", () => {
    it("should derive a 32-byte seed", () => {
      const seed = deriveLshSeed(TEST_MNEMONIC);
      expect(seed).toBeInstanceOf(Uint8Array);
      expect(seed.length).toBe(32);
    });

    it("should be deterministic", () => {
      const s1 = deriveLshSeed(TEST_MNEMONIC);
      const s2 = deriveLshSeed(TEST_MNEMONIC);
      expect(Buffer.from(s1).equals(Buffer.from(s2))).toBe(true);
    });

    it("should differ for different mnemonics", () => {
      const s1 = deriveLshSeed(TEST_MNEMONIC);
      const s2 = deriveLshSeed("zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong");
      expect(Buffer.from(s1).equals(Buffer.from(s2))).toBe(false);
    });
  });

  describe("computeAuthKeyHash", () => {
    it("should return a 64-char hex string", () => {
      const keys = deriveKeysFromMnemonic(TEST_MNEMONIC);
      const hash = computeAuthKeyHash(keys.authKey);
      expect(hash).toMatch(/^[a-f0-9]{64}$/);
    });

    it("should be deterministic", () => {
      const keys = deriveKeysFromMnemonic(TEST_MNEMONIC);
      const h1 = computeAuthKeyHash(keys.authKey);
      const h2 = computeAuthKeyHash(keys.authKey);
      expect(h1).toBe(h2);
    });
  });

  describe("mnemonicToKeys", () => {
    it("should derive encryption key (32 bytes)", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys.encryptionKey).toBeInstanceOf(Buffer);
      expect(keys.encryptionKey.length).toBe(32);
    });

    it("should derive auth key (32 bytes)", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys.authKey).toBeInstanceOf(Buffer);
      expect(keys.authKey.length).toBe(32);
    });

    it("should derive dedup key (32 bytes)", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys.dedupKey).toBeInstanceOf(Buffer);
      expect(keys.dedupKey.length).toBe(32);
    });

    it("should derive a private key (32 bytes)", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys.privateKey).toBeInstanceOf(Buffer);
      expect(keys.privateKey.length).toBe(32);
    });

    it("should derive an EOA address (20 bytes hex)", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys.eoaAddress).toMatch(/^0x[0-9a-fA-F]{40}$/);
    });

    it("should derive a Smart Account address (20 bytes hex)", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys.smartAccountAddress).toMatch(/^0x[0-9a-fA-F]{40}$/);
    });

    it("should return a salt", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys.salt).toBeInstanceOf(Buffer);
      expect(keys.salt.length).toBe(32);
    });

    it("should derive different EOA and Smart Account addresses", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys.smartAccountAddress).not.toEqual(keys.eoaAddress);
    });

    it("should be deterministic (same mnemonic = same keys)", async () => {
      const keys1 = await mnemonicToKeys(TEST_MNEMONIC);
      const keys2 = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys1.encryptionKey.equals(keys2.encryptionKey)).toBe(true);
      expect(keys1.authKey.equals(keys2.authKey)).toBe(true);
      expect(keys1.dedupKey.equals(keys2.dedupKey)).toBe(true);
      expect(keys1.privateKey.equals(keys2.privateKey)).toBe(true);
      expect(keys1.eoaAddress).toEqual(keys2.eoaAddress);
      expect(keys1.smartAccountAddress).toEqual(keys2.smartAccountAddress);
    });

    it("should derive different keys for different mnemonics", async () => {
      const m2 = "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong";
      const keys1 = await mnemonicToKeys(TEST_MNEMONIC);
      const keys2 = await mnemonicToKeys(m2);
      expect(keys1.encryptionKey.equals(keys2.encryptionKey)).toBe(false);
    });

    it("should derive different encryption and auth keys", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC);
      expect(keys.encryptionKey.equals(keys.authKey)).toBe(false);
    });

    it("encryption keys should match deriveKeysFromMnemonic", async () => {
      const fullKeys = await mnemonicToKeys(TEST_MNEMONIC);
      const cryptoKeys = deriveKeysFromMnemonic(TEST_MNEMONIC);
      expect(fullKeys.encryptionKey.equals(cryptoKeys.encryptionKey)).toBe(true);
      expect(fullKeys.authKey.equals(cryptoKeys.authKey)).toBe(true);
      expect(fullKeys.dedupKey.equals(cryptoKeys.dedupKey)).toBe(true);
    });

    it("should throw for invalid mnemonic", async () => {
      await expect(mnemonicToKeys("invalid mnemonic")).rejects.toThrow(
        "Invalid BIP-39 mnemonic"
      );
    });

    it("should accept a custom chainId parameter", async () => {
      const keys = await mnemonicToKeys(TEST_MNEMONIC, 100);
      expect(keys.smartAccountAddress).toMatch(/^0x[0-9a-fA-F]{40}$/);
    });
  });

  describe("mnemonicToSmartAccountAddress", () => {
    it("should return a valid Ethereum address", async () => {
      const addr = await mnemonicToSmartAccountAddress(TEST_MNEMONIC);
      expect(addr).toMatch(/^0x[0-9a-fA-F]{40}$/);
    });

    it("should be deterministic", async () => {
      const a1 = await mnemonicToSmartAccountAddress(TEST_MNEMONIC);
      const a2 = await mnemonicToSmartAccountAddress(TEST_MNEMONIC);
      expect(a1).toEqual(a2);
    });

    it("should return different addresses for different mnemonics", async () => {
      const a1 = await mnemonicToSmartAccountAddress(TEST_MNEMONIC);
      const m2 = "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong";
      const a2 = await mnemonicToSmartAccountAddress(m2);
      expect(a1).not.toEqual(a2);
    });

    it("should throw for invalid mnemonic", async () => {
      await expect(
        mnemonicToSmartAccountAddress("invalid mnemonic")
      ).rejects.toThrow("Invalid BIP-39 mnemonic");
    });
  });

  describe("DEFAULT_CHAIN_ID", () => {
    it("should be Gnosis mainnet (100)", () => {
      expect(DEFAULT_CHAIN_ID).toBe(100);
    });
  });

  describe("DERIVATION_PATH", () => {
    it("should be standard Ethereum path", () => {
      expect(DERIVATION_PATH).toBe("m/44'/60'/0'/0/0");
    });
  });
});
