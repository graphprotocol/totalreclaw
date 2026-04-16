"""
Contradiction detection for the TotalReclaw Python agent layer.

For each new fact with entities, recalls existing facts from the vault that
share an entity, then delegates to ``totalreclaw_core.resolve_with_candidates()``
to check for semantic contradictions (cosine similarity in the [0.3, 0.85)
band). Facts where an existing claim wins (SkipNew) are filtered out.

Falls back to "store everything" if ``totalreclaw_core`` is not installed or
any error occurs — contradiction detection is best-effort and must never block
the store pipeline.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from totalreclaw.client import TotalReclaw
    from .extraction import ExtractedFact

logger = logging.getLogger(__name__)

# Cosine similarity thresholds for contradiction detection.
# Pairs with similarity < lower are unrelated; pairs >= upper are near-dupes
# (handled by store-time dedup). The contradiction band is [lower, upper).
CONTRADICTION_THRESHOLD_LOWER = 0.30
CONTRADICTION_THRESHOLD_UPPER = 0.85


async def detect_and_resolve_contradictions(
    new_facts: List["ExtractedFact"],
    client: "TotalReclaw",
    log: Optional[Any] = None,
) -> List["ExtractedFact"]:
    """Filter out new facts that lose to existing vault claims.

    For each new fact that has entities and an embedding, this function:

    1. Computes entity trapdoors and recalls existing claims from the vault
       that share at least one entity.
    2. Decrypts the candidates and pairs them with their embeddings.
    3. Calls ``totalreclaw_core.resolve_with_candidates()`` to run the P2-3
       contradiction formula.
    4. If any resolution action is ``skip_new``, the fact is dropped.

    Returns the subset of ``new_facts`` that should proceed to storage.

    On any error (missing core, subgraph issues, decrypt failures), returns
    the full ``new_facts`` list unchanged — contradiction detection is
    best-effort.

    Parameters
    ----------
    new_facts : list[ExtractedFact]
        Facts from the extraction pipeline, each with ``.text``,
        ``.entities``, ``.importance``, ``.confidence``, ``.type``.
    client : TotalReclaw
        Configured client instance (used for recall queries).
    log : logger-like, optional
        Falls back to module-level ``logger`` if not provided.
    """
    if log is None:
        log = logger

    try:
        import totalreclaw_core
    except ImportError:
        log.debug("totalreclaw_core not available — skipping contradiction detection")
        return list(new_facts)

    try:
        from totalreclaw.embedding import get_embedding
        from totalreclaw.claims_helper import (
            build_canonical_claim,
            compute_entity_trapdoor,
        )
    except ImportError:
        log.debug("Required modules not available — skipping contradiction detection")
        return list(new_facts)

    # Load default resolution weights once
    try:
        weights_json = totalreclaw_core.default_resolution_weights()
    except Exception as exc:
        log.debug("Failed to load default weights: %s", exc)
        return list(new_facts)

    # Get tie-zone tolerance
    try:
        tie_tolerance = totalreclaw_core.tie_zone_score_tolerance()
    except Exception:
        tie_tolerance = 0.01

    now_unix = int(time.time())
    kept: List["ExtractedFact"] = []

    for fact in new_facts:
        # Only run contradiction detection on facts with entities
        if not fact.entities or len(fact.entities) == 0:
            kept.append(fact)
            continue

        try:
            # Get embedding for the new fact
            embedding = get_embedding(fact.text)
            if not embedding:
                kept.append(fact)
                continue

            # Compute entity trapdoors to search for overlapping claims
            entity_trapdoors = []
            for entity in fact.entities:
                name = entity.name if hasattr(entity, "name") else entity.get("name", "")
                if name:
                    entity_trapdoors.append(compute_entity_trapdoor(name))

            if not entity_trapdoors:
                kept.append(fact)
                continue

            # Recall existing facts that share entities
            # Use a broad recall with entity trapdoors as the query
            # to find overlapping claims
            entity_names = [
                (e.name if hasattr(e, "name") else e.get("name", ""))
                for e in fact.entities
            ]
            query_str = " ".join(n for n in entity_names if n)
            if not query_str:
                kept.append(fact)
                continue

            try:
                existing_results = await client.recall(
                    query_str,
                    query_embedding=embedding,
                    top_k=20,
                )
            except Exception as exc:
                log.debug("Recall for contradiction candidates failed: %s", exc)
                kept.append(fact)
                continue

            if not existing_results:
                kept.append(fact)
                continue

            # Build the new claim JSON for the resolver
            importance_int = max(1, min(10, int(round(
                fact.importance if fact.importance > 1 else fact.importance * 10
            ))))
            fact_stub = {
                "text": fact.text,
                "type": fact.type,
                "confidence": fact.confidence,
                "entities": [
                    {
                        "name": e.name if hasattr(e, "name") else e.get("name", ""),
                        "type": e.type if hasattr(e, "type") else e.get("type", "concept"),
                    }
                    for e in fact.entities
                ] if fact.entities else None,
            }
            new_claim_json = build_canonical_claim(
                fact_stub,
                importance=importance_int,
                source_agent="hermes-auto",
            )
            new_claim_id = f"pending-{id(fact)}"

            # Build candidates array for the resolver
            # Each candidate needs: {claim: <Claim JSON>, id: <string>, embedding: <float[]>}
            candidates = []
            for result in existing_results:
                if not result.embedding or not result.text:
                    continue
                # Parse the result text back into a claim-like structure.
                # The results come from the reranker as RerankerResult with
                # .text, .id, .embedding, .importance, .category
                try:
                    # Build a minimal canonical claim for the existing fact
                    existing_stub = {
                        "text": result.text,
                        "type": result.category or "fact",
                        "confidence": 0.85,
                        "entities": None,
                    }
                    existing_importance = max(1, min(10, int(round(
                        result.importance * 10 if result.importance <= 1 else result.importance
                    ))))
                    existing_claim_json = build_canonical_claim(
                        existing_stub,
                        importance=existing_importance,
                        source_agent="unknown",
                    )
                    existing_claim = json.loads(existing_claim_json)
                    candidates.append({
                        "claim": existing_claim,
                        "id": result.id,
                        "embedding": list(result.embedding),
                    })
                except Exception as exc:
                    log.debug("Failed to build candidate from result %s: %s", result.id, exc)
                    continue

            if not candidates:
                kept.append(fact)
                continue

            # Call the Rust core resolver
            new_claim_obj = json.loads(new_claim_json)
            actions_json = totalreclaw_core.resolve_with_candidates(
                json.dumps(new_claim_obj, ensure_ascii=False, separators=(",", ":")),
                new_claim_id,
                json.dumps(embedding),
                json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
                weights_json,
                CONTRADICTION_THRESHOLD_LOWER,
                CONTRADICTION_THRESHOLD_UPPER,
                now_unix,
                tie_tolerance,
            )

            actions = json.loads(actions_json)

            # Check if any action tells us to skip the new fact
            skip = False
            for action in actions:
                action_type = action.get("type", "")
                if action_type == "skip_new":
                    reason = action.get("reason", "unknown")
                    existing_id = action.get("existing_id", "?")
                    log.info(
                        "Contradiction: skipping new fact %r — existing %s wins (reason=%s)",
                        fact.text[:60],
                        existing_id,
                        reason,
                    )
                    skip = True
                    break

            if skip:
                continue

            # Log supersede actions (informational — the new fact proceeds,
            # but we don't tombstone the old one from here; that's a future
            # enhancement matching the plugin's full flow)
            for action in actions:
                if action.get("type") == "supersede_existing":
                    log.info(
                        "Contradiction: new fact %r supersedes existing %s (not tombstoned yet)",
                        fact.text[:60],
                        action.get("existing_id", "?"),
                    )

            kept.append(fact)

        except Exception as exc:
            # Any per-fact error: keep the fact and continue
            log.debug("Contradiction check failed for fact %r: %s", fact.text[:60], exc)
            kept.append(fact)

    log.info(
        "Contradiction detection: %d/%d facts passed (removed %d)",
        len(kept),
        len(new_facts),
        len(new_facts) - len(kept),
    )
    return kept
