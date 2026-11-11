#!/usr/bin/env python3
"""Generate ``write-gate-v1.json`` — store-time write gate parity fixture.

Consumers (all must stay green):
  * Rust:   rust/totalreclaw-core/src/write_gate.rs   (tests::parity_fixture_vectors)
  * WASM:   tests/parity/write-gate-parity.test.ts    (CI job cross-language-parity)
  * PyO3 + pure-Python fallback:
            python/tests/test_write_gate_parity.py    (CI job python-tests)

The ``expected`` values come from ``reference_*`` below — an independent Python
statement of the rules, NOT the Rust code under test.

Normalization (``normalize_fact_text``):
  1. Unicode NFC.  2. Lowercase.
  3. Fold typographic quotes / dashes / ellipsis to ASCII.
  4. Collapse runs of Unicode White_Space to one space.
  5. Strip EDGE_PUNCTUATION and White_Space from both ends, repeatedly.

Decision (``decide_write``):
  1. Exact: normalized text equal to a candidate's (non-empty id) → skip, any
     mode; a pinned candidate is preferred, otherwise the first.
  2. mode exact_only, or no embedding → store.
  3. Cosine >= threshold against candidates with a same-length embedding
     (auto mode ignores pinned candidates); best similarity wins, ties keep
     the first → auto: skip, explicit: confirm.
  4. Otherwise store.

Cosine is computed on float32-rounded inputs in float64, like the Rust code.

Run from the repo root:
    python3 tests/parity/fixtures/generate-write-gate-v1.py
"""
from __future__ import annotations

import json
import math
import struct
import unicodedata
from pathlib import Path

OUT_PATH = Path(__file__).resolve().parent / "write-gate-v1.json"
DEFAULT_THRESHOLD = 0.85

WHITE_SPACE = set(
    "\t\n\x0b\x0c\r \x85\xa0 "
    "           "
    "    　"
)
EDGE_PUNCTUATION = set(".,;:!?'\"`()[]{}<>-*_~•¡¿«»‹›"
                       "。，；：！？、")
FOLD = {
    **{c: "'" for c in "‘’‛′´"},
    **{c: '"' for c in "“”„″"},
    **{c: "-" for c in "‐‑‒–—―"},
    "…": "...",
}


def reference_normalize(text: str) -> str:
    s = unicodedata.normalize("NFC", text).lower()
    s = "".join(FOLD.get(c, c) for c in s)
    out, prev_ws = [], False
    for c in s:
        if c in WHITE_SPACE:
            if not prev_ws:
                out.append(" ")
            prev_ws = True
        else:
            out.append(c)
            prev_ws = False
    s = "".join(out)
    strip = WHITE_SPACE | EDGE_PUNCTUATION
    start, end = 0, len(s)
    while start < end and s[start] in strip:
        start += 1
    while end > start and s[end - 1] in strip:
        end -= 1
    return s[start:end]


def f32(xs):
    return [struct.unpack("f", struct.pack("f", float(x)))[0] for x in xs]


def cosine(a, b):
    a, b = f32(a), f32(b)
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na * nb == 0 else dot / (na * nb)


def reference_decide(text, embedding, candidates, threshold, mode):
    threshold = DEFAULT_THRESHOLD if threshold is None else threshold
    wanted = reference_normalize(text)
    if wanted:
        exact = None
        for c in candidates:
            if not c.get("id") or reference_normalize(c.get("text") or "") != wanted:
                continue
            if exact is None or (c.get("pinned") and not exact.get("pinned")):
                exact = c
            if c.get("pinned"):
                break
        if exact is not None:
            return {"action": "skip", "match": _match(exact, "exact", 1.0)}
    if mode == "exact_only" or not embedding:
        return {"action": "store", "match": None}
    best, best_sim = None, None
    for c in candidates:
        if not c.get("id") or (mode == "auto" and c.get("pinned")):
            continue
        emb = c.get("embedding")
        if not emb or len(emb) != len(embedding):
            continue
        sim = cosine(embedding, emb)
        if sim >= threshold and (best is None or sim > best_sim):
            best, best_sim = c, sim
    if best is None:
        return {"action": "store", "match": None}
    return {
        "action": "skip" if mode == "auto" else "confirm",
        "match": _match(best, "near_duplicate", best_sim),
    }


def _match(c, kind, sim):
    return {
        "fact_id": c["id"],
        "text": c.get("text") or "",
        "kind": kind,
        "similarity": sim,
        "pinned": bool(c.get("pinned")),
    }


NORMALIZE_INPUTS = [
    "User is vegetarian",
    "User is vegetarian.",
    "  user   is VEGETARIAN!  ",
    "\"User is vegetarian.\"",
    "- User is vegetarian",
    "**User is vegetarian**",
    "User is vegetarian…",
    "(User is vegetarian)",
    "User knows C++",
    "User knows C#.",
    "Battery at 100%",
    "User’s laptop is a 2023 MacBook Pro M3",
    "Lisbon — Porto",
    "“quoted”",
    "café",
    "café",
    "1:1 with Inês on Tuesdays",
    "tabs\tand\nnewlines and　ideographic",
    " ... ",
    "",
    "¿Qué tal?",
    "用户住在里斯本。",
    "\x1funit separator is not white space\x1f",
]


