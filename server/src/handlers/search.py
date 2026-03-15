"""
Search endpoint for TotalReclaw Server.

Implements blind index search using PostgreSQL GIN index.
"""
import re
import time

from fastapi import APIRouter, Depends, HTTPException, status, Header, Query
from pydantic import BaseModel, Field
from typing import Optional, List
import uuid

from ..auth import verify_auth_key, hash_auth_key, AuthError
from ..db import get_db, Database
from ..dependencies import get_current_user
from ..search_telemetry import telemetry_store, SearchSample

_HEX64_RE = re.compile(r'^[a-f0-9]{64}$')

router = APIRouter(tags=["search"])


# ============ JSON Request/Response Models ============

class SearchRequestJSON(BaseModel):
    """JSON search request."""
    user_id: str = Field(..., description="User's UUID", max_length=100)
    trapdoors: List[str] = Field(..., description="Blind trapdoors (SHA-256 hashes)", max_length=1000)
    max_candidates: int = Field(3000, description="Maximum candidates to return", ge=1, le=10000)
    min_decay_score: float = Field(0.0, description="Minimum decay score filter", ge=0.0)


class SearchResultJSON(BaseModel):
    """JSON search result."""
    fact_id: str
    encrypted_blob: str
    decay_score: float
    timestamp: int
    version: int
    encrypted_embedding: Optional[str] = None  # PoC v2: hex-encoded encrypted embedding


class SearchResponseJSON(BaseModel):
    """JSON search response."""
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    results: Optional[List[SearchResultJSON]] = None
    total_candidates: Optional[int] = None
    total_candidates_matched: Optional[int] = None


# ============ Error Codes ============

class ErrorCode:
    OK = "OK"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTH_FAILED = "AUTH_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ============ Endpoint ============

@router.post("/search", response_model=SearchResponseJSON)
async def search(
    request_obj: SearchRequestJSON,
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db)
):
    """
    Search for facts using blind indices.

    The client sends:
    - user_id: User's UUID (must match authenticated user)
    - trapdoors: List of blind trapdoors (SHA-256 hashes of LSH buckets + keywords)
    - max_candidates: Maximum number of candidates to return
    - min_decay_score: Filter by minimum decay score

    The server:
    1. Validates user_id matches authenticated user
    2. Queries facts where blind_indices overlap with trapdoors
    3. Returns encrypted blobs for client-side re-ranking

    The server NEVER sees the plaintext or knows what the search terms are.
    The blind indices are one-way SHA-256 hashes.
    """
    try:
        # Validate user_id matches authenticated user
        if request_obj.user_id != user_id:
            return SearchResponseJSON(
                success=False,
                error_code=ErrorCode.AUTH_FAILED,
                error_message="User ID mismatch"
            )

        # Validate request
        if not request_obj.trapdoors:
            return SearchResponseJSON(
                success=False,
                error_code=ErrorCode.INVALID_REQUEST,
                error_message="No trapdoors provided"
            )

        # Validate trapdoor format (must be SHA-256 hashes)
        for trapdoor in request_obj.trapdoors:
            if not _HEX64_RE.match(trapdoor):
                return SearchResponseJSON(
                    success=False,
                    error_code=ErrorCode.INVALID_REQUEST,
                    error_message="Invalid trapdoor format: expected 64-char hex SHA-256 hashes"
                )

        # Clamp max_candidates to reasonable range
        max_candidates = min(max(request_obj.max_candidates, 1), 10000)

        # Search using GIN index on blind_indices array
        # Uses PostgreSQL's array overlap operator (&&)
        try:
            t0 = time.monotonic()
            facts, total_matched = await db.search_facts_by_blind_indices(
                user_id=user_id,
                trapdoors=request_obj.trapdoors,
                max_candidates=max_candidates,
                min_decay_score=request_obj.min_decay_score
            )
            gin_query_ms = (time.monotonic() - t0) * 1000
        except ValueError as e:
            return SearchResponseJSON(
                success=False,
                error_code=ErrorCode.INVALID_REQUEST,
                error_message=str(e)
            )

        # Record search telemetry for per-user observability
        telemetry_store.record(user_id, SearchSample(
            total_candidates_matched=total_matched,
            max_candidates_requested=max_candidates,
            gin_query_ms=gin_query_ms,
        ))

        # Convert to response format
        results = []
        for fact in facts:
            # Convert timestamp to Unix milliseconds
            timestamp_ms = int(fact.created_at.timestamp() * 1000)

            results.append(SearchResultJSON(
                fact_id=fact.id,
                encrypted_blob=fact.encrypted_blob.hex(),
                decay_score=fact.decay_score,
                timestamp=timestamp_ms,
                version=fact.version,
                encrypted_embedding=getattr(fact, 'encrypted_embedding', None)
            ))

        return SearchResponseJSON(
            success=True,
            results=results,
            total_candidates=len(results),
            total_candidates_matched=total_matched,
        )

    except HTTPException:
        raise
    except Exception as e:
        return SearchResponseJSON(
            success=False,
            error_code=ErrorCode.INTERNAL_ERROR,
            error_message="Search failed"
        )


