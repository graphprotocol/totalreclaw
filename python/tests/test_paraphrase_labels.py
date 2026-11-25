"""Labelled paraphrase pairs for the forget guard and the explicit write gate.

Fixture: ``tests/parity/fixtures/paraphrase-labels-v1.json`` (internal #715, #777).
Cosines come from Harrier q4; tests feed 2-d unit vectors at those cosines.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from totalreclaw.embedding import get_embedding, is_model_available_locally

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "parity" / "fixtures" / "paraphrase-labels-v1.json"
LABELS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_fixture_shape():
    assert LABELS["paraphrase_min_cosine"] == 0.8
    assert LABELS["contained_min_cosine"] == 0.65
    for key in ("forget", "gate"):
        rows = LABELS[key]
        for row in rows:
            assert isinstance(row["label"], bool)
            assert 0 < row["cos"] < 1
        counts = LABELS["expected_counts"][key]
        assert sum(counts.values()) == len(rows)
    by_name = {r["name"]: r for k in ("forget", "gate") for r in LABELS[k]}
    for name in LABELS["must_store"]:
        assert name in by_name
        assert by_name[name]["label"] is False


@pytest.mark.skipif(not is_model_available_locally(), reason="Harrier not cached")
def test_fixture_cosines_match_real_harrier():
    bad = []
    for key, a_key in (("forget", "entry"), ("gate", "existing")):
        for row in LABELS[key]:
            real = _cos(get_embedding(row[a_key]), get_embedding(row["text"]))
            if abs(real - row["cos"]) > 0.01:
                bad.append(f"{key}/{row['name']}: fixture {row['cos']} real {real:.4f}")
    assert not bad, "\n".join(bad)
