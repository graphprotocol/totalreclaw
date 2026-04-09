"""
Store endpoint for TotalReclaw Server.

Stores encrypted facts with blind indices.
"""
import json
import logging
import re
from fastapi import APIRouter, Depends, HTTPException, status, Header
from pydantic import BaseModel, Field
from typing import Optional, List
import uuid
from datetime import datetime

from ..auth import verify_auth_key, hash_auth_key, AuthError
from ..db import get_db, Database
from ..dependencies import get_current_user

logger = logging.getLogger(__name__)

_HEX64_RE = re.compile(r'^[a-f0-9]{64}$')

router = APIRouter(tags=["storage"])


# ============ JSON Request/Response Models ============

class FactJSON(BaseModel):
    """JSON representation of a fact."""
    id: str = Field(..., description="UUIDv7 fact identifier")
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    encrypted_blob: str = Field(..., description="Hex-encoded XChaCha20-Poly1305 ciphertext", max_length=2097152)  # 2MB hex = 1MB binary
    blind_indices: List[str] = Field(..., description="List of SHA-256 hashes for blind search", max_length=1000)
    decay_score: float = Field(1.0, description="Importance score", ge=0.0, le=10.0)
    is_active: bool = Field(True, description="Whether fact is active")
    version: int = Field(1, description="Version for optimistic locking", ge=1)
    source: str = Field(..., description="Origin: conversation | pre_compaction | explicit", max_length=100)
    # v0.3.1b fields
    content_fp: Optional[str] = Field(None, description="HMAC-SHA256 content fingerprint for dedup")
    agent_id: Optional[str] = Field(None, description="Identifier of the creating agent")
    # PoC v2 fields (LSH + reranking)
    encrypted_embedding: Optional[str] = Field(None, description="Hex-encoded XChaCha20-Poly1305 encrypted embedding vector", max_length=2097152)


class StoreRequestJSON(BaseModel):
    """JSON store request."""
    user_id: str = Field(..., description="User's UUID", max_length=100)
    facts: List[FactJSON] = Field(..., description="Facts to store", max_length=500)


class StoreResponseJSON(BaseModel):
    """JSON store response."""
    success: bool
    ids: Optional[List[str]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    version: Optional[int] = None
    # v0.3.1b: duplicate tracking
    duplicate_ids: Optional[List[str]] = None


# ============ Error Codes ============

class ErrorCode:
    OK = "OK"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_REQUEST = "INVALID_REQUEST"
    STORAGE_ERROR = "STORAGE_ERROR"
    AUTH_FAILED = "AUTH_FAILED"
    DUPLICATE_CONTENT = "DUPLICATE_CONTENT"


# ============ Endpoint ============

@router.post("/store", response_model=StoreResponseJSON)
async def store(
    request_obj: StoreRequestJSON,
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db)
):
    """
    Store encrypted facts.

    The client sends:
    - user_id: User's UUID (must match authenticated user)
    - facts: List of encrypted facts with blind indices

    The server:
    1. Validates user_id matches authenticated user
    2. Stores facts with encrypted_blob and blind_indices
    3. Returns list of stored fact IDs

    The server NEVER decrypts the blob - it's stored as-is.
    """
    try:
        # Validate user_id matches authenticated user
        if request_obj.user_id != user_id:
            return StoreResponseJSON(
                success=False,
                error_code=ErrorCode.AUTH_FAILED,
                error_message="User ID mismatch"
            )

        # Validate request
        if not request_obj.facts:
            return StoreResponseJSON(
                success=False,
                error_code=ErrorCode.INVALID_REQUEST,
                error_message="No facts provided"
            )

        stored_ids = []
        duplicate_ids = []
        max_version = 1

        for fact in request_obj.facts:
            # Decode hex blob
            try:
                encrypted_blob = bytes.fromhex(fact.encrypted_blob)
            except ValueError:
                return StoreResponseJSON(
                    success=False,
                    error_code=ErrorCode.INVALID_REQUEST,
                    error_message=f"Invalid hex encoding in fact {fact.id}"
                )

            # Validate blind index format (must be SHA-256 hashes)
            for idx in fact.blind_indices:
                if not _HEX64_RE.match(idx):
                    return StoreResponseJSON(
                        success=False,
                        error_code=ErrorCode.INVALID_REQUEST,
                        error_message=f"Invalid blind index format in fact {fact.id}: expected 64-char hex"
                    )

            # Validate content fingerprint format (must be SHA-256 hash)
            if fact.content_fp and not _HEX64_RE.match(fact.content_fp):
                return StoreResponseJSON(
                    success=False,
                    error_code=ErrorCode.INVALID_REQUEST,
                    error_message=f"Invalid content fingerprint format in fact {fact.id}: expected 64-char hex"
                )

            # v0.3.1b: Content fingerprint dedup check
            if fact.content_fp:
                existing_id = await db.find_fact_by_fingerprint(
                    user_id=user_id,
                    content_fp=fact.content_fp
                )
                if existing_id:
                    duplicate_ids.append(existing_id)
                    continue  # Skip this fact, it's a duplicate

            # Store fact (with new v0.3.1b + PoC v2 fields)
            stored_fact = await db.store_fact(
                fact_id=fact.id,
                user_id=user_id,
                encrypted_blob=encrypted_blob,
                blind_indices=fact.blind_indices,
                decay_score=fact.decay_score,
                source=fact.source,
                content_fp=fact.content_fp,
                agent_id=fact.agent_id,
                encrypted_embedding=fact.encrypted_embedding
            )
            stored_ids.append(fact.id)
            max_version = max(max_version, stored_fact.version)

        # Audit log: record the store operation (IDs and metadata only, no auth keys)
        try:
            audit_record = json.dumps({
                "action": "store",
                "user_id": user_id,
                "fact_ids": stored_ids,
                "fact_count": len(stored_ids),
                "duplicate_count": len(duplicate_ids),
            }).encode("utf-8")
            await db.log_raw_event(user_id, audit_record)
        except Exception:
            # Audit logging failure must not break the store operation
            logger.warning("Failed to write audit log for store operation")

        return StoreResponseJSON(
            success=True,
            ids=stored_ids,
            version=max_version,
            duplicate_ids=duplicate_ids if duplicate_ids else None
        )

    except HTTPException:
        raise
    except Exception as e:
        return StoreResponseJSON(
            success=False,
            error_code=ErrorCode.STORAGE_ERROR,
            error_message="Failed to store facts"
        )
