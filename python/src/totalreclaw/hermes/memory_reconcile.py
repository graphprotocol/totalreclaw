"""Reconcile Hermes' built-in memory files with the TotalReclaw vault.

``TotalReclawMemoryProvider.on_memory_write`` mirrors the built-in
``memory`` tool's writes, but Hermes only reports writes made by the agent
it is running. Hermes 0.22.5 builds its background memory review (the main
writer of ``USER.md`` / ``MEMORY.md``) with ``skip_memory=True``, so that
fork has no memory manager and its writes never reach a provider. Hand
edits don't either. This module closes the gap by diffing the files.

A pass reads ``$HERMES_HOME/memories/USER.md`` and ``MEMORY.md`` the way
Hermes' ``MemoryStore`` does (split on ``"\\n§\\n"``, entries stripped,
empty ones dropped, duplicates collapsed), runs each entry through Hermes'
egress scrub (the same one the manager applies before handing text to a
provider), and compares the result with a snapshot of the entries last
reconciled:

- a new entry is stored (exact copies already in the vault are skipped);
- a changed entry (a removed entry paired with a similar new one) stores
  the new text; the old fact is forgotten only when the two entries are
  clearly the same fact (``memory_provider._same_fact``) and the vault match
  is confident, so a merge keeps the old facts;
- a removed entry is NOT forgotten: the files are size-capped and Hermes
  evicts entries to make room, so a removal is not a request to forget;
- entries Hermes' threat scanner flags are never stored.

The first pass for a Hermes home and account (no snapshot yet) stores every
current entry once. A pass does at most
:data:`MAX_OPS_PER_PASS` vault operations; the rest wait for the next pass.
The snapshot only advances for work that is done, so a failed store is
retried next time.

Passes run on the mirror's worker thread (queued by the provider at
``initialize`` and ``on_session_end``), the same thread that runs mirrored
writes, so the two never interleave. Mirrored writes update the snapshot as
well, so a pass doesn't redo them.

The snapshot lives at ``<state dir>/hermes-memory-snapshot-<id>.json``
(mode 600): entry hashes plus the scrubbed entry text, per target.
``TOTALRECLAW_HERMES_MEMORY_RECONCILE=0`` turns the automatic passes off.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .state import PluginState

logger = logging.getLogger(__name__)

#: Hermes' entry delimiter (``tools/memory_tool_store.py``).
ENTRY_DELIMITER = "\n§\n"
#: Provider ``target`` → file under ``$HERMES_HOME/memories``.
TARGET_FILES = {"user": "USER.md", "memory": "MEMORY.md"}
#: Most vault operations (store / forget) one pass performs.
MAX_OPS_PER_PASS = 50
#: A removed and a new entry at least this similar (difflib ratio) are one
#: changed entry.
CHANGE_PAIR_RATIO = 0.6
SNAPSHOT_VERSION = 1
_DISABLE_ENV = "TOTALRECLAW_HERMES_MEMORY_RECONCILE"

# Per snapshot path: the memory files' (mtime_ns, size) after the last pass
# that left nothing deferred. Lets the session-end pass return early.
_LAST_CLEAN_STAT: Dict[Path, Tuple[Any, ...]] = {}


def auto_reconcile_enabled() -> bool:
    return os.environ.get(_DISABLE_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def _hermes_home(hermes_home: Optional[Path] = None) -> Path:
    from .install_memory_provider import _hermes_home as default_home

    return (hermes_home or default_home()).expanduser()


def _state_dir() -> Path:
    from .tools import _resolve_state_dir

    return _resolve_state_dir()


def parse_entries(raw: str) -> List[str]:
    """Entries of a memory file, as Hermes' ``MemoryStore`` loads them."""
    entries = (e.strip() for e in raw.split(ENTRY_DELIMITER))
    return list(dict.fromkeys(e for e in entries if e))