# ============ Additional Endpoints ============

class DeleteRequestJSON(BaseModel):
    """JSON delete request."""
    user_id: str
    fact_id: str


class DeleteResponseJSON(BaseModel):
    """JSON delete response."""
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@router.delete("/facts/{fact_id}", response_model=DeleteResponseJSON)
async def delete_fact(
    fact_id: str,
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db)
):
    """
    Soft delete a fact (create tombstone).

    The fact is marked as inactive and a tombstone is created.
    Tombstones are retained for 30 days for undo capability.
    """
    try:
        deleted = await db.soft_delete_fact(fact_id, user_id)

        if not deleted:
            return DeleteResponseJSON(
                success=False,
                error_code="NOT_FOUND",
                error_message="Fact not found"
            )

        return DeleteResponseJSON(success=True)

    except HTTPException:
        raise
    except Exception:
        return DeleteResponseJSON(
            success=False,
            error_code="INTERNAL_ERROR",
            error_message="Delete failed"
        )


class BatchDeleteRequestJSON(BaseModel):
    """JSON batch delete request."""
    fact_ids: List[str] = Field(..., description="List of fact IDs to delete", max_length=500)


class BatchDeleteResponseJSON(BaseModel):
    """JSON batch delete response."""
    success: bool
    deleted_count: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@router.post("/facts/batch-delete", response_model=BatchDeleteResponseJSON)
async def batch_delete_facts(
    request_obj: BatchDeleteRequestJSON,
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db)
):
    """
    Batch soft-delete facts by ID list.

    Accepts up to 500 fact IDs per request. Each fact is soft-deleted
    (tombstoned) individually. Individual failures are skipped silently;
    the response reports how many were actually deleted.
    """
    try:
        deleted_count = 0
        for fact_id in request_obj.fact_ids:
            try:
                deleted = await db.soft_delete_fact(fact_id, user_id)
                if deleted:
                    deleted_count += 1
            except Exception:
                # Skip individual failures silently
                continue

        return BatchDeleteResponseJSON(
            success=True,
            deleted_count=deleted_count,
        )

    except HTTPException:
        raise
    except Exception:
        return BatchDeleteResponseJSON(
            success=False,
            error_code="INTERNAL_ERROR",
            error_message="Batch delete failed",
        )


EXPORT_MAX_LIMIT = 5000
EXPORT_DEFAULT_LIMIT = 1000


class ExportResponseJSON(BaseModel):
    """JSON export response with cursor-based pagination."""
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    facts: Optional[List[dict]] = None
    cursor: Optional[str] = None
    has_more: bool = False
    total_count: Optional[int] = None


@router.get("/export", response_model=ExportResponseJSON)
async def export_facts(
    limit: int = Query(default=EXPORT_DEFAULT_LIMIT, ge=1, le=EXPORT_MAX_LIMIT, description="Number of facts per page"),
    cursor: Optional[str] = Query(default=None, description="Last fact_id from previous page"),
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db)
):
    """
    Export user facts with cursor-based pagination.

    Returns active facts for the authenticated user, paginated to prevent
    OOM on large accounts. Use the returned `cursor` value as the `cursor`
    query parameter in the next request to fetch the next page.

    Query parameters:
    - limit: Number of facts per page (default: 1000, max: 5000)
    - cursor: Last fact_id from previous page (omit for first page)
    """
    try:
        # Clamp limit to allowed range
        clamped_limit = min(max(limit, 1), EXPORT_MAX_LIMIT)

        facts, next_cursor, has_more, total_count = await db.get_facts_paginated(
            user_id=user_id,
            limit=clamped_limit,
            cursor=cursor
        )

        fact_list = []
        for f in facts:
            fact_dict = {
                "id": f.id,
                "encrypted_blob": f.encrypted_blob.hex(),
                "blind_indices": f.blind_indices,
                "decay_score": f.decay_score,
                "version": f.version,
                "source": f.source,
                "created_at": f.created_at.isoformat(),
                "updated_at": f.updated_at.isoformat()
            }
            enc_emb = getattr(f, 'encrypted_embedding', None)
            if enc_emb is not None:
                fact_dict["encrypted_embedding"] = enc_emb
            fact_list.append(fact_dict)

        return ExportResponseJSON(
            success=True,
            facts=fact_list,
            cursor=next_cursor,
            has_more=has_more,
            total_count=total_count
        )

    except HTTPException:
        raise
    except Exception:
        return ExportResponseJSON(
            success=False,
            error_code="INTERNAL_ERROR",
            error_message="Export failed"
        )
