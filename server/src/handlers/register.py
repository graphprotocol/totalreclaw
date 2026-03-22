"""
Registration endpoint for TotalReclaw Server.

Implements user registration with HKDF-derived auth keys.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional
import uuid

from ..auth import generate_user_id, hash_auth_key, AuthError
from ..db import get_db, Database
from ..config import get_settings

router = APIRouter(tags=["auth"])


# ============ JSON Request/Response Models ============
# For debugging convenience, we support both Protobuf and JSON

class RegisterRequestJSON(BaseModel):
    """JSON registration request."""
    auth_key_hash: str = Field(..., description="Hex-encoded SHA256 of auth key")
    salt: str = Field(..., description="Hex-encoded 32-byte salt")


class RegisterResponseJSON(BaseModel):
    """JSON registration response."""
    success: bool
    user_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


# ============ Error Codes ============

class ErrorCode:
    OK = "OK"
    USER_EXISTS = "USER_EXISTS"
    INVALID_REQUEST = "INVALID_REQUEST"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ============ Endpoint ============

@router.post("/register", response_model=RegisterResponseJSON)
async def register(
    request_obj: RegisterRequestJSON,
    db: Database = Depends(get_db)
):
    """
    Register a new user.

    The client sends:
    - auth_key_hash: SHA256(HKDF(master_password, salt, "totalreclaw-auth-key-v1"))
    - salt: Random 32 bytes used for HKDF derivation

    The server:
    1. Generates a user_id (UUIDv7)
    2. Stores auth_key_hash and salt
    3. Returns user_id to client

    The client must store user_id and salt locally (in OS keychain).
    The recovery phrase is NEVER sent to the server.
    """
    try:
        # Validate request
        if not request_obj.auth_key_hash or not request_obj.salt:
            return RegisterResponseJSON(
                success=False,
                error_code=ErrorCode.INVALID_REQUEST,
                error_message="Missing auth_key_hash or salt"
            )

        # Decode hex values
        try:
            auth_key_hash = bytes.fromhex(request_obj.auth_key_hash)
            salt = bytes.fromhex(request_obj.salt)
        except ValueError as e:
            return RegisterResponseJSON(
                success=False,
                error_code=ErrorCode.INVALID_REQUEST,
                error_message="Invalid hex encoding in auth_key_hash or salt"
            )

        # Validate lengths
        if len(auth_key_hash) != 32:
            return RegisterResponseJSON(
                success=False,
                error_code=ErrorCode.INVALID_REQUEST,
                error_message="auth_key_hash must be 32 bytes (SHA256)"
            )

        if len(salt) != 32:
            return RegisterResponseJSON(
                success=False,
                error_code=ErrorCode.INVALID_REQUEST,
                error_message="salt must be 32 bytes"
            )

        # Idempotent registration: if user already exists, return their user_id
        existing_user = await db.get_user_by_auth_hash(auth_key_hash)
        if existing_user:
            return RegisterResponseJSON(
                success=True,
                user_id=existing_user.user_id
            )

        # Generate user ID (UUIDv7-like, using UUID4 for PoC)
        user_id = generate_user_id()

        # Create user
        user = await db.create_user(
            user_id=user_id,
            auth_key_hash=auth_key_hash,
            salt=salt
        )

        return RegisterResponseJSON(
            success=True,
            user_id=user.user_id
        )

    except Exception as e:
        # Log error internally (don't expose details to client)
        return RegisterResponseJSON(
            success=False,
            error_code=ErrorCode.INTERNAL_ERROR,
            error_message="Registration failed"
        )
