"""Store-time write gate shared by every write path (internal #632, #501).

Before a fact is written, :func:`check_store` asks "is this already stored?"
against two candidate sets:

(a) facts the subgraph returns for the new text (topical recall, with
    embeddings), and
(b) a process-local :class:`RecentWriteLedger` of facts written in the last
    few minutes. The subgraph lags the chain by up to a minute or two, so
    back-to-back writes cannot see each other through (a) alone.

The decision itself is core's ``decide_write`` (``rust/totalreclaw-core/src/
write_gate.rs``): a normalized exact-text match, then an embedding cosine
match at 0.85. This module adds the pure-Python fallback for an installed
core that predates it (same rules, checked against the same parity fixture),
the ledger, and the candidate plumbing.

Modes (who is writing):

* ``auto`` — auto-extraction. Exact or near duplicate of an unpinned fact →
  skip. A near duplicate of a pinned fact is stored: the extraction prompt
  asks the LLM to ADD new information about a pin rather than update it.
* ``explicit`` — the user's explicit ``remember``. Exact → "already stored";
  near duplicate → "confirm" (nothing written; the agent sees the existing
  fact and can retry with ``force``).
* ``exact_only`` — USER.md mirror, reconcile and forced remember: only exact
  duplicates are skipped. With Harrier, value changes score like paraphrases
  ("lives in Lisbon" vs "lives in Porto" = 0.898), and these paths have no
  LLM to tell them apart.

Nothing here replaces or tombstones a fact, so a duplicate of a pinned fact
never touches the pin.

The ledger lives in memory only and is never written to disk. It holds the
plaintext the process just wrote (the same text the caller already holds), and
dies with the process. One ledger per client object.
"""
from __future__ import annotations

import array
import json
import logging
import math
import struct
import threading
import time
import unicodedata
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

#: Cosine threshold (core ``STORE_DEDUP_COSINE_THRESHOLD``).
STORE_DEDUP_THRESHOLD = 0.85

#: Minimum cosine for the explicit-mode ``contained`` rule (core ``CONTAINED_MIN_COSINE``).
CONTAINED_MIN_COSINE = 0.65

#: How long a written fact stays in the ledger. Covers subgraph indexing lag
#: (normally under a minute) with a wide margin.
LEDGER_TTL_SECONDS = 15 * 60

#: Upper bound on ledger entries per client.
LEDGER_MAX_ENTRIES = 256

#: Topical candidates fetched from the subgraph per gated write.
GATE_TOPICAL_TOP_K = 20

MODES = ("auto", "explicit", "exact_only")


# ---------------------------------------------------------------------------
# Decision (core first, pure-Python fallback)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateMatch:
    fact_id: str
    text: str
    kind: str  # "exact" | "near_duplicate" | "contained"
    similarity: float
    pinned: bool


@dataclass(frozen=True)
class GateDecision:
    action: str  # "store" | "skip" | "confirm"
    match: Optional[GateMatch] = None

    @property
    def store(self) -> bool:
        return self.action == "store"


_WHITE_SPACE = frozenset(
    "\t\n\x0b\x0c\r \x85\xa0 "
    "           "
    "    　"
)
_EDGE_PUNCTUATION = frozenset(
    ".,;:!?'\"`()[]{}<>-*_~•¡¿«»‹›"
    "。，；：！？、"
)
_STRIP = _WHITE_SPACE | _EDGE_PUNCTUATION
_FOLD = {
    **{c: "'" for c in "‘’‛′´"},
    **{c: '"' for c in "“”„″"},
    **{c: "-" for c in "‐‑‒–—―"},
    "…": "...",
}


def _core():
    try:
        import totalreclaw_core

        return totalreclaw_core
    except ImportError:  # pragma: no cover - core is a hard dependency
        return None


def _py_normalize(text: str) -> str:
    s = unicodedata.normalize("NFC", text or "").lower()
    s = "".join(_FOLD.get(c, c) for c in s)
    out: list[str] = []
    prev_ws = False
    for c in s:
        if c in _WHITE_SPACE:
            if not prev_ws:
                out.append(" ")
            prev_ws = True
        else:
            out.append(c)
            prev_ws = False
    s = "".join(out)
    start, end = 0, len(s)
    while start < end and s[start] in _STRIP:
        start += 1
    while end > start and s[end - 1] in _STRIP:
        end -= 1
    return s[start:end]


