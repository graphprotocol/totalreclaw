"""Phase 2 Slice 2f: feedback wiring + weight-tuning loop (Python port).

Mirrors ``skill/plugin/contradiction-sync.ts`` exactly so all three clients
write byte-compatible ``feedback.jsonl`` rows and drive the same per-user
``weights.json`` updates.

Used by ``operations.pin_fact``/``unpin_fact`` (to write feedback entries
when a pin overrides a prior auto-resolution) and by the Hermes digest
compile hook (to replay those entries into adjusted weights).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import totalreclaw_core as _core

FEEDBACK_LOG_MAX_LINES = 10_000
TUNING_LOOP_MIN_INTERVAL_SECONDS = 3600

Role = Literal["loser", "winner"]
PinAction = Literal["pin_loser", "unpin_winner"]
TargetStatus = Literal["pinned", "active"]


def resolve_state_dir() -> Path:
    """Return the state dir — honours ``TOTALRECLAW_STATE_DIR`` for tests."""
    override = os.environ.get("TOTALRECLAW_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".totalreclaw"


def ensure_state_dir() -> Path:
    """Create the state dir if missing; best-effort."""
    d = resolve_state_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def decisions_log_path() -> Path:
    """Return ``~/.totalreclaw/decisions.jsonl`` (or test override)."""
    return resolve_state_dir() / "decisions.jsonl"


def feedback_log_path() -> Path:
    """Return ``~/.totalreclaw/feedback.jsonl`` (or test override)."""
    return resolve_state_dir() / "feedback.jsonl"


def weights_file_path() -> Path:
    """Return ``~/.totalreclaw/weights.json`` (or test override)."""
    return resolve_state_dir() / "weights.json"


def find_decision_for_pin(
    fact_id: str,
    role: Role,
    log_content: str,
) -> Optional[dict]:
    """Walk decisions.jsonl in reverse, return the newest matching row.

    Slice 2f rows must have both ``winner_components`` and
    ``loser_components`` — legacy rows (pre-Slice 2f) are skipped.
    """
    if not log_content:
        return None
    lines = [l for l in log_content.split("\n") if l]
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("action") != "supersede_existing":
            continue
        if not entry.get("winner_components") or not entry.get("loser_components"):
            continue
        if role == "loser" and entry.get("existing_claim_id") == fact_id:
            return entry
        if role == "winner" and entry.get("new_claim_id") == fact_id:
            return entry
    return None


def build_feedback_from_decision(
    decision: dict,
    action: PinAction,
    now_unix_seconds: int,
) -> Optional[dict]:
    """Construct a FeedbackEntry dict from a matching decision row."""
    winner = decision.get("winner_components")
    loser = decision.get("loser_components")
    if not winner or not loser:
        return None
    return {
        "ts": now_unix_seconds,
        "claim_a_id": decision["existing_claim_id"],
        "claim_b_id": decision["new_claim_id"],
        "formula_winner": "b",
        "user_decision": "pin_a" if action == "pin_loser" else "pin_b",
        "winner_components": winner,
        "loser_components": loser,
    }


def append_feedback_log(entry: dict) -> None:
    """Append a feedback entry via core helpers; rotates over the cap."""
    try:
        ensure_state_dir()
        p = feedback_log_path()
        existing = ""
        if p.exists():
            existing = p.read_text(encoding="utf-8")
        appended = _core.append_feedback_to_jsonl(existing, json.dumps(entry))
        rotated = _core.rotate_feedback_log(appended, FEEDBACK_LOG_MAX_LINES)
        p.write_text(rotated, encoding="utf-8")
    except Exception:
        # Best effort: feedback logging is never fatal.
        pass


def maybe_write_feedback_for_pin(
    fact_id: str,
    target_status: TargetStatus,
    now_unix_seconds: int,
    logger: Optional[logging.Logger] = None,
) -> Optional[dict]:
    """On pin/unpin, record a counterexample if the user overrode the formula.

    Returns the feedback entry that was written, or ``None`` for voluntary
    pins (no matching decision row).
    """
    log = ""
    try:
        if decisions_log_path().exists():
            log = decisions_log_path().read_text(encoding="utf-8")
    except Exception:
        log = ""
    role: Role = "loser" if target_status == "pinned" else "winner"
    decision = find_decision_for_pin(fact_id, role, log)
    if not decision:
        if logger is not None:
            short = fact_id[:10]
            logger.info(
                "Pin feedback: no matching auto-resolution for %s... (voluntary, no signal)",
                short,
            )
        return None
    action: PinAction = "pin_loser" if target_status == "pinned" else "unpin_winner"
    entry = build_feedback_from_decision(decision, action, now_unix_seconds)
    if not entry:
        return None
    append_feedback_log(entry)
    if logger is not None:
        logger.info(
            "Pin feedback: recorded counterexample (%s) for %s...",
            entry["user_decision"],
            fact_id[:10],
        )
    return entry


# ─── Weight tuning loop ─────────────────────────────────────────────────────


@dataclass
class TuningLoopResult:
    """Return shape of ``run_weight_tuning_loop`` — exposed for tests."""

    processed: int
    gradient_steps: int
    skipped: Optional[str]
    last_tuning_ts: int


def _load_weights_file(now_unix_seconds: int) -> dict:
    p = weights_file_path()
    if p.exists():
        try:
            parsed = _core.parse_weights_file(p.read_text(encoding="utf-8"))
            return json.loads(parsed)
        except Exception:
            pass
    # Defaults on missing/malformed.
    return json.loads(_core.default_weights_file(now_unix_seconds))


def _save_weights_file(file: dict) -> None:
    try:
        ensure_state_dir()
        serialized = _core.serialize_weights_file(json.dumps(file))
        weights_file_path().write_text(serialized, encoding="utf-8")
    except Exception:
        pass


def run_weight_tuning_loop(
    now_unix_seconds: int,
    logger: Optional[logging.Logger] = None,
) -> TuningLoopResult:
    """Replay feedback.jsonl → adjusted weights.json via WASM core calls.

    Idempotent, rate-limited, and never throws. See the plugin companion
    function in ``skill/plugin/contradiction-sync.ts``.
    """
    file = _load_weights_file(now_unix_seconds)
    prior_ts = int(file.get("last_tuning_ts") or 0)
    updated_at = int(file.get("updated_at") or 0)

    if prior_ts > 0 and updated_at > 0 and now_unix_seconds - updated_at < TUNING_LOOP_MIN_INTERVAL_SECONDS:
        return TuningLoopResult(0, 0, "rate-limited", prior_ts)

    fb_path = feedback_log_path()
    if not fb_path.exists():
        return TuningLoopResult(0, 0, "no-new-entries", prior_ts)
    content = fb_path.read_text(encoding="utf-8")
    if not content:
        return TuningLoopResult(0, 0, "no-new-entries", prior_ts)

    try:
        parsed = json.loads(_core.read_feedback_jsonl(content))
    except Exception as e:
        if logger is not None:
            logger.warning("Tuning loop: failed to parse feedback.jsonl: %s", e)
        return TuningLoopResult(0, 0, "no-new-entries", prior_ts)
    for w in parsed.get("warnings", []):
        if logger is not None:
            logger.warning("Tuning loop: %s", w)

    new_entries = [e for e in parsed.get("entries", []) if int(e.get("ts", 0)) > prior_ts]
    if not new_entries:
        return TuningLoopResult(0, 0, "no-new-entries", prior_ts)

    weights_json = json.dumps(file.get("weights") or {})
    gradient_steps = 0
    max_ts = prior_ts
    for entry in new_entries:
        ts = int(entry.get("ts", 0))
        if ts > max_ts:
            max_ts = ts
        try:
            cx_json = _core.feedback_to_counterexample(json.dumps(entry))
        except Exception as e:
            if logger is not None:
                logger.warning("Tuning loop: feedback_to_counterexample failed: %s", e)
            continue
        if cx_json == "null":
            continue
        try:
            weights_json = _core.apply_feedback(weights_json, cx_json)
            gradient_steps += 1
        except Exception as e:
            if logger is not None:
                logger.warning("Tuning loop: apply_feedback failed: %s", e)

    try:
        adjusted = json.loads(weights_json)
    except Exception:
        return TuningLoopResult(len(new_entries), gradient_steps, "no-weights", max_ts)

    next_file = dict(file)
    next_file["weights"] = adjusted
    next_file["updated_at"] = now_unix_seconds
    next_file["last_tuning_ts"] = max_ts
    next_file["feedback_count"] = int(file.get("feedback_count") or 0) + len(new_entries)
    _save_weights_file(next_file)

    if logger is not None:
        logger.info(
            "Tuning loop: processed %d feedback entries, applied %d gradient steps",
            len(new_entries),
            gradient_steps,
        )
    return TuningLoopResult(
        processed=len(new_entries),
        gradient_steps=gradient_steps,
        skipped=None,
        last_tuning_ts=max_ts,
    )
