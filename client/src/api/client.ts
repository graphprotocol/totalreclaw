/**
 * TotalReclaw HTTP Client
 *
 * Handles communication with the TotalReclaw server over HTTP using Protobuf.
 */

import * as http from 'http';
import * as https from 'https';
import * as crypto from 'crypto';
import {
  TotalReclawConfig,
  RegisterRequest,
  RegisterResponse,
  StoreRequest,
  StoreResponse,
  SearchRequest,
  SearchResponse,
  EncryptedFact,
  EncryptedSearchResult,
  TotalReclawError,
  TotalReclawErrorCode,
} from '../types';
import { ProtobufSerializer, protobufSerializer } from './protobuf';
import { createAuthProof } from '../crypto/kdf';

/**
 * Default configuration values
 */
const DEFAULT_CONFIG: Partial<TotalReclawConfig> = {
  timeout: 30000,
};

/**
 * HTTP Client for TotalReclaw Server
 */
export class TotalReclawClient {
  private serverUrl: string;
  private timeout: number;
  private serializer: ProtobufSerializer;
  private initialized: boolean = false;

  /**
   * Create a new TotalReclaw client
   *
   * @param config - Client configuration
   */
  constructor(config: TotalReclawConfig) {
    const fullConfig = { ...DEFAULT_CONFIG, ...config };
    this.serverUrl = fullConfig.serverUrl.replace(/\/$/, ''); // Remove trailing slash
    this.timeout = fullConfig.timeout || 30000;
    this.serializer = protobufSerializer;
  }

  /**
   * Initialize the client (must be called before making requests)
   */
  async init(): Promise<void> {
    if (this.initialized) return;
    await this.serializer.init();
    this.initialized = true;
  }

  /**
   * Ensure client is initialized
   */
  private ensureInitialized(): void {
    if (!this.initialized) {
      throw new TotalReclawError(
        TotalReclawErrorCode.NETWORK_ERROR,
        'Client not initialized. Call init() first.'
      );
    }
  }

