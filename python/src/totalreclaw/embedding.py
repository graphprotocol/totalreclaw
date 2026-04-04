"""
TotalReclaw Embedding Pipeline.

Uses Harrier-OSS-v1-270M via ONNX Runtime for embedding generation.
Client-side only — preserves E2EE guarantee.

Model: onnx-community/harrier-oss-v1-270m-ONNX (quantized)
Pooling: last-token
Normalization: L2 unit-length
Dimensions: 640
No instruction prefix needed.
"""
from __future__ import annotations

import os

import numpy as np

_session = None
_tokenizer = None
_num_layers: int = 0
_num_heads: int = 0
_head_dim: int = 0

MODEL_ID = "onnx-community/harrier-oss-v1-270m-ONNX"
EMBEDDING_DIMS = 640


def _ensure_loaded():
    global _session, _tokenizer, _num_layers, _num_heads, _head_dim
    if _session is not None and _tokenizer is not None:
        return _session, _tokenizer

    import onnxruntime as ort
    from transformers import AutoTokenizer
    from huggingface_hub import hf_hub_download

    model_path = hf_hub_download(
        repo_id=MODEL_ID,
        filename="onnx/model_quantized.onnx",
    )

    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    _session = ort.InferenceSession(model_path, sess_options)
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

    # Detect KV-cache dimensions from model inputs.
    # Inputs like past_key_values.{layer}.key have shape
    # (batch, num_heads, past_seq_len, head_dim).
    kv_inputs = [
        inp for inp in _session.get_inputs()
        if inp.name.startswith("past_key_values.") and inp.name.endswith(".key")
    ]
    _num_layers = len(kv_inputs)
    if _num_layers > 0:
        # Shape is [batch_size, num_heads, past_sequence_length, head_dim]
        shape = kv_inputs[0].shape
        _num_heads = shape[1] if isinstance(shape[1], int) else 8
        _head_dim = shape[3] if isinstance(shape[3], int) else 128

    return _session, _tokenizer


def get_embedding(text: str) -> list[float]:
    """Generate a 640-dim L2-normalized embedding (last-token pooling)."""
    session, tokenizer = _ensure_loaded()

    inputs = tokenizer(
        text,
        return_tensors="np",
        padding=True,
        truncation=True,
        max_length=512,
    )

    input_ids = inputs["input_ids"].astype(np.int64)
    attention_mask = inputs["attention_mask"].astype(np.int64)
    seq_len = input_ids.shape[1]

    # Build the feed dict with all required inputs.
    feed: dict[str, np.ndarray] = {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
    }

    # Add position_ids if the model expects them.
    input_names = {inp.name for inp in session.get_inputs()}
    if "position_ids" in input_names:
        feed["position_ids"] = np.arange(seq_len, dtype=np.int64).reshape(1, seq_len)

    # Add empty KV-cache tensors for all layers (past_sequence_length = 0).
    for layer in range(_num_layers):
        empty_kv = np.zeros((1, _num_heads, 0, _head_dim), dtype=np.float32)
        feed[f"past_key_values.{layer}.key"] = empty_kv
        feed[f"past_key_values.{layer}.value"] = empty_kv

    outputs = session.run(None, feed)
    last_hidden_state = outputs[0]  # (batch, seq_len, hidden_dim)

    # Last-token pooling: use the last non-padding token.
    last_token_idx = int(attention_mask.sum(axis=1)[0]) - 1
    embedding = last_hidden_state[0, last_token_idx, :]

    # L2 normalize.
    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm

    return embedding.tolist()


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts."""
    return [get_embedding(text) for text in texts]


def get_embedding_dims() -> int:
    return EMBEDDING_DIMS
