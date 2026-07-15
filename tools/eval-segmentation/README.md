# Segmentation eyeball harness

A **manual evaluation tool** for the Gemini-import session-segmentation path. It
runs the **exact production segmentation** over a real Google Takeout export and
emits a markdown report for a human to eyeball: *are the session groupings
right? over-merged? over-split?*

This is **not** part of the pytest suite — it needs a real Takeout export plus
the ~344 MB Harrier ONNX embedding model, so it is run by hand when tuning the
segmentation threshold / gap.

## What it does

Replays the production Gemini-import pipeline, read-only:

```
core parse_gemini (via GeminiAdapter)  ->  flatten to turns
  ->  embed prompt+reply (Harrier 640d, local)  ->  segment_sessions(gap, threshold)
  ->  markdown report of every multi-turn session + singleton stats
```

- **No network, no on-chain writes, no LLM calls.** Embedding is the local
  Harrier ONNX model only.
- Turn text = `prompt + " " + reply` (capped 1000 chars), per-turn timestamp =
  the record timestamp (flat Takeout has per-record times).
- `segment_sessions(timestamps, embeddings, gap_seconds, sim_threshold)` is the
  shared centroid-walk segmenter (`totalreclaw.crystals.session_segmentation`;
  prefers the Rust core `totalreclaw_core.segment_sessions`).
- The report lists every **multi-turn** session (≥2 turns — these are the ones
  that would mint a Crystal) with turn previews, plus the size histogram and the
  singleton count (singletons are gated out of Crystal minting by design).

## Usage

From the repo root, with the Python client installed in a venv:

```bash
PYTHONPATH=python/src .venv/bin/python tools/eval-segmentation/eval_segmentation_eyeball.py \
    "/path/to/My Activity.html" \
    [--threshold 0.55] [--gap 1800] \
    [--out segmentation_eyeball_report.md] [--max-sessions 200]
```

Defaults match production: `--threshold 0.55`, `--gap 1800` (seconds).

## Validation record (2026-07-14)

Pedro reviewed the report over his real **3,942-record** Gemini Takeout export.
Verdict: **LOOKS RIGHT** at `threshold=0.55`, `gap=1800s`.

| metric | value |
|---|---|
| turns parsed | 3,795 |
| sessions | 1,651 |
| singletons (1-turn, gated → no Crystal) | 902 (55%) |
| multi-turn sessions (≥2 turns → one Crystal each) | 749 |
| old per-chunk scheme Crystals (for comparison) | ~1,397 |

The new turn-granularity segmentation mints **~46% fewer Crystals** than the old
per-chunk scheme — almost entirely by collapsing the single-Q&A noise the old
scheme over-split into separate Crystals. The **≥2-turn Crystal gate stays**.

## Notes

- The harness is intentionally a thin read-only replay — it imports
  `GeminiAdapter`, `segment_sessions`, and `get_embedding` from the installed
  client, so it tracks production behaviour automatically as those move.
- The generated report contains the reviewer's **personal conversation text**;
  it is git-ignored (see `.gitignore`) and must never be committed.
