"""Tests for totalreclaw.tuning_loop — Phase 2 Slice 2f (Python port).

Mirrors the plugin's digest-tuning.test.ts: empty log, one counterexample,
user-agreed entries, idempotence, clamping, rate limiting, mixed entries.
All tests run against the real totalreclaw_core bindings.
"""
from __future__ import annotations

import json

import pytest

import totalreclaw_core
from totalreclaw.tuning_loop import (
    FEEDBACK_LOG_MAX_LINES,
    TUNING_LOOP_MIN_INTERVAL_SECONDS,
    append_feedback_log,
    build_feedback_from_decision,
    feedback_log_path,
    find_decision_for_pin,
    run_weight_tuning_loop,
    weights_file_path,
)


@pytest.fixture
def isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
    return tmp_path


def _components(weighted: float, recency: float = 0.5) -> dict:
    return {
        "confidence": 0.85,
        "corroboration": 1.0,
        "recency": recency,
        "validation": 0.7,
        "weighted_total": weighted,
    }


def _entry(ts: int, decision: str = "pin_b") -> dict:
    # Winner and loser must differ on at least one component so apply_feedback
    # has a gradient signal to work with — recency is the lever.
    return {
        "ts": ts,
        "claim_a_id": "0xaaa",
        "claim_b_id": "0xbbb",
        "formula_winner": "a",
        "user_decision": decision,
        "winner_components": _components(0.83, recency=0.8),
        "loser_components": _components(0.73, recency=0.2),
    }


def _load_weights(state_dir) -> dict:
    p = state_dir / "weights.json"
    return json.loads(totalreclaw_core.parse_weights_file(p.read_text()))


class TestFindDecisionForPin:
    def test_empty_log(self):
        assert find_decision_for_pin("any", "loser", "") is None

    def test_matches_loser(self):
        entry = {
            "ts": 1,
            "entity_id": "x",
            "new_claim_id": "w",
            "existing_claim_id": "l",
            "similarity": 0.5,
            "action": "supersede_existing",
            "winner_components": _components(0.83),
            "loser_components": _components(0.73),
        }
        log = json.dumps(entry) + "\n"
        found = find_decision_for_pin("l", "loser", log)
        assert found is not None
        assert found["existing_claim_id"] == "l"

    def test_skips_rows_without_components(self):
        legacy = {
            "ts": 1,
            "entity_id": "x",
            "new_claim_id": "w",
            "existing_claim_id": "l",
            "similarity": 0.5,
            "action": "supersede_existing",
        }
        log = json.dumps(legacy) + "\n"
        assert find_decision_for_pin("l", "loser", log) is None

    def test_returns_most_recent(self):
        older = {
            "ts": 1,
            "entity_id": "x",
            "new_claim_id": "old-w",
            "existing_claim_id": "l",
            "similarity": 0.5,
            "action": "supersede_existing",
            "winner_components": _components(0.8),
            "loser_components": _components(0.7),
        }
        newer = dict(older)
        newer["ts"] = 2
        newer["new_claim_id"] = "new-w"
        log = json.dumps(older) + "\n" + json.dumps(newer) + "\n"
        found = find_decision_for_pin("l", "loser", log)
        assert found["new_claim_id"] == "new-w"


class TestBuildFeedbackFromDecision:
    def test_pin_loser(self):
        decision = {
            "existing_claim_id": "l",
            "new_claim_id": "w",
            "winner_components": _components(0.83),
            "loser_components": _components(0.73),
        }
        fb = build_feedback_from_decision(decision, "pin_loser", 1_777_000_000)
        assert fb["claim_a_id"] == "l"
        assert fb["claim_b_id"] == "w"
        assert fb["formula_winner"] == "b"
        assert fb["user_decision"] == "pin_a"

    def test_unpin_winner(self):
        decision = {
            "existing_claim_id": "l",
            "new_claim_id": "w",
            "winner_components": _components(0.83),
            "loser_components": _components(0.73),
        }
        fb = build_feedback_from_decision(decision, "unpin_winner", 1_777_000_000)
        assert fb["user_decision"] == "pin_b"


