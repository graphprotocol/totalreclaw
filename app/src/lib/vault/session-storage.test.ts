import { describe, it, expect, beforeEach } from "vitest";
import { saveSessionKeys, loadSessionKeys, clearSessionKeys } from "./session-storage";
import type { SessionKeys } from "../types";

// vitest.config.ts runs this suite under environment: "node" (no jsdom), so
// sessionStorage isn't a global here — stand in a minimal Storage-shaped
// polyfill, same spirit as idb.test.ts importing fake-indexeddb/auto.
class MemoryStorage {
  private store = new Map<string, string>();
  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  setItem(key: string, value: string): void {
    this.store.set(key, value);
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  clear(): void {
    this.store.clear();
  }
}

beforeEach(() => {
  (globalThis as { sessionStorage?: unknown }).sessionStorage = new MemoryStorage();
});

function keys(overrides: Partial<SessionKeys> = {}): SessionKeys {
  return {
    authKey: new Uint8Array(32).fill(0x11),
    encryptionKey: new Uint8Array(32).fill(0x22),
    authKeyHex: "11".repeat(32),
    eoaAddress: "0xEoaAddress",
    walletAddress: "0xWalletAddress",
    chainId: 100,
    ...overrides,
  };
}

describe("session-storage (Stage A, #440)", () => {
  it("returns null when nothing is persisted", () => {
    expect(loadSessionKeys()).toBeNull();
  });

  it("round-trips a full SessionKeys through sessionStorage", () => {
    const sk = keys();
    saveSessionKeys(sk);
    const restored = loadSessionKeys();
    expect(restored).not.toBeNull();
    expect(restored!.authKey).toEqual(sk.authKey);
    expect(restored!.encryptionKey).toEqual(sk.encryptionKey);
    expect(restored!.authKeyHex).toBe(sk.authKeyHex);
    expect(restored!.eoaAddress).toBe(sk.eoaAddress);
    expect(restored!.walletAddress).toBe(sk.walletAddress);
    expect(restored!.chainId).toBe(sk.chainId);
  });

  it("never persists anything resembling a mnemonic — only the two 32-byte derived keys", () => {
    const sk = keys();
    saveSessionKeys(sk);
    const raw = sessionStorage.getItem("totalreclaw-spa:session:v1")!;
    const parsed = JSON.parse(raw);
    // Exactly the derived-key fields SessionKeys carries — no extra/mnemonic field.
    expect(Object.keys(parsed).sort()).toEqual(
      ["authKey", "authKeyHex", "chainId", "eoaAddress", "encryptionKey", "v", "walletAddress"].sort(),
    );
  });

  it("clearSessionKeys removes the persisted session", () => {
    saveSessionKeys(keys());
    expect(loadSessionKeys()).not.toBeNull();
    clearSessionKeys();
    expect(loadSessionKeys()).toBeNull();
  });

  it("falls back to null on malformed JSON instead of throwing", () => {
    sessionStorage.setItem("totalreclaw-spa:session:v1", "{not json");
    expect(() => loadSessionKeys()).not.toThrow();
    expect(loadSessionKeys()).toBeNull();
  });

  it("falls back to null on a wrong schema version", () => {
    sessionStorage.setItem(
      "totalreclaw-spa:session:v1",
      JSON.stringify({ v: 2, authKey: "x", encryptionKey: "x", authKeyHex: "x", walletAddress: "0x0", chainId: 100 }),
    );
    expect(loadSessionKeys()).toBeNull();
  });

  it("falls back to null when required fields are missing", () => {
    sessionStorage.setItem("totalreclaw-spa:session:v1", JSON.stringify({ v: 1, authKey: "x" }));
    expect(loadSessionKeys()).toBeNull();
  });

  it("falls back to null when a key doesn't decode to 32 bytes", () => {
    const sk = keys();
    saveSessionKeys(sk);
    const rec = JSON.parse(sessionStorage.getItem("totalreclaw-spa:session:v1")!);
    rec.authKey = btoa("too-short");
    sessionStorage.setItem("totalreclaw-spa:session:v1", JSON.stringify(rec));
    expect(loadSessionKeys()).toBeNull();
  });

  it("saveSessionKeys does not throw when sessionStorage is unavailable", () => {
    (globalThis as { sessionStorage?: unknown }).sessionStorage = {
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
    };
    expect(() => saveSessionKeys(keys())).not.toThrow();
  });

  it("loadSessionKeys returns null when sessionStorage.getItem throws", () => {
    (globalThis as { sessionStorage?: unknown }).sessionStorage = {
      getItem: () => {
        throw new Error("SecurityError");
      },
    };
    expect(loadSessionKeys()).toBeNull();
  });
});
