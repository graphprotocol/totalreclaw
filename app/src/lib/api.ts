import {
  ExportResponse,
  AccountInfo,
  RawFact,
  VaultItem,
  MemoryClaimV1,
  MemoryTypeV1,
  MEMORY_TYPES_V1,
} from "./types";
import { decryptBlob, encryptBlob } from "./crypto";
import { SessionKeys } from "./types";

const SERVER_URL =
  import.meta.env.VITE_SERVER_URL?.replace(/\/$/, "") ?? "https://relay.totalreclaw.xyz";

function authHeaders(keys: SessionKeys): HeadersInit {
  return {
    Authorization: `Bearer ${keys.authKeyHex}`,
    "Content-Type": "application/json",
    Accept: "application/json",
  };
}

async function apiFetch<T>(
  path: string,
  keys: SessionKeys,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${SERVER_URL}${path}`, {
    ...init,
    headers: {
      ...authHeaders(keys),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`API ${path} → ${res.status}: ${body}`);
  }
  return res.json() as Promise<T>;
}

export async function getAccount(keys: SessionKeys): Promise<AccountInfo> {
  return apiFetch<AccountInfo>("/v1/account", keys);
}

/** Fetch all facts via cursor-paginated export. Returns raw encrypted facts. */
export async function exportAllFacts(
  keys: SessionKeys,
  onProgress?: (loaded: number, total: number | undefined) => void,
): Promise<RawFact[]> {
  const all: RawFact[] = [];
  let cursor: string | undefined;
  let total: number | undefined;

  do {
    const params = new URLSearchParams({ limit: "1000" });
    if (cursor) params.set("cursor", cursor);
    const page = await apiFetch<ExportResponse>(
      `/v1/export?${params}`,
      keys,
    );
    if (!page.success) {
      throw new Error(page.error_message ?? "Export failed");
    }
    all.push(...page.facts);
    total = page.total_count;
    cursor = page.has_more ? page.cursor : undefined;
    onProgress?.(all.length, total);
  } while (cursor);

  return all;
}

function resolveType(claim: MemoryClaimV1, tags: string[]): MemoryTypeV1 | string {
  if (claim.type && MEMORY_TYPES_V1.includes(claim.type)) return claim.type;
  const fromTags = tags[0];
  if (fromTags && MEMORY_TYPES_V1.includes(fromTags as MemoryTypeV1))
    return fromTags as MemoryTypeV1;
  return fromTags ?? "claim";
}

/** Decrypt all raw facts into VaultItems. Skips items that fail decryption. */
export function decryptFacts(
  facts: RawFact[],
  keys: SessionKeys,
): VaultItem[] {
  const items: VaultItem[] = [];
  for (const fact of facts) {
    try {
      const plaintext = decryptBlob(fact.encrypted_blob, keys.encryptionKey);
      const claim = JSON.parse(plaintext) as MemoryClaimV1;
      const tags: string[] = claim.tags ?? [];
      items.push({
        id: fact.id,
        claim,
        type: resolveType(claim, tags),
        pinned: claim.pin_status === "pinned",
        createdAt: new Date(fact.created_at),
        rawBlob: fact.encrypted_blob,
        blindIndices: fact.blind_indices,
        decayScore: fact.decay_score,
      });
    } catch {
      // skip undecryptable facts silently (wrong key, corrupt, etc.)
    }
  }
  return items;
}

export async function deleteFact(
  factId: string,
  keys: SessionKeys,
): Promise<void> {
  await apiFetch<unknown>(`/v1/facts/${factId}`, keys, { method: "DELETE" });
}

export async function batchDeleteFacts(
  factIds: string[],
  keys: SessionKeys,
): Promise<void> {
  if (factIds.length === 0) return;
  // Server supports up to 500 per batch
  for (let i = 0; i < factIds.length; i += 500) {
    const batch = factIds.slice(i, i + 500);
    await apiFetch<unknown>("/v1/facts/batch-delete", keys, {
      method: "POST",
      body: JSON.stringify({ fact_ids: batch }),
    });
  }
}

/** Re-store a modified claim (retype or pin update). Reuses existing blind indices. */
export async function updateClaim(
  item: VaultItem,
  updatedClaim: MemoryClaimV1,
  keys: SessionKeys,
): Promise<void> {
  const newBlob = encryptBlob(
    JSON.stringify(updatedClaim),
    keys.encryptionKey,
  );

  // First delete the old fact, then store the new one
  // (server /v1/store is append-only; deletion is the update mechanism)
  await apiFetch<unknown>(`/v1/facts/${item.id}`, keys, { method: "DELETE" });

  await apiFetch<unknown>("/v1/store", keys, {
    method: "POST",
    body: JSON.stringify({
      facts: [
        {
          id: item.id,
          encrypted_blob: newBlob,
          blind_indices: item.blindIndices,
          decay_score: item.decayScore,
        },
      ],
    }),
  });
}
