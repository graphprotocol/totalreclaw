"""
TotalReclaw Embedding Pipeline.

Uses Harrier-OSS-v1-270M via ONNX Runtime for embedding generation.
Client-side only — preserves E2EE guarantee.

Model: onnx-community/harrier-oss-v1-270m-ONNX (q4 quantized)
Output: pre-pooled sentence_embedding (no manual pooling needed)
Normalization: already L2-normalized
Dimensions: 640
No instruction prefix needed.
"""
from __future__ import annotations

import numpy as np

_session = None
_tokenizer = None

MODEL_ID = "onnx-community/harrier-oss-v1-270m-ONNX"
EMBEDDING_DIMS = 640


def _ensure_loaded():
    global _session, _tokenizer
    if _session is not None and _tokenizer is not None:
        return _session, _tokenizer

    import onnxruntime as ort
    from transformers import AutoTokenizer
    from huggingface_hub import hf_hub_download

    model_path = hf_hub_download(
        repo_id=MODEL_ID,
        filename="onnx/model_q4.onnx",
    )

    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    _session = ort.InferenceSession(model_path, sess_options)
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

    return _session, _tokenizer


def get_embedding(text: str) -> list[float]:
    """Generate a 640-dim L2-normalized embedding."""
    session, tokenizer = _ensure_loaded()

    inputs = tokenizer(
        text,
        return_tensors="np",
        padding=True,
        truncation=True,
        max_length=512,
    )

    feed: dict[str, np.ndarray] = {
        "input_ids": inputs["input_ids"].astype(np.int64),
        "attention_mask": inputs["attention_mask"].astype(np.int64),
    }

    output_names = [o.name for o in session.get_outputs()]
    results = session.run(output_names, feed)

    # Use sentence_embedding output (pre-pooled, already L2-normalized)
    se_idx = output_names.index("sentence_embedding")
    embedding = results[se_idx][0]  # (640,)

    return embedding.tolist()


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts."""
    return [get_embedding(text) for text in texts]


def get_embedding_dims() -> int:
    return EMBEDDING_DIMS
