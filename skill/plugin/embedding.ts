/**
 * TotalReclaw Plugin - Local Embedding via @huggingface/transformers
 *
 * Generates text embeddings locally using an ONNX model. No API key needed,
 * no data leaves the machine. Preserves the E2EE guarantee.
 *
 * Three model options (selected via CONFIG.embeddingModel):
 *   - "default": onnx-community/harrier-oss-v1-270m-ONNX (640d, fp16 ~553MB, best accuracy/size ratio)
 *   - "small": Xenova/multilingual-e5-small (384d, q8 ~34MB, fast, low RAM)
 *   - "large": onnx-community/Qwen3-Embedding-0.6B-ONNX (1024d, q8 ~600MB, legacy)
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
  /** ONNX quantization dtype. Must match an available variant in the HF repo. */
  dtype: string;
}

const MODELS: Record<string, ModelConfig> = {
  default: {
    id: 'Xenova/multilingual-e5-small',
    dims: 384,
    pooling: 'mean',
    size: '~34MB',
    dtype: 'q8',
  },
  harrier: {
    id: 'onnx-community/harrier-oss-v1-270m-ONNX',
    dims: 640,
    pooling: 'last_token',
    size: '~553MB',
    dtype: 'fp16',  // q4 uses unsupported GatherBlockQuantized op
  },
  large: {
    id: 'onnx-community/Qwen3-Embedding-0.6B-ONNX',
    dims: 1024,
    pooling: 'last_token',
    size: '~600MB',
    dtype: 'q8',
  },
};

function getModelConfig(): ModelConfig {
  const key = CONFIG.embeddingModel || 'default';
  return MODELS[key] || MODELS.default;
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
    console.error('[TotalReclaw] This enables semantic search across your encrypted memories.');
    extractor = await pipeline('feature-extraction', activeModel.id, {
      dtype: activeModel.dtype as any,
    });
    console.error('[TotalReclaw] Embedding model ready. Future startups will be instant.');
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
 * Returns 640 (default/Harrier), 384 (small), or 1024 (large) depending on model selection.
 */
export function getEmbeddingDims(): number {
  return getModelConfig().dims;
}