def normalize_fact_text(text: str) -> str:
    """Normalize a fact for the exact-duplicate check (core ``normalize_fact_text``)."""
    core = _core()
    fn = getattr(core, "normalize_fact_text", None) if core else None
    if fn is not None:
        return fn(text or "")
    return _py_normalize(text)


def _f32(xs) -> list[float]:
    return [struct.unpack("f", struct.pack("f", float(x)))[0] for x in xs]


def _cosine(a, b) -> float:
    a, b = _f32(a), _f32(b)
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na * nb == 0 else dot / (na * nb)


def _py_decide(text, embedding, candidates, threshold, mode) -> dict:
    wanted = _py_normalize(text)
    if wanted:
        exact = None
        for c in candidates:
            if not c.get("id") or _py_normalize(c.get("text") or "") != wanted:
                continue
            if exact is None or (c.get("pinned") and not exact.get("pinned")):
                exact = c
            if c.get("pinned"):
                break
        if exact is not None:
            return {"action": "skip", "match": _match_dict(exact, "exact", 1.0)}
    if mode == "exact_only" or not embedding:
        return {"action": "store", "match": None}
    best, best_sim = None, 0.0
    for c in candidates:
        if not c.get("id") or (mode == "auto" and c.get("pinned")):
            continue
        emb = c.get("embedding")
        if not emb or len(emb) != len(embedding):
            continue
        sim = _cosine(embedding, emb)
        if sim >= threshold and (best is None or sim > best_sim):
            best, best_sim = c, sim
    if best is None and mode == "explicit":
        # Lazy import: forget_guard must not import write_gate at module load.
        from .forget_guard import _adds_relation_word, _tokens_and_anchors

        new_tokens, new_anchors = _tokens_and_anchors(text)
        cbest, csim = None, 0.0
        for c in candidates:
            if not c.get("id"):
                continue
            emb = c.get("embedding")
            if not emb or len(emb) != len(embedding):
                continue
            sim = _cosine(embedding, emb)
            if sim < CONTAINED_MIN_COSINE or (cbest is not None and sim <= csim):
                continue
            existing, _ = _tokens_and_anchors(c.get("text") or "")
            if (
                new_tokens
                and len(new_tokens) < len(existing)
                and new_anchors <= existing
                and (new_tokens <= existing or new_anchors)
                and not _adds_relation_word(new_tokens, existing)
                and not _adds_relation_word(existing, new_tokens)
            ):
                cbest, csim = c, sim
        if cbest is not None:
            return {"action": "confirm", "match": _match_dict(cbest, "contained", csim)}
    if best is None:
        return {"action": "store", "match": None}
    return {
        "action": "skip" if mode == "auto" else "confirm",
        "match": _match_dict(best, "near_duplicate", best_sim),
    }


def _match_dict(c: dict, kind: str, sim: float) -> dict:
    return {
        "fact_id": c["id"],
        "text": c.get("text") or "",
        "kind": kind,
        "similarity": sim,
        "pinned": bool(c.get("pinned")),
    }


def _clean_candidates(candidates: Iterable[Any]) -> list[dict]:
    out: list[dict] = []
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        emb = c.get("embedding")
        out.append({
            "id": str(c.get("id") or ""),
            "text": str(c.get("text") or ""),
            "embedding": [float(x) for x in emb] if emb else None,
            "pinned": bool(c.get("pinned")),
        })
    return out


def decide_write(
    text: str,
    embedding: Optional[list[float]],
    candidates: Iterable[dict],
    *,
    mode: str = "auto",
    threshold: float = STORE_DEDUP_THRESHOLD,
) -> GateDecision:
    """Decide whether ``text`` should be written. Pure; no I/O.

    ``candidates`` are ``{"id", "text", "embedding"?, "pinned"?}`` dicts. Uses
    core ``decide_write`` when the installed wheel has it, else the identical
    Python rules.
    """
    if mode not in MODES:
        raise ValueError(f"decide_write: unknown mode {mode!r}")
    cands = _clean_candidates(candidates)
    emb = [float(x) for x in embedding] if embedding else []
    core = _core()
    fn = getattr(core, "decide_write", None) if core else None
    if fn is not None:
        raw = json.loads(fn(text or "", json.dumps(emb), json.dumps(cands), mode, threshold))
    else:
        raw = _py_decide(text or "", emb, cands, threshold, mode)
    m = raw.get("match")
    match = GateMatch(
        fact_id=m["fact_id"], text=m["text"], kind=m["kind"],
        similarity=float(m["similarity"]), pinned=bool(m["pinned"]),
    ) if m else None
    return GateDecision(action=raw["action"], match=match)


