/**
 * TotalReclaw Plugin - Local Embedding via @huggingface/transformers
 *
 * Generates text embeddings locally using an ONNX model. No API key needed,
 * no data leaves the machine. Preserves the E2EE guarantee.
 *
 * Locked to Harrier-OSS-v1-270M (640d, q4, ~344MB, pre-pooled). Changing the
 * embedding model breaks search across an existing vault, so the
 * `TOTALRECLAW_EMBEDDING_MODEL` user-facing env var was removed in v1.
 *
 * Dependencies: @huggingface/transformers
 */

// @ts-ignore - @huggingface/transformers types may not be perfect
import { AutoTokenizer, AutoModel, pipeline, type FeatureExtractionPipeline } from '@huggingface/transformers';

interface ModelConfig {
  id: string;
  dims: number;
  /** 'sentence_embedding' for models with pre-pooled output, 'mean'/'last_token' for pipeline models */
  pooling: string;
  size: string;
  /** ONNX quantization dtype. Must match an available variant in the HF repo. */
  dtype: string;
}

const HARRIER_MODEL: ModelConfig = {
  id: 'onnx-community/harrier-oss-v1-270m-ONNX',
  dims: 640,
  pooling: 'sentence_embedding',
  size: '~344MB',
  dtype: 'q4',
};

function getModelConfig(): ModelConfig {
  return HARRIER_MODEL;
}

/** Lazily initialized model instances. */
let pipelineExtractor: FeatureExtractionPipeline | null = null;
let autoTokenizer: any = null;
let autoModel: any = null;
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
  if (!activeModel) {
    activeModel = getModelConfig();
    console.error(`[TotalReclaw] Downloading embedding model (${activeModel.size}, one-time setup)...`);
    console.error('[TotalReclaw] This enables semantic search across your encrypted memories.');

    if (activeModel.pooling === 'sentence_embedding') {
      // Harrier: use AutoModel (pipeline doesn't support sentence_embedding output)
      autoTokenizer = await AutoTokenizer.from_pretrained(activeModel.id);
      autoModel = await AutoModel.from_pretrained(activeModel.id, {
        dtype: activeModel.dtype as any,
      });
    } else {
      // e5-small / Qwen: use pipeline
      pipelineExtractor = await pipeline('feature-extraction', activeModel.id, {
        dtype: activeModel.dtype as any,
      });
    }
    console.error('[TotalReclaw] Embedding model ready. Future startups will be instant.');
  }

  const model = activeModel!;

  if (model.pooling === 'sentence_embedding') {
    // Harrier: pre-pooled, pre-normalized output
    const inputs = await autoTokenizer(text, { return_tensors: 'pt', padding: true });
    const output = await autoModel(inputs);
    return Array.from(output.sentence_embedding.data as Float32Array);
  } else {
    // Pipeline models: use pooling option
    const input = model.pooling === 'mean' && options?.isQuery
      ? `query: ${text}`
      : text;
    const output = await pipelineExtractor!(input, { pooling: model.pooling as any, normalize: true });
    return Array.from(output.data as Float32Array);
  }
}

/**
 * Get the embedding vector dimensionality.
 * Returns 640 (default/Harrier), 384 (small), or 1024 (large) depending on model selection.
 */
export function getEmbeddingDims(): number {
  return getModelConfig().dims;
}
