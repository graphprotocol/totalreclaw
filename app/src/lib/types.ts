export const MEMORY_TYPES_V1 = [
  "claim",
  "preference",
  "directive",
  "commitment",
  "episode",
  "summary",
] as const;

export type MemoryTypeV1 = (typeof MEMORY_TYPES_V1)[number];

export type PinStatus = "pinned" | "unpinned";

/** Inner JSON blob stored encrypted in the vault (v1.1 schema) */
export interface MemoryClaimV1 {
  id: string;
  text: string;
  type: MemoryTypeV1;
  source: string;
  created_at: string;
  schema_version: "1.0";
  importance?: number;
  tags?: string[];
  scope?: string;
  reasoning?: string;
  pin_status?: PinStatus;
  supersedes?: string[];
  superseded_by?: string;
}

/** Decrypted vault item ready for UI consumption */
export interface VaultItem {
  id: string;
  claim: MemoryClaimV1;
  /** Derived from tags[0] if claim.type is missing (legacy fallback) */
  type: MemoryTypeV1 | string;
  pinned: boolean;
  createdAt: Date;
  /** Original hex-encoded encrypted blob from the server (for re-store) */
  rawBlob: string;
  /** Blind indices from the server (reused when re-storing) */
  blindIndices: string[];
  decayScore: number;
}

/** Raw fact shape consumed by the SPA's decrypt path. Normalized from the
 *  subgraph GraphQL response, which uses camelCase + hex strings. */
export interface RawFact {
  id: string;
  encrypted_blob: string;
  blind_indices: string[];
  decay_score: number;
  version: number;
  source: string;
  /** ISO-8601 — derived from the subgraph's `createdAt` Unix timestamp */
  created_at: string;
  updated_at: string;
  encrypted_embedding?: string;
}

/** Keys derived from the 12-word mnemonic — held in CryptoContext only.
 *  walletAddress is the deterministic ERC-4337 Smart Account address that owns
 *  the on-chain vault (returned by the relay's /v1/smart-account endpoint). */
export interface SessionKeys {
  mnemonic: string;
  authKey: Uint8Array;
  encryptionKey: Uint8Array;
  authKeyHex: string;
  /** EOA derived from BIP-32 m/44'/60'/0'/0/0 — owns the Smart Account */
  eoaAddress: string;
  /** Smart Account address — the `owner` field used to query the subgraph */
  walletAddress: string;
  /** Chain ID for subgraph routing (84532 free / 100 pro). Pro detection
   *  happens after billing/status returns — defaults to free at derivation. */
  chainId: number;
}

/** Response from GET /v1/billing/status?wallet_address=... */
export interface BillingStatus {
  wallet_address: string;
  tier: "free" | "pro";
  writes_used?: number;
  writes_limit?: number;
  reads_used?: number;
  reads_limit?: number;
  features?: Record<string, unknown>;
  /** Server-computed extraction tuning knobs (not relevant for read-only SPA) */
  extraction_interval?: number;
  max_facts_per_extraction?: number;
  max_candidate_pool?: number;
}

/** Subgraph GraphQL response — `facts` entity per docs/specs/subgraph/seed-to-subgraph.md.
 *  Note: `source` was removed from the schema in v0.6.0 (Session 53). */
export interface SubgraphFact {
  id: string;
  encryptedBlob: string;
  encryptedEmbedding?: string;
  decayScore: string;
  timestamp: string;
  createdAt: string;
  version: number;
  isActive: boolean;
}

// Warm "Keeper" type palette — soft tint + accessible ink (see /DESIGN.md).
// Tokens defined in tailwind.config.ts. Color reinforces the mandatory text
// label; it is never the sole signal (color-blind + AA).
export const TYPE_COLORS: Record<string, string> = {
  claim: "bg-type-claim text-type-claim-ink",
  preference: "bg-type-preference text-type-preference-ink",
  directive: "bg-type-directive text-type-directive-ink",
  commitment: "bg-type-commitment text-type-commitment-ink",
  episode: "bg-type-episode text-type-episode-ink",
  summary: "bg-type-summary text-type-summary-ink",
};
