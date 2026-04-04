/**
 * ONNX Embedding Model
 *
 * Uses @huggingface/transformers to run Qwen3-Embedding-0.6B model for
 * text embeddings. Produces 1024-dimensional normalized vectors suitable
 * for semantic search with 100+ language support.
 *
 * The @huggingface/transformers library handles:
 *   - Model download (auto-downloads ONNX model from HuggingFace Hub)
 *   - Tokenization (uses the model's actual tokenizer.json)
 *   - ONNX inference
 *   - Last-token pooling + normalization
 *
 * Model details:
 *   - onnx-community/Qwen3-Embedding-0.6B-ONNX (ONNX-optimized)
 *   - Quantized (int8): ~600MB download, cached in ~/.cache/huggingface/
 *   - 1024-dimensional output vectors
 *   - 100+ multilingual support (EN, PT, ES, ZH, etc.)
 *   - Lazy initialization: first call ~3-5s, subsequent ~100ms
 */

import { pipeline, type FeatureExtractionPipeline } from '@huggingface/transformers';
import { TotalReclawError, TotalReclawErrorCode } from '../types';

/** Expected embedding dimension for Qwen3-Embedding-0.6B */
const EMBEDDING_DIM = 1024;

/** Model ID on HuggingFace Hub (ONNX-optimized version) */
const MODEL_ID = 'onnx-community/Qwen3-Embedding-0.6B-ONNX';

/**
 * Embedding model using @huggingface/transformers + ONNX Runtime
 */
export class EmbeddingModel {
  private extractor: FeatureExtractionPipeline | null = null;
  private isLoaded: boolean = false;

  /**
   * Load the ONNX model.
   *
   * Downloads the model on first use (~600MB, quantized). Subsequent calls
   * use the cached model from ~/.cache/huggingface/.
   *
   * @param _modelPath - Ignored (kept for backward compatibility). The model
   *   is always downloaded from HuggingFace Hub.
   */
  async load(_modelPath?: string): Promise<void> {
    try {
      console.error('[TotalReclaw] Downloading embedding model (~600MB, first run only)...');
      this.extractor = await pipeline('feature-extraction', MODEL_ID, {
        quantized: true,
      } as Record<string, unknown>);
      console.error('[TotalReclaw] Embedding model ready.');
      this.isLoaded = true;
    } catch (error) {
      throw new TotalReclawError(
        TotalReclawErrorCode.MODEL_LOAD_FAILED,
        `Failed to load ONNX model: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  /**
   * Check if the model is loaded
   */
  isReady(): boolean {
    return this.isLoaded && this.extractor !== null;
  }

  /**
   * Get the embedding dimension
   */
  getDimension(): number {
    return EMBEDDING_DIM;
  }

  /**
   * Embed a single text
   *
   * @param text - Text to embed
   * @param options - Options. isQuery is accepted for forward compatibility but
   *                  does not change behavior (Qwen3 needs no instruction prefix).
   * @returns 1024-dimensional normalized embedding vector
   */
  async embed(text: string, options?: { isQuery?: boolean }): Promise<number[]> {
    if (!this.isReady()) {
      throw new TotalReclawError(
        TotalReclawErrorCode.EMBEDDING_FAILED,
        'Model not loaded. Call load() first.'
      );
    }

    try {
      const input = text;
      const output = await this.extractor!(input, { pooling: 'last_token', normalize: true });
      return Array.from(output.data as Float32Array);
    } catch (error) {
      throw new TotalReclawError(
        TotalReclawErrorCode.EMBEDDING_FAILED,
        `Embedding failed: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  /**
   * Embed multiple texts in batch
   *
   * @param texts - Texts to embed
   * @returns Array of 1024-dimensional embedding vectors
   */
  async embedBatch(texts: string[]): Promise<number[][]> {
    const embeddings: number[][] = [];
    for (const text of texts) {
      const embedding = await this.embed(text);
      embeddings.push(embedding);
    }
    return embeddings;
  }

  /**
   * Dispose of the model resources
   */
  async dispose(): Promise<void> {
    // @huggingface/transformers handles cleanup internally
    this.extractor = null;
    this.isLoaded = false;
  }
}

/**
 * Create a dummy embedding for testing
 *
 * Useful when ONNX model is not available.
 *
 * @param seed - Seed for reproducible random embeddings
 * @returns 1024-dimensional random embedding
 */
export function createDummyEmbedding(seed?: number): number[] {
  const dim = EMBEDDING_DIM;
  const embedding: number[] = [];
  let s = seed ?? Date.now();

  // Simple seeded random
  const random = () => {
    s = (s * 1103515245 + 12345) & 0x7fffffff;
    return s / 0x7fffffff;
  };

  // Generate random values
  for (let i = 0; i < dim; i++) {
    // Gaussian distribution via Box-Muller
    const u1 = random();
    const u2 = random();
    const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
    embedding.push(z);
  }

  // Normalize
  let norm = 0;
  for (const val of embedding) {
    norm += val * val;
  }
  norm = Math.sqrt(norm);

  return embedding.map((val) => val / norm);
}

/**
 * Create deterministic embedding from text hash
 *
 * This creates a consistent (but not semantically meaningful) embedding
 * based on the text content. Useful for testing without loading the model.
 *
 * @param text - Text to create embedding for
 * @returns 1024-dimensional deterministic embedding
 */
export function createHashBasedEmbedding(text: string): number[] {
  const crypto = require('crypto');
  const embedding: number[] = [];

  // Generate embedding dimensions from text hash
  for (let i = 0; i < EMBEDDING_DIM; i++) {
    const hash = crypto
      .createHash('sha256')
      .update(`${text}:${i}`)
      .digest();
    // Convert first 4 bytes to float in range [-1, 1]
    const value = hash.readInt32LE(0) / 2147483648.0;
    embedding.push(value);
  }

  // Normalize
  let norm = 0;
  for (const val of embedding) {
    norm += val * val;
  }
  norm = Math.sqrt(norm);

  return embedding.map((val) => val / norm);
}
