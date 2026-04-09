/**
 * TotalReclaw Client Library
 *
 * A TypeScript library for end-to-end encrypted memory operations.
 *
 * This library provides:
 * - End-to-end encryption of memories and embeddings
 * - LSH-based blind index search
 * - Client-side reranking with BM25 + RRF fusion
 * - Importance decay for memory lifecycle management
 *
 * @example
 * ```typescript
 * import { TotalReclaw } from '@totalreclaw/client';
 *
 * const client = new TotalReclaw({
 *   serverUrl: 'http://127.0.0.1:8080',
 * });
 *
 * // Register a new user
 * const userId = await client.register('my-secure-password');
 *
 * // Store a memory
 * const factId = await client.remember('I prefer coffee over tea');
 *
 * // Recall memories
 * const results = await client.recall('what do I like to drink?');
 * ```
 */

import * as crypto from 'crypto';
import {
  TotalReclawConfig,
  Fact,
  FactMetadata,
  EncryptedFact,
  EncryptedSearchResult,
  RerankedResult,
  ExportedData,
  LSHConfig,
  TotalReclawError,
  TotalReclawErrorCode,
  DEFAULT_LSH_CONFIG,
} from './types';
import { deriveKeys, generateSalt, createAuthProof } from './crypto';
import type { KeyDerivationParams } from './crypto';
import { encrypt, decrypt } from './crypto/cipher';
import { generateBlindIndices, generateTrapdoors } from './crypto/blind';
import { LSHHasher, mergeLSHConfig } from './lsh';
import { deriveLshSeed } from './crypto/seed';
import { EmbeddingModel, createHashBasedEmbedding } from './embedding';
import { cosineSimilarity, BM25Scorer, rrfFusion, normalizeScores, calculateDecayScore } from './search';
import { TotalReclawClient } from './api';

/**
 * Internal state for the TotalReclaw client
 */
interface ClientState {
  userId: string | null;
  authKey: Buffer | null;
  encryptionKey: Buffer | null;
  salt: Buffer | null;
  isRegistered: boolean;
}

/**
 * Main TotalReclaw Client
 *
 * Provides a high-level API for end-to-end encrypted memory operations.
 */
export class TotalReclaw {
  private config: TotalReclawConfig & { lshConfig: Required<LSHConfig> };
  private apiClient: TotalReclawClient;
  private lshHasher: LSHHasher | null = null;
  private embeddingModel: EmbeddingModel;
  private bm25Scorer: BM25Scorer;
  private state: ClientState = {
    userId: null,
    authKey: null,
    encryptionKey: null,
    salt: null,
    isRegistered: false,
  };

  /**
   * Create a new TotalReclaw client
   *
   * @param config - Client configuration
   */
  constructor(config: TotalReclawConfig) {
    this.config = {
      ...config,
      lshConfig: mergeLSHConfig(config.lshConfig) as Required<LSHConfig>,
    };

    this.apiClient = new TotalReclawClient(this.config);
    // LSHHasher is created lazily after mnemonic-based seed derivation
    this.embeddingModel = new EmbeddingModel();
    this.bm25Scorer = new BM25Scorer();
  }

  /**
   * Initialize the client
   *
   * Must be called before any other operations.
   */
  async init(): Promise<void> {
    await this.apiClient.init();

    // Try to load embedding model (optional)
    try {
      if (this.config.modelPath) {
        await this.embeddingModel.load(this.config.modelPath);
      } else {
        // Try default path
        await this.embeddingModel.load();
      }
    } catch {
      // Model not available - will use hash-based embeddings
      console.warn(
        'ONNX model not available. Using hash-based embeddings for testing.'
      );
    }
  }

  /**
   * Initialize LSH hasher from a mnemonic.
   * Must be called before remember() or recall() when using the mnemonic path.
   */
  initLshFromMnemonic(mnemonic: string): void {
    const lshSeed = deriveLshSeed(mnemonic);
    this.lshHasher = new LSHHasher(
      lshSeed,
      640, // harrier-oss-v1-270m dimension
      this.config.lshConfig.n_tables,
      this.config.lshConfig.n_bits_per_table,
    );
  }

  /**
   * Initialize LSH hasher from a raw seed (Uint8Array).
   */
  initLshFromSeed(seed: Uint8Array): void {
    this.lshHasher = new LSHHasher(
      seed,
      640, // harrier-oss-v1-270m dimension
      this.config.lshConfig.n_tables,
      this.config.lshConfig.n_bits_per_table,
    );
  }