# ---------------------------------------------------------------------------
# Recent-write ledger
# ---------------------------------------------------------------------------

class RecentWriteLedger:
    """Facts this process wrote in the last ``ttl_seconds``, plus ids it
    recently forgot. In memory only; thread-safe (the Hermes mirror worker and
    the hook thread share it)."""

    def __init__(
        self,
        ttl_seconds: float = LEDGER_TTL_SECONDS,
        max_entries: int = LEDGER_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
        self._forgotten: "OrderedDict[str, float]" = OrderedDict()

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        for store in (self._entries, self._forgotten):
            for key in list(store):
                ts = store[key][0] if store is self._entries else store[key]
                if ts > cutoff:
                    break
                del store[key]
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
        while len(self._forgotten) > self._max:
            self._forgotten.popitem(last=False)

    def record(self, fact_id: str, text: str, embedding: Optional[list[float]], pinned: bool = False) -> None:
        if not fact_id or not text:
            return
        now = self._clock()
        with self._lock:
            self._entries.pop(fact_id, None)
            self._entries[fact_id] = (now, {
                "id": fact_id,
                "text": text,
                "embedding": list(embedding) if embedding else None,
                "pinned": bool(pinned),
            })
            self._forgotten.pop(fact_id, None)
            self._prune(now)

    def forget(self, fact_id: str) -> None:
        if not fact_id:
            return
        now = self._clock()
        with self._lock:
            self._entries.pop(fact_id, None)
            self._forgotten.pop(fact_id, None)
            self._forgotten[fact_id] = now
            self._prune(now)

    def rename(self, old_id: str, new_id: str, pinned: Optional[bool] = None) -> None:
        """A rewrite (pin / unpin / retype / set_scope) tombstoned ``old_id``
        and wrote ``new_id`` with the same text."""
        now = self._clock()
        with self._lock:
            item = self._entries.pop(old_id, None)
            self._forgotten.pop(old_id, None)
            self._forgotten[old_id] = now
            if item is not None and new_id:
                entry = dict(item[1], id=new_id)
                if pinned is not None:
                    entry["pinned"] = bool(pinned)
                self._entries[new_id] = (now, entry)
            self._prune(now)

    def candidates(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            return [dict(e) for _, e in self._entries.values()]

    def forgotten_ids(self) -> set[str]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            return set(self._forgotten)


_LEDGERS: "weakref.WeakKeyDictionary[Any, RecentWriteLedger]" = weakref.WeakKeyDictionary()
_LEDGERS_LOCK = threading.Lock()


def ledger_for(client: Any) -> RecentWriteLedger:
    """The ledger for ``client`` (created on first use)."""
    with _LEDGERS_LOCK:
        try:
            led = _LEDGERS.get(client)
            if led is None:
                led = RecentWriteLedger()
                _LEDGERS[client] = led
            return led
        except TypeError:  # unhashable / not weak-referenceable: no memory
            return RecentWriteLedger()


# ---------------------------------------------------------------------------
# Keyed-write record (internal#573, pin / retype guard)
# ---------------------------------------------------------------------------

class KeyedWriteRecord:
    """Ids of facts whose payload THIS process encoded (keyed by construction).

    The pin / retype index-scheme guard skips its subgraph query for these
    ids: a freshly written fact may not be indexed yet, and the query would
    otherwise get ``fact: null`` and refuse. Ids only (no text, no keys); in
    memory only; thread-safe; same TTL and cap as the write ledger. Kept
    apart from :class:`RecentWriteLedger` because that ledger also records
    ops recovered from the on-disk pending queue, whose payloads an older
    unkeyed install may have encoded.
    """

    def __init__(
        self,
        ttl_seconds: float = LEDGER_TTL_SECONDS,
        max_entries: int = LEDGER_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._ids: "OrderedDict[str, float]" = OrderedDict()

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        for key in list(self._ids):
            if self._ids[key] > cutoff:
                break
            del self._ids[key]
        while len(self._ids) > self._max:
            self._ids.popitem(last=False)

    def add(self, fact_id: Optional[str]) -> None:
        if not fact_id or not isinstance(fact_id, str):
            return
        now = self._clock()
        with self._lock:
            self._ids.pop(fact_id, None)
            self._ids[fact_id] = now
            self._prune(now)

    def add_many(self, fact_ids: Any) -> None:
        for fid in list(fact_ids or []):
            self.add(fid)

    def discard(self, fact_id: Optional[str]) -> None:
        if not fact_id:
            return
        with self._lock:
            self._ids.pop(fact_id, None)

    def contains(self, fact_id: Optional[str]) -> bool:
        if not fact_id:
            return False
        now = self._clock()
        with self._lock:
            self._prune(now)
            return fact_id in self._ids


_KEYED_WRITES: "weakref.WeakKeyDictionary[Any, KeyedWriteRecord]" = weakref.WeakKeyDictionary()
_KEYED_WRITES_LOCK = threading.Lock()


def keyed_writes_for(client: Any) -> KeyedWriteRecord:
    """The keyed-write record for ``client`` (created on first use).

    One record per client object, so ``TotalReclaw.from_bundle`` (which skips
    ``__init__``) gets one too. A client that cannot be weak-referenced gets
    a throwaway record, so the guard simply queries (fail closed).
    """
    with _KEYED_WRITES_LOCK:
        try:
            rec = _KEYED_WRITES.get(client)
            if rec is None:
                rec = KeyedWriteRecord()
                _KEYED_WRITES[client] = rec
            return rec
        except TypeError:
            return KeyedWriteRecord()


# ---------------------------------------------------------------------------
# Cached vault view (used when relay reads are paused)
# ---------------------------------------------------------------------------

VIEW_CACHE_TTL_SECONDS = 6 * 3600
VIEW_CACHE_MAX_ENTRIES = 300


class VaultViewCache:
    """Facts (with embeddings) this process fetched or wrote, kept for
    ``ttl_seconds`` (``VIEW_CACHE_TTL_SECONDS`` by default). When a read pause
    makes the topical fetch fail, the gate checks these as well as the ledger.
    In memory only, never persisted; thread-safe.

    Embeddings are stored as float32 by design (``array('f')``; a Python float
    list costs about 8x more memory), so they round-trip with float32
    precision.

    Limits: blind auto-extraction (past the deferral cap) still decides on the
    ledger only, not on this cache. A forget or edit made on another device
    cannot be seen, so a stale entry can stay for up to the TTL during a
    pause.
    """

    def __init__(
        self,
        ttl_seconds: float = VIEW_CACHE_TTL_SECONDS,
        max_entries: int = VIEW_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        for key in list(self._entries):
            if self._entries[key][0] > cutoff:
                break
            del self._entries[key]
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def update(self, candidates: Iterable[dict]) -> None:
        now = self._clock()
        with self._lock:
            for c in candidates or []:
                if not isinstance(c, dict):
                    continue
                fid = str(c.get("id") or "")
                text = str(c.get("text") or "")
                emb = c.get("embedding")
                if not fid or not text or emb is None or len(emb) == 0:
                    continue
                self._entries.pop(fid, None)
                self._entries[fid] = (now, {
                    "id": fid,
                    "text": text,
                    "embedding": array.array("f", emb),
                    "pinned": bool(c.get("pinned")),
                })
            self._prune(now)

    def forget(self, fact_id: str) -> None:
        if not fact_id:
            return
        with self._lock:
            self._entries.pop(str(fact_id), None)

    def rename(self, old_id: str, new_id: str, pinned: Optional[bool] = None) -> None:
        """A rewrite (pin / unpin / retype / set_scope) tombstoned ``old_id``
        and wrote ``new_id`` with the same text."""
        if not old_id:
            return
        now = self._clock()
        with self._lock:
            item = self._entries.pop(str(old_id), None)
            if item is not None and new_id:
                entry = dict(item[1], id=str(new_id))
                if pinned is not None:
                    entry["pinned"] = bool(pinned)
                self._entries.pop(str(new_id), None)
                self._entries[str(new_id)] = (now, entry)
            self._prune(now)

    def candidates(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            self._prune(now)
            return [
                dict(e, embedding=list(e["embedding"]), section="cached")
                for _, e in self._entries.values()
            ]


_VIEW_CACHES: "weakref.WeakKeyDictionary[Any, VaultViewCache]" = weakref.WeakKeyDictionary()
_VIEW_CACHES_LOCK = threading.Lock()


def view_cache_for(client: Any) -> VaultViewCache:
    """The cached vault view for ``client`` (created on first use)."""
    with _VIEW_CACHES_LOCK:
        try:
            cache = _VIEW_CACHES.get(client)
            if cache is None:
                cache = VaultViewCache()
                _VIEW_CACHES[client] = cache
            return cache
        except TypeError:  # unhashable / not weak-referenceable: no memory
            return VaultViewCache()


def record_write(client: Any, fact_id: Optional[str], text: str, embedding: Optional[list[float]]) -> None:
    """Remember a confirmed write so the next gate check sees it before the
    subgraph does."""
    if fact_id:
        ledger_for(client).record(str(fact_id), text, embedding)
        if text and embedding:
            try:  # outlives the ledger TTL during a long read pause
                view_cache_for(client).update(
                    [{"id": str(fact_id), "text": text, "embedding": embedding}]
                )
            except Exception:  # noqa: BLE001 - never fail a write over the cache
                pass


# ---------------------------------------------------------------------------
# Candidate fetch + gate
# ---------------------------------------------------------------------------

def merge_candidates(client: Any, *sections: Iterable[dict], exclude_ids: Iterable[str] = ()) -> list[dict]:
    """``sections`` followed by the ledger, deduplicated by id, without
    ``exclude_ids`` and without ids the ledger saw forgotten (the subgraph
    keeps listing a tombstoned fact until it indexes the tombstone)."""
    led = ledger_for(client)
    drop = set(exclude_ids or ()) | led.forgotten_ids()
    seen: set[str] = set()
    out: list[dict] = []
    for section in (*sections, led.candidates()):
        for c in section or []:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "")
            if not cid or cid in drop or cid in seen:
                continue
            seen.add(cid)
            out.append(c)
    return out


def results_to_candidates(results: Iterable[Any]) -> list[dict]:
    """``RerankerResult`` objects → candidate dicts."""
    out: list[dict] = []
    for r in results or []:
        fid = getattr(r, "id", None)
        text = getattr(r, "text", None)
        if fid and text:
            out.append({
                "id": str(fid),
                "text": str(text),
                "embedding": list(getattr(r, "embedding", None) or []) or None,
                "pinned": bool(getattr(r, "pinned", False)),
            })
    return out


async def fetch_topical_candidates(
    client: Any, text: str, embedding: Optional[list[float]], top_k: int = GATE_TOPICAL_TOP_K,
) -> list[dict]:
    """Subgraph facts related to ``text``, with embeddings. Raises on failure."""
    results = await client.recall(
        text, query_embedding=embedding, top_k=top_k, include_embeddings=True,
    )
    return results_to_candidates(results)


async def check_store(
    client: Any,
    text: str,
    embedding: Optional[list[float]],
    *,
    mode: str,
    exclude_ids: Iterable[str] = (),
) -> GateDecision:
    """Gate one write: fetch topical candidates, add the ledger, decide.

    A failed subgraph read (reads paused, network) degrades to the ledger
    plus the cached view of facts this process saw
    (``VIEW_CACHE_TTL_SECONDS``), so writes are never blocked by a read
    failure. Interpreter shutdown errors propagate.
    """
    from .agent.loop_runner import InterpreterShutdownError, is_interpreter_shutdown_error

    topical: list[dict] = []
    try:
        topical = await fetch_topical_candidates(client, text, embedding)
        try:
            view_cache_for(client).update(topical)
        except Exception:  # noqa: BLE001 - the cache must never break a write
            pass
    except InterpreterShutdownError:
        raise
    except Exception as e:  # noqa: BLE001 - best effort
        if is_interpreter_shutdown_error(e):
            raise InterpreterShutdownError(str(e)) from e
        from .relay import RelayReadBlocked

        # Decide on what this process already saw (last 6 h) plus the ledger.
        topical = view_cache_for(client).candidates()
        if isinstance(e, RelayReadBlocked):
            logger.info(
                "vault view degraded: reads paused (write gate: ledger + %d cached)", len(topical),
            )
        else:
            logger.debug(
                "write gate: topical candidate fetch failed (%s); ledger + %d cached",
                type(e).__name__, len(topical),
            )
    candidates = merge_candidates(client, topical, exclude_ids=exclude_ids)
    decision = decide_write(text, embedding, candidates, mode=mode)
    if decision.match is not None:
        logger.info(
            "write gate: action=%s kind=%s sim=%.3f pinned=%s mode=%s",
            decision.action, decision.match.kind, decision.match.similarity,
            decision.match.pinned, mode,
        )
    return decision
