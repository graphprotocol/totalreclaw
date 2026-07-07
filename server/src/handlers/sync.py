"""
Sync endpoint for TotalReclaw Server (v0.3.1b).

Provides delta sync for agent reconnection.
Returns all facts for a user since a given sequence_id.

Spec: docs/specs/totalreclaw/server.md v0.3.1b section 4, 8.2
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Header, Query
from pydantic import BaseModel, Field
from typing import Optional, List

from ..db import get_db, Database
from ..dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sync"])


# ============ Response Models ============

class SyncedFactJSON(BaseModel):
    """A fact returned by the sync endpoint."""
    id: str
    sequence_id: Optional[int] = None
    encrypted_blob: str  # hex-encoded
    blind_indices: List[str]
    decay_score: float
    is_active: bool
    version: int
    source: str
    content_fp: Optional[str] = None
    agent_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    encrypted_embedding: Optional[str] = None  # PoC v2: hex-encoded encrypted embedding


class SyncResponseJSON(BaseModel):
    """Response from GET /sync."""
    success: bool
    facts: List[SyncedFactJSON] = []
    latest_sequence: int = 0
    has_more: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None


# ============ Endpoint ============

@router.get("/sync", response_model=SyncResponseJSON)
async def sync(
    since_sequence: int = Query(0, description="Return facts with sequence_id > this value"),
    limit: int = Query(1000, ge=1, le=10000, description="Max facts to return"),
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db)
):
    """
    Delta sync endpoint for agent reconnection (v0.3.1b).

    Returns all facts for the authenticated user where sequence_id > since_sequence.
    Used by agents after coming online to pull changes made by other agents.

    Response includes:
    - facts: array of facts with all metadata
    - latest_sequence: current highest sequence_id for this user
    - has_more: true if more facts beyond limit (client should paginate)
    """
    try:
        facts, latest_seq, has_more = await db.get_facts_since_sequence(
            user_id=user_id,
            since_sequence=since_sequence,
            limit=limit
        )

        synced_facts = []
        for fact in facts:
            synced_facts.append(SyncedFactJSON(
                id=fact.id,
                sequence_id=getattr(fact, 'sequence_id', None),
                encrypted_blob=fact.encrypted_blob.hex(),
                blind_indices=fact.blind_indices,
                decay_score=fact.decay_score,
                is_active=fact.is_active,
                version=fact.version,
                source=fact.source,
                content_fp=getattr(fact, 'content_fp', None),
                agent_id=getattr(fact, 'agent_id', None),
                created_at=fact.created_at.isoformat() if fact.created_at else None,
                updated_at=fact.updated_at.isoformat() if fact.updated_at else None,
                encrypted_embedding=getattr(fact, 'encrypted_embedding', None),
            ))

        return SyncResponseJSON(
            success=True,
            facts=synced_facts,
            latest_sequence=latest_seq,
            has_more=has_more,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Sync failed: {e}", exc_info=True)
        return SyncResponseJSON(
            success=False,
            error_code="INTERNAL_ERROR",
            error_message="Sync failed"
        )
