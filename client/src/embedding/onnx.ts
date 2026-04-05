/**
 * ONNX Embedding Model
 *
 * Uses @huggingface/transformers to run multilingual-e5-small for
 * text embeddings. Produces 384-dimensional normalized vectors suitable
 * for semantic search.
 *
 * Model details:
 *   - Xenova/multilingual-e5-small (ONNX-optimized, int8 quantized)
 *   - ~34MB download, cached in ~/.cache/huggingface/
 *   - 384-dimensional output vectors
 *   - 100+ languages (multilingual)
 *   - mean pooling
 *   - Lazy initialization: first call ~2-3s, subsequent ~50ms
 */

import { pipeline, type FeatureExtractionPipeline } from '@huggingface/transformers';
import { TotalReclawError, TotalReclawErrorCode } from '../types';

/** Expected embedding dimension */
const EMBEDDING_DIM = 384;

/** Model ID on HuggingFace Hub */
const MODEL_ID = 'Xenova/multilingual-e5-small';

/**
 * Embedding model using @huggingface/transformers + ONNX Runtime
 */
export class EmbeddingModel {
  private extractor: FeatureExtractionPipeline | null = null;
  private isLoaded: boolean = false;

  /**
   * Load the ONNX model.
   *
   * Downloads the model on first use (~553MB, fp16). Subsequent calls
   * use the cached model from ~/.cache/huggingface/.
   *
   * @param _modelPath - Ignored (kept for backward compatibility). The model
   *   is always downloaded from HuggingFace Hub.
   */
  async load(_modelPath?: string): Promise<void> {
    try {
      console.error('[TotalReclaw] Downloading embedding model (~34MB, first run only)...');
      this.extractor = await pipeline('feature-extraction', MODEL_ID, {
        dtype: 'q8',
      });
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
   *                  does not change behavior (Harrier needs no instruction prefix).
   * @returns 640-dimensional normalized embedding vector
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
      const output = await this.extractor!(input, { pooling: 'mean', normalize: true });
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
   * @returns Array of 640-dimensional embedding vectors
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
 * @returns Random embedding with current model dimensions
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
 * @returns Deterministic embedding with current model dimensions
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
