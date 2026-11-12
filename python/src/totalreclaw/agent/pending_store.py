"""Facts waiting to be stored after the relay refused them for quota (#747).

When the relay refuses an auto-extracted batch because the account's monthly
write quota is used up, the facts are kept here instead of being dropped.
They are stored later, once a write succeeds again (the allowance reset or the
user upgraded): see ``agent.lifecycle.drain_pending_stores``.

This is a different queue from ``agent.pending_drain``, which holds raw
*messages* whose extraction never ran (interpreter-shutdown race, #148) and
re-runs the LLM on them. Facts here are already extracted, filtered and
gated; draining them needs no LLM call.

File layout
-----------
One file per account: ``<state_dir>/pending-store-<eoa>.enc`` (the EOA is
public). The body is the base64 XChaCha20-Poly1305 ciphertext, under the
vault encryption key, of::

    {"v": 1, "entries": [{"queued_at": "<iso8601>", "reason": "write_quota",
                          "attempts": 0, "fact": {<remember_batch fact dict>}}]}

Same pattern as the forget ledger (``forget_guard.ForgetLedger``): fact text
is never written to disk in plaintext. The fact dict is stored without its
embedding (the drain recomputes it), so the file stays small. At most
:attr:`PendingStoreQueue.MAX_ENTRIES` entries are kept; the oldest go first.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from ..forget_guard import _state_dir

logger = logging.getLogger(__name__)

# Keys copied from a ``remember_batch`` fact dict. ``embedding`` is left out
# on purpose: it is large and deterministic, so the drain recomputes it.
_FACT_KEYS = (
    "text",
    "importance",
    "fact_type",
    "entities",
    "confidence",
    "provenance",
    "scope",
    "reasoning",
    "volatility",
    "extra_metadata",
    "agent_name",
)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _queueable(fact: dict) -> Optional[dict]:
    text = fact.get("text") if isinstance(fact, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None
    return {k: _jsonable(fact[k]) for k in _FACT_KEYS if fact.get(k) is not None}


# One lock per queue file, shared by every ``PendingStoreQueue`` instance in
# the process: auto-extraction can run on the sync-loop thread and on the
# host's thread at once, and add/take/put_back are read-modify-write.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


class PendingStoreQueue:
    """Encrypted, machine-local list of facts waiting to be stored."""

    VERSION = 1
    #: Twice the free tier's monthly allowance. A month of refused
    #: auto-extraction fits; beyond that the oldest facts are dropped.
    MAX_ENTRIES = 500

    def __init__(self, path: Path, encryption_key: bytes) -> None:
        self._path = Path(path)
        self._key = encryption_key
        self._lock = _lock_for(self._path)

    @classmethod
    def for_owner(cls, owner: str, encryption_key: bytes) -> "PendingStoreQueue":
        name = re.sub(r"[^0-9a-z]", "", (owner or "default").lower()) or "default"
        return cls(_state_dir() / f"pending-store-{name}.enc", encryption_key)

    @property
    def path(self) -> Path:
        return self._path

    # -- persistence --------------------------------------------------------

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            from ..crypto import decrypt

            doc = json.loads(decrypt(self._path.read_text(encoding="ascii").strip(), self._key))
        except Exception as e:
            # A corrupt file (the name is the EOA and the key comes from the
            # same credentials, so it is not another account's). Nobody can
            # decrypt it, so treat it as empty; the next write replaces it.
            # Never crash the write path over it.
            logger.warning(
                "pending store: %s is unreadable; ignoring it (%s)",
                self._path, type(e).__name__,
            )
            return []
        raw = doc.get("entries") if isinstance(doc, dict) else None
        if not isinstance(raw, list):
            return []
        return [e for e in raw if isinstance(e, dict) and isinstance(e.get("fact"), dict)]

    def _save(self, entries: list[dict]) -> bool:
        if not entries:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("pending store: cannot remove %s: %s", self._path, e)
                return False
            return True
        from ..crypto import encrypt

        body = encrypt(
            json.dumps({"v": self.VERSION, "entries": entries}, separators=(",", ":"), ensure_ascii=False),
            self._key,
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(body)
            os.replace(tmp, self._path)
            return True
        except OSError as e:
            logger.warning("pending store: cannot write %s: %s", self._path, e)
            return False

    # -- public API ---------------------------------------------------------

    def count(self) -> int:
        return len(self._load())

    def add(self, facts: Iterable[dict], *, reason: str) -> int:
        """Append *facts* (``remember_batch`` dicts). Returns how many were
        written; ``0`` on a disk failure. Over :attr:`MAX_ENTRIES`, the
        oldest entries are dropped (logged)."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        new = []
        for fact in facts:
            q = _queueable(fact)
            if q is not None:
                new.append({"queued_at": now, "reason": reason, "attempts": 0, "fact": q})
        if not new:
            return 0
        with self._lock:
            entries = self._load() + new
            overflow = len(entries) - self.MAX_ENTRIES
            if overflow > 0:
                logger.warning(
                    "pending store: queue full (%d); dropping the %d oldest fact(s)",
                    self.MAX_ENTRIES, overflow,
                )
                entries = entries[overflow:]
            return len(new) if self._save(entries) else 0

    def take(self, limit: int) -> list[dict]:
        """Remove and return up to *limit* entries, oldest first. Hand back
        any that were not stored with :meth:`put_back`."""
        with self._lock:
            entries = self._load()
            if not entries or limit <= 0:
                return []
            taken, rest = entries[:limit], entries[limit:]
            if not self._save(rest):
                return []
            return taken

    def put_back(self, entries: list[dict]) -> None:
        """Return *entries* to the front of the queue, in order."""
        if entries:
            with self._lock:
                self._save((list(entries) + self._load())[: self.MAX_ENTRIES])