def c(cid, text, emb=None, pinned=False):
    item = {"id": cid, "text": text}
    if emb is not None:
        item["embedding"] = emb
    if pinned:
        item["pinned"] = True
    return item


DECIDE_CASES = [
    {"name": "no_candidates_store", "text": "User is vegetarian", "embedding": [1, 0, 0],
     "candidates": [], "mode": "auto"},
    {"name": "trailing_period_is_exact", "text": "User is vegetarian",
     "embedding": None, "candidates": [c("f1", "User is vegetarian.")], "mode": "exact_only"},
    {"name": "exact_skips_in_explicit_mode", "text": "user is VEGETARIAN!",
     "embedding": [1, 0, 0], "candidates": [c("f1", "User is vegetarian", [0, 1, 0])], "mode": "explicit"},
    {"name": "exact_prefers_pinned", "text": "User is vegetarian",
     "embedding": None,
     "candidates": [c("a", "User is vegetarian"), c("p", "user is vegetarian.", pinned=True)],
     "mode": "auto"},
    {"name": "exact_first_unpinned_when_no_pin", "text": "User is vegetarian",
     "embedding": None, "candidates": [c("a", "User is vegetarian."), c("b", "User is vegetarian")],
     "mode": "auto"},
    {"name": "empty_id_never_matches", "text": "User is vegetarian", "embedding": [1, 0],
     "candidates": [c("", "User is vegetarian", [1, 0])], "mode": "auto"},
    {"name": "near_duplicate_auto_skips", "text": "User is vegetarian", "embedding": [1, 0, 0],
     "candidates": [c("f1", "User follows a vegetarian diet", [0.99, 0.1, 0])], "mode": "auto"},
    {"name": "near_duplicate_explicit_confirms", "text": "User is vegetarian", "embedding": [1, 0, 0],
     "candidates": [c("f1", "User follows a vegetarian diet", [0.99, 0.1, 0])], "mode": "explicit"},
    {"name": "near_duplicate_exact_only_stores", "text": "User is vegetarian", "embedding": [1, 0, 0],
     "candidates": [c("f1", "User follows a vegetarian diet", [0.99, 0.1, 0])], "mode": "exact_only"},
    {"name": "different_fact_below_threshold_stores", "text": "User drinks black coffee",
     "embedding": [1, 0, 0], "candidates": [c("f1", "User prefers green tea", [0.6, 0.8, 0])],
     "mode": "auto"},
    {"name": "auto_ignores_pinned_near_duplicate", "text": "1:1 with Ines moved to Thursdays",
     "embedding": [0.99, 0.05], "candidates": [c("p", "1:1 with Ines on Tuesdays", [1, 0], pinned=True)],
     "mode": "auto"},
    {"name": "explicit_reports_pinned_near_duplicate", "text": "1:1 with Ines moved to Thursdays",
     "embedding": [0.99, 0.05], "candidates": [c("p", "1:1 with Ines on Tuesdays", [1, 0], pinned=True)],
     "mode": "explicit"},
    {"name": "best_similarity_wins_ties_keep_first", "text": "new fact", "embedding": [1, 0],
     "candidates": [c("a", "x", [0.9, 0.3]), c("b", "y", [1, 0.01]), c("c", "z", [1, 0.01])],
     "mode": "auto"},
    {"name": "mismatched_dims_and_missing_embeddings_skipped", "text": "new fact", "embedding": [1, 0],
     "candidates": [c("a", "x", [1, 0, 0]), c("b", "y"), c("d", "w", [])], "mode": "auto"},
    {"name": "custom_threshold", "text": "new fact", "embedding": [1, 0],
     "candidates": [c("a", "x", [0.8, 0.6])], "threshold": 0.75, "mode": "auto"},
    {"name": "punctuation_only_never_exact", "text": "...", "embedding": None,
     "candidates": [c("a", "!!!")], "mode": "auto"},
    {"name": "inner_punctuation_kept", "text": "User knows C", "embedding": None,
     "candidates": [c("a", "User knows C++")], "mode": "auto"},
    {"name": "typographic_apostrophe_is_exact", "text": "User's laptop is a MacBook",
     "embedding": None, "candidates": [c("a", "User’s laptop is a MacBook.")], "mode": "auto"},
]


def main() -> None:
    normalize = [{"input": s, "expected": reference_normalize(s)} for s in NORMALIZE_INPUTS]
    decide = []
    for case in DECIDE_CASES:
        case = dict(case)
        case.setdefault("threshold", None)
        case["expected"] = reference_decide(
            case["text"], case["embedding"], case["candidates"], case["threshold"], case["mode"]
        )
        decide.append(case)
    fixture = {
        "_generator": "tests/parity/fixtures/generate-write-gate-v1.py. Expected values come from an independent Python reference implementation, not from the Rust code under test.",
        "version": 1,
        "default_threshold": DEFAULT_THRESHOLD,
        "normalize": normalize,
        "decide": decide,
    }
    OUT_PATH.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH.name}: {len(normalize)} normalize + {len(decide)} decide cases")


if __name__ == "__main__":
    main()
