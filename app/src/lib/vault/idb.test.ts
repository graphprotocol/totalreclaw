import "fake-indexeddb/auto";
import { describe, it, expect, beforeEach } from "vitest";
import { clear, keys, set } from "idb-keyval";
import {
  saveVaultRecord,
  loadVaultRecord,
  hasAnyVault,
  clearVault,
  isVaultRecordExpired,
  refreshVaultRecordTtl,
  VAULT_RECORD_TTL_SECONDS,
  type VaultRecord,
} from "./idb";
import { wrapKey } from "../auth/wrap";

// Mirrors the storage prefix in idb.ts (not exported) so tests can assert a
// record was actually DELETED from IndexedDB, not merely hidden by the TTL.
const PREFIX = "totalreclaw-spa:vault:";

function record(sa: string, createdAt: number = Math.floor(Date.now() / 1000)): VaultRecord {
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
    created_at: createdAt,
  };
}

async function vaultKeysInIdb(): Promise<string[]> {
  return (await keys()).map(String).filter((k) => k.startsWith(PREFIX));
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

describe("forward-version tolerance (spa-passkey-unlock.md §2.2, §10.1)", () => {
  // A Phase 2 build may write `v: 2` records (two extra wrapped fields,
  // `wrapped_lsh_seed` + `wrapped_dedup_key`, no `salt` field — see
  // derived-bundle-v1.md §5.1). Both versions coexist on the same device
  // across a deploy, so a Phase 1 build must read the v1 fields it
  // understands and IGNORE the rest, rather than discarding or mutating the
  // record. This is a regression test that a Phase 1 build cannot start
  // gating on `.v` and silently log out Phase 2 users.
  it("a v2-shaped record (extra wrapped fields) still loads, unmodified, on a v1 build", async () => {
    const sa = "0x00000000000000000000000000000000000000cc";
    const v1Fields = record(sa);
    const v2Shaped = {
      ...v1Fields,
      v: 2,
      wrapped_lsh_seed: wrapKey(new Uint8Array(32).fill(3), new Uint8Array(32).fill(4)),
      wrapped_dedup_key: wrapKey(new Uint8Array(32).fill(5), new Uint8Array(32).fill(6)),
    };
    // Bypass the VaultRecord type (which is Phase-1-only, `v: 1`) to write a
    // record shaped the way a future Phase 2 build would — mirrors what
    // `set()` inside saveVaultRecord() does today, just with `v: 2`.
    await set(PREFIX + sa.toLowerCase(), v2Shaped);

    expect(await hasAnyVault()).toBe(true);
    const loaded = await loadVaultRecord(sa);
    expect(loaded).not.toBeNull();
    // v1 fields this build understands are intact and usable.
    expect(loaded!.smart_account).toBe(sa);
    expect(loaded!.wrapped_vault_key).toEqual(v1Fields.wrapped_vault_key);
    expect(loaded!.wrapped_auth_key).toEqual(v1Fields.wrapped_auth_key);
    expect(loaded!.wrapped_master_key).toEqual(v1Fields.wrapped_master_key);
    // The extra v2 fields survive the round-trip rather than being stripped —
    // proof this build doesn't rewrite/discard what it doesn't understand.
    expect((loaded as unknown as Record<string, unknown>).wrapped_lsh_seed).toEqual(
      v2Shaped.wrapped_lsh_seed,
    );
    expect((loaded as unknown as Record<string, unknown>).wrapped_dedup_key).toEqual(
      v2Shaped.wrapped_dedup_key,
    );
    // Not purged, not treated as expired/absent.
    expect(await vaultKeysInIdb()).toContain(PREFIX + sa.toLowerCase());
  });
});

describe("vault record TTL (#440)", () => {
  it("treats a record exactly at the TTL as still live (strict >)", () => {
    const created = 1_000_000;
    const atTtl = record("0x000000000000000000000000000000000000000d", created);
    // age == TTL  -> live
    expect(isVaultRecordExpired(atTtl, created + VAULT_RECORD_TTL_SECONDS)).toBe(false);
    // age == TTL + 1s -> expired
    expect(isVaultRecordExpired(atTtl, created + VAULT_RECORD_TTL_SECONDS + 1)).toBe(true);
  });

  it("loads a fresh record normally (an active user is unaffected)", async () => {
    const sa = "0x000000000000000000000000000000000000000e";
    await saveVaultRecord(record(sa)); // created_at = now
    expect(await hasAnyVault()).toBe(true);
    expect((await loadVaultRecord(sa))?.smart_account).toBe(sa);
  });

  it("purges a record older than the TTL on load — reads as absent and is gone from IDB", async () => {
    const sa = "0x000000000000000000000000000000000000000f";
    const expiredAt = Math.floor(Date.now() / 1000) - VAULT_RECORD_TTL_SECONDS - 60;
    await saveVaultRecord(record(sa, expiredAt));
    expect(await loadVaultRecord(sa)).toBeNull();
    // DELETED, not merely hidden — no vault key remains in IndexedDB:
    expect(await vaultKeysInIdb()).toEqual([]);
    expect(await hasAnyVault()).toBe(false);
  });

  it("purges an expired record on the no-SA (first-vault) load path", async () => {
    const sa = "0x0000000000000000000000000000000000000010";
    const expiredAt = Math.floor(Date.now() / 1000) - VAULT_RECORD_TTL_SECONDS - 60;
    await saveVaultRecord(record(sa, expiredAt));
    expect(await loadVaultRecord()).toBeNull();
    expect(await vaultKeysInIdb()).toEqual([]);
  });

  it("hasAnyVault purges an expired record and reports no vault", async () => {
    const sa = "0x0000000000000000000000000000000000000011";
    const expiredAt = Math.floor(Date.now() / 1000) - VAULT_RECORD_TTL_SECONDS - 60;
    await saveVaultRecord(record(sa, expiredAt));
    expect(await hasAnyVault()).toBe(false);
    expect(await vaultKeysInIdb()).toEqual([]);
  });

  it("refreshVaultRecordTtl bumps created_at to ~now (sliding TTL), preserving wrapped keys", async () => {
    const sa = "0x0000000000000000000000000000000000000012";
    const stale = record(sa, 1_700_000_000); // would otherwise be long expired
    await saveVaultRecord(stale);
    await refreshVaultRecordTtl(stale);
    const loaded = await loadVaultRecord(sa);
    expect(loaded).not.toBeNull();
    const now = Math.floor(Date.now() / 1000);
    expect(loaded!.created_at).toBeGreaterThanOrEqual(now - 2);
    expect(loaded!.created_at).toBeLessThanOrEqual(now);
    // Only the timestamp moved — identity + wrapped keys are byte-identical.
    expect(loaded!.smart_account).toBe(sa);
    expect(loaded!.wrapped_vault_key).toEqual(stale.wrapped_vault_key);
    expect(loaded!.wrapped_auth_key).toEqual(stale.wrapped_auth_key);
    expect(loaded!.wrapped_master_key).toEqual(stale.wrapped_master_key);
  });
});
