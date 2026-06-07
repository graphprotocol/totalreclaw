import {
  BillingStatus,
  RawFact,
  VaultItem,
  MemoryClaimV1,
  MemoryTypeV1,
  MEMORY_TYPES_V1,
  SubgraphFact,
} from "./types";
import { decryptBlob } from "./crypto";
import { SessionKeys } from "./types";

const SERVER_URL =
  import.meta.env.VITE_SERVER_URL?.replace(/\/$/, "") ??
  "https://api.totalreclaw.xyz";

export function getServerUrl(): string {
  return SERVER_URL;
}

function authHeaders(keys: SessionKeys): HeadersInit {
  return {
    Authorization: `Bearer ${keys.authKeyHex}`,
    "Content-Type": "application/json",
    Accept: "application/json",
    "X-Wallet-Address": keys.walletAddress,
    "X-TotalReclaw-Client": "ts-spa-vault",
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

/**
 * Idempotent vault registration. Required before any authenticated relay call
 * succeeds — the relay looks up the user by sha256(authKey), and 401s if no
 * row exists. Re-registering is a no-op.
 */
export async function registerSession(keys: SessionKeys): Promise<void> {
  const authKeyHashHex = await sha256Hex(keys.authKey);
  // salt is the first 32 bytes of the BIP-39 seed, but the SPA already exposes
  // a stable salt via the relay's persisted record. Per the auth design,
  // re-registering with the same auth_key_hash is idempotent regardless of
  // salt — relay does an existsByAuthHash short-circuit. We send a 32-byte
  // dummy salt that's deterministic from authKey so the relay never sees a
  // changing value across logins from the same SPA session.
  const saltHex = await sha256Hex(
    concat(new TextEncoder().encode("totalreclaw-spa-salt-v1"), keys.authKey),
  );
  const res = await fetch(`${SERVER_URL}/v1/register`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-TotalReclaw-Client": "ts-spa-vault",
    },
    body: JSON.stringify({
      auth_key_hash: authKeyHashHex,
      salt: saltHex,
    }),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`register → ${res.status}: ${body}`);
  }
}

export async function getAccount(keys: SessionKeys): Promise<BillingStatus> {
  const path = `/v1/billing/status?wallet_address=${keys.walletAddress}`;
  return apiFetch<BillingStatus>(path, keys);
}

interface SubgraphResponse<T> {
  data?: T;
  errors?: Array<{ message: string }>;
}

const FACT_FIELDS = `
      id
      encryptedBlob
      encryptedEmbedding
      decayScore
      timestamp
      createdAt
      version
      isActive`;

const PAGE_QUERY = `
  query VaultExport($owner: Bytes!, $first: Int!, $skip: Int!) {
    facts(
      where: { owner: $owner, isActive: true }
      first: $first
      skip: $skip
      orderBy: createdAt
      orderDirection: desc
    ) {${FACT_FIELDS}
    }
  }
`;

// Full history (active + tombstoned/superseded) — powers Lineage + the Review
// "changed" feed, which need the superseded versions the active view filters out.
const PAGE_QUERY_ALL = `
  query VaultHistory($owner: Bytes!, $first: Int!, $skip: Int!) {
    facts(
      where: { owner: $owner }
      first: $first
      skip: $skip
      orderBy: createdAt
      orderDirection: desc
    ) {${FACT_FIELDS}
    }
  }
`;

const PAGE_SIZE = 1000; // Graph Studio caps `first` at 1000

/**
 * Fetch all active facts owned by the Smart Account via the relay's
 * subgraph proxy. Pagination via skip (deterministic ordering on createdAt).
 */
export async function exportAllFacts(
  keys: SessionKeys,
  onProgress?: (loaded: number, total: number | undefined) => void,
  opts?: { includeInactive?: boolean },
): Promise<RawFact[]> {
  const all: RawFact[] = [];
  let skip = 0;
  const query = opts?.includeInactive ? PAGE_QUERY_ALL : PAGE_QUERY;

  for (;;) {
    const res = await apiFetch<SubgraphResponse<{ facts: SubgraphFact[] }>>(
      "/v1/subgraph",
      keys,
      {
        method: "POST",
        body: JSON.stringify({
          query,
          variables: {
            owner: keys.walletAddress,
            first: PAGE_SIZE,
            skip,
          },
        }),
      },
    );

    if (res.errors?.length) {
      throw new Error(`subgraph: ${res.errors.map((e) => e.message).join("; ")}`);
    }

    const page = res.data?.facts ?? [];
    for (const fact of page) {
      all.push(subgraphFactToRawFact(fact));
    }
    onProgress?.(all.length, undefined);

    if (page.length < PAGE_SIZE) break;
    skip += PAGE_SIZE;
  }

  return all;
}

function subgraphFactToRawFact(sf: SubgraphFact): RawFact {
  const createdAtMs = Number(sf.createdAt || sf.timestamp) * 1000;
  const iso = Number.isFinite(createdAtMs)
    ? new Date(createdAtMs).toISOString()
    : new Date().toISOString();
  return {
    id: sf.id,
    encrypted_blob: sf.encryptedBlob.replace(/^0x/, ""),
    blind_indices: [], // subgraph carries blind indices separately; not needed for read path
    decay_score: Number(sf.decayScore),
    version: sf.version,
    // `source` was removed from the subgraph Fact entity in v0.6.0. The
    // decrypted MemoryClaim carries its own `source` field (taxonomy v1),
    // so this stub is only used to keep the RawFact shape stable.
    source: "",
    created_at: iso,
    updated_at: iso,
    encrypted_embedding: sf.encryptedEmbedding?.replace(/^0x/, "") || undefined,
    is_active: sf.isActive,
  };
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
        isActive: fact.is_active ?? true,
      });
    } catch {
      // skip undecryptable facts silently (wrong key, corrupt, etc.)
    }
  }
  return items;
}

// ---------------------------------------------------------------------------
// Write path — Phase 2 (managed-mode UserOp construction not yet implemented).
// These stubs preserve the hook surface so the UI compiles + renders. Calls
// that hit the write path will surface a clear error to the user.
// ---------------------------------------------------------------------------

const WRITES_NOT_IMPLEMENTED =
  "Vault writes are not yet available in the web app. Use a TotalReclaw agent (Claude Desktop, OpenClaw, etc.) to modify your vault.";

export async function deleteFact(
  _factId: string,
  _keys: SessionKeys,
): Promise<void> {
  throw new Error(WRITES_NOT_IMPLEMENTED);
}

export async function batchDeleteFacts(
  _factIds: string[],
  _keys: SessionKeys,
): Promise<void> {
  throw new Error(WRITES_NOT_IMPLEMENTED);
}

export async function updateClaim(
  _item: VaultItem,
  _updatedClaim: MemoryClaimV1,
  _keys: SessionKeys,
): Promise<void> {
  throw new Error(WRITES_NOT_IMPLEMENTED);
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", bytes as unknown as ArrayBuffer);
  const arr = new Uint8Array(hash);
  return Array.from(arr)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}