  /**
   * Make an HTTP request
   */
  private async request(
    method: string,
    path: string,
    body?: Buffer
  ): Promise<Buffer> {
    this.ensureInitialized();

    const url = new URL(`${this.serverUrl}${path}`);
    const isHttps = url.protocol === 'https:';
    const httpModule = isHttps ? https : http;

    return new Promise((resolve, reject) => {
      const requestOptions: http.RequestOptions = {
        hostname: url.hostname,
        port: url.port || (isHttps ? 443 : 80),
        path: url.pathname + url.search,
        method: method,
        headers: {
          'Content-Type': 'application/x-protobuf',
          'Accept': 'application/x-protobuf',
        },
        timeout: this.timeout,
      };

      if (body) {
        (requestOptions.headers as Record<string, string | number>)['Content-Length'] = Buffer.byteLength(body);
      }

      const req = httpModule.request(requestOptions, (res) => {
        const chunks: Buffer[] = [];

        res.on('data', (chunk) => {
          chunks.push(chunk);
        });

        res.on('end', () => {
          const responseBuffer = Buffer.concat(chunks);

          if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
            resolve(responseBuffer);
          } else {
            reject(
              new TotalReclawError(
                TotalReclawErrorCode.NETWORK_ERROR,
                `HTTP ${res.statusCode}: ${responseBuffer.toString('utf-8')}`
              )
            );
          }
        });
      });

      req.on('error', (error) => {
        reject(
          new TotalReclawError(
            TotalReclawErrorCode.NETWORK_ERROR,
            `Network error: ${error.message}`
          )
        );
      });

      req.on('timeout', () => {
        req.destroy();
        reject(
          new TotalReclawError(
            TotalReclawErrorCode.NETWORK_ERROR,
            `Request timeout after ${this.timeout}ms`
          )
        );
      });

      if (body) {
        req.write(body);
      }
      req.end();
    });
  }

  /**
   * Generate a UUID v7 (time-sorted UUID)
   *
   * @returns UUID v7 string
   */
  generateUUIDv7(): string {
    const timestamp = Date.now();
    const timestampBytes = Buffer.alloc(8);
    timestampBytes.writeBigInt64BE(BigInt(timestamp), 0);

    // Use only first 6 bytes for timestamp (48 bits)
    const randomBytes = crypto.randomBytes(10);

    // Build UUID v7 format
    const uuid = Buffer.alloc(16);

    // timestamp (48 bits)
    timestampBytes.copy(uuid, 0, 2, 8);

    // version (4 bits) + random (12 bits)
    uuid[6] = (0x7 << 4) | (randomBytes[0] >> 4);

    // variant (2 bits) + random (6 bits)
    uuid[8] = (0x2 << 6) | (randomBytes[1] >> 2);

    // remaining random bytes
    randomBytes.copy(uuid, 9, 2, 10);

    // Format as UUID string
    const hex = uuid.toString('hex');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  /**
   * Check server health
   */
  async healthCheck(): Promise<{ status: string; version: string; database: string }> {
    const response = await this.request('GET', '/health');
    return this.serializer.deserializeHealthResponse(response);
  }

  /**
   * Register a new user
   *
   * @param authKeyHash - Hashed auth key (from HKDF)
   * @param salt - Salt used for key derivation
   * @returns User ID (UUID v7)
   */
  async register(authKeyHash: Buffer, salt: Buffer): Promise<string> {
    const userId = this.generateUUIDv7();

    const request: RegisterRequest = {
      userId,
      authKeyHash,
      salt,
    };

    const body = this.serializer.serializeRegisterRequest(request);
    const response = await this.request('POST', '/v1/register', body);
    const registerResult = this.serializer.deserializeRegisterResponse(response);

    if (!registerResult.success) {
      throw new TotalReclawError(
        TotalReclawErrorCode.AUTH_FAILED,
        `Registration failed: ${registerResult.errorCode} - ${registerResult.errorMessage}`
      );
    }

    return userId;
  }

  /**
   * Store an encrypted fact
   *
   * @param userId - User ID
   * @param authKey - Auth key for HMAC
   * @param fact - Encrypted fact to store
   */
  async store(userId: string, authKey: Buffer, fact: EncryptedFact): Promise<number> {
    // Create auth proof (HMAC of request body without auth_proof field)
    const dataToSign = Buffer.concat([
      Buffer.from(userId, 'utf-8'),
      Buffer.from(fact.id, 'utf-8'),
      fact.encryptedDoc,
      fact.encryptedEmbedding,
      Buffer.from(fact.blindIndices.join(''), 'utf-8'),
    ]);
    const authProof = createAuthProof(authKey, dataToSign);

    const request: StoreRequest = {
      userId,
      authProof,
      fact,
    };

    const body = this.serializer.serializeStoreRequest(request);
    const response = await this.request('POST', '/v1/store', body);
    const storeResult = this.serializer.deserializeStoreResponse(response);

    if (!storeResult.success) {
      throw new TotalReclawError(
        TotalReclawErrorCode.NETWORK_ERROR,
        `Store failed: ${storeResult.errorCode}`
      );
    }

    return storeResult.version || 0;
  }

  /**
   * Search for memories using trapdoors
   *
   * @param userId - User ID
   * @param authKey - Auth key for HMAC
   * @param trapdoors - Blind index trapdoors from query
   * @param maxCandidates - Maximum candidates to retrieve
   * @returns Encrypted search results
   */
  async search(
    userId: string,
    authKey: Buffer,
    trapdoors: string[],
    maxCandidates: number = 3000
  ): Promise<EncryptedSearchResult[]> {
    // Create auth proof
    const dataToSign = Buffer.concat([
      Buffer.from(userId, 'utf-8'),
      Buffer.from(trapdoors.join(''), 'utf-8'),
      Buffer.from([maxCandidates >> 24, maxCandidates >> 16, maxCandidates >> 8, maxCandidates]),
    ]);
    const authProof = createAuthProof(authKey, dataToSign);

    const request: SearchRequest = {
      userId,
      authProof,
      trapdoors,
      maxCandidates,
    };

    const body = this.serializer.serializeSearchRequest(request);
    const response = await this.request('POST', '/v1/search', body);
    const searchResult = this.serializer.deserializeSearchResponse(response);

    if (!searchResult.success) {
      throw new TotalReclawError(
        TotalReclawErrorCode.NETWORK_ERROR,
        `Search failed: ${searchResult.errorCode}`
      );
    }

    return searchResult.results;
  }

  /**
   * Delete a fact (if supported by server)
   *
   * @param userId - User ID
   * @param authKey - Auth key for HMAC
   * @param factId - Fact ID to delete
   */
  async delete(userId: string, authKey: Buffer, factId: string): Promise<void> {
    // Create auth proof
    const dataToSign = Buffer.concat([
      Buffer.from(userId, 'utf-8'),
      Buffer.from(factId, 'utf-8'),
    ]);
    const authProof = createAuthProof(authKey, dataToSign);

    // Note: Delete endpoint may not be implemented in PoC
    // This is a placeholder for future implementation
    await this.request('DELETE', `/v1/facts/${factId}`, authProof);
  }

  /**
   * Get the server URL
   */
  getServerUrl(): string {
    return this.serverUrl;
  }
}
