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

/** Paginated response from GET /v1/export */
export interface ExportResponse {
  success: boolean;
  error_code?: string;
  error_message?: string;
  facts: RawFact[];
  cursor?: string;
  has_more: boolean;
  total_count?: number;
}

export interface RawFact {
  id: string;
  encrypted_blob: string;
  blind_indices: string[];
  decay_score: number;
  version: number;
  source: string;
  created_at: string;
  updated_at: string;
  encrypted_embedding?: string;
}

/** Keys derived from the 12-word mnemonic — held in CryptoContext only */
export interface SessionKeys {
  mnemonic: string;
  authKey: Uint8Array;
  encryptionKey: Uint8Array;
  authKeyHex: string;
}

/** Response from GET /v1/account */
export interface AccountInfo {
  user_id: string;
  created_at: string;
  fact_count: number;
}

export const TYPE_COLORS: Record<string, string> = {
  preference: "bg-blue-100 text-blue-800",
  directive: "bg-purple-100 text-purple-800",
  commitment: "bg-orange-100 text-orange-800",
  episode: "bg-gray-100 text-gray-700",
  summary: "bg-green-100 text-green-800",
  claim: "bg-slate-100 text-slate-700",
};
