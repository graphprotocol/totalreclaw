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

import os
import sys
from pathlib import Path

import numpy as np

_session = None
_tokenizer = None

MODEL_ID = "onnx-community/harrier-oss-v1-270m-ONNX"
EMBEDDING_DIMS = 640

# 2.3.1rc2 — approximate download size for the Harrier model + tokenizer.
# Actual measured bytes land between 200-230 MB depending on HF revision;
# we advertise 216 MB as the typical figure. The number isn't used for
# any correctness logic, only for the pre-download banner.
APPROX_MODEL_SIZE_MB = 216


def _hf_cache_has_model() -> bool:
    """Best-effort probe: does the HF hub cache already contain Harrier?

    Returns True when the expected ``models--onnx-community--harrier-oss-v1-270m-ONNX``
    directory exists under ``$HF_HOME/hub`` (or the default cache root).
    False means the next hf_hub_download will fetch ~216 MB from HF.
    """
    try:
        hf_home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")))
        hub = hf_home / "hub"
        if not hub.exists():
            return False
        candidate = hub / "models--onnx-community--harrier-oss-v1-270m-ONNX"
        return candidate.exists()
    except Exception:
        return False


def _emit_download_banner() -> None:
    """Print a single-line banner BEFORE the (potentially slow) download.

    Sent to stderr so piped stdout stays clean. Respects
    ``TOTALRECLAW_QUIET_EMBEDDING_BANNER=1`` for silent mode (useful in
    CI / tests that don't want the line noise).
    """
    if os.environ.get("TOTALRECLAW_QUIET_EMBEDDING_BANNER") == "1":
        return
    try:
        sys.stderr.write(
            f"\n[TotalReclaw] Downloading embedding model from HuggingFace "
            f"(~{APPROX_MODEL_SIZE_MB} MB, one-time)…\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _emit_download_done() -> None:
    if os.environ.get("TOTALRECLAW_QUIET_EMBEDDING_BANNER") == "1":
        return
    try:
        sys.stderr.write("[TotalReclaw] Embedding model ready.\n\n")
        sys.stderr.flush()
    except Exception:
        pass


def _enable_hf_progress_bar() -> None:
    """Turn on huggingface_hub's built-in tqdm progress bar.

    HF defaults to NO progress output when stderr isn't a TTY. We force
    it on so users see SOMETHING while 216 MB downloads. The bar itself
    goes to stderr (tqdm default).
    """
    try:
        from huggingface_hub.utils import enable_progress_bars

        enable_progress_bars()
    except Exception:
        # Older huggingface_hub versions (<0.19) lack this helper. Tqdm
        # is still used internally — we just can't force it.
        pass


def _ensure_loaded():
    global _session, _tokenizer
    if _session is not None and _tokenizer is not None:
        return _session, _tokenizer

    import onnxruntime as ort
    from transformers import AutoTokenizer
    from huggingface_hub import hf_hub_download

    # 2.3.1rc2 — pre-download banner + progress bar. If the cache is
    # already warm, this is a no-op (banner skipped, downloads short-circuit).
    needs_download = not _hf_cache_has_model()
    if needs_download:
        _emit_download_banner()
        _enable_hf_progress_bar()

    model_path = hf_hub_download(
        repo_id=MODEL_ID,
        filename="onnx/model_q4.onnx",
    )
    # q4 models store weights in a companion file that must also be present
    hf_hub_download(
        repo_id=MODEL_ID,
        filename="onnx/model_q4.onnx_data",
    )

    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    _session = ort.InferenceSession(model_path, sess_options)
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

    if needs_download:
        _emit_download_done()

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
