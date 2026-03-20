/**
 * TotalReclaw MCP - Local Embedding via @huggingface/transformers
 *
 * Uses the Xenova/bge-small-en-v1.5 ONNX model to generate 384-dimensional
 * text embeddings locally. No API key needed, no data leaves the machine.
 *
 * This preserves the E2EE guarantee: embeddings are generated
 * CLIENT-SIDE before encryption, so no plaintext ever reaches an external API.
 *
 * Model details:
 *   - Quantized (int8) ONNX model: ~33.8MB download on first use
 *   - Cached in ~/.cache/huggingface/ after first download
 *   - Lazy initialization: first call ~2-3s (model load), subsequent ~15ms
 *   - Output: 384-dimensional normalized embedding vector
 *   - For retrieval, queries should be prefixed with an instruction string
 *     (documents/passages should NOT be prefixed)
 *
 * Dependencies: @huggingface/transformers (handles model download, WordPiece
 * tokenization, ONNX inference, mean pooling, and normalization).
 */

// @ts-ignore - @huggingface/transformers types may not be perfect
import { pipeline, type FeatureExtractionPipeline } from '@huggingface/transformers';

/** ONNX-optimized bge-small-en-v1.5 from HuggingFace Hub. */
const MODEL_ID = 'Xenova/bge-small-en-v1.5';

/** Fixed output dimensionality for bge-small-en-v1.5. */
const EMBEDDING_DIM = 384;

/**
 * Query instruction prefix for bge-small-en-v1.5 retrieval tasks.
 *
 * Per the BAAI model card: prepend this to short queries when searching
 * for relevant passages. Do NOT prepend for documents/passages being stored.
 */
const QUERY_PREFIX = 'Represent this sentence for searching relevant passages: ';

/** Lazily initialized feature extraction pipeline. */
let extractor: FeatureExtractionPipeline | null = null;

/**
 * Generate a 384-dimensional embedding vector for the given text.
 *
 * On first call, downloads and loads the ONNX model (~33.8MB, cached).
 * Subsequent calls reuse the loaded model and run in ~15ms.
 *
 * For bge-small-en-v1.5, queries should set `isQuery: true` to prepend the
 * retrieval instruction prefix for improved retrieval accuracy (default: false).
 *
 * @param text - The text to embed.
 * @param options - Optional settings.
 * @param options.isQuery - If true, prepend the BGE query instruction prefix
 *                          for improved retrieval accuracy (default: false).
 * @returns 384-dimensional normalized embedding as a number array.
 */
export async function generateEmbedding(
  text: string,
  options?: { isQuery?: boolean },
): Promise<number[]> {
  if (!extractor) {
    extractor = await pipeline('feature-extraction', MODEL_ID, {
      // @ts-ignore - quantized option exists at runtime but not in type defs
      quantized: true,
    } as any);
  }

  const input = options?.isQuery ? QUERY_PREFIX + text : text;
  const output = await extractor(input, { pooling: 'mean', normalize: true });
  // output.data is a Float32Array; convert to plain number[]
  return Array.from(output.data as Float32Array);
}

/**
 * Get the embedding vector dimensionality.
 *
 * Always returns 384 (fixed for bge-small-en-v1.5).
 * This is needed by downstream code (e.g. LSH hasher) to know the vector
 * size without calling the embedding model.
 */
export function getEmbeddingDims(): number {
  return EMBEDDING_DIM;
}
