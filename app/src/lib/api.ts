import {
  BillingStatus,
  RawFact,
  VaultItem,
  MemoryClaimV1,
  MemoryTypeV1,
  MemorySource,
  MemoryScope,
  MEMORY_TYPES_V1,
  MEMORY_SOURCES,
  MEMORY_SCOPES,
  SubgraphFact,
} from "./types";
import { decryptBlob, encryptBlob } from "./crypto";
import type { PinStatus } from "./types";
import { SessionKeys } from "./types";
// Type-only: erased at compile time, so it does NOT pull the userop/wasm write
// chunk into the read bundle. The runtime import is dynamic (see deleteFact).
import type { SignUserOpHash } from "./userop";
export type { SignUserOpHash } from "./userop";

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
  const response = await fetch(`${SERVER_URL}${path}`, {
    ...init,
    headers: {
      ...authHeaders(keys),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`API ${path} → ${response.status}: ${body}`);
  }
  return response.json() as Promise<T>;
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
  const response = await fetch(`${SERVER_URL}/v1/register`, {
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
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new Error(`register → ${response.status}: ${body}`);
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

// NOTE: encryptedEmbedding is intentionally NOT fetched here. It's the 640-d
// search vector (~5.2KB hex/fact) used only for semantic rerank, which the SPA
// doesn't do (keyword filter + "ask your agent"). decryptFacts never reads it,
// so fetching it was pure wasted bandwidth (~7.7× the browse download). When
// in-SPA semantic search lands, fetch embeddings via a separate, candidate-pool
// -scoped query — not the full-vault browse.
const FACT_FIELDS = `
      id
      encryptedBlob
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
    const subgraphResponse = await apiFetch<SubgraphResponse<{ facts: SubgraphFact[] }>>(
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

    if (subgraphResponse.errors?.length) {
      throw new Error(`subgraph: ${subgraphResponse.errors.map((e) => e.message).join("; ")}`);
    }

    const page = subgraphResponse.data?.facts ?? [];
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
    // encrypted_embedding intentionally omitted — not fetched on the browse path.
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

/**
 * Coerce the decrypted JSON into a well-typed MemoryClaimV1. On-chain blobs
 * may carry source/scope strings outside the v1 closed enums (malformed or
 * future values); we clamp them to safe taxonomy members here so the narrow
 * types downstream are honest rather than casting blindly.
 */
function normalizeClaim(raw: MemoryClaimV1): MemoryClaimV1 {
  const source: MemorySource = MEMORY_SOURCES.includes(raw.source)
    ? raw.source
    : "external";
  const scope: MemoryScope | undefined =
    raw.scope === undefined
      ? undefined
      : MEMORY_SCOPES.includes(raw.scope)
        ? raw.scope
        : "unspecified";
  return { ...raw, source, scope };
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
      const claim = normalizeClaim(JSON.parse(plaintext) as MemoryClaimV1);
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
// Write path — Keeper A.2 curation writes.
//
// Phase 1 (delete/tombstone) + Phase 2 (pin/unpin via 2-call supersession) are
// implemented. The @totalreclaw/core WASM + UserOp assembler + claim rebuilder
// are lazy-loaded on the FIRST write only (dynamic import) so the 2.3 MB WASM
// never lands in the read chunk (see wasm.ts / userop.ts / claim.ts).
//
// Phase 3 (retype/set_scope — same supersession engine) remains a stub.
// ---------------------------------------------------------------------------

const WRITES_NOT_IMPLEMENTED =
  "This edit isn’t available in the web app yet. Use a TotalReclaw agent (Claude Desktop, OpenClaw, etc.) to modify your vault.";

/**
 * Resolve the authoritative chain + DataEdge for the wallet from the relay's
 * billing status. Writing to the wrong DataEdge silently strands the fact
 * outside the indexing subgraph, so `data_edge_address` must be present.
 */
async function resolveWriteTarget(
  keys: SessionKeys,
): Promise<{ dataEdgeAddress: string; chainId: number }> {
  const billing = await getAccount(keys);
  const dataEdgeAddress = billing.data_edge_address;
  if (!dataEdgeAddress || !/^0x[0-9a-fA-F]{40}$/.test(dataEdgeAddress)) {
    throw new Error(
      "The relay did not report a DataEdge address for this wallet; cannot safely write.",
    );
  }
  return { dataEdgeAddress, chainId: billing.chain_id ?? keys.chainId };
}

/**
 * Soft-delete (tombstone) a single fact on-chain. `sign` is the caller's
 * master-key signer (CryptoContext.withMasterKey → WASM signUserOp).
 */
export async function deleteFact(
  factId: string,
  keys: SessionKeys,
  sign: SignUserOpHash,
): Promise<void> {
  const target = await resolveWriteTarget(keys);
  const { submitTombstones } = await import("./userop");
  const res = await submitTombstones([factId], { keys, sign, ...target });
  if (!res.success) {
    throw new Error("The delete was submitted but not confirmed on-chain. Try again.");
  }
}

/** Soft-delete N facts in a single batched UserOp (`executeBatch`). */
export async function batchDeleteFacts(
  factIds: string[],
  keys: SessionKeys,
  sign: SignUserOpHash,
): Promise<void> {
  if (factIds.length === 0) return;
  const target = await resolveWriteTarget(keys);
  const { submitTombstones } = await import("./userop");
  const res = await submitTombstones(factIds, { keys, sign, ...target });
  if (!res.success) {
    throw new Error("The delete was submitted but not confirmed on-chain. Try again.");
  }
}

/**
 * Fetch the on-chain wire fields of a single fact that the browse query
 * deliberately skips (`encryptedEmbedding` is ~5.2KB/fact) plus its blind
 * indices (derived `blindIndexEntries`). A supersession write copies both
 * forward — the text never changes on pin/unpin, so the old search vectors
 * stay exact. Missing fact → empty fields (the write still proceeds; trapdoor
 * search simply won't cover the superseding row, same as a Hermes pin).
 */
async function fetchFactWireFields(
  keys: SessionKeys,
  factId: string,
): Promise<{ encryptedEmbedding?: string; blindIndices: string[] }> {
  const query = `
    query FactWireFields($id: ID!) {
      fact(id: $id) {
        encryptedEmbedding
        blindIndexEntries { hash }
      }
    }
  `;
  const resp = await apiFetch<
    SubgraphResponse<{
      fact: {
        encryptedEmbedding?: string | null;
        blindIndexEntries?: Array<{ hash: string }>;
      } | null;
    }>
  >("/v1/subgraph", keys, {
    method: "POST",
    body: JSON.stringify({ query, variables: { id: factId } }),
  });
  if (resp.errors?.length) {
    throw new Error(`subgraph: ${resp.errors.map((e) => e.message).join("; ")}`);
  }
  const fact = resp.data?.fact;
  return {
    encryptedEmbedding: fact?.encryptedEmbedding ?? undefined,
    blindIndices: (fact?.blindIndexEntries ?? []).map((e) => e.hash),
  };
}

/** Result of a pin/unpin write. `idempotent: true` → no UserOp was sent. */
export interface PinWriteResult {
  idempotent: boolean;
  /** The superseding fact's fresh UUID (absent on idempotent no-op). */
  newFactId?: string;
  /** The superseding claim (decrypted shape) for optimistic cache updates. */
  newClaim?: MemoryClaimV1;
  /** Hex wire blob of the superseding claim (becomes the item's rawBlob). */
  newBlobHex?: string;
}

/**
 * Pin or unpin a memory (A.2 Phase 2) — atomic 2-call `executeBatch`
 * supersession `[tombstone(old), newClaim(superseded_by=old, pin_status)]`,
 * mirroring `mcp/src/tools/pin.ts:executePinOperation`.
 *
 * Idempotent (pin.ts:667): pinning an already-pinned memory (or unpinning an
 * unpinned one — absence of `pin_status` counts as unpinned) sends NO UserOp.
 */
export async function setPinStatus(
  item: VaultItem,
  target: PinStatus,
  keys: SessionKeys,
  sign: SignUserOpHash,
): Promise<PinWriteResult> {
  const current: PinStatus =
    item.claim.pin_status === "pinned" ? "pinned" : "unpinned";
  if (current === target) {
    return { idempotent: true };
  }

  const targetChain = await resolveWriteTarget(keys);
  const [{ loadCore }, { rebuildClaimJson }, { submitSupersession }] =
    await Promise.all([import("./wasm"), import("./claim"), import("./userop")]);
  const core = await loadCore();

  // Rebuild from the RAW decrypted blob (not the normalized VaultItem.claim —
  // normalization clamps enums, which must not leak into the re-encrypted
  // wire). Full field carry-forward + metadata preservation live in claim.ts.
  const plaintext = decryptBlob(item.rawBlob, keys.encryptionKey);
  const newFactId = crypto.randomUUID();
  const canonicalJson = rebuildClaimJson(core, plaintext, {
    newId: newFactId,
    supersededBy: item.id,
    pinStatus: target,
  });
  const newBlobHex = encryptBlob(canonicalJson, keys.encryptionKey);

  // Copy embedding + blind indices forward from the old on-chain fact.
  const wire = await fetchFactWireFields(keys, item.id);
  const blindIndices =
    item.blindIndices.length > 0 ? item.blindIndices : wire.blindIndices;

  const res = await submitSupersession(
    item.id,
    {
      id: newFactId,
      encryptedBlobHex: newBlobHex,
      blindIndices,
      encryptedEmbedding: wire.encryptedEmbedding,
      source: target === "pinned" ? "spa_pin" : "spa_unpin",
    },
    { keys, sign, ...targetChain },
  );
  if (!res.success) {
    throw new Error(
      "The pin update was submitted but not confirmed on-chain. Try again.",
    );
  }
  return {
    idempotent: false,
    newFactId,
    newClaim: normalizeClaim(JSON.parse(canonicalJson) as MemoryClaimV1),
    newBlobHex,
  };
}

// Phase 3: retype/set_scope (same 2-call supersession engine — claim.ts /
// submitSupersession are ready for it) — not yet wired.
export async function updateClaim(
  _item: VaultItem,
  _updatedClaim: MemoryClaimV1,
  _keys: SessionKeys,
  _sign?: SignUserOpHash,
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
