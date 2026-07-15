import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { PinStatus, SessionKeys, VaultItem, MemoryClaimV1 } from "../lib/types";
import {
  exportAllFacts,
  decryptFacts,
  deleteFact,
  batchDeleteFacts,
  setPinStatus,
  updateClaim,
} from "../lib/api";
import { useCrypto } from "../contexts/CryptoContext";
import { bytesToHex } from "../lib/crypto";

/**
 * Build the master-key signer for a write. Returns a `(userOpHashHex) =>
 * Promise<sigHex>` that runs a fresh passkey assertion, transiently unwraps the
 * master key, WASM-signs the hash, and zeroes the key — all inside
 * `withMasterKey`. The WASM is lazy-loaded here (write path only).
 */
function useUserOpSigner(): (userOpHashHex: string) => Promise<string> {
  const { withMasterKey } = useCrypto();
  return (userOpHashHex: string) =>
    withMasterKey(async (masterPriv) => {
      const { loadCore } = await import("../lib/wasm");
      const core = await loadCore();
      return core.signUserOp(userOpHashHex, bytesToHex(masterPriv));
    });
}

export const VAULT_QUERY_KEY = ["vault"] as const;
export const VAULT_HISTORY_QUERY_KEY = ["vault", "history"] as const;

/** Decrypted vault + how many ciphertext rows the subgraph returned. `fetched`
 *  lets the UI tell "0 on chain" apart from "fetched N but couldn't decrypt"
 *  (wrong key / older format / wrong chain) — so "empty" never lies. */
export interface VaultData {
  items: VaultItem[];
  fetched: number;
}

export function useVault(keys: SessionKeys | null) {
  return useQuery({
    queryKey: VAULT_QUERY_KEY,
    queryFn: async (): Promise<VaultData> => {
      if (!keys) return { items: [], fetched: 0 };
      const raw = await exportAllFacts(keys);
      return { items: decryptFacts(raw, keys), fetched: raw.length };
    },
    enabled: !!keys,
    staleTime: 30_000,
  });
}

/** Full history incl. tombstoned/superseded facts — for Lineage + the Review
 *  "changed" feed (the active view filters superseded versions out). */
export function useVaultHistory(keys: SessionKeys | null) {
  return useQuery({
    queryKey: VAULT_HISTORY_QUERY_KEY,
    queryFn: async () => {
      if (!keys) return [];
      const raw = await exportAllFacts(keys, undefined, { includeInactive: true });
      return decryptFacts(raw, keys);
    },
    enabled: !!keys,
    staleTime: 30_000,
  });
}

export function useDeleteFact(keys: SessionKeys | null) {
  const qc = useQueryClient();
  const sign = useUserOpSigner();
  return useMutation({
    mutationFn: (factId: string) => {
      if (!keys) throw new Error("Vault is locked.");
      return deleteFact(factId, keys, sign);
    },
    onSuccess: (_data, factId) => {
      // The vault cache holds VaultData ({items, fetched}), not VaultItem[].
      qc.setQueryData<VaultData>(VAULT_QUERY_KEY, (prev) =>
        prev
          ? { ...prev, items: prev.items.filter((it) => it.id !== factId) }
          : prev,
      );
    },
  });
}

export function useBatchDelete(keys: SessionKeys | null) {
  const qc = useQueryClient();
  const sign = useUserOpSigner();
  return useMutation({
    mutationFn: (ids: string[]) => {
      if (!keys) throw new Error("Vault is locked.");
      return batchDeleteFacts(ids, keys, sign);
    },
    onSuccess: (_data, ids) => {
      const idSet = new Set(ids);
      qc.setQueryData<VaultData>(VAULT_QUERY_KEY, (prev) =>
        prev
          ? { ...prev, items: prev.items.filter((it) => !idSet.has(it.id)) }
          : prev,
      );
    },
  });
}

/**
 * Pin/unpin a memory on-chain (A.2 Phase 2 — 2-call supersession batch).
 * On receipt, the cached item is reconciled in place: new fact id, new claim
 * (pin_status + superseded_by), new rawBlob — so an immediate follow-up toggle
 * rebuilds from the CURRENT on-chain blob without a refetch. Idempotent no-ops
 * (already in the target state) resolve without touching the cache.
 */
export function usePinFact(keys: SessionKeys | null) {
  const qc = useQueryClient();
  const sign = useUserOpSigner();
  return useMutation({
    mutationFn: ({ item, target }: { item: VaultItem; target: PinStatus }) => {
      if (!keys) throw new Error("Vault is locked.");
      return setPinStatus(item, target, keys, sign);
    },
    onSuccess: (result, { item, target }) => {
      if (result.idempotent) return;
      qc.setQueryData<VaultData>(VAULT_QUERY_KEY, (prev) =>
        prev
          ? {
              ...prev,
              items: prev.items.map((it) =>
                it.id === item.id
                  ? {
                      ...it,
                      id: result.newFactId ?? it.id,
                      claim: result.newClaim ?? it.claim,
                      pinned: target === "pinned",
                      rawBlob: result.newBlobHex ?? it.rawBlob,
                    }
                  : it,
              ),
            }
          : prev,
      );
    },
  });
}

export function useUpdateClaim(keys: SessionKeys) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      item,
      updatedClaim,
    }: {
      item: VaultItem;
      updatedClaim: MemoryClaimV1;
    }) => updateClaim(item, updatedClaim, keys),
    onSuccess: (_data, { item, updatedClaim }) => {
      qc.setQueryData<VaultData>(VAULT_QUERY_KEY, (prev) =>
        prev
          ? {
              ...prev,
              items: prev.items.map((it) =>
                it.id === item.id
                  ? {
                      ...it,
                      claim: updatedClaim,
                      type: updatedClaim.type ?? it.type,
                      pinned: updatedClaim.pin_status === "pinned",
                    }
                  : it,
              ),
            }
          : prev,
      );
    },
  });
}
