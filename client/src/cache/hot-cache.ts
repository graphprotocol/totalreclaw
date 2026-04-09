/**
 * Hot Cache - Persistent Encrypted Cache
 *
 * Stores the top ~30 high-importance facts (encrypted at rest) for instant
 * auto-recall on conversation start. Also caches fact count, last-synced
 * block number, and Smart Account address.
 *
 * File format: [nonce:24][tag:16][ciphertext] (XChaCha20-Poly1305)
 * Default location: ~/.totalreclaw/cache.enc
 */

import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";

/**
 * A cached fact with ID, text, and importance score.
 */
export interface HotFact {
  id: string;
  text: string;
  importance: number;
}

/**
 * Internal structure persisted to disk (encrypted).
 */
interface CachePayload {
  hotFacts: HotFact[];
  factCount: number;
  lastSyncedBlock: number;
  smartAccountAddress: string;
}

/** Maximum number of hot facts to retain */
const MAX_HOT_FACTS = 30;

/** GCM initialization vector length in bytes */
const IV_LENGTH = 12;

/** GCM authentication tag length in bytes */
const TAG_LENGTH = 16;

/**
 * A small persistent cache that stores the top ~30 high-importance facts
 * encrypted at rest for instant auto-recall on conversation start.
 */
export class HotCache {
  private hotFacts: HotFact[] = [];
  private factCount = 0;
  private lastSyncedBlock = 0;
  private smartAccountAddress = "";
  private key: Buffer;

  /**
   * Create a new HotCache instance.
   *
   * @param cachePath - Filesystem path for the encrypted cache file
   * @param hexKey - 64-character hex string (32-byte AES-256 key)
   */
  constructor(private cachePath: string, hexKey: string) {
    this.key = Buffer.from(hexKey, "hex");
  }

  // ── Getters ──────────────────────────────────────────────────────────

  /** Return a shallow copy of the cached hot facts. */
  getHotFacts(): HotFact[] {
    return [...this.hotFacts];
  }

  /** Return the cached total fact count. */
  getFactCount(): number {
    return this.factCount;
  }

  /** Return the last synced block number. */
  getLastSyncedBlock(): number {
    return this.lastSyncedBlock;
  }

  /** Return the cached Smart Account address. */
  getSmartAccountAddress(): string {
    return this.smartAccountAddress;
  }

  // ── Setters ──────────────────────────────────────────────────────────

  /**
   * Replace the hot facts list.
   *
   * Facts are sorted by importance (descending) and truncated to
   * {@link MAX_HOT_FACTS} entries.
   */
  setHotFacts(facts: HotFact[]): void {
    const sorted = [...facts].sort((a, b) => b.importance - a.importance);
    this.hotFacts = sorted.slice(0, MAX_HOT_FACTS);
  }

  /** Set the total fact count. */
  setFactCount(count: number): void {
    this.factCount = count;
  }

  /** Set the last synced block number. */
  setLastSyncedBlock(block: number): void {
    this.lastSyncedBlock = block;
  }

  /** Set the Smart Account address. */
  setSmartAccountAddress(addr: string): void {
    this.smartAccountAddress = addr;
  }

  // ── Persistence ──────────────────────────────────────────────────────

  /**
   * Encrypt the current cache state and write it to disk.
   *
   * File format: `[iv:12][tag:16][ciphertext]`
   * Creates parent directories as needed.
   */
  flush(): void {
    const payload: CachePayload = {
      hotFacts: this.hotFacts,
      factCount: this.factCount,
      lastSyncedBlock: this.lastSyncedBlock,
      smartAccountAddress: this.smartAccountAddress,
    };

    const plaintext = Buffer.from(JSON.stringify(payload), "utf-8");
    const iv = crypto.randomBytes(IV_LENGTH);
    const cipher = crypto.createCipheriv("aes-256-gcm", this.key, iv);
    const encrypted = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    const tag = cipher.getAuthTag();

    // Format: [iv:12][tag:16][ciphertext]
    const output = Buffer.concat([iv, tag, encrypted]);

    const dir = path.dirname(this.cachePath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
    fs.writeFileSync(this.cachePath, output);
  }

  /**
   * Read the cache file from disk and decrypt it.
   *
   * Gracefully degrades to an empty cache if the file is missing,
   * corrupted, or encrypted with a different key.
   */
  load(): void {
    if (!fs.existsSync(this.cachePath)) return;

    try {
      const data = fs.readFileSync(this.cachePath);
      if (data.length < IV_LENGTH + TAG_LENGTH) return;

      const iv = data.subarray(0, IV_LENGTH);
      const tag = data.subarray(IV_LENGTH, IV_LENGTH + TAG_LENGTH);
      const ciphertext = data.subarray(IV_LENGTH + TAG_LENGTH);

      const decipher = crypto.createDecipheriv("aes-256-gcm", this.key, iv);
      decipher.setAuthTag(tag);
      const decrypted = Buffer.concat([
        decipher.update(ciphertext),
        decipher.final(),
      ]);

      const payload: CachePayload = JSON.parse(decrypted.toString("utf-8"));
      this.hotFacts = payload.hotFacts || [];
      this.factCount = payload.factCount || 0;
      this.lastSyncedBlock = payload.lastSyncedBlock || 0;
      this.smartAccountAddress = payload.smartAccountAddress || "";
    } catch {
      // Graceful degradation: wrong key or corrupt file -> empty cache
      this.hotFacts = [];
      this.factCount = 0;
      this.lastSyncedBlock = 0;
      this.smartAccountAddress = "";
    }
  }
}
