"""Heuristic query-entity extractor (entity-trapdoor read-side).

Pure-stdlib port of the validated TypeScript spike ``extractEntities()``
(``totalreclaw-internal/e2e/retrieval-benchmark/entity-trapdoor.ts``). The
Task 0 asymmetry gate (offline, N=150) approved the heuristic path:
asymmetric collision 30.0% == 76% of symmetric 39.3%.

This module extracts candidate entity-name strings from a free-text query so
they can be matched against entity trapdoors (a retrieval signal). It does
**not** call an LLM and does **not** canonicalize aggressively -- that is the
banked canonicalizer lever (TODO #370), deferred until proven necessary.

Public API:
    extract_query_entities(query: str) -> list[str]

Behavior is a faithful, verbatim port of the spike's five rules:

1. Multi-word capitalized phrases (``[A-Z][a-z]+`` runs of 2-4 words).
2. Single capitalized word ``[A-Z][a-z]{2,}`` not at sentence start.
3. Domain seeds (hard-coded vocabulary that commonly appears in queries).
4. Acronyms (``[A-Z]{2,5}``).
5. Dates (Month-day-year and ISO ``YYYY/M/D``).

All candidates pass through :func:`normalize_entity` (NFC + lowercase +
collapse whitespace + trim) and are de-duplicated preserving insertion order
(matching the JS ``Set`` semantics).
"""

from __future__ import annotations

import re
import unicodedata
from typing import List

__all__ = ["extract_query_entities", "derive_entities_for_write"]


def normalize_entity(name: str) -> str:
    """NFC-normalize, lowercase, collapse runs of whitespace, and trim.

    Mirrors the spike's ``normalizeEntity`` exactly.
    """
    nfc = unicodedata.normalize("NFC", name)
    collapsed = re.sub(r"\s+", " ", nfc)
    return collapsed.lower().strip()


# Deterministic insertion-ordered de-duplication container. A plain ``dict``
# with ``None`` values preserves insertion order on CPython 3.7+ and matches
# the JS ``Set`` + ``Array.from`` semantics used in the spike.
_OrderedSet = dict


STOP = frozenset(
    [
        "i", "my", "me", "mine", "we", "our", "ours", "you", "your", "yours",
        "he", "she", "it", "they", "their", "them",
        "a", "an", "the", "and", "or", "but", "if", "then", "so", "because",
        "as", "of", "at", "by", "for", "with", "about",
        "against", "between", "into", "through", "during", "before", "after",
        "above", "below", "to", "from", "up", "down",
        "in", "out", "on", "off", "over", "under", "again", "further",
        "do", "does", "did", "have", "has", "had",
        "is", "are", "was", "were", "be", "been", "being",
        "this", "that", "these", "those", "what", "which", "who", "whom",
        "whose", "where", "why", "how",
        "all", "any", "both", "each", "few", "more", "most", "other", "some",
        "such",
        "no", "nor", "not", "only", "own", "same", "than", "too", "very",
        "can", "will", "just", "should", "now", "also",
        "one", "two", "three", "first", "second", "third", "last", "next",
        "today", "tomorrow", "yesterday", "week", "month",
        "year", "day", "time", "minute", "hour",
        "quick", "fine", "sure", "okay", "ok", "yes", "yeah", "nope", "please",
        "sorry", "thanks", "thank",
        "really", "actually", "basically", "definitely", "probably", "maybe",
        "perhaps",
    ]
)

DOMAIN_SEEDS = frozenset(
    [
        "gps", "wifi", "vpn", "iphone", "android", "samsung", "google", "apple",
        "tesla", "honda", "toyota", "bmw",
        "kia", "ev4", "ev6", "airbnb", "uber", "spotify", "youtube", "netflix",
        "linkedin", "github", "docker", "kubernetes",
        "wireguard", "postgres", "mysql", "redis", "python", "rust",
        "typescript", "javascript", "solidity",
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday",
        "breakfast", "lunch", "dinner", "workshop", "webinar", "meeting",
        "conference", "class", "course", "training",
        "gym", "run", "hike", "flight", "trip", "vacation", "car", "phone",
        "laptop", "headphones", "tv",
        "recipe", "flour", "sugar", "butter", "eggs", "salt", "pepper",
        "garlic", "ginger", "onion", "tomato", "rice", "pasta",
        "bsl", "mit", "apache", "license", "contract", "agreement",
    ]
)

# --- Rule regexes (ASCII-centric, matching the JS spike). ---
# Python ``re`` literal-range classes such as ``[A-Z]`` / ``[a-z]`` already
# match only ASCII letters; we do NOT set re.UNICODE so the behavior mirrors
# the JS originals byte-for-byte.
_RE_MULTI_CAPS = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b")
_RE_SINGLE_CAPS_WORD = re.compile(r"^[A-Z][a-z]{2,}$")
_RE_SENTENCE_END = re.compile(r"[.!?]$")
_RE_ACRONYM = re.compile(r"\b([A-Z]{2,5})\b")
_RE_DATE_MONTH = re.compile(
    r"\b("
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?"
    r")\b"
)
_RE_DATE_ISO = re.compile(r"\b(\d{4}/\d{1,2}/\d{1,2})\b")

