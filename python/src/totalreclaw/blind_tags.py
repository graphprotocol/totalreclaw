"""Keyed blind-index tags, trapdoors and content fingerprints (internal#573).

Single module that produces every tag, query trapdoor and content
fingerprint the client sends. All hashing happens inside
``totalreclaw_core``: the vault ``encryption_key`` / ``dedup_key`` the
client already holds are handed to core constructors, which derive the
index keys internally and never expose them. This module does no hashing
and no key derivation of its own, and never logs keys, tags or queries.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

import totalreclaw_core


def indexer(keys: Any) -> "totalreclaw_core.BlindIndexer":
    """Build a core ``BlindIndexer`` from the vault encryption key (per call)."""
    return totalreclaw_core.BlindIndexer(keys.encryption_key)


def _name_of(entity: Any) -> Any:
    if isinstance(entity, dict):
        return entity.get("name")
    return getattr(entity, "name", None)


def entity_names(entities: Optional[Iterable[Any]]) -> List[str]:
    """Extract non-empty string names (dicts or objects), order kept, deduped."""
    if not entities:
        return []
    seen: set = set()
    out: List[str] = []
    for e in entities:
        name = _name_of(e)
        if not isinstance(name, str) or not name.strip():
            continue
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def fact_tags(
    keys: Any,
    text: str,
    *,
    embedding: Optional[Sequence[float]] = None,
    lsh_hasher: Any = None,
    entity_names: Sequence[str] = (),
) -> List[str]:
    """Final tag list for a fact: sorted, deduplicated, keyed."""
    ix = indexer(keys)
    tags: List[str] = list(ix.stem_tags(text or ""))
    if embedding and lsh_hasher is not None:
        tags.extend(lsh_hasher.hash_keyed(list(embedding)))
    tags.extend(ix.entity_tag(n) for n in entity_names)
    return totalreclaw_core.sort_dedup_tags(tags)


def query_trapdoors(
    keys: Any,
    query: str,
    *,
    query_embedding: Optional[Sequence[float]] = None,
    lsh_hasher: Any = None,
    entity_names: Sequence[str] = (),
) -> List[str]:
    """Final trapdoor list for a query (sorted, deduplicated, keyed)."""
    ix = indexer(keys)
    if query_embedding and lsh_hasher is not None:
        return list(
            totalreclaw_core.generate_search_trapdoors_keyed(
                query, list(entity_names), list(query_embedding), ix, lsh_hasher.core
            )
        )
    tags = list(ix.stem_tags(query or "")) + [ix.entity_tag(n) for n in entity_names]
    return totalreclaw_core.sort_dedup_tags(tags)


def content_fp(keys: Any, text: str) -> str:
    """Content fingerprint v2 (keyed)."""
    return totalreclaw_core.generate_content_fingerprint_v2(text, keys.dedup_key)


def tag_counter(keys: Any):
    """Return ``payload -> int`` estimating a batch payload's tag count, or
    ``None`` when ``keys`` cannot build an indexer (callers then use the
    size estimate's fallback).

    Stems of the payload text plus its entity names (LSH tags are costed
    separately by the batch-size estimate). Used for calldata sizing only.
    """
    try:
        ix = indexer(keys)
    except Exception:
        return None

    def _count(payload: dict) -> int:
        text = payload.get("text") or ""
        names = entity_names(payload.get("entities"))
        return len(ix.stem_tags(text)) + len(names)

    return _count
