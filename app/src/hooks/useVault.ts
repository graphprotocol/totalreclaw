import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { SessionKeys, VaultItem, MemoryClaimV1 } from "../lib/types";
import {
  exportAllFacts,
  decryptFacts,
  deleteFact,
  batchDeleteFacts,
  updateClaim,
} from "../lib/api";

export const VAULT_QUERY_KEY = ["vault"] as const;
export const VAULT_HISTORY_QUERY_KEY = ["vault", "history"] as const;

export function useVault(keys: SessionKeys | null) {
  return useQuery({
    queryKey: VAULT_QUERY_KEY,
    queryFn: async () => {
      if (!keys) return [];
      const raw = await exportAllFacts(keys);
      return decryptFacts(raw, keys);
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

export function useDeleteFact(keys: SessionKeys) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (factId: string) => deleteFact(factId, keys),
    onSuccess: (_data, factId) => {
      qc.setQueryData<VaultItem[]>(VAULT_QUERY_KEY, (prev) =>
        prev?.filter((it) => it.id !== factId) ?? [],
      );
    },
  });
}

export function useBatchDelete(keys: SessionKeys) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ids: string[]) => batchDeleteFacts(ids, keys),
    onSuccess: (_data, ids) => {
      const idSet = new Set(ids);
      qc.setQueryData<VaultItem[]>(VAULT_QUERY_KEY, (prev) =>
        prev?.filter((it) => !idSet.has(it.id)) ?? [],
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
      qc.setQueryData<VaultItem[]>(VAULT_QUERY_KEY, (prev) =>
        prev?.map((it) =>
          it.id === item.id
            ? {
                ...it,
                claim: updatedClaim,
                type: updatedClaim.type ?? it.type,
                pinned: updatedClaim.pin_status === "pinned",
              }
            : it,
        ) ?? [],
      );
    },
  });
}
