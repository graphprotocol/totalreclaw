/**
 * TotalReclaw MCP - Local Embedding via @huggingface/transformers
 *
 * Uses onnx-community/harrier-oss-v1-270m-ONNX to generate 640-dimensional
 * text embeddings locally. No API key needed, no data leaves the machine.
 *
 * Model details:
 *   - Quantized (q4) ONNX model: ~344MB download on first use
 *   - Cached in ~/.cache/huggingface/ after first download
 *   - Lazy initialization: first call ~5-10s (model load), subsequent ~100ms
 *   - Output: 640-dimensional L2-normalized embedding vector
 *   - Pre-pooled sentence_embedding output (no manual pooling needed)
 *
 * Dependencies: @huggingface/transformers
 */

// @ts-ignore - @huggingface/transformers types may not be perfect
import { AutoTokenizer, AutoModel } from '@huggingface/transformers';

/** Harrier-OSS-v1-270M ONNX model (q4 quantized). */
const MODEL_ID = 'onnx-community/harrier-oss-v1-270m-ONNX';

/** Fixed output dimensionality. */
const EMBEDDING_DIM = 640;

/** Lazily initialized model and tokenizer. */
let tokenizer: any = null;
let model: any = null;

/**
 * Generate a 640-dimensional embedding vector for the given text.
 *
 * On first call, downloads and loads the ONNX model (~344MB, cached).
 * Subsequent calls reuse the loaded model and run in ~100ms.
 *
 * @param text - The text to embed.
 * @param options - Optional settings (isQuery accepted for compatibility but unused).
 * @returns 640-dimensional L2-normalized embedding as a number array.
 */
export async function generateEmbedding(
  text: string,
  options?: { isQuery?: boolean },
): Promise<number[]> {
  if (!model) {
    console.error('[TotalReclaw] Downloading embedding model (~344MB, first run only)...');
    tokenizer = await AutoTokenizer.from_pretrained(MODEL_ID);
    model = await AutoModel.from_pretrained(MODEL_ID, { dtype: 'q4' });
    console.error('[TotalReclaw] Embedding model ready.');
  }

  const inputs = await tokenizer(text, { return_tensors: 'pt', padding: true });
  const output = await model(inputs);
  return Array.from(output.sentence_embedding.data as Float32Array);
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
