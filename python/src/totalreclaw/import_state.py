"""
Import state persistence for background imports.

Reads/writes ~/.totalreclaw/import-state/<import_id>.json so that both
the plugin (TypeScript) and Hermes (Python) share the same state format.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

IMPORT_STATE_DIR: Path = Path.home() / ".totalreclaw" / "import-state"
# 2h (#401, was 1h): the 2026-06-29 Gemini run showed individual batches
# taking 15+ min when nonce retries stacked; 1h produced false-positive
# stale-flagging on healthy long imports. 2h absorbs retry storms without
# masking genuinely-hung imports (which the abort flag handles explicitly).
STALE_THRESHOLD_SECONDS: int = 7200  # 2 hours


@dataclass
class ImportState:
    import_id: str
    source: str
    status: str  # pending | running | completed | failed | aborted
    started_at: str
    last_updated: str
    total_chunks: int = 0
    total_messages: int = 0
    batch_done: int = 0
    batch_total: int = 0
    facts_stored: int = 0
    facts_extracted: int = 0
    dups_skipped: int = 0
    errors: List[str] = field(default_factory=list)
    file_path: Optional[str] = None
    estimated_total_facts: int = 0
    estimated_minutes: int = 0
    estimated_completion_iso: str = ""
    disclosure_confirmed: bool = False
    #: Set True once the agent has proactively told the user this import
    #: finished (the completion-notification one-shot). Prevents re-announcing
    #: the same completed import on every subsequent turn.
    announced: bool = False


def _coerce_state(data: dict) -> "ImportState":
    """Build ImportState from a dict, tolerating unknown/legacy keys."""
    allowed = {f.name for f in __import__("dataclasses").fields(ImportState)}
    return ImportState(**{k: v for k, v in data.items() if k in allowed})


def _state_path(import_id: str) -> Path:
    return IMPORT_STATE_DIR / f"{import_id}.json"


def write_import_state(state: ImportState) -> None:
    IMPORT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import timezone, datetime
    state.last_updated = datetime.now(timezone.utc).isoformat()
    _state_path(state.import_id).write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")


def read_import_state(import_id: str) -> Optional[ImportState]:
    try:
        data = json.loads(_state_path(import_id).read_text(encoding="utf-8"))
        return _coerce_state(data)
    except Exception:
        return None


def is_import_stale(state: ImportState) -> bool:
    try:
        from datetime import datetime, timezone
        last = datetime.fromisoformat(state.last_updated.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age > STALE_THRESHOLD_SECONDS
    except Exception:
        return False


def read_most_recent_active_import() -> Optional[ImportState]:
    """Return the most recently started running/pending import, or None."""
    try:
        candidates: List[ImportState] = []
        for p in IMPORT_STATE_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                state = _coerce_state(data)
                if state.status in ("running", "pending"):
                    candidates.append(state)
            except Exception:
                pass
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.started_at)
    except Exception:
        return None


def read_most_recent_import(max_age_hours: int = 48) -> Optional[ImportState]:
    """Return the most recent import of any status within the age window.

    Used by ``import_status()`` as a fallback when no active import is running
    and no explicit ``import_id`` was supplied (#401). Without this, a caller
    asking "how did the last import go?" after completion would get a blind
    ``no_active_import`` response because ``read_most_recent_active_import``
    only matches ``running``/``pending``. This surfaces the most recent
    completed/failed/aborted import so the agent can report final state.

    ``last_updated`` (not ``started_at``) is the sort key: a long-running
    import that finished 10 min ago is "more recent" than one that started
    later but is still mid-flight. Age is measured from ``last_updated`` so
    a completed import stays reportable for ``max_age_hours`` after it
    finished, not after it started.
    """
    try:
        from datetime import datetime, timezone, timedelta
        candidates: List[ImportState] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        for p in IMPORT_STATE_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                state = _coerce_state(data)
                try:
                    updated = datetime.fromisoformat(state.last_updated.replace("Z", "+00:00"))
                except Exception:
                    continue
                if updated > cutoff:
                    candidates.append(state)
            except Exception:
                pass
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.last_updated)
    except Exception:
        return None


def read_completed_unannounced_imports() -> List[ImportState]:
    """Return completed imports the agent has not yet proactively reported.

    Used by the Hermes ``pre_llm_call`` hook to inject a one-shot
    "import finished" note so the agent tells the user without being asked.
    """
    out: List[ImportState] = []
    try:
        for p in IMPORT_STATE_DIR.glob("*.json"):
            try:
                state = _coerce_state(json.loads(p.read_text(encoding="utf-8")))
                if state.status == "completed" and not state.announced:
                    out.append(state)
            except Exception:
                pass
    except Exception:
        pass
    return sorted(out, key=lambda s: s.last_updated)


def mark_import_announced(import_id: str) -> None:
    """Latch a completed import as announced so it is not reported twice."""
    state = read_import_state(import_id)
    if state is not None and not state.announced:
        state.announced = True
        write_import_state(state)


# ── imp-2 (#244): one-time import-onboarding nudge bookkeeping ────────────

_NUDGE_SENTINEL_NAME = "import-onboarding-nudge-shown"


def import_nudge_shown() -> bool:
    """True once the one-time import-discovery nudge has been emitted."""
    return (IMPORT_STATE_DIR / _NUDGE_SENTINEL_NAME).exists()


def mark_import_nudge_shown() -> None:
    """Latch the one-time import-discovery nudge (never emitted again)."""
    try:
        IMPORT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        (IMPORT_STATE_DIR / _NUDGE_SENTINEL_NAME).write_text("1")
    except OSError:
        pass  # best-effort — a missed latch means one extra nudge, not a failure


def any_import_exists() -> bool:
    """True if any import (running or finished) has ever been recorded."""
    try:
        return any(IMPORT_STATE_DIR.glob("*.json"))
    except OSError:
        return False
