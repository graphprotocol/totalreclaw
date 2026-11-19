"""
Contradiction detection for the TotalReclaw Python agent layer.

For each new fact with entities, recalls existing facts from the vault that
share an entity, then delegates to ``totalreclaw_core.resolve_with_candidates()``
to check for semantic contradictions (cosine similarity in the [0.3, 0.85)
band).

Pin contract (PRD-04 F1 / DEP-5): the new claim and every candidate reach core
with their entity refs (short-key ``e``) and, for pinned candidates, the v0
``st: "p"`` sentinel. Core compares only claims that share an entity and reads
pin state from ``st`` on this shape, so both are required for its pin check
(``SkipNew { reason: ExistingPinned }``) to fire. A new fact that contradicts
a pinned fact is dropped. Every other resolver outcome (``skip_new`` with
``existing_wins``, ``supersede_existing``, ``tie_leave_both``) is logged and
NOT acted on -- applying those is PRD-04 DEP-12.

Falls back to "store everything" if ``totalreclaw_core`` is not installed or
any error occurs -- contradiction detection is best-effort and must never block
the store pipeline. Log lines carry categories and counts only, never fact
text.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, List, Optional

from ..relay import RelayReadBlocked

if TYPE_CHECKING:
    from totalreclaw.client import TotalReclaw
    from .extraction import ExtractedFact

logger = logging.getLogger(__name__)

# Cosine similarity thresholds for contradiction detection.
# Pairs with similarity < lower are unrelated; pairs >= upper are near-dupes
# (handled by store-time dedup). The contradiction band is [lower, upper).
CONTRADICTION_THRESHOLD_LOWER = 0.30
CONTRADICTION_THRESHOLD_UPPER = 0.85

#: Resolver ``skip_new`` reason for a new claim that contradicts a pinned claim.
SKIP_REASON_EXISTING_PINNED = "existing_pinned"

# v1 memory type -> short-key category accepted by core's ``ClaimCategory``.
_V1_TO_SHORT_CATEGORY = {
    "claim": "fact",
    "preference": "pref",
    "directive": "rule",
    "commitment": "goal",
    "episode": "epi",
    "summary": "sum",
}


def _short_key_claim_for_resolver(
    *,
    text: str,
    fact_type: str,
    importance: int,
    confidence: float,
    source_agent: str,
    created_at: str,
    entities: Optional[List[dict]] = None,
    pinned: bool = False,
) -> dict:
    """Build a short-key canonical claim for core.resolve_with_candidates().

    The resolver accepts the v0 short-key format (``{t, c, cf, i, sa, ea}``)
    -- the v1 claim shape is NOT accepted by core's resolver. We emit short
    keys only for the resolver's transient input; the actual on-chain write
    goes through ``build_canonical_claim_v1`` which emits a v1 JSON blob.

    ``entities`` are short-key refs (``{"n", "tp", "r"?}``, see
    :func:`totalreclaw.claims_helper.to_entity_refs`) emitted as ``e``.
    ``pinned`` emits ``st: "p"`` -- the pin sentinel core's
    ``is_pinned_claim`` reads on this shape.
    """
    claim: dict = {
        "t": text,
        "c": _V1_TO_SHORT_CATEGORY.get(fact_type, "fact"),
        "cf": confidence,
        "i": importance,
        "sa": source_agent,
        "ea": created_at,
    }
    if entities:
        claim["e"] = list(entities)
    if pinned:
        claim["st"] = "p"
    return claim


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _pinned_candidate_stats(
    embedding: List[float],
    candidates: List[dict],
    actions: List[dict],
) -> "tuple[int, int, int]":
    """Count, for one fact, its pinned candidates and why they did not match.

    Returns ``(pinned_candidates, no_shared_entity, out_of_band)``. A pinned
    candidate is ``out_of_band`` when its cosine is outside the resolver band,
    ``no_shared_entity`` when it is in band but core raised no pinned skip for
    it (no shared name), and neither when core skipped the new fact because of it.
    Observability only: nothing here changes what is stored.
    """
    skipped_ids = {
        a.get("existing_id")
        for a in actions
        if a.get("type") == "skip_new" and a.get("reason") == SKIP_REASON_EXISTING_PINNED
    }
    pinned_n = no_share = oob = 0
    for cand in candidates:
        if (cand.get("claim") or {}).get("st") != "p":
            continue
        pinned_n += 1
        sim = _cosine(embedding, cand.get("embedding") or [])
        if not (CONTRADICTION_THRESHOLD_LOWER <= sim < CONTRADICTION_THRESHOLD_UPPER):
            oob += 1
        elif cand.get("id") not in skipped_ids:
            no_share += 1
    return pinned_n, no_share, oob


async def detect_and_resolve_contradictions(
    new_facts: List["ExtractedFact"],
    client: "TotalReclaw",
    log: Optional[Any] = None,
) -> List["ExtractedFact"]:
    """Filter out new facts that contradict a pinned vault claim.

    For each new fact that has entities and an embedding, this function:

    1. Recalls existing claims from the vault that share its entities.
    2. Projects the new fact and each candidate to short-key claims carrying
       entity refs and (candidates only) pin state.
    3. Calls ``totalreclaw_core.resolve_with_candidates()``.
    4. Drops the fact if any action is ``skip_new`` with reason
       ``existing_pinned``. Other actions are logged, not applied (DEP-12).

    Returns the subset of ``new_facts`` that should proceed to storage.

    On any error (missing core, subgraph issues, decrypt failures), returns
    the full ``new_facts`` list unchanged -- contradiction detection is
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
            compute_entity_trapdoor,
            to_entity_refs,
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

    try:
        tie_tolerance = totalreclaw_core.tie_zone_score_tolerance()
    except Exception:
        tie_tolerance = 0.01

    now_unix = int(time.time())
    kept: List["ExtractedFact"] = []
    pinned_conflicts = 0
    pinned_candidates = 0
    no_shared_entity = 0
    out_of_band = 0

    for fact_idx, fact in enumerate(new_facts):
        # Only run contradiction detection on facts with entities
        if not fact.entities or len(fact.entities) == 0:
            kept.append(fact)
            continue

        try:
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
            except RelayReadBlocked as exc:
                # Reads are paused (quota / rate limit) — every remaining
                # fact's recall would short-circuit identically. Keep this
                # fact AND every fact still to come (best-effort: unresolved
                # contradiction detection beats dropping a fact silently),
                # log once, and stop the loop. #662.
                log.info(
                    "Contradiction detection skipped for remaining facts: "
                    "reads paused (%s)",
                    exc,
                )
                kept.append(fact)
                kept.extend(new_facts[fact_idx + 1 :])
                break
            except Exception as exc:
                log.debug("Recall for contradiction candidates failed: %s", exc)
                kept.append(fact)
                continue

            if not existing_results:
                kept.append(fact)
                continue

            # Build the new claim JSON for the resolver (short-key shape, with
            # entity refs so core's shared-entity detection can run).
            importance_int = max(1, min(10, int(round(
                fact.importance if fact.importance > 1 else fact.importance * 10
            ))))
            new_claim_json_obj = _short_key_claim_for_resolver(
                text=fact.text,
                fact_type=fact.type,
                importance=importance_int,
                confidence=fact.confidence,
                source_agent="hermes-auto",
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                entities=to_entity_refs(fact.entities),
            )
            new_claim_json = json.dumps(new_claim_json_obj, ensure_ascii=False, separators=(",", ":"))
            new_claim_id = f"pending-{id(fact)}"

            # Build candidates array for the resolver
            # Each candidate needs: {claim: <Claim JSON>, id: <string>, embedding: <float[]>}
            candidates = []
            for result in existing_results:
                if not result.embedding or not result.text:
                    continue
                try:
                    existing_importance = max(1, min(10, int(round(
                        result.importance * 10 if result.importance <= 1 else result.importance
                    ))))
                    result_entities = getattr(result, "entities", None)
                    existing_short = _short_key_claim_for_resolver(
                        text=result.text,
                        fact_type=result.category or "claim",
                        importance=existing_importance,
                        confidence=0.85,
                        source_agent="unknown",
                        created_at=time.strftime(
                            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(result.created_at)
                        ) if getattr(result, "created_at", None) else time.strftime(
                            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                        ),
                        entities=result_entities if isinstance(result_entities, list) else None,
                        pinned=getattr(result, "pinned", False) is True,
                    )
                    candidates.append({
                        "claim": existing_short,
                        "id": result.id,
                        "embedding": list(result.embedding),
                    })
                except Exception as exc:
                    log.debug("Failed to build a contradiction candidate (%s)", type(exc).__name__)
                    continue

            if not candidates:
                kept.append(fact)
                continue

            # Call the Rust core resolver — short-key claim format.
            actions_json = totalreclaw_core.resolve_with_candidates(
                new_claim_json,
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

            # F3 observability: why did pinned candidates not (or did) trigger.
            _pin_n, _no_share, _oob = _pinned_candidate_stats(
                embedding, candidates, actions
            )
            pinned_candidates += _pin_n
            no_shared_entity += _no_share
            out_of_band += _oob

            if any(
                action.get("type") == "skip_new"
                and action.get("reason") == SKIP_REASON_EXISTING_PINNED
                for action in actions
            ):
                pinned_conflicts += 1
                log.info(
                    "Contradiction: new fact not stored, it contradicts a pinned fact (reason=%s)",
                    SKIP_REASON_EXISTING_PINNED,
                )
                continue

            # Every other outcome is observed only; PRD-04 DEP-12 owns applying
            # skip_new/existing_wins and supersede_existing.
            for action in actions:
                log.debug(
                    "Contradiction: observed %s (reason=%s), not applied (PRD-04 DEP-12)",
                    action.get("type", "?"),
                    action.get("reason", "-"),
                )

            kept.append(fact)

        except Exception as exc:
            # Any per-fact error: keep the fact and continue
            log.debug("Contradiction check failed for a fact (%s); keeping it", type(exc).__name__)
            kept.append(fact)

    log.info(
        "Contradiction detection: %d/%d facts passed (removed %d, pinned conflicts %d)"
        " pinned_candidates=%d no_shared_entity=%d out_of_band=%d",
        len(kept),
        len(new_facts),
        len(new_facts) - len(kept),
        pinned_conflicts,
        pinned_candidates,
        no_shared_entity,
        out_of_band,
    )
    return kept
