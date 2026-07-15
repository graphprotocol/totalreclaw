"""Eyeball harness for Gemini-import session segmentation (read-only).

Runs the EXACT production segmentation path over a real Gemini Takeout export
and prints a human-readable session report for manual review:

  parse (core parse_gemini via GeminiAdapter) -> flatten turns
  -> embed prompt+reply (Harrier 640d, local) -> segment_sessions(1800, 0.55)

No network, no on-chain writes, no LLM calls. Output: a markdown report of
every multi-turn session (these are the ones that would get Crystals) plus
singleton stats, so a human can judge: are the groupings right? over-merged?
over-split?

Usage:
  PYTHONPATH=python/src .venv/bin/python python/eval_segmentation_eyeball.py \
      "/path/to/My Activity.html" [--threshold 0.55] [--gap 1800] \
      [--out report.md] [--max-sessions 200]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export_path")
    ap.add_argument("--threshold", type=float, default=0.55)
    ap.add_argument("--gap", type=int, default=1800)
    ap.add_argument("--out", default="segmentation_eyeball_report.md")
    ap.add_argument("--max-sessions", type=int, default=200)
    args = ap.parse_args()

    from totalreclaw.imports.adapters.gemini_adapter import GeminiAdapter
    from totalreclaw.crystals.session_segmentation import segment_sessions
    from totalreclaw.embedding import get_embedding

    print(f"parsing {args.export_path} ...", flush=True)
    parsed = GeminiAdapter().parse(file_path=args.export_path)
    if parsed.errors:
        print("adapter errors:", parsed.errors[:3])

    # Flatten to turns exactly like the engine: user->assistant pairs per
    # chunk, turn text = prompt + " " + reply (capped 1000 chars), per-turn
    # timestamp = chunk timestamp (flat Takeout has per-record times).
    turns: list[tuple[float | None, str, str]] = []  # (ts, text_for_embed, prompt_preview)
    for chunk in parsed.chunks:
        ts = None
        if chunk.timestamp:
            try:
                ts = datetime.fromisoformat(
                    chunk.timestamp.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                ts = None
        msgs = chunk.messages or []
        i = 0
        while i < len(msgs):
            m = msgs[i]
            role = (m.get("role") or "user") if isinstance(m, dict) else "user"
            if role != "user":
                i += 1
                continue
            text_u = (m.get("text") or m.get("content") or "") if isinstance(m, dict) else str(m)
            text_a = ""
            if i + 1 < len(msgs):
                nm = msgs[i + 1]
                if isinstance(nm, dict) and (nm.get("role") == "assistant"):
                    text_a = nm.get("text") or nm.get("content") or ""
                    i += 1
            combined = (text_u + " " + text_a).strip()[:1000]
            if combined:
                turns.append((ts, combined, text_u.strip()[:110]))
            i += 1

    print(f"{len(parsed.chunks)} chunks -> {len(turns)} turns; embedding (Harrier, local)...", flush=True)
    timestamps = [t[0] for t in turns]
    embeddings = []
    for k, (_, text, _) in enumerate(turns):
        embeddings.append(get_embedding(text))
        if (k + 1) % 250 == 0:
            print(f"  embedded {k+1}/{len(turns)}", flush=True)

    groups = segment_sessions(timestamps, embeddings, args.gap, args.threshold)

    sizes = Counter(len(g) for g in groups)
    multi = [g for g in groups if len(g) >= 2]
    singletons = sum(1 for g in groups if len(g) == 1)

    def fmt_ts(ts: float | None) -> str:
        if ts is None:
            return "??"
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# Segmentation eyeball report",
        f"",
        f"- export: `{args.export_path}`",
        f"- turns: **{len(turns)}**  |  sessions: **{len(groups)}**  |  "
        f"singletons: **{singletons}** ({100*singletons/max(1,len(groups)):.0f}%) — no Crystal  |  "
        f"multi-turn: **{len(multi)}** — one Crystal each",
        f"- params: threshold={args.threshold}, gap={args.gap}s",
        f"- size histogram: " + ", ".join(f"{n}-turn×{c}" for n, c in sorted(sizes.items())[:12]),
        f"",
        f"## Multi-turn sessions (each would become ONE Crystal)",
        f"",
        f"Judge: do the turns in each session belong together? Any session mixing",
        f"unrelated topics = over-merge. Any conversation you remember as one",
        f"chat split across adjacent sessions = over-split.",
        f"",
    ]
    shown = 0
    for gi, g in enumerate(groups):
        if len(g) < 2 or shown >= args.max_sessions:
            continue
        shown += 1
        t0, t1 = turns[g[0]][0], turns[g[-1]][0]
        lines.append(f"### Session {gi} — {len(g)} turns — {fmt_ts(t0)} → {fmt_ts(t1)} UTC")
        for idx in g:
            lines.append(f"- [{fmt_ts(turns[idx][0])}] {turns[idx][2]}")
        lines.append("")
    if shown >= args.max_sessions:
        lines.append(f"_(truncated at {args.max_sessions} sessions)_")

    with open(args.out, "w") as f:
        f.write("\n".join(lines))
    print(f"\nwrote {args.out} — sessions={len(groups)} singletons={singletons} multi={len(multi)} (showing {shown})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
