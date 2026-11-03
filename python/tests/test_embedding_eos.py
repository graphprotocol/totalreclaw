"""Harrier pools the last token, so every input must end with <eos>.

The ONNX repo's tokenizer config omits ``add_eos_token`` and transformers'
Gemma tokenizer then drops <eos>. These tests run without the model: a fake
tokenizer and session record what reaches the ONNX graph.
"""
from __future__ import annotations

import sys
import types

import numpy as np

from totalreclaw import embedding

EOS = 1


class _Tok:
    eos_token_id = EOS

    def __init__(self, ids):
        self._ids = ids

    def __call__(self, text, **kw):
        ids = np.array([self._ids], dtype=np.int64)
        return {"input_ids": ids, "attention_mask": np.ones_like(ids)}


class _Out:
    def __init__(self, name):
        self.name = name


class _Session:
    def __init__(self):
        self.feed = None

    def get_outputs(self):
        return [_Out("sentence_embedding")]

    def run(self, names, feed):
        self.feed = feed
        return [np.ones((1, embedding.EMBEDDING_DIMS), dtype=np.float32)]


def _run(monkeypatch, ids):
    sess = _Session()
    monkeypatch.setattr(embedding, "_session", sess)
    monkeypatch.setattr(embedding, "_tokenizer", _Tok(ids))
    embedding.get_embedding("hello world")
    return sess.feed


def test_appends_eos_when_tokenizer_drops_it(monkeypatch):
    feed = _run(monkeypatch, [2, 23391, 1902])
    assert feed["input_ids"].tolist() == [[2, 23391, 1902, EOS]]
    assert feed["attention_mask"].tolist() == [[1, 1, 1, 1]]
    assert feed["input_ids"].dtype == np.int64


def test_leaves_input_alone_when_eos_present(monkeypatch):
    feed = _run(monkeypatch, [2, 23391, 1902, EOS])
    assert feed["input_ids"].tolist() == [[2, 23391, 1902, EOS]]


def test_full_length_input_ends_with_eos(monkeypatch):
    feed = _run(monkeypatch, [2] + [7] * 511)
    assert feed["input_ids"].shape == (1, 512)
    assert feed["input_ids"][0, -1] == EOS


def test_loader_asks_the_tokenizer_for_eos(monkeypatch):
    calls = {}

    class _AutoTokenizer:
        @staticmethod
        def from_pretrained(model_id, **kw):
            calls["kw"] = kw
            return _Tok([2, EOS])

    fake_ort = types.SimpleNamespace(
        SessionOptions=lambda: types.SimpleNamespace(graph_optimization_level=None),
        GraphOptimizationLevel=types.SimpleNamespace(ORT_ENABLE_ALL=99),
        InferenceSession=lambda path, opts: _Session(),
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(AutoTokenizer=_AutoTokenizer))
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=lambda **kw: "/fake/model.onnx")
    )
    monkeypatch.setattr(embedding, "_hf_cache_has_model", lambda: True)
    monkeypatch.setattr(embedding, "_session", None)
    monkeypatch.setattr(embedding, "_tokenizer", None)

    embedding._ensure_loaded()
    assert calls["kw"].get("add_eos_token") is True
