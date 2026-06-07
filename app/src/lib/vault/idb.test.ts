import "fake-indexeddb/auto";
import { describe, it, expect, beforeEach } from "vitest";
import { clear } from "idb-keyval";
import {
  saveVaultRecord,
  loadVaultRecord,
  hasAnyVault,
  clearVault,
  type VaultRecord,
} from "./idb";
import { wrapKey } from "../auth/wrap";

function record(sa: string): VaultRecord {
  const secret = new Uint8Array(32).fill(7);
  const key = new Uint8Array(32).fill(9);
  return {
    v: 1,
    smart_account: sa,
    chain_id: 100,
    credential_id: "Y3JlZA",
    wrapped_vault_key: wrapKey(key, secret),
    wrapped_auth_key: wrapKey(key, secret),
    wrapped_master_key: wrapKey(key, secret),
    created_at: 1_700_000_000,
  };
}

beforeEach(async () => {
  await clear();
});

describe("vault idb store", () => {
  it("reports no vault on a fresh device", async () => {
    expect(await hasAnyVault()).toBe(false);
    expect(await loadVaultRecord()).toBeNull();
  });

  it("round-trips a vault record (wrapped keys preserved)", async () => {
    const rec = record("0xABCdef0000000000000000000000000000000001");
    await saveVaultRecord(rec);
    expect(await hasAnyVault()).toBe(true);
    const loaded = await loadVaultRecord(rec.smart_account);
    expect(loaded?.smart_account).toBe(rec.smart_account);
    expect(loaded?.chain_id).toBe(100);
    expect(loaded?.wrapped_vault_key.ciphertext).toEqual(rec.wrapped_vault_key.ciphertext);
    expect(loaded?.wrapped_vault_key.nonce.length).toBe(24);
  });

  it("loads the first vault when no SA is given (single-vault v1)", async () => {
    const rec = record("0x00000000000000000000000000000000000000aa");
    await saveVaultRecord(rec);
    const loaded = await loadVaultRecord();
    expect(loaded?.smart_account).toBe(rec.smart_account);
  });

  it("is case-insensitive on the SA key", async () => {
    const rec = record("0xABCDEF0000000000000000000000000000000002");
    await saveVaultRecord(rec);
    expect(await loadVaultRecord("0xabcdef0000000000000000000000000000000002")).not.toBeNull();
  });

  it("clearVault removes the record", async () => {
    const rec = record("0x00000000000000000000000000000000000000bb");
    await saveVaultRecord(rec);
    await clearVault(rec.smart_account);
    expect(await hasAnyVault()).toBe(false);
  });
});
