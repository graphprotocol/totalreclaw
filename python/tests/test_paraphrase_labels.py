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


# ---------------------------------------------------------------------------
# Forget guard
# ---------------------------------------------------------------------------

from totalreclaw import forget_guard as fg  # noqa: E402
import totalreclaw_core  # noqa: E402


def _at(c):
    return [c, math.sqrt(1 - c * c)]


def _count(preds_labels):
    tp = sum(1 for p, l in preds_labels if p and l)
    fp = sum(1 for p, l in preds_labels if p and not l)
    fn = sum(1 for p, l in preds_labels if not p and l)
    tn = sum(1 for p, l in preds_labels if not p and not l)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def _report(title, counts):
    p = counts["tp"] / max(1, counts["tp"] + counts["fp"])
    r = counts["tp"] / max(1, counts["tp"] + counts["fn"])
    print(f"{title}: {counts} P {p:.2f} R {r:.2f}")


def _run_forget(match):
    rows, matched = [], []
    for row in LABELS["forget"]:
        entry = {"id": "e", "text": row["entry"], "embedding": [1.0, 0.0]}
        pred = match(row["text"], _at(row["cos"]), [entry]) is not None
        rows.append((pred, row["label"]))
        if pred:
            matched.append(row["name"])
    counts = _count(rows)
    _report("forget guard", counts)
    assert counts == LABELS["expected_counts"]["forget"]
    assert not [n for n in matched if n in LABELS["must_store"]]


def test_forget_labels_python_port():
    _run_forget(fg._py_match_forgotten)


def _core_has_paraphrase():
    if not hasattr(totalreclaw_core, "match_forgotten"):
        return False
    out = totalreclaw_core.match_forgotten(
        "User doesn't eat meat.",
        "[0.83, 0.5578]",
        '[{"id":"v","text":"User is vegetarian.","embedding":[1,0]}]',
    )
    return json.loads(out) is not None


@pytest.mark.skipif(not _core_has_paraphrase(), reason="installed totalreclaw_core lacks the paraphrase rule")
def test_forget_labels_core():
    def match(text, emb, entries):
        return json.loads(totalreclaw_core.match_forgotten(text, json.dumps(emb), json.dumps(entries)))

    _run_forget(match)


# ---------------------------------------------------------------------------
# Explicit write gate
# ---------------------------------------------------------------------------

from totalreclaw import write_gate  # noqa: E402


def _run_gate(decide):
    rows, matched = [], []
    for row in LABELS["gate"]:
        cand = {"id": "e", "text": row["existing"], "embedding": _at(row["cos"])}
        pred = decide(row["text"], cand) != "store"
        rows.append((pred, row["label"]))
        if pred:
            matched.append(row["name"])
    counts = _count(rows)
    _report("write gate", counts)
    assert counts == LABELS["expected_counts"]["gate"]
    assert not [n for n in matched if n in LABELS["must_store"]]


def test_gate_labels_python_fallback():
    _run_gate(lambda text, cand: write_gate._py_decide(text, [1.0, 0.0], [cand], 0.85, "explicit")["action"])


def _core_has_contained():
    if not hasattr(totalreclaw_core, "decide_write"):
        return False
    h15 = LABELS["gate"][0]
    out = totalreclaw_core.decide_write(
        h15["text"],
        "[1.0, 0.0]",
        json.dumps([{"id": "e", "text": h15["existing"], "embedding": _at(0.71)}]),
        "explicit",
        None,
    )
    m = json.loads(out)["match"]
    return m is not None and m["kind"] == "contained"


@pytest.mark.skipif(not _core_has_contained(), reason="installed totalreclaw_core lacks the contained rule")
def test_gate_labels_core():
    def decide(text, cand):
        out = totalreclaw_core.decide_write(text, "[1.0, 0.0]", json.dumps([cand]), "explicit", None)
        return json.loads(out)["action"]

    _run_gate(decide)
