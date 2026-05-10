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
STALE_THRESHOLD_SECONDS: int = 3600  # 1 hour


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
        return ImportState(**data)
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
                state = ImportState(**data)
                if state.status in ("running", "pending"):
                    candidates.append(state)
            except Exception:
                pass
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.started_at)
    except Exception:
        return None
