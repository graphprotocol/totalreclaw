#!/usr/bin/env python3
"""Phase 2 core smoke test — validates the full contradiction + feedback + tuning
pipeline end-to-end via the installed PyO3 wheel. No network, no disk I/O beyond
the temp weights file. Run after every Rust core change to catch regressions
before the VPS QA run.

Usage:
    TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz \\
    /Users/pdiogo/Documents/code/totalreclaw/python/.venv/bin/python3 \\
    tests/smoke/phase2_core_smoke.py

Exits 0 on success, non-zero on any assertion failure.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TOTALRECLAW_SERVER_URL", "https://api-staging.totalreclaw.xyz")

import totalreclaw_core as core  # noqa: E402


def log(step: str, detail: str = "") -> None:
    print(f"  [{step}] {detail}")


def fail(msg: str) -> None:
    print(f"\n  FAIL: {msg}")
    sys.exit(1)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    print("Phase 2 core smoke test\n")

    # ──────────────────────────────────────────────────────────────────────
    # Step 1: Default weights match P2-3 spec exactly
    # ──────────────────────────────────────────────────────────────────────
    log("1", "default_resolution_weights")
    weights_json = core.default_resolution_weights()
    weights = json.loads(weights_json)
    expected = {"confidence": 0.25, "corroboration": 0.15, "recency": 0.4, "validation": 0.2}
    if weights != expected:
        fail(f"Default weights mismatch: got {weights}, expected {expected}")
    log("1", f"OK — {weights}")

    # ──────────────────────────────────────────────────────────────────────
    # Step 2: Build two conflicting claims (Vim-vs-VSCode scenario)
    # ──────────────────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    vim_claim = {
        "t": "uses Vim as primary editor",
        "c": "pref",
        "cf": 0.8,
        "i": 8,
        "sa": "openclaw-plugin",
        "ea": iso(now - timedelta(days=60)),
        "cc": 3,
        "e": [{"n": "editor", "tp": "tool"}],
    }
    vscode_claim = {
        "t": "uses VS Code with Vim keybindings",
        "c": "pref",
        "cf": 0.9,
        "i": 8,
        "sa": "openclaw-plugin",
        "ea": iso(now - timedelta(days=7)),
        "e": [{"n": "editor", "tp": "tool"}],
    }
    log("2", "built 2 conflicting claims")

    # ──────────────────────────────────────────────────────────────────────
    # Step 3: Canonicalize both claims (round-trip through Rust serde)
    # ──────────────────────────────────────────────────────────────────────
    log("3", "canonicalize_claim (vim)")
    vim_canonical = core.canonicalize_claim(json.dumps(vim_claim))
    log("3", f"  → {vim_canonical[:80]}...")

    log("3", "canonicalize_claim (vscode)")
    vscode_canonical = core.canonicalize_claim(json.dumps(vscode_claim))
    log("3", f"  → {vscode_canonical[:80]}...")

    # ──────────────────────────────────────────────────────────────────────
    # Step 4: Compute score components for each claim
    # ──────────────────────────────────────────────────────────────────────
    now_ts = int(now.timestamp())
    log("4", "compute_score_components")
    vim_comp = json.loads(core.compute_score_components(vim_canonical, now_ts, weights_json))
    vscode_comp = json.loads(core.compute_score_components(vscode_canonical, now_ts, weights_json))
    log("4", f"  vim:    total={vim_comp['weighted_total']:.4f}, "
              f"recency={vim_comp['recency']:.4f}, corroboration={vim_comp['corroboration']:.4f}")
    log("4", f"  vscode: total={vscode_comp['weighted_total']:.4f}, "
              f"recency={vscode_comp['recency']:.4f}, corroboration={vscode_comp['corroboration']:.4f}")

    # Vim has higher corroboration (3x) but older. VS Code has higher confidence
    # and much higher recency. With default weights, recency dominates → VSCode wins.
    if vscode_comp["weighted_total"] <= vim_comp["weighted_total"]:
        fail(f"VSCode should beat Vim on default weights (recency dominates): "
             f"vim={vim_comp['weighted_total']} vscode={vscode_comp['weighted_total']}")
    log("4", "OK — VSCode weighted_total > Vim weighted_total")

    # ──────────────────────────────────────────────────────────────────────
    # Step 5: Resolve the pair
    # ──────────────────────────────────────────────────────────────────────
    log("5", "resolve_pair")
    outcome_json = core.resolve_pair(
        vim_canonical, "vim_id",
        vscode_canonical, "vscode_id",
        now_ts, weights_json,
    )
    outcome = json.loads(outcome_json)
    log("5", f"  winner={outcome['winner_id']} score_delta={outcome['score_delta']:.4f}")
    if outcome["winner_id"] != "vscode_id":
        fail(f"Expected winner=vscode_id, got {outcome['winner_id']}")
    if "winner_components" not in outcome or "loser_components" not in outcome:
        fail("ResolutionOutcome missing component breakdowns")
    log("5", "OK — winner is vscode_id with full component breakdown")

    # ──────────────────────────────────────────────────────────────────────
    # Step 6: User override — pin Vim (the loser) → build FeedbackEntry
    # ──────────────────────────────────────────────────────────────────────
    log("6", "build FeedbackEntry (user pins loser)")
    feedback = {
        "ts": now_ts,
        "claim_a_id": "vim_id",        # the pinned loser
        "claim_b_id": "vscode_id",     # the formula's winner
        "formula_winner": "b",          # formula picked B
        "user_decision": "pin_a",       # user pinned A (loser override)
        "winner_components": outcome["winner_components"],
        "loser_components": outcome["loser_components"],
    }
    log("6", f"  user_decision={feedback['user_decision']}")

    # ──────────────────────────────────────────────────────────────────────
    # Step 7: Feedback → Counterexample → Apply → new weights
    # ──────────────────────────────────────────────────────────────────────
    log("7", "feedback_to_counterexample")
    cx_json = core.feedback_to_counterexample(json.dumps(feedback))
    if cx_json == "null":
        fail("Expected counterexample, got null — feedback_to_counterexample is broken")
    log("7", "  → non-null counterexample (gradient signal present)")

    log("7", "apply_feedback")
    new_weights_json = core.apply_feedback(weights_json, cx_json)
    new_weights = json.loads(new_weights_json)
    log("7", f"  → {new_weights}")

    if new_weights == weights:
        fail("apply_feedback did not move weights — gradient step is broken")

    # The user pinned Vim (higher corroboration, lower recency).
    # Expectation: corroboration weight should go UP, recency weight should go DOWN.
    if new_weights["recency"] >= weights["recency"]:
        fail(f"recency weight should decrease after pin-loser, "
             f"got {weights['recency']} → {new_weights['recency']}")
    if new_weights["corroboration"] <= weights["corroboration"]:
        fail(f"corroboration weight should increase after pin-loser, "
             f"got {weights['corroboration']} → {new_weights['corroboration']}")
    log("7", f"  OK — recency {weights['recency']} → {new_weights['recency']:.4f} "
              f"(down)")
    log("7", f"     — corroboration {weights['corroboration']} → {new_weights['corroboration']:.4f} "
              f"(up)")

    # ──────────────────────────────────────────────────────────────────────
    # Step 8: Feedback log JSONL round-trip
    # ──────────────────────────────────────────────────────────────────────
    log("8", "append_feedback_to_jsonl + read_feedback_jsonl round-trip")
    jsonl_content = core.append_feedback_to_jsonl("", json.dumps(feedback))
    if not jsonl_content.endswith("\n"):
        fail("append_feedback_to_jsonl should end with newline")

    jsonl_content = core.append_feedback_to_jsonl(jsonl_content, json.dumps(feedback))
    read_result = json.loads(core.read_feedback_jsonl(jsonl_content))
    if len(read_result["entries"]) != 2:
        fail(f"Expected 2 entries, got {len(read_result['entries'])}")
    if read_result["warnings"]:
        fail(f"Unexpected warnings: {read_result['warnings']}")
    log("8", f"  → 2 entries, 0 warnings")

    # ──────────────────────────────────────────────────────────────────────
    # Step 9: Weights file round-trip
    # ──────────────────────────────────────────────────────────────────────
    log("9", "default_weights_file + parse round-trip")
    weights_file_json = core.default_weights_file(now_ts)
    weights_file = json.loads(weights_file_json)
    if weights_file["version"] != 1:
        fail(f"Expected version 1, got {weights_file['version']}")
    if weights_file["feedback_count"] != 0:
        fail(f"Expected feedback_count=0, got {weights_file['feedback_count']}")
    if weights_file.get("last_tuning_ts", 0) != 0:
        fail(f"Expected last_tuning_ts=0, got {weights_file.get('last_tuning_ts')}")
    log("9", f"  → version={weights_file['version']}, "
              f"thresholds=({weights_file['threshold_lower']}, {weights_file['threshold_upper']})")

    # Serialize + parse round-trip
    serialized = core.serialize_weights_file(weights_file_json)
    reparsed = json.loads(core.parse_weights_file(serialized))
    if reparsed != weights_file:
        fail("WeightsFile round-trip failed")
    log("9", "  OK — serialize + parse round-trip clean")

    # ──────────────────────────────────────────────────────────────────────
    # Step 10: Legacy parse (old weights file without last_tuning_ts)
    # ──────────────────────────────────────────────────────────────────────
    log("10", "parse_weights_file legacy (no last_tuning_ts)")
    legacy_json = json.dumps({
        "version": 1,
        "updated_at": 0,
        "weights": weights,
        "threshold_lower": 0.3,
        "threshold_upper": 0.85,
        "feedback_count": 0,
    })
    legacy_parsed = json.loads(core.parse_weights_file(legacy_json))
    if legacy_parsed.get("last_tuning_ts", -1) != 0:
        fail(f"Legacy parse should default last_tuning_ts to 0, got {legacy_parsed.get('last_tuning_ts')}")
    log("10", "  OK — legacy weights file parses with last_tuning_ts=0")

    # ──────────────────────────────────────────────────────────────────────
    print("\n  ALL CHECKS PASSED ✓")
    print("\nPhase 2 core pipeline verified end-to-end:")
    print("  default weights → canonicalize → score → resolve → feedback →")
    print("  counterexample → apply → persist → legacy parse")


if __name__ == "__main__":
    main()
