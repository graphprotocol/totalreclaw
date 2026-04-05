/**
 * TotalReclaw MCP - Local Embedding via @huggingface/transformers
 *
 * Uses Xenova/multilingual-e5-small to generate 384-dimensional text
 * embeddings locally. No API key needed, no data leaves the machine.
 *
 * Model details:
 *   - Quantized (int8) ONNX model: ~34MB download on first use
 *   - Cached in ~/.cache/huggingface/ after first download
 *   - Lazy initialization: first call ~2-3s (model load), subsequent ~50ms
 *   - Output: 384-dimensional normalized embedding vector
 *   - 100+ languages (multilingual)
 *   - mean pooling
 *
 * Dependencies: @huggingface/transformers
 */

// @ts-ignore - @huggingface/transformers types may not be perfect
import { pipeline, type FeatureExtractionPipeline } from '@huggingface/transformers';

/** Multilingual E5-small ONNX model. */
const MODEL_ID = 'Xenova/multilingual-e5-small';

/** Fixed output dimensionality. */
const EMBEDDING_DIM = 384;

/** Lazily initialized feature extraction pipeline. */
let extractor: FeatureExtractionPipeline | null = null;

/**
 * Generate a 384-dimensional embedding vector for the given text.
 *
 * On first call, downloads and loads the ONNX model (~34MB, cached).
 * Subsequent calls reuse the loaded model and run in ~50ms.
 *
 * @param text - The text to embed.
 * @param options - Optional settings.
 * @param options.isQuery - Prepends "query: " prefix for e5-small.
 * @returns 384-dimensional normalized embedding as a number array.
 */
export async function generateEmbedding(
  text: string,
  options?: { isQuery?: boolean },
): Promise<number[]> {
  if (!extractor) {
    console.error('[TotalReclaw] Downloading embedding model (~34MB, first run only)...');
    extractor = await pipeline('feature-extraction', MODEL_ID, {
      dtype: 'q8',
    });
    console.error('[TotalReclaw] Embedding model ready.');
  }

  const input = options?.isQuery ? `query: ${text}` : text;
  const output = await extractor(input, { pooling: 'mean', normalize: true });
  // output.data is a Float32Array; convert to plain number[]
  return Array.from(output.data as Float32Array);
}

/**
 * Get the embedding vector dimensionality.
 *
 * Always returns 640 (fixed for Harrier-OSS-v1-270M).
 * This is needed by downstream code (e.g. LSH hasher) to know the vector
 * size without calling the embedding model.
 */
export function getEmbeddingDims(): number {
  return EMBEDDING_DIM;
}
