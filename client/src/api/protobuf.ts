/**
 * Protobuf Serialization
 *
 * Defines and handles Protocol Buffer serialization for API communication.
 */

import * as protobuf from 'protobufjs';
import {
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

/**
 * Protobuf schema definitions for TotalReclaw API
 */
const PROTO_SCHEMA = `
syntax = "proto3";

package totalreclaw;

// Registration
message RegisterRequest {
  string user_id = 1;
  bytes auth_key_hash = 2;
  bytes salt = 3;
}

message RegisterResponse {
  bool success = 1;
  string error_code = 2;
  string error_message = 3;
}

// Storage
message StoreRequest {
  string user_id = 1;
  bytes auth_proof = 2;
  string fact_id = 3;
  bytes encrypted_doc = 4;
  bytes encrypted_embedding = 5;
  repeated string blind_indices = 6;
  double decay_score = 7;
  int64 timestamp = 8;
  bytes doc_iv = 9;
  bytes doc_tag = 10;
  bytes emb_iv = 11;
  bytes emb_tag = 12;
}

message StoreResponse {
  bool success = 1;
  string error_code = 2;
  int32 version = 3;
}

// Search
message SearchRequest {
  string user_id = 1;
  bytes auth_proof = 2;
  repeated string trapdoors = 3;
  int32 max_candidates = 4;
}

message SearchResult {
  string fact_id = 1;
  bytes encrypted_doc = 2;
  bytes encrypted_embedding = 3;
  double decay_score = 4;
  int64 timestamp = 5;
  bytes doc_iv = 6;
  bytes doc_tag = 7;
  bytes emb_iv = 8;
  bytes emb_tag = 9;
}

message SearchResponse {
  bool success = 1;
  string error_code = 2;
  repeated SearchResult results = 3;
  int32 total_candidates = 4;
}

// Health
message HealthResponse {
  string status = 1;
  string version = 2;
  string database = 3;
}
`;

/**
 * Protobuf serializer/deserializer for TotalReclaw messages
 */
export class ProtobufSerializer {
  private root: protobuf.Root | null = null;
  private initialized: boolean = false;

  /**
   * Initialize the protobuf schema
   */
  async init(): Promise<void> {
    if (this.initialized) return;

    try {
      this.root = protobuf.parse(PROTO_SCHEMA).root;
      this.initialized = true;
    } catch (error) {
      throw new TotalReclawError(
        TotalReclawErrorCode.INVALID_INPUT,
        `Failed to parse protobuf schema: ${error instanceof Error ? error.message : 'Unknown error'}`,
        error instanceof Error ? error : undefined
      );
    }
  }

  /**
   * Ensure the serializer is initialized
   */
  private ensureInitialized(): void {
    if (!this.initialized || !this.root) {
      throw new TotalReclawError(
        TotalReclawErrorCode.INVALID_INPUT,
        'ProtobufSerializer not initialized. Call init() first.'
      );
    }
  }

  /**
   * Serialize a RegisterRequest
   */
  serializeRegisterRequest(req: RegisterRequest): Buffer {
    this.ensureInitialized();
    const MessageType = this.root!.lookupType('totalreclaw.RegisterRequest');

    const message = MessageType.create({
      user_id: req.userId,
      auth_key_hash: req.authKeyHash,
      salt: req.salt,
    });

    const buffer = MessageType.encode(message).finish();
    return Buffer.from(buffer);
  }

  /**
   * Deserialize a RegisterResponse
   */
  deserializeRegisterResponse(data: Buffer): RegisterResponse {
    this.ensureInitialized();
    const MessageType = this.root!.lookupType('totalreclaw.RegisterResponse');

    const message = MessageType.decode(data);
    const decoded = MessageType.toObject(message, { bytes: Buffer });

    return {
      success: decoded.success,
      errorCode: decoded.error_code || undefined,
      errorMessage: decoded.error_message || undefined,
    };
  }

  /**
   * Serialize a StoreRequest
   */
  serializeStoreRequest(req: StoreRequest): Buffer {
    this.ensureInitialized();
    const MessageType = this.root!.lookupType('totalreclaw.StoreRequest');

    const message = MessageType.create({
      user_id: req.userId,
      auth_proof: req.authProof,
      fact_id: req.fact.id,
      encrypted_doc: req.fact.encryptedDoc,
      encrypted_embedding: req.fact.encryptedEmbedding,
      blind_indices: req.fact.blindIndices,
      decay_score: req.fact.decayScore,
      timestamp: req.fact.timestamp,
      doc_iv: req.fact.docIv,
      doc_tag: req.fact.docTag,
      emb_iv: req.fact.embIv,
      emb_tag: req.fact.embTag,
    });

    const buffer = MessageType.encode(message).finish();
    return Buffer.from(buffer);
  }

  /**
   * Deserialize a StoreResponse
   */
  deserializeStoreResponse(data: Buffer): StoreResponse {
    this.ensureInitialized();
    const MessageType = this.root!.lookupType('totalreclaw.StoreResponse');

    const message = MessageType.decode(data);
    const decoded = MessageType.toObject(message, { bytes: Buffer });

    return {
      success: decoded.success,
      errorCode: decoded.error_code || undefined,
      version: decoded.version || undefined,
    };
  }

  /**
   * Serialize a SearchRequest
   */
  serializeSearchRequest(req: SearchRequest): Buffer {
    this.ensureInitialized();
    const MessageType = this.root!.lookupType('totalreclaw.SearchRequest');

    const message = MessageType.create({
      user_id: req.userId,
      auth_proof: req.authProof,
      trapdoors: req.trapdoors,
      max_candidates: req.maxCandidates,
    });

    const buffer = MessageType.encode(message).finish();
    return Buffer.from(buffer);
  }

  /**
   * Deserialize a SearchResponse
   */
  deserializeSearchResponse(data: Buffer): SearchResponse {
    this.ensureInitialized();
    const MessageType = this.root!.lookupType('totalreclaw.SearchResponse');

    const message = MessageType.decode(data);
    const decoded = MessageType.toObject(message, { bytes: Buffer });

    return {
      success: decoded.success,
      errorCode: decoded.error_code || undefined,
      results: (decoded.results || []).map((r: any) => ({
        factId: r.fact_id,
        encryptedDoc: Buffer.from(r.encrypted_doc),
        encryptedEmbedding: Buffer.from(r.encrypted_embedding),
        decayScore: r.decay_score,
        timestamp: r.timestamp,
        docIv: Buffer.from(r.doc_iv),
        docTag: Buffer.from(r.doc_tag),
        embIv: Buffer.from(r.emb_iv),
        embTag: Buffer.from(r.emb_tag),
      })),
      totalCandidates: decoded.total_candidates,
    };
  }

  /**
   * Deserialize a HealthResponse
   */
  deserializeHealthResponse(data: Buffer): { status: string; version: string; database: string } {
    this.ensureInitialized();
    const MessageType = this.root!.lookupType('totalreclaw.HealthResponse');

    const message = MessageType.decode(data);
    const decoded = MessageType.toObject(message);

    return {
      status: decoded.status,
      version: decoded.version,
      database: decoded.database,
    };
  }
}

/**
 * Default serializer instance
 */
export const protobufSerializer = new ProtobufSerializer();
