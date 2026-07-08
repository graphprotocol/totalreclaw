export const MEMORY_TYPES_V1 = [
  "claim",
  "preference",
  "directive",
  "commitment",
  "episode",
  "summary",
] as const;

export type MemoryTypeV1 = (typeof MEMORY_TYPES_V1)[number];

/** Provenance axis — closed enum per docs/specs/totalreclaw/memory-taxonomy-v1.md */
export const MEMORY_SOURCES = [
  "user",
  "user-inferred",
  "assistant",
  "external",
  "derived",
] as const;

export type MemorySource = (typeof MEMORY_SOURCES)[number];

/** Life-domain axis — closed enum per docs/specs/totalreclaw/memory-taxonomy-v1.md */
export const MEMORY_SCOPES = [
  "work",
  "personal",
  "health",
  "family",
  "creative",
  "finance",
  "misc",
  "unspecified",
] as const;

export type MemoryScope = (typeof MEMORY_SCOPES)[number];

export type PinStatus = "pinned" | "unpinned";

/** Structured entity reference inside a v1 claim (core MemoryEntityV1). */
export interface MemoryEntityV1 {
  name: string;
  type?: string;
  role?: string;
}

/** Typed metadata payload inside the encrypted v1 blob (core MemoryMetadataV1).
 *  Carries the Hermes session-end Crystal structure. All fields optional —
 *  pre-v1.1 / non-batched entries omit `metadata` entirely. */
export interface MemoryMetadataV1 {
  /** "session_crystal" marks the Crystal debrief of a session. */
  subtype?: string;
  /** Client-local UUIDv7 tying a Crystal + N atomic facts to one session. */
  session_id?: string;
  context?: string;
  key_outcomes?: string[];
  open_threads?: string[];
  lessons?: string[];
  topics_discussed?: string[];
  files_affected?: string[];
  /** Provider when this memory came from an import (e.g. "Gemini", "ChatGPT").
   *  Written by the import engine (per-fact typed metadata, #356). */
  import_source?: string;
  /** Agent-instance provenance (#317). Free text, ≤64 chars at write, untrusted
   *  display string. On the encrypted wire it rides as a TOP-LEVEL blob key
   *  (see MemoryClaimV1.agent_name); the Python recall path also re-nests it
   *  here. Read it via `agentNameOf()` which checks both locations. */
  agent_name?: string;
}

/** Inner JSON blob stored encrypted in the vault (v1.1 schema — mirrors
 *  rust/totalreclaw-core MemoryClaimV1). Unmodeled fields are ignored on parse. */
export interface MemoryClaimV1 {
  id: string;
  text: string;
  type: MemoryTypeV1;
  source: MemorySource;
  created_at: string;
  schema_version: "1.0";
  scope?: MemoryScope;
  volatility?: string;
  entities?: MemoryEntityV1[];
  reasoning?: string;
  expires_at?: string;
  importance?: number;
  confidence?: number;
  pin_status?: PinStatus;
  superseded_by?: string;
  metadata?: MemoryMetadataV1;
  /** Agent-instance provenance (#317). Canonical wire location: Hermes writes
   *  this as a top-level blob key (`canonical["agent_name"]`). Untrusted free
   *  text (≤64 chars at write); render via normal JSX auto-escaping only. */
  agent_name?: string;
  /** legacy/extra fields tolerated for back-compat */
  tags?: string[];
  supersedes?: string[];
}

/** Decrypted vault item ready for UI consumption */
export interface VaultItem {
  id: string;
  claim: MemoryClaimV1;
  /** Derived from tags[0] if claim.type is missing (legacy fallback).
   *  `| string` retained for legacy v0 on-chain type tokens (fact, context,
   *  decision, episodic, goal, rule) that predate the v1 closed enum. */
  type: MemoryTypeV1 | string;
  pinned: boolean;
  createdAt: Date;
  /** Original hex-encoded encrypted blob from the server (for re-store) */
  rawBlob: string;
  /** Blind indices from the server (reused when re-storing) */
  blindIndices: string[];
  decayScore: number;
  /** On-chain active flag. false = tombstoned/superseded (only present when the
   *  history fetch includes inactive facts; defaults true for the active view). */
  isActive: boolean;
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
  is_active: boolean;
}

/** Keys derived from the 12-word mnemonic — held in CryptoContext only.
 *  walletAddress is the deterministic ERC-4337 Smart Account address that owns
 *  the on-chain vault (returned by the relay's /v1/smart-account endpoint). */
export interface SessionKeys {
  // NOTE: the mnemonic is intentionally NOT held here. It touches RAM only as a
  // transient local during bootstrap/recovery, then is dropped — never persisted
  // in app-wide state (phrase-safety invariant). See CryptoContext.
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

export const TYPE_COLORS: Record<string, string> = {
  preference: "bg-blue-100 text-blue-800",
  directive: "bg-purple-100 text-purple-800",
  commitment: "bg-orange-100 text-orange-800",
  episode: "bg-gray-100 text-gray-700",
  summary: "bg-green-100 text-green-800",
  claim: "bg-slate-100 text-slate-700",
};