# Full non-alphanumeric strip -- mirrors the JS spike's
# ``tok.replace(/[^A-Za-z0-9]/g, '')``. ALL non-alphanumerics are removed,
# including interior punctuation, so "Michael's" -> "Michaels" and
# "Node.js" -> "Nodejs" and these DO pass the ``[A-Z][a-z]{2,}`` single-cap
# test. Fidelity to the spike matters: the Task 0 gate measured this exact
# behavior, so the port must match it for the collision numbers to transfer.
_RE_NONALNUM = re.compile(r"[^A-Za-z0-9]")


def extract_query_entities(query: str, *, use_domain_seeds: bool = True) -> List[str]:
    """Extract candidate entity-name strings from a free-text query.

    ``use_domain_seeds=False`` skips Rule 3 (the domain-seed vocabulary). The
    write path sets it: seeds are meant to widen recall, but stored as
    entities they add junk (``may``, ``meeting``). Read-side default unchanged.

    Returns a de-duplicated list of normalized entity strings in insertion
    order. Pure stdlib; no network, no LLM.
    """
    if not query:
        return []

    found: _OrderedSet = _OrderedSet()

    def _add(candidate: str) -> None:
        norm = normalize_entity(candidate)
        if norm:
            found.setdefault(norm, None)

    # Rule 1: multi-word capitalized phrases.
    for m in _RE_MULTI_CAPS.finditer(query):
        phrase = m.group(1)
        if len(normalize_entity(phrase)) >= 3:
            _add(phrase)

    # Rule 2: single capitalized word, not at sentence start.
    # Token walk mirrors the JS split(/(\s+)/): separators are retained as
    # their own tokens so we can drive the prevSentenceEnd flag off the raw
    # token text (including any trailing punctuation).
    prev_sentence_end = True
    for tok in re.split(r"(\s+)", query):
        if not tok:
            continue
        if tok.isspace():
            # whitespace separator -- does not change sentence-end state.
            continue
        word = _RE_NONALNUM.sub("", tok)
        if not prev_sentence_end and _RE_SINGLE_CAPS_WORD.match(word):
            norm = normalize_entity(word)
            if norm and norm not in STOP:
                found.setdefault(norm, None)
        prev_sentence_end = bool(_RE_SENTENCE_END.search(tok))

    # Rule 3: domain seeds (match against the lowered text).
    lowered = query.lower()
    for seed in (DOMAIN_SEEDS if use_domain_seeds else ()):
        if re.search(r"\b" + re.escape(seed) + r"\b", lowered):
            found.setdefault(seed, None)

    # Rule 4: acronyms.
    for m in _RE_ACRONYM.finditer(query):
        acr = m.group(1).lower()
        if len(acr) >= 2 and acr not in STOP:
            found.setdefault(acr, None)

    # Rule 5: dates.
    for m in _RE_DATE_MONTH.finditer(query):
        _add(m.group(1))
    for m in _RE_DATE_ISO.finditer(query):
        _add(m.group(1))

    return list(found.keys())


#: Entity type given to heuristically derived entities. The heuristic only finds
#: names, not kinds, so it uses the most generic of the v1 entity types.
DERIVED_ENTITY_TYPE = "concept"

#: Bare month names are dates, not entities ("in May" is not the word "may").
_WRITE_SKIP_NAMES = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
})

#: Same per-claim cap the v1 claim builder applies.
_MAX_DERIVED_ENTITIES = 8


def derive_entities_for_write(text: str) -> List[dict]:
    """Derive v1 claim entities (``{"name", "type"}``) from fact text.

    For explicit remembers, which carry no LLM-extracted entities. Uses
    :func:`extract_query_entities`, the helper recall uses, so a stored fact
    and a later query agree on entity names. Never raises: any error returns
    ``[]`` (the write then proceeds without entities).
    """
    try:
        text = text or ""
        names = [
            n for n in extract_query_entities(text, use_domain_seeds=False)
            if n not in _WRITE_SKIP_NAMES
        ]
        # Drop a name contained in a longer derived name ("acme corp" keeps
        # itself, drops "acme" and "corp").
        names = [
            n for n in names
            if not any(n != m and re.search(r"\b" + re.escape(n) + r"\b", m) for m in names)
        ]
        out = []
        for n in names[:_MAX_DERIVED_ENTITIES]:
            # Restore the casing the user wrote (case-insensitive search).
            m = re.search(r"\b" + re.escape(n) + r"\b", text, re.IGNORECASE)
            out.append({"name": m.group(0) if m else n, "type": DERIVED_ENTITY_TYPE})
        return out
    except Exception:
        import logging

        logging.getLogger(__name__).debug(
            "entity derivation failed; storing without entities", exc_info=True
        )
        return []