  /**
   * Register a new user with the server
   *
   * @param masterPassword - User's recovery phrase
   * @returns User ID
   */
  async register(masterPassword: string): Promise<string> {
    if (this.state.isRegistered) {
      throw new TotalReclawError(
        TotalReclawErrorCode.ALREADY_REGISTERED,
        'Client is already registered'
      );
    }

    // Generate salt
    const salt = generateSalt(32);

    // Derive keys
    const { authKey, encryptionKey } = await deriveKeys(masterPassword, salt);

    // Create auth key hash for server (double-hash for extra security)
    const authKeyHash = crypto
      .createHash('sha256')
      .update(authKey)
      .digest();

    // Register with server
    const userId = await this.apiClient.register(authKeyHash, salt);

    // Store state
    this.state = {
      userId,
      authKey,
      encryptionKey,
      salt,
      isRegistered: true,
    };

    return userId;
  }

  /**
   * Login with existing credentials
   *
   * @param userId - User ID from previous registration
   * @param masterPassword - User's recovery phrase
   * @param salt - Salt from previous registration
   */
  async login(userId: string, masterPassword: string, salt: Buffer): Promise<void> {
    // Derive keys
    const { authKey, encryptionKey } = await deriveKeys(masterPassword, salt);

    // Verify auth with server (health check)
    await this.apiClient.healthCheck();

    // Store state
    this.state = {
      userId,
      authKey,
      encryptionKey,
      salt,
      isRegistered: true,
    };
  }

