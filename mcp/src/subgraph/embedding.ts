/**
 * TotalReclaw MCP - Local Embedding via @huggingface/transformers
 *
 * Uses the Harrier-OSS-v1-270M ONNX model to generate 640-dimensional
 * text embeddings locally. No API key needed, no data leaves the machine.
 *
 * This preserves the E2EE guarantee: embeddings are generated
 * CLIENT-SIDE before encryption, so no plaintext ever reaches an external API.
 *
 * Model details:
 *   - Quantized ONNX model: ~164MB download on first use
 *   - Cached in ~/.cache/huggingface/ after first download
 *   - Lazy initialization: first call ~3-5s (model load), subsequent ~100ms
 *   - Output: 640-dimensional normalized embedding vector
 *   - No instruction prefix needed
 *
 * Dependencies: @huggingface/transformers (handles model download,
 * tokenization, ONNX inference, last-token pooling, and normalization).
 */

// @ts-ignore - @huggingface/transformers types may not be perfect
import { pipeline, type FeatureExtractionPipeline } from '@huggingface/transformers';

/** ONNX-optimized Harrier-OSS-v1-270M from HuggingFace Hub. */
const MODEL_ID = 'onnx-community/harrier-oss-v1-270m-ONNX';

/** Fixed output dimensionality for Harrier-OSS-v1-270M. */
const EMBEDDING_DIM = 640;

/** Lazily initialized feature extraction pipeline. */
let extractor: FeatureExtractionPipeline | null = null;

/**
 * Generate a 640-dimensional embedding vector for the given text.
 *
 * On first call, downloads and loads the ONNX model (~164MB, cached).
 * Subsequent calls reuse the loaded model and run in ~100ms.
 *
 * The isQuery option is accepted for forward compatibility but does not
 * change behavior -- Harrier performs better without instruction prefixes.
 *
 * @param text - The text to embed.
 * @param options - Optional settings.
 * @param options.isQuery - Accepted for forward compatibility (no-op).
 * @returns 640-dimensional normalized embedding as a number array.
 */
export async function generateEmbedding(
  text: string,
  options?: { isQuery?: boolean },
): Promise<number[]> {
  if (!extractor) {
    console.error('[TotalReclaw] Downloading embedding model (~164MB, first run only)...');
    extractor = await pipeline('feature-extraction', MODEL_ID, {
      // @ts-ignore - quantized option exists at runtime but not in type defs
      quantized: true,
    } as any);
    console.error('[TotalReclaw] Embedding model ready.');
  }

  const input = text;
  const output = await extractor(input, { pooling: 'last_token', normalize: true });
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
