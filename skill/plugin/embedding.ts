/**
 * TotalReclaw Plugin - Local Embedding via @huggingface/transformers
 *
 * Generates text embeddings locally using an ONNX model. No API key needed,
 * no data leaves the machine. Preserves the E2EE guarantee.
 *
 * Two model options (selected via CONFIG.embeddingModel):
 *   - "small" (default): Xenova/multilingual-e5-small (384d, ~34MB, fast, low RAM)
 *   - "large": onnx-community/Qwen3-Embedding-0.6B-ONNX (1024d, ~600MB, best accuracy)
 *
 * The small model is the default because the plugin runs inside the host
 * agent process (OpenClaw, etc.) which already uses significant RAM.
 *
 * Dependencies: @huggingface/transformers
 */

// @ts-ignore - @huggingface/transformers types may not be perfect
import { pipeline, type FeatureExtractionPipeline } from '@huggingface/transformers';
import { CONFIG } from './config.js';

interface ModelConfig {
  id: string;
  dims: number;
  pooling: string;
  size: string;
}

const MODELS: Record<string, ModelConfig> = {
  small: {
    id: 'Xenova/multilingual-e5-small',
    dims: 384,
    pooling: 'mean',
    size: '~34MB',
  },
  large: {
    id: 'onnx-community/Qwen3-Embedding-0.6B-ONNX',
    dims: 1024,
    pooling: 'last_token',
    size: '~600MB',
  },
};

function getModelConfig(): ModelConfig {
  const key = CONFIG.embeddingModel || 'small';
  return MODELS[key] || MODELS.small;
}

/** Lazily initialized feature extraction pipeline. */
let extractor: FeatureExtractionPipeline | null = null;
let activeModel: ModelConfig | null = null;

/**
 * Generate an embedding vector for the given text.
 *
 * On first call, downloads and loads the ONNX model (cached after download).
 * Subsequent calls reuse the loaded model and run in ~100ms.
 */
export async function generateEmbedding(
  text: string,
  options?: { isQuery?: boolean },
): Promise<number[]> {
  if (!extractor) {
    activeModel = getModelConfig();
    console.error(`[TotalReclaw] Downloading embedding model (${activeModel.size}, one-time setup)...`);
    extractor = await pipeline('feature-extraction', activeModel.id, {
      quantized: true,
    });
    console.error('[TotalReclaw] Embedding model ready.');
  }

  const model = activeModel!;
  const input = model.pooling === 'mean' && options?.isQuery
    ? `query: ${text}`
    : text;
  const output = await extractor(input, { pooling: model.pooling as any, normalize: true });
  return Array.from(output.data as Float32Array);
}

/**
 * Get the embedding vector dimensionality.
 * Returns 384 (small/default) or 1024 (large) depending on model selection.
 */
export function getEmbeddingDims(): number {
  return getModelConfig().dims;
}
