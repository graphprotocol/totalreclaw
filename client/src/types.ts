/**
 * TotalReclaw Client - Type Definitions
 *
 * Core types for the end-to-end encrypted memory client library.
 */

// ============================================================================
// Configuration Types
// ============================================================================

/**
 * LSH configuration parameters (32-bit x 20 tables -- matching MCP)
 */
export interface LSHConfig {
  /** Number of bits per hash table (default: 32) */
  n_bits_per_table: number;
  /** Number of independent hash tables (default: 20) */
  n_tables: number;
  /** Number of candidates to retrieve for re-ranking (default: 3000) */
  candidate_pool: number;
}

/**
 * Default LSH configuration (matches mcp/src/subgraph/lsh.ts)
 */
export const DEFAULT_LSH_CONFIG: LSHConfig = {
  n_bits_per_table: 32,
  n_tables: 20,
  candidate_pool: 3000,
};

/**
 * TotalReclaw client configuration
 */
export interface TotalReclawConfig {
  /** Server URL (e.g., http://127.0.0.1:8080) */
  serverUrl: string;
  /** Path to ONNX model file (optional, will use default if not provided) */
  modelPath?: string;
  /** LSH configuration (optional, uses defaults if not provided) */
  lshConfig?: Partial<LSHConfig>;
  /** Request timeout in milliseconds (default: 30000) */
  timeout?: number;
}

// ============================================================================
// Encryption Types
// ============================================================================

// Note: EncryptedData and KeyDerivationParams are defined in crypto/ module

// ============================================================================
// Memory Types
// ============================================================================

/**
 * Metadata associated with a stored fact
 */
export interface FactMetadata {
  /** Source of the memory (e.g., "conversation", "document") */
  source?: string;
  /** Original timestamp of the memory */
  timestamp?: Date;
  /** User-defined importance (0-1) */
  importance?: number;
  /** Access count for decay calculation */
  accessCount?: number;
  /** Last access timestamp */
  lastAccessed?: Date;
  /** Additional tags */
  tags?: string[];
}

/**
 * A decrypted fact returned from recall
 */
export interface Fact {
  /** Unique fact ID (UUID v7) */
  id: string;
  /** The memory text content */
  text: string;
  /** The embedding vector (640 dimensions for Harrier-OSS-v1-270M) */
  embedding: number[];
  /** Associated metadata */
  metadata: FactMetadata;
  /** Calculated decay score */
  decayScore: number;
  /** Creation timestamp */
  createdAt: Date;
}

/**
 * Encrypted fact for server storage.
 *
 * Encrypted fields use base64 wire format: iv(12) || tag(16) || ciphertext.
 */
export interface EncryptedFact {
  /** Unique fact ID (UUID v7) */
  id: string;
  /** Encrypted document content (base64-encoded: iv || tag || ciphertext) */
  encryptedDoc: string;
  /** Encrypted embedding vector (base64-encoded: iv || tag || ciphertext) */
  encryptedEmbedding: string;
  /** Blind indices for search (SHA-256 hashes of tokens + LSH buckets) */
  blindIndices: string[];
  /** Initial decay score */
  decayScore: number;
  /** Creation timestamp (Unix milliseconds) */
  timestamp: number;
}

// ============================================================================
// Search Types
// ============================================================================

/**
 * Search result before decryption.
 *
 * Encrypted fields use base64 wire format: iv(12) || tag(16) || ciphertext.
 */
export interface EncryptedSearchResult {
  /** Fact ID */
  factId: string;
  /** Encrypted document (base64-encoded) */
  encryptedDoc: string;
  /** Encrypted embedding (base64-encoded) */
  encryptedEmbedding: string;
  /** Server-side decay score */
  decayScore: number;
  /** Timestamp */
  timestamp: number;
}

/**
 * Reranked search result
 */
