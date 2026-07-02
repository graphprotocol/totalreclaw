"""Unit tests for totalreclaw.import_state."""
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

import totalreclaw.import_state as m


@pytest.fixture(autouse=True)
def tmp_state_dir(tmp_path, monkeypatch):
    """Redirect IMPORT_STATE_DIR to a temp dir so tests don't touch ~/.totalreclaw."""
    monkeypatch.setattr(m, "IMPORT_STATE_DIR", tmp_path / "import-state")
    yield


def make_state(**overrides) -> m.ImportState:
    now = datetime.now(timezone.utc).isoformat()
    base = dict(
        import_id="test-id-1234",
        source="chatgpt",
        status="running",
        started_at=now,
        last_updated=now,
        total_chunks=100,
        total_messages=2000,
        batch_done=10,
        batch_total=4,
        facts_stored=25,
        facts_extracted=28,
        dups_skipped=3,
        errors=[],
        file_path="/tmp/conversations.json",
        estimated_total_facts=250,
        estimated_minutes=12,
        estimated_completion_iso=datetime.now(timezone.utc).isoformat(),
        disclosure_confirmed=True,
    )
    base.update(overrides)
    return m.ImportState(**base)


def test_write_and_read_roundtrip():
    state = make_state(import_id="rw-test-1", dups_skipped=7, errors=["e1"])
    m.write_import_state(state)
    read = m.read_import_state("rw-test-1")
    assert read is not None
    assert read.import_id == "rw-test-1"
    assert read.dups_skipped == 7
    assert read.errors == ["e1"]


def test_write_updates_last_updated():
    state = make_state(import_id="ts-test", last_updated="2020-01-01T00:00:00+00:00")
    m.write_import_state(state)
    read = m.read_import_state("ts-test")
    assert read.last_updated != "2020-01-01T00:00:00+00:00"


def test_read_missing_returns_none():
    result = m.read_import_state("does-not-exist")
    assert result is None


def test_is_stale_fresh():
    state = make_state(last_updated=datetime.now(timezone.utc).isoformat())
    assert m.is_import_stale(state) is False


def test_is_stale_old():
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    state = make_state(last_updated=old)
    assert m.is_import_stale(state) is True


def test_most_recent_active_none_when_empty():
    result = m.read_most_recent_active_import()
    assert result is None


def test_most_recent_active_skips_completed():
    m.write_import_state(make_state(import_id="c1", status="completed"))
    result = m.read_most_recent_active_import()
    assert result is None


def test_most_recent_active_returns_newest():
    older = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    newer = datetime.now(timezone.utc).isoformat()
    m.write_import_state(make_state(import_id="old-1", status="running", started_at=older))
    m.write_import_state(make_state(import_id="new-1", status="running", started_at=newer))
    result = m.read_most_recent_active_import()
    assert result is not None
    assert result.import_id == "new-1"


# ---------------------------------------------------------------------------
# #401 — read_most_recent_import (any-status fallback for import_status())
# ---------------------------------------------------------------------------

def test_most_recent_import_none_when_empty():
    assert m.read_most_recent_import() is None


def test_most_recent_import_returns_completed_within_window():
    """The headline #401 case: a completed import is surfaced post-completion."""
    now = datetime.now(timezone.utc).isoformat()
    m.write_import_state(make_state(
        import_id="c-done", status="completed", started_at=now, last_updated=now,
        facts_stored=42,
    ))
    result = m.read_most_recent_import(max_age_hours=48)
    assert result is not None
    assert result.import_id == "c-done"
    assert result.status == "completed"


def test_most_recent_import_skips_older_than_window():
    # write_import_state() overwrites last_updated=now, so write raw JSON to
    # plant a timestamp outside the 48h window (#401 age-window behavior).
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    raw = make_state(import_id="c-old", status="completed",
                     started_at=old, last_updated=old)
    m.IMPORT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    (m.IMPORT_STATE_DIR / "c-old.json").write_text(json.dumps(__import__("dataclasses").asdict(raw)))
    assert m.read_most_recent_import(max_age_hours=48) is None


def test_most_recent_import_picks_latest_by_last_updated():
    """When multiple imports exist, the most recently updated one wins."""
    earlier = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    later = datetime.now(timezone.utc).isoformat()
    m.IMPORT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict
    raw_a = asdict(make_state(import_id="a", status="completed",
                              started_at=earlier, last_updated=earlier))
    raw_b = asdict(make_state(import_id="b", status="failed",
                              started_at=later, last_updated=later))
    (m.IMPORT_STATE_DIR / "a.json").write_text(json.dumps(raw_a))
    (m.IMPORT_STATE_DIR / "b.json").write_text(json.dumps(raw_b))
    result = m.read_most_recent_import(max_age_hours=48)
    assert result is not None
    assert result.import_id == "b"  # later last_updated wins, even though failed


def test_state_file_is_valid_json():
    state = make_state(import_id="json-test")
    m.write_import_state(state)
    path = m.IMPORT_STATE_DIR / "json-test.json"
    data = json.loads(path.read_text())
    assert data["import_id"] == "json-test"
    assert data["source"] == "chatgpt"
