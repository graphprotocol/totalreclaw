"""
Account management endpoints for TotalReclaw Server.

Includes GDPR-compliant account deletion.
"""
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Header, Request
from pydantic import BaseModel
from typing import Optional

from ..db import get_db, Database
from ..dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])


# ============ Response Models ============

class AccountDeletionResponse(BaseModel):
    """Account deletion response."""
    success: bool
    message: Optional[str] = None
    purge_scheduled_at: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


# ============ Endpoint ============

@router.delete("/account", response_model=AccountDeletionResponse)
async def delete_account(
    request: Request,
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db)
):
    """
    Delete user account (GDPR compliance).

    Soft-deletes the user record and deactivates all facts.
    Data is permanently purged after 30 days.

    To request immediate purge, contact support.
    """
    try:
        deleted = await db.soft_delete_user(user_id)

        if not deleted:
            return AccountDeletionResponse(
                success=False,
                error_code="NOT_FOUND",
                error_message="Account not found or already deleted"
            )

        purge_date = datetime.now(timezone.utc) + timedelta(days=30)

        logger.info(f"Account deleted: user_id={user_id}, purge_at={purge_date.isoformat()}")

        return AccountDeletionResponse(
            success=True,
            message="Account scheduled for deletion. All data will be permanently purged after 30 days.",
            purge_scheduled_at=purge_date.isoformat()
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Account deletion failed: {e}", exc_info=True)
        return AccountDeletionResponse(
            success=False,
            error_code="INTERNAL_ERROR",
            error_message="Account deletion failed"
        )
