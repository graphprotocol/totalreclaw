"""Keep auto-extraction from re-creating facts the user forgot (internal#605, #640).

``totalreclaw_forget`` writes an on-chain tombstone. Auto-extraction keeps
running afterwards (every N turns, on Hermes compaction, at session end) and
reads text that still restates the forgotten fact: the turn that first said
it, the forget turn itself, or an assistant answer built from the host's own
transcript search. The dedup context only holds active facts, so nothing told
the write path that the user deleted this, and the fact came back.

This module holds two pieces:

* :class:`ForgetLedger`: the facts the user explicitly forgot, kept on this
  machine in one file encrypted with the vault's encryption key
  (XChaCha20-Poly1305, the same cipher as the on-chain blobs). Nothing is
  stored in plaintext and nothing new goes on-chain.
* :func:`match_forgotten` / :func:`is_forget_request`: the pure write-time
  checks. They call ``totalreclaw_core`` (``forget_guard`` module) when the
  installed core has them and otherwise run the Python port below, which
  ``tests/parity/fixtures/forget-guard-v1.json`` keeps byte-for-byte in step.

Resolution stays at write time and client-side. Recall never reads the ledger.

What clears an entry: an explicit ``totalreclaw_remember`` of the same fact
(the user wants it back). Auto-extraction never clears it.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

#: Minimum cosine for a ``subset`` match (the candidate adds no content word).
SUBSET_MIN_COSINE = 0.70
#: Minimum cosine for a ``superset`` match (the candidate keeps every content word).
SUPERSET_MIN_COSINE = 0.80
#: Minimum cosine for an ``anchor`` match (2+ shared names / products / places).
ANCHOR_MIN_COSINE = 0.80

#: Mirrors ``totalreclaw_core::forget_guard::STOPWORDS``. Keep sorted.
STOPWORDS = frozenset((
    "about", "again", "all", "also", "and", "any", "anymore", "are", "asked",
    "assistant", "been", "being", "but", "called", "can", "considered",
    "could", "currently", "delete", "deleted", "did", "does", "doing", "done",
    "erase", "erased", "for", "forget", "forgot", "forgotten", "from", "had",
    "has", "have", "her", "him", "his", "into", "its", "just", "know", "knows",
    "like", "liked", "likes", "longer", "memories", "memory", "mentioned",
    "more", "most", "named", "not", "noted", "now", "only", "onto", "our",
    "outdated", "over", "prefer", "preferred", "prefers", "previously",
    "really", "record", "records", "remember", "remembered", "removal",
    "remove", "removed", "said", "same", "say", "says", "she", "should",
    "some", "still", "store", "stored", "such", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "think", "thinks", "this",
    "those", "today", "told", "too", "use", "used", "user", "users", "uses",
    "using", "very", "want", "wanted", "wants", "was", "were", "what", "when",
    "where", "which", "who", "will", "with", "would", "you", "your",
))

#: Mirrors ``totalreclaw_core::forget_guard::FORGET_REQUEST_PATTERNS``.
_FORGET_REQUEST_PATTERNS = (
    r"\b(?:asked|asks|ask|wants|wanted|want|requested|requests|request|told|tells|instructed|chose|decided)\b[^.;]{0,40}?\bto\s+(?:forget|unlearn)\b",
    r"\b(?:forget|forgot|forgotten|delete|deleted|remove|removed|erase|erased|purge|purged|wipe|wiped|drop|dropped|clear|cleared)\b[^.;]{0,60}?\bfrom\s+(?:the\s+|your\s+|my\s+|its\s+|their\s+)?(?:memory|memories|vault|records)\b",
    r"\b(?:that|this|the)\s+(?:memory|memories|record|entry|fact)\b[^.;]{0,40}?\b(?:forgotten|deleted|removed|erased|purged|wiped)\b",
    r"\b(?:no\s+longer|doesn['’]?t|does\s+not|don['’]?t|do\s+not)\s+wants?\b[^.;]{0,40}?\b(?:remembered|stored|saved|kept\s+in\s+memory)\b",
    r"\b(?:asked|requested|wants|wanted)\b[^.;]{0,30}?\b(?:removal|deletion|erasure)\s+of\b",
)
_FORGET_REQUEST_RES = tuple(re.compile(p, re.IGNORECASE) for p in _FORGET_REQUEST_PATTERNS)


def _core():
    try:
        import totalreclaw_core
    except ImportError:  # pragma: no cover - core is a hard dependency
        return None
    return totalreclaw_core


# ---------------------------------------------------------------------------
# Pure checks (Python port of totalreclaw_core::forget_guard)
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.category(c).startswith("M")).lower()


def _normalize_text(text: str) -> str:
    core = _core()
    fn = getattr(core, "normalize_text", None) if core else None
    if fn is not None:
        return fn(text)
    return " ".join(unicodedata.normalize("NFC", text).lower().split())


def _tokens_and_anchors(text: str) -> tuple[set[str], set[str]]:
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.category(c).startswith("M")
    )
    tokens: set[str] = set()
    anchors: set[str] = set()
    cur: list[str] = []
    for c in stripped + " ":
        if c.isalnum():
            cur.append(c)
            continue
        if cur:
            word = "".join(cur)
            cur = []
            lower = word.lower()
            if len(lower) < 3 or lower in STOPWORDS:
                continue
            if word[0].isupper() or any(ch.isnumeric() for ch in word):
                anchors.add(lower)
            tokens.add(lower)
    return tokens, anchors


def guard_tokens(text: str) -> list[str]:
    """Content tokens of *text*, sorted and unique (see the core module docs)."""
    return sorted(_tokens_and_anchors(text)[0])


def guard_anchors(text: str) -> list[str]:
    """Anchor tokens of *text*: content tokens whose word starts with a
    capital letter or contains a digit. Sorted and unique."""
    return sorted(_tokens_and_anchors(text)[1])


def _cosine(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    if not a or len(a) != len(b):
        return None
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return None if denom == 0.0 else dot / denom


def _py_match_forgotten(
    text: str, embedding: Optional[Sequence[float]], entries: Sequence[dict]
) -> Optional[dict]:
    if not text or not text.strip() or not entries:
        return None
    norm = _normalize_text(text)
    cand = set(guard_tokens(text))
    for entry in entries:
        etext = entry.get("text") or ""
        eemb = entry.get("embedding")
        cos = _cosine(embedding, eemb) if embedding and eemb else None
        if etext.strip() and _normalize_text(etext) == norm:
            return {"id": entry.get("id", ""), "reason": "exact", "cosine": cos}
        if cos is None:
            continue
        ent, anchors = _tokens_and_anchors(etext)
        if not ent or not cand or not (ent & cand):
            continue
        if cand <= ent and cos >= SUBSET_MIN_COSINE:
            return {"id": entry.get("id", ""), "reason": "subset", "cosine": cos}
        if ent <= cand and cos >= SUPERSET_MIN_COSINE:
            return {"id": entry.get("id", ""), "reason": "superset", "cosine": cos}
        shared = len(anchors & cand)
        if len(anchors) >= 2 and shared >= 2 and shared * 2 >= len(anchors) and cos >= ANCHOR_MIN_COSINE:
            return {"id": entry.get("id", ""), "reason": "anchor", "cosine": cos}
    return None


def match_forgotten(
    text: str, embedding: Optional[Sequence[float]], entries: Sequence[dict]
) -> Optional[dict]:
    """Return ``{"id", "reason", "cosine"}`` for the first forgotten entry that
    *text* restates, or ``None``.

    *entries* are ``{"id", "text", "embedding"?}`` dicts. Uses the core binding
    when available, else the Python port.
    """
    if not entries:
        return None
    core = _core()
    fn = getattr(core, "match_forgotten", None) if core else None
    if fn is not None:
        try:
            emb_json = json.dumps(list(embedding)) if embedding else "null"
            out = fn(text or "", emb_json, json.dumps(list(entries)))
            return json.loads(out)
        except Exception as e:  # pragma: no cover - defensive; fall back
            logger.debug("core match_forgotten failed, using Python port: %s", e)
    return _py_match_forgotten(text, embedding, entries)


def is_forget_request(text: str) -> bool:
    """True when *text* is a note about deleting a memory ("User asked to
    forget that ...") rather than a fact about the user."""
    if not isinstance(text, str) or not text.strip():
        return False
    core = _core()
    fn = getattr(core, "is_forget_request", None) if core else None
    if fn is not None:
        try:
            return bool(fn(text))
        except Exception as e:  # pragma: no cover - defensive; fall back
            logger.debug("core is_forget_request failed, using Python port: %s", e)
    folded = _fold(text)
    return any(r.search(folded) for r in _FORGET_REQUEST_RES)


# ---------------------------------------------------------------------------
# Encrypted ledger
# ---------------------------------------------------------------------------


def _state_dir() -> Path:
    override = os.environ.get("TOTALRECLAW_STATE_DIR")
    return Path(override) if override else Path.home() / ".totalreclaw"


class ForgetLedger:
    """Facts the user explicitly forgot, encrypted at rest on this machine.

    One file per account: ``<state_dir>/forgotten-<eoa>.enc`` (the EOA address
    is public). The file body is the base64 XChaCha20-Poly1305 ciphertext of
    ``{"v": 1, "entries": [{"id", "text", "embedding", "at"}]}`` under the
    vault encryption key. The decrypted list lives only in process memory.

    Every method is best-effort: an unreadable or undecryptable file reads as
    empty, and a failed write is logged, never raised. Losing the ledger only
    brings back the pre-fix behavior; it can never block a write the user
    asked for.
    """

    VERSION = 1
    MAX_ENTRIES = 500

    def __init__(self, path: Path, encryption_key: bytes) -> None:
        self._path = Path(path)
        self._key = encryption_key
        self._entries: Optional[list[dict]] = None
        self._mtime: Optional[float] = None

    @classmethod
    def for_owner(cls, owner: str, encryption_key: bytes) -> "ForgetLedger":
        name = re.sub(r"[^0-9a-z]", "", (owner or "default").lower()) or "default"
        return cls(_state_dir() / f"forgotten-{name}.enc", encryption_key)

    @property
    def path(self) -> Path:
        return self._path

    # -- persistence --------------------------------------------------------

    def _load(self) -> list[dict]:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._entries, self._mtime = [], None
            return self._entries
        except OSError as e:
            logger.debug("forget ledger: cannot stat %s: %s", self._path, e)
            return self._entries or []
        if self._entries is not None and self._mtime == mtime:
            return self._entries
        entries: list[dict] = []
        try:
            from .crypto import decrypt

            doc = json.loads(decrypt(self._path.read_text(encoding="ascii").strip(), self._key))
            raw = doc.get("entries") if isinstance(doc, dict) else None
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict) and e.get("id")]
        except Exception as e:
            logger.warning("forget ledger: %s is unreadable; treating it as empty (%s)", self._path, type(e).__name__)
        self._entries, self._mtime = entries, mtime
        return entries

    def _save(self, entries: list[dict]) -> None:
        from .crypto import encrypt

        entries = entries[-self.MAX_ENTRIES:]
        body = encrypt(json.dumps({"v": self.VERSION, "entries": entries}, separators=(",", ":")), self._key)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(body)
            os.replace(tmp, self._path)
            self._entries = entries
            try:
                self._mtime = self._path.stat().st_mtime
            except OSError:
                self._mtime = None
        except OSError as e:
            logger.warning("forget ledger: cannot write %s: %s", self._path, e)

    # -- API ----------------------------------------------------------------

    def entries(self) -> list[dict]:
        return list(self._load())

    def __len__(self) -> int:
        return len(self._load())

    def record(self, fact_id: str, text: str, embedding: Optional[Sequence[float]] = None) -> None:
        """Remember that the user forgot *fact_id* (whose content is *text*)."""
        if not fact_id or not text or not text.strip():
            return
        entries = [e for e in self._load() if e.get("id") != fact_id]
        entry: dict[str, Any] = {"id": fact_id, "text": text, "at": int(time.time())}
        if embedding:
            entry["embedding"] = [round(float(x), 6) for x in embedding]
        entries.append(entry)
        self._save(entries)

    def match(self, text: str, embedding: Optional[Sequence[float]] = None) -> Optional[dict]:
        """The forgotten entry *text* restates, or ``None``."""
        entries = self._load()
        if not entries:
            return None
        return match_forgotten(text, embedding, entries)

    def release(self, text: str, embedding: Optional[Sequence[float]] = None) -> int:
        """Drop every entry *text* restates (an explicit remember brings the
        fact back). Returns how many entries were dropped."""
        entries = self._load()
        if not entries:
            return 0
        kept = [e for e in entries if match_forgotten(text, embedding, [e]) is None]
        dropped = len(entries) - len(kept)
        if dropped:
            self._save(kept)
        return dropped
