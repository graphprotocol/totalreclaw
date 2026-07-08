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
from typing import Iterator, List, Optional

IMPORT_STATE_DIR: Path = Path.home() / ".totalreclaw" / "import-state"
# 2h (#401, was 1h): the 2026-06-29 Gemini run showed individual batches
# taking 15+ min when nonce retries stacked; 1h produced false-positive
# stale-flagging on healthy long imports. 2h absorbs retry storms without
# masking genuinely-hung imports (which the abort flag handles explicitly).
STALE_THRESHOLD_SECONDS: int = 7200  # 2 hours
# #457b: a running import that made ZERO progress (batch_done==0) and hasn't
# updated in >10 min almost certainly had its spawning process exit (one-shot
# `hermes chat -q`) — the background task died with it. Distinct, shorter
# threshold than STALE_THRESHOLD_SECONDS (which covers a genuinely-hung import
# that IS making occasional progress) so we surface the orphan promptly instead
# of making the user wait 2h.
EARLY_STALE_THRESHOLD_SECONDS: int = 600  # 10 minutes


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
    #: #457 accounting: session Crystals stored beyond the extracted atomic
    #: facts, so facts_stored ≈ facts_extracted + derived_facts − dups_skipped.
    derived_facts: int = 0
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


def is_import_early_stale(state: ImportState) -> bool:
    """True for a ``running`` import stuck at ZERO progress past the 10-min
    early threshold — the orphaned-background-task signature (#457b).

    Only fires when ``batch_done == 0``: an import that has completed at least
    one batch is making progress and is governed by the 2h
    :data:`STALE_THRESHOLD_SECONDS` instead.
    """
    if state.status != "running" or state.batch_done != 0:
        return False
    try:
        from datetime import datetime, timezone
        last = datetime.fromisoformat(state.last_updated.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age > EARLY_STALE_THRESHOLD_SECONDS
    except Exception:
        return False


# ── #460: state-dir file discrimination ───────────────────────────────────
#
# IMPORT_STATE_DIR holds import-state RECORDS ({import_id}.json, JSON objects)
# alongside non-record sidecars — notably the #436 conversation registry
# ``imported-conversations-<source>.json`` (a JSON LIST) and the disclosure /
# nudge sentinels. A consumer that globs ``*.json`` and treats every hit as a
# state dict crashes on the list payload (``list.get`` → AttributeError, which
# an ``except (OSError, ValueError)`` misses) — it deterministically bricked
# import batch 2 and every re-import once the registry existed. ALL record
# scans now go through these guards so a registry file in the same directory
# can never be parsed as a state record.
_NON_STATE_JSON_PREFIXES = ("imported-conversations-",)


def _is_state_record_file(path: Path) -> bool:
    """True only for a genuine ``{import_id}.json`` state record — excludes the
    #436 conversation-registry ledgers (and any future prefixed sidecar)."""
    name = path.name
    return name.endswith(".json") and not any(
        name.startswith(pfx) for pfx in _NON_STATE_JSON_PREFIXES
    )


def _iter_state_files() -> Iterator[Path]:
    """Yield Paths of genuine state-record files (registry ledgers excluded)."""
    try:
        for p in IMPORT_STATE_DIR.glob("*.json"):
            if _is_state_record_file(p):
                yield p
    except OSError:
        return


def iter_import_state_records() -> Iterator[ImportState]:
    """Yield an :class:`ImportState` for every parseable DICT record in the
    state dir, skipping registry ledgers and any non-dict / unreadable payload.

    This is the single safe entry point for "scan all import records" — every
    glob-based consumer routes through it so a non-record JSON file sharing the
    directory can never crash a scan (#460).
    """
    for p in _iter_state_files():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            yield _coerce_state(data)
        except Exception:
            continue


def read_most_recent_active_import() -> Optional[ImportState]:
    """Return the most recently started running/pending import, or None."""
    try:
        candidates = [
            s for s in iter_import_state_records()
            if s.status in ("running", "pending")
        ]
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
        for state in iter_import_state_records():
            try:
                updated = datetime.fromisoformat(state.last_updated.replace("Z", "+00:00"))
            except Exception:
                continue
            if updated > cutoff:
                candidates.append(state)
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
        for state in iter_import_state_records():
            if state.status == "completed" and not state.announced:
                out.append(state)
    except Exception:
        pass  # best-effort — state dir missing/unreadable yields no imports
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
    """True if any import (running or finished) has ever been recorded.

    Counts only genuine state records — a #436 conversation-registry ledger in
    the same dir must NOT read as "an import exists" (it would make the
    onboarding nudge think the user already imported). (#460)
    """
    return any(True for _ in _iter_state_files())


# ── F2 (#436): per-source imported-conversation registry ──────────────────
#
# Re-imports RE-EXTRACT via the LLM, and paraphrase gives every fact a fresh
# text fingerprint — so text-fingerprint dedup can never catch a re-import.
# The durable fix records which SOURCE conversations have already been
# imported (by ``conversation_id``) and drops their chunks before extraction
# on any later run. Persisted per source so ChatGPT / Claude registries don't
# collide. Gemini exports carry no conversation_id and are unaffected.


def _imported_conversations_path(source: str):
    # Reference IMPORT_STATE_DIR late (module global) so tests that
    # monkeypatch it take effect — same pattern as the state helpers.
    return IMPORT_STATE_DIR / f"imported-conversations-{source}.json"


def load_imported_conversations(source: str) -> set:
    """Return the set of ``conversation_id``s already imported for *source*.

    Tolerates a missing or corrupt registry file (returns an empty set) so a
    damaged registry degrades to "nothing imported yet" rather than raising.
    """
    try:
        raw = _imported_conversations_path(source).read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception:
        pass  # best-effort — missing/corrupt registry means "nothing imported yet"
    return set()


def record_imported_conversations(source: str, ids) -> None:
    """Add ``ids`` to the imported-conversation registry for *source*.

    Idempotent: merges with the existing registry and de-duplicates. Writes
    the union back as a JSON list. Best-effort — an IO error is swallowed
    (a missed record means at worst a future re-import re-processes that
    conversation, never data loss).
    """
    new_ids = {str(x) for x in (ids or [])}
    if not new_ids:
        return
    try:
        IMPORT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        existing = load_imported_conversations(source)
        merged = sorted(existing | new_ids)
        _imported_conversations_path(source).write_text(
            json.dumps(merged), encoding="utf-8"
        )
    except OSError:
        pass