class TestRunWeightTuningLoop:
    def test_empty_feedback_is_noop(self, isolated_state_dir):
        result = run_weight_tuning_loop(1_777_000_000)
        assert result.processed == 0
        assert result.gradient_steps == 0
        assert result.skipped == "no-new-entries"

    def test_one_counterexample_adjusts_weights(self, isolated_state_dir):
        append_feedback_log(_entry(1_776_500_000, "pin_b"))

        # Capture defaults before the loop runs.
        before = json.loads(totalreclaw_core.default_weights_file(1_777_000_000))

        result = run_weight_tuning_loop(1_777_000_000)
        assert result.processed == 1
        assert result.gradient_steps == 1
        assert result.skipped is None
        assert result.last_tuning_ts == 1_776_500_000

        after = _load_weights(isolated_state_dir)
        assert after["last_tuning_ts"] == 1_776_500_000
        assert after["feedback_count"] == 1
        assert json.dumps(after["weights"]) != json.dumps(before["weights"])

    def test_user_agreed_no_gradient(self, isolated_state_dir):
        append_feedback_log(_entry(1_776_500_000, "pin_a"))  # formula A + pin A = agree
        before = json.loads(totalreclaw_core.default_weights_file(1_777_000_000))
        result = run_weight_tuning_loop(1_777_000_000)
        assert result.processed == 1
        assert result.gradient_steps == 0

        after = _load_weights(isolated_state_dir)
        assert json.dumps(after["weights"]) == json.dumps(before["weights"])
        assert after["last_tuning_ts"] == 1_776_500_000

    def test_ten_counterexamples_accumulate(self, isolated_state_dir):
        for i in range(10):
            append_feedback_log(_entry(1_776_500_000 + i, "pin_b"))
        result = run_weight_tuning_loop(1_777_000_000)
        assert result.processed == 10
        assert result.gradient_steps == 10
        after = _load_weights(isolated_state_dir)
        assert after["feedback_count"] == 10
        assert after["last_tuning_ts"] == 1_776_500_009

    def test_already_processed_entries_skipped(self, isolated_state_dir):
        append_feedback_log(_entry(1_776_500_000, "pin_b"))
        first = run_weight_tuning_loop(1_777_000_000)
        assert first.processed == 1

        # Force past the rate limit.
        stale = _load_weights(isolated_state_dir)
        stale["updated_at"] = 1_776_000_000
        weights_file_path().write_text(
            totalreclaw_core.serialize_weights_file(json.dumps(stale))
        )
        second = run_weight_tuning_loop(1_777_000_000)
        assert second.processed == 0
        assert second.skipped == "no-new-entries"

    def test_idempotent_across_runs(self, isolated_state_dir):
        append_feedback_log(_entry(1_776_500_000, "pin_b"))
        run_weight_tuning_loop(1_777_000_000)
        after_first = _load_weights(isolated_state_dir)

        stale = dict(after_first)
        stale["updated_at"] = 1_776_000_000
        weights_file_path().write_text(
            totalreclaw_core.serialize_weights_file(json.dumps(stale))
        )
        run_weight_tuning_loop(1_777_000_000)
        after_second = _load_weights(isolated_state_dir)
        assert json.dumps(after_second["weights"]) == json.dumps(after_first["weights"])
        assert after_second["feedback_count"] == after_first["feedback_count"]

    def test_weight_clamping_100_entries(self, isolated_state_dir):
        for i in range(100):
            append_feedback_log(_entry(1_776_500_000 + i, "pin_b"))
        result = run_weight_tuning_loop(1_777_000_000)
        assert result.gradient_steps == 100
        after = _load_weights(isolated_state_dir)
        ws = after["weights"]
        for k, v in ws.items():
            assert 0.05 <= v <= 0.60, f"{k}={v} out of clamp range"
        total = sum(ws.values())
        assert 0.9 <= total <= 1.1, f"total={total} out of sum range"

    def test_mixed_agreed_and_disagreed(self, isolated_state_dir):
        append_feedback_log(_entry(1_776_500_000, "pin_a"))
        append_feedback_log(_entry(1_776_500_001, "pin_a"))
        append_feedback_log(_entry(1_776_500_002, "pin_b"))
        append_feedback_log(_entry(1_776_500_003, "pin_a"))
        append_feedback_log(_entry(1_776_500_004, "pin_b"))

        result = run_weight_tuning_loop(1_777_000_000)
        assert result.processed == 5
        assert result.gradient_steps == 2

    def test_rate_limit_skips_within_window(self, isolated_state_dir):
        append_feedback_log(_entry(1_776_500_000, "pin_b"))
        first = run_weight_tuning_loop(1_777_000_000)
        assert first.skipped is None

        # Immediate second call with a fresh entry — rate-limited.
        append_feedback_log(_entry(1_777_000_100, "pin_b"))
        second = run_weight_tuning_loop(1_777_000_100)
        assert second.skipped == "rate-limited"
        assert second.gradient_steps == 0

        # Past the window — runs again.
        third = run_weight_tuning_loop(1_777_000_000 + TUNING_LOOP_MIN_INTERVAL_SECONDS + 1)
        assert third.skipped != "rate-limited"
        assert third.gradient_steps == 1

    def test_format_is_parseable_by_core(self, isolated_state_dir):
        append_feedback_log(_entry(1_776_500_000, "pin_b"))
        append_feedback_log(_entry(1_776_500_001, "pin_a"))
        content = feedback_log_path().read_text()
        parsed = json.loads(totalreclaw_core.read_feedback_jsonl(content))
        assert len(parsed["entries"]) == 2
        assert len(parsed["warnings"]) == 0

    def test_constants(self):
        assert FEEDBACK_LOG_MAX_LINES == 10_000
        assert TUNING_LOOP_MIN_INTERVAL_SECONDS == 3600