def read_entries(path: Path) -> List[str]:
    try:
        return parse_entries(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        return []


def scrub(text: str) -> str:
    """Hermes' egress scrub (secrets masked), or the text unchanged outside
    Hermes. The memory manager applies the same scrub to mirrored writes, so
    both paths hash the same text."""
    try:
        from agent.redact import redact_for_egress  # type: ignore[import-not-found]
    except Exception:
        return text
    return redact_for_egress(text)


def is_flagged(text: str) -> bool:
    """True when Hermes' threat scanner flags the entry (prompt injection,
    invisible characters). Hermes keeps such entries out of its own prompt;
    they must not reach the vault either."""
    try:
        from tools.threat_patterns import scan_for_threats  # type: ignore[import-not-found]
    except Exception:
        return False
    try:
        return bool(scan_for_threats(text, scope="strict"))
    except Exception:
        return True  # fail closed


def entry_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _account_of(state: "PluginState") -> str:
    try:
        address = getattr(state.get_client(), "eoa_address", "")
    except Exception:
        return ""
    return address.lower() if isinstance(address, str) else ""


class Snapshot:
    """The entries last reconciled for one Hermes home + account."""

    def __init__(self, path: Path, hermes_home: Path, account: str) -> None:
        self.path = path
        self._meta = {"hermes_home": str(hermes_home), "account": account}
        self.targets: Dict[str, Dict[str, str]] = {t: {} for t in TARGET_FILES}
        self.exists = False

    @classmethod
    def load(cls, state: "PluginState", hermes_home: Optional[Path] = None) -> "Snapshot":
        home = _hermes_home(hermes_home)
        account = _account_of(state)
        scope = entry_key(f"{home.resolve()}|{account}")[:12]
        snap = cls(_state_dir() / f"hermes-memory-snapshot-{scope}.json", home, account)
        try:
            data = json.loads(snap.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return snap
        except (OSError, ValueError) as exc:
            # Unreadable: treat as a first run (stores only, never forgets).
            logger.warning("memory reconcile: snapshot unreadable, starting over: %s", exc)
            return snap
        targets = data.get("targets") if isinstance(data, dict) else None
        if isinstance(targets, dict):
            for target in TARGET_FILES:
                entries = targets.get(target)
                if isinstance(entries, dict):
                    snap.targets[target] = {
                        k: v for k, v in entries.items() if isinstance(v, str)
                    }
        snap.exists = True
        return snap

    def has(self, target: str, text: str) -> bool:
        return entry_key(text) in self.targets.get(target, {})

    def add(self, target: str, text: str) -> None:
        self.targets.setdefault(target, {})[entry_key(text)] = text

    def discard(self, target: str, text: str) -> None:
        self.targets.setdefault(target, {}).pop(entry_key(text), None)

    def save(self) -> None:
        """Atomic write, mode 600. Never raises: a lost save only means the
        next pass redoes some work (deduplicated by the vault)."""
        payload = {"version": SNAPSHOT_VERSION, **self._meta, "targets": self.targets}
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            self.exists = True
        except OSError as exc:
            logger.warning("memory reconcile: snapshot not saved: %s", exc)
            try:
                tmp.unlink()
            except OSError:
                pass


def _file_stat(home: Path) -> Tuple[Any, ...]:
    out = []
    for name in TARGET_FILES.values():
        try:
            st = (home / "memories" / name).stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _builtin_memory_off(home: Path) -> bool:
    from .install_memory_provider import _builtin_memory_off as off, _read_config_text

    return off(_read_config_text(home))


def _pair_changes(removed: List[str], added: List[str]) -> List[Tuple[str, str]]:
    """Pair each removed entry with its most similar new entry (greedy)."""
    pairs: List[Tuple[str, str]] = []
    pool = list(added)
    for old in removed:
        best, best_ratio = None, CHANGE_PAIR_RATIO
        for new in pool:
            ratio = difflib.SequenceMatcher(None, old, new).ratio()
            if ratio >= best_ratio:
                best, best_ratio = new, ratio
        if best is not None:
            pool.remove(best)
            pairs.append((old, best))
    return pairs


def _already_in_vault(state: "PluginState", texts: List[str]) -> List[bool]:
    """Exact-copy check (content fingerprints only cross the wire). Fails open."""
    if not texts:
        return []
    from totalreclaw.agent.loop_runner import run_sync

    try:
        flags = run_sync(state.get_client().find_duplicate_texts(texts))
        if isinstance(flags, list) and len(flags) == len(texts):
            return [bool(f) for f in flags]
    except Exception as exc:
        logger.debug("memory reconcile: duplicate check skipped: %s", exc)
    return [False] * len(texts)


def reconcile(
    state: "PluginState",
    hermes_home: Optional[Path] = None,
    *,
    max_ops: int = MAX_OPS_PER_PASS,
    force: bool = False,
) -> Dict[str, Any]:
    """Run one reconcile pass. Returns the summary it logs. Never raises
    for vault errors (those entries are retried next pass)."""
    from .memory_provider import (
        _find_mirrored_facts,
        _mirror_forget,
        _mirror_remember,
        _same_fact,
    )

    if not state.is_configured():
        return {"skipped": "unconfigured"}
    home = _hermes_home(hermes_home)
    if _builtin_memory_off(home):
        return {"skipped": "builtin_memory_off"}

    snap = Snapshot.load(state, home)
    stat = _file_stat(home)
    if not force and snap.exists and _LAST_CLEAN_STAT.get(snap.path) == stat:
        logger.info("memory reconcile: no changes")
        return {"unchanged": True}

    first_run = not snap.exists
    counts = {"added": 0, "changed": 0, "removed": 0, "forgotten": 0, "skipped": 0, "deferred": 0}
    ops = 0

    def budget() -> bool:
        nonlocal ops
        if ops >= max_ops:
            counts["deferred"] += 1
            return False
        ops += 1
        return True

    for target, name in TARGET_FILES.items():
        current: Dict[str, str] = {}
        for raw in read_entries(home / "memories" / name):
            text = scrub(raw).strip()
            if text:
                current.setdefault(entry_key(text), text)
        previous = snap.targets.get(target, {})
        added = [t for k, t in current.items() if k not in previous]
        removed = [] if first_run else [t for k, t in previous.items() if k not in current]

        flagged = [t for t in added if is_flagged(t)]
        counts["skipped"] += len(flagged)
        added = [t for t in added if t not in flagged]

        pairs = _pair_changes(removed, added)
        paired_new = {new for _old, new in pairs}
        paired_old = {old for old, _new in pairs}
        added = [t for t in added if t not in paired_new]
        removed = [t for t in removed if t not in paired_old]

        for old, new in pairs:
            if not budget():
                continue
            old_ids: List[str] = []
            if _same_fact(old, new):
                try:
                    old_ids = _find_mirrored_facts(state, old)
                except Exception as exc:
                    logger.debug("memory reconcile: lookup failed: %s", exc)
                    counts["deferred"] += 1
                    continue
            try:
                stored = _mirror_remember(state, {"text": new})
            except Exception as exc:
                logger.debug("memory reconcile: store failed: %s", exc)
                stored = False
            if not stored:
                counts["deferred"] += 1
                continue
            counts["changed"] += 1
            counts["forgotten"] += _mirror_forget(state, old_ids)
            snap.discard(target, old)
            snap.add(target, new)
            snap.save()

        exists = _already_in_vault(state, added)
        for text, in_vault in zip(added, exists):
            if in_vault:
                counts["skipped"] += 1
                snap.add(target, text)
                continue
            if not budget():
                continue
            try:
                stored = _mirror_remember(state, {"text": text})
            except Exception as exc:
                logger.debug("memory reconcile: store failed: %s", exc)
                stored = False
            if not stored:
                counts["deferred"] += 1
                continue
            counts["added"] += 1
            snap.add(target, text)
            snap.save()

        # Removed from the profile (usually evicted for space): kept in the
        # vault, only the snapshot moves on. No vault operation.
        for old in removed:
            counts["removed"] += 1
            snap.discard(target, old)
        if removed:
            snap.save()

    snap.save()
    if counts["deferred"]:
        _LAST_CLEAN_STAT.pop(snap.path, None)
    else:
        _LAST_CLEAN_STAT[snap.path] = stat
    # removed = left the Hermes files (kept in TotalReclaw); forgotten =
    # old facts superseded by a same-fact edit.
    logger.info(
        "memory reconcile: first_run=%s added=%d changed=%d removed=%d "
        "forgotten=%d skipped=%d deferred=%d",
        str(first_run).lower(), counts["added"], counts["changed"], counts["removed"],
        counts["forgotten"], counts["skipped"], counts["deferred"],
    )
    return {"first_run": first_run, **counts}


def snapshot_for(state: "PluginState") -> Optional[Snapshot]:
    """The current snapshot, for mirrored writes to keep it in step. None on
    any error (the mirror then works without it)."""
    try:
        return Snapshot.load(state)
    except Exception as exc:
        logger.debug("memory reconcile: snapshot unavailable: %s", exc)
        return None