export interface RerankedResult {
  /** The decrypted fact */
  fact: Fact;
  /** Combined relevance score (0-1) */
  score: number;
  /** Vector similarity component */
  vectorScore: number;
  /** BM25 text score component */
  textScore: number;
  /** Decay-adjusted score */
  decayAdjustedScore: number;
}

// ============================================================================
// API Types
// ============================================================================

/**
 * Registration request data
 */
export interface RegisterRequest {
  /** User ID (UUID v7) */
  userId: string;
  /** Hashed auth key (HKDF-SHA256) */
  authKeyHash: Buffer;
  /** Salt for key derivation */
  salt: Buffer;
}

/**
 * Registration response
 */
export interface RegisterResponse {
  /** Whether registration succeeded */
  success: boolean;
  /** Error code if failed */
  errorCode?: 'USER_EXISTS' | 'INVALID_REQUEST' | 'SERVER_ERROR';
  /** Error message if failed */
  errorMessage?: string;
}

/**
 * Store request data
 */
export interface StoreRequest {
  /** User ID */
  userId: string;
  /** Authentication proof (HMAC) */
  authProof: Buffer;
  /** Fact to store */
  fact: EncryptedFact;
}

/**
 * Store response
 */
export interface StoreResponse {
  /** Whether storage succeeded */
  success: boolean;
  /** Error code if failed */
  errorCode?: 'AUTH_FAILED' | 'STORAGE_ERROR' | 'INVALID_REQUEST';
  /** Version for optimistic locking */
  version?: number;
}

/**
 * Search request data
 */
export interface SearchRequest {
  /** User ID */
  userId: string;
  /** Authentication proof (HMAC) */
  authProof: Buffer;
  /** Trapdoor queries (SHA-256 of LSH buckets + optional keyword hashes) */
  trapdoors: string[];
  /** Maximum candidates to retrieve */
  maxCandidates: number;
}

/**
 * Search response
 */
export interface SearchResponse {
  /** Whether search succeeded */
  success: boolean;
  /** Error code if failed */
  errorCode?: 'AUTH_FAILED' | 'SEARCH_ERROR' | 'INVALID_REQUEST';
  /** Search results */
  results: EncryptedSearchResult[];
  /** Total candidates found */
  totalCandidates: number;
}

// ============================================================================
// Export Types
// ============================================================================

/**
 * Exported data for portability
 */
export interface ExportedData {
  /** Format version */
  version: string;
  /** Export timestamp */
  exportedAt: Date;
  /** Encrypted facts */
  facts: EncryptedFact[];
  /** Key derivation parameters (salt and optional costs) */
  keyParams: {
    salt: Buffer;
    memoryCost?: number;
    timeCost?: number;
    parallelism?: number;
  };
  /** LSH configuration used */
  lshConfig: LSHConfig;
}

// ============================================================================
// Error Types
// ============================================================================

/**
 * TotalReclaw error codes
 */
export enum TotalReclawErrorCode {
  ENCRYPTION_FAILED = 'ENCRYPTION_FAILED',
  DECRYPTION_FAILED = 'DECRYPTION_FAILED',
  KEY_DERIVATION_FAILED = 'KEY_DERIVATION_FAILED',
  EMBEDDING_FAILED = 'EMBEDDING_FAILED',
  LSH_HASH_FAILED = 'LSH_HASH_FAILED',
  NETWORK_ERROR = 'NETWORK_ERROR',
  AUTH_FAILED = 'AUTH_FAILED',
  NOT_REGISTERED = 'NOT_REGISTERED',
  ALREADY_REGISTERED = 'ALREADY_REGISTERED',
  INVALID_INPUT = 'INVALID_INPUT',
  MODEL_LOAD_FAILED = 'MODEL_LOAD_FAILED',
}

/**
 * TotalReclaw error class
 */
export class TotalReclawError extends Error {
  constructor(
    public readonly code: TotalReclawErrorCode,
    message: string,
    public readonly cause?: Error
  ) {
    super(message);
    this.name = 'TotalReclawError';
  }
}