  /**
   * Store a new memory
   *
   * @param text - Memory text to store
   * @param metadata - Optional metadata
   * @returns Fact ID
   */
  async remember(text: string, metadata?: FactMetadata): Promise<string> {
    this.ensureReady();

    if (!this.lshHasher) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        'LSH hasher not initialized. Call initLshFromMnemonic() or initLshFromSeed() first.',
      );
    }

    // Generate embedding
    const embedding = await this.getEmbedding(text);

    // Generate LSH buckets (already blind-hashed by LSHHasher)
    const lshBuckets = this.lshHasher.hash(embedding);

    // Generate blind indices (word + stem hashes)
    const wordIndices = generateBlindIndices(text);

    // Merge word indices and LSH bucket hashes
    const blindIndices = [...wordIndices, ...lshBuckets];

    // Encrypt document and embedding
    const encryptedDoc = encrypt(text, this.state.encryptionKey!);
    const encryptedEmbedding = encrypt(
      JSON.stringify(embedding),
      this.state.encryptionKey!,
    );

    // Calculate initial decay score
    const importance = metadata?.importance ?? 0.5;
    const decayScore = calculateDecayScore(importance, 0, 0);

    // Create encrypted fact
    const fact: EncryptedFact = {
      id: this.apiClient.generateUUIDv7(),
      encryptedDoc: encryptedDoc,
      encryptedEmbedding: encryptedEmbedding,
      blindIndices,
      decayScore,
      timestamp: Date.now(),
    };

    // Store on server
    await this.apiClient.store(this.state.userId!, this.state.authKey!, fact);

    return fact.id;
  }

  /**
   * Search for memories
   *
   * @param query - Search query
   * @param k - Number of results to return (default: 8)
   * @returns Reranked search results
   */
  async recall(query: string, k: number = 8): Promise<RerankedResult[]> {
    this.ensureReady();

    if (!this.lshHasher) {
      throw new TotalReclawError(
        TotalReclawErrorCode.LSH_HASH_FAILED,
        'LSH hasher not initialized. Call initLshFromMnemonic() or initLshFromSeed() first.',
      );
    }

    // Generate query embedding
    const queryEmbedding = await this.getEmbedding(query);

    // Generate LSH buckets for query (already blind-hashed)
    const queryBuckets = this.lshHasher.hash(queryEmbedding);

    // Generate trapdoors (word + stem hashes merged with LSH bucket hashes)
    const trapdoors = generateTrapdoors(query, queryBuckets);

    // Search server
    const encryptedResults = await this.apiClient.search(
      this.state.userId!,
      this.state.authKey!,
      trapdoors,
      this.config.lshConfig.candidate_pool
    );

    if (encryptedResults.length === 0) {
      return [];
    }

    // Decrypt results
    const decryptedFacts = await this.decryptResults(encryptedResults);

    // Rerank results
    const reranked = this.rerankResults(query, queryEmbedding, decryptedFacts);

    // Return top k
    return reranked.slice(0, k);
  }

  /**
   * Delete a memory
   *
   * @param factId - Fact ID to delete
   */
  async forget(factId: string): Promise<void> {
    this.ensureReady();
    await this.apiClient.delete(this.state.userId!, this.state.authKey!, factId);
  }

  /**
   * Export all data for portability
   *
   * Note: This requires fetching all facts from the server,
   * which may not be implemented in the PoC.
   *
   * @returns Exported data
   */
  async export(): Promise<ExportedData> {
    this.ensureReady();

    // In the PoC, we return just the keys and config
    // Full implementation would fetch all encrypted facts
    return {
      version: '0.3.0',
      exportedAt: new Date(),
      facts: [], // Would need server endpoint to fetch all
      keyParams: {
        salt: this.state.salt!,
        memoryCost: 65536,
        timeCost: 3,
        parallelism: 4,
      },
      lshConfig: this.config.lshConfig,
    };
  }

  /**
   * Get embedding for text
   */
  private async getEmbedding(text: string): Promise<number[]> {
    if (this.embeddingModel.isReady()) {
      return this.embeddingModel.embed(text);
    } else {
      // Fallback to hash-based embedding
      return createHashBasedEmbedding(text);
    }
  }

  /**
   * Decrypt search results
   */
  private async decryptResults(
    results: EncryptedSearchResult[]
  ): Promise<Fact[]> {
    const facts: Fact[] = [];

    for (const result of results) {
      try {
        // Decrypt document (base64 wire format)
        const text = decrypt(result.encryptedDoc, this.state.encryptionKey!);

        // Decrypt embedding (base64 wire format, JSON-encoded array)
        const embeddingJson = decrypt(result.encryptedEmbedding, this.state.encryptionKey!);
        const embedding: number[] = JSON.parse(embeddingJson);

        const fact: Fact = {
          id: result.factId,
          text,
          embedding,
          metadata: {
            importance: result.decayScore,
          },
          decayScore: result.decayScore,
          createdAt: new Date(result.timestamp),
        };

        facts.push(fact);
      } catch (error) {
        // Skip malformed results
        console.warn(`Failed to decrypt result ${result.factId}:`, error);
      }
    }

    return facts;
  }

  /**
   * Rerank search results using BM25 + cosine similarity + RRF
   */
  private rerankResults(
    query: string,
    queryEmbedding: number[],
    facts: Fact[]
  ): RerankedResult[] {
    // Index facts for BM25
    this.bm25Scorer.indexDocuments(
      facts.map((f) => ({ id: f.id, text: f.text }))
    );

    // Calculate individual scores
    const vectorScores = new Map<string, number>();
    const textScores = new Map<string, number>();
    const decayScores = new Map<string, number>();

    for (const fact of facts) {
      // Cosine similarity
      const vecScore = cosineSimilarity(queryEmbedding, fact.embedding);
      vectorScores.set(fact.id, vecScore);

      // BM25 score (normalized)
      const bm25Raw = this.bm25Scorer.score(query, fact.id, fact.text);
      textScores.set(fact.id, bm25Raw);

      // Decay score
      decayScores.set(fact.id, fact.decayScore);
    }

    // Normalize BM25 scores
    const bm25Values = Array.from(textScores.values());
    const bm25Norm = normalizeScores(bm25Values);
    let i = 0;
    for (const id of textScores.keys()) {
      textScores.set(id, bm25Norm[i++]);
    }

    // Combine scores
    const results: RerankedResult[] = facts.map((fact) => {
      const vecScore = vectorScores.get(fact.id) || 0;
      const txtScore = textScores.get(fact.id) || 0;
      const decScore = decayScores.get(fact.id) || 0.5;

      // Weighted combination: 40% vector, 40% text, 20% decay
      const combinedScore = vecScore * 0.4 + txtScore * 0.4 + decScore * 0.2;

      return {
        fact,
        score: combinedScore,
        vectorScore: vecScore,
        textScore: txtScore,
        decayAdjustedScore: decScore,
      };
    });

    // Sort by combined score
    results.sort((a, b) => b.score - a.score);

    return results;
  }

  /**
   * Ensure client is ready for operations
   */
  private ensureReady(): void {
    if (!this.state.isRegistered) {
      throw new TotalReclawError(
        TotalReclawErrorCode.NOT_REGISTERED,
        'Client not registered. Call register() or login() first.'
      );
    }
  }

  /**
   * Get the current user ID
   */
  getUserId(): string | null {
    return this.state.userId;
  }

  /**
   * Get the salt (for export/backup)
   */
  getSalt(): Buffer | null {
    return this.state.salt;
  }

  /**
   * Check if the client is ready
   */
  isReady(): boolean {
    return this.state.isRegistered;
  }
}

// Re-export types and utilities
export {
  // Types
  LSHConfig,
  DEFAULT_LSH_CONFIG,
  TotalReclawConfig,
  Fact,
  FactMetadata,
  EncryptedFact,
  EncryptedSearchResult,
  RerankedResult,
  ExportedData,
  RegisterRequest,
  RegisterResponse,
  StoreRequest,
  StoreResponse,
  SearchRequest,
  SearchResponse,
  TotalReclawError,
  TotalReclawErrorCode,
} from './types';

export {
  // Crypto
  deriveKeys,
  deriveAuthKey,
  deriveEncryptionKey,
  generateSalt,
  createAuthProof,
  verifyAuthProof,
  encrypt,
  decrypt,
  generateBlindIndices,
  generateTrapdoors,
  tokenize,
  sha256Hash,
} from './crypto';

export type { KeyDerivationParams } from './crypto';

export * from './lsh';
export * from './embedding';
export * from './search';
export * from './api';

// Default export
export default TotalReclaw;
