"""
FastAPI routes for TotalReclaw billing.

Endpoints:
    POST /v1/billing/checkout        -- Create a Stripe Checkout session (auth required)
    POST /v1/billing/checkout/crypto  -- Create a Coinbase Commerce charge (auth required)
    POST /v1/billing/webhook/stripe   -- Stripe webhook receiver (no auth)
    POST /v1/billing/webhook/coinbase -- Coinbase Commerce webhook receiver (no auth)
    GET  /v1/billing/status           -- Get subscription status (auth required)
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..db import get_db, Database
from ..dependencies import get_current_user
from .stripe_service import StripeService
from .coinbase_service import CoinbaseService, CoinbaseServiceError, CoinbaseWebhookError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class CheckoutRequest(BaseModel):
    """Request body for POST /v1/billing/checkout."""
    wallet_address: str = Field(
        ...,
        description="ERC-4337 Smart Account address",
        max_length=100,
    )
    tier: str = Field(
        "pro",
        description="Target subscription tier (currently only 'pro')",
        max_length=20,
    )


class CheckoutResponse(BaseModel):
    """Response for POST /v1/billing/checkout."""
    success: bool
    checkout_url: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class StatusResponse(BaseModel):
    """Response for GET /v1/billing/status."""
    success: bool
    wallet_address: Optional[str] = None
    tier: Optional[str] = None
    source: Optional[str] = None
    expires_at: Optional[str] = None
    free_writes_used: Optional[int] = None
    free_writes_limit: Optional[int] = None
    features: Optional[dict] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class CryptoCheckoutResponse(BaseModel):
    """Response for POST /v1/billing/checkout/crypto."""
    success: bool
    checkout_url: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class WebhookResponse(BaseModel):
    """Response for POST /v1/billing/webhook/stripe and /webhook/coinbase."""
    success: bool
    event_type: Optional[str] = None
    status: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    body: CheckoutRequest,
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """
    Create a Stripe Checkout session for subscription upgrade.

    The authenticated user must own the wallet_address. Since wallet-based
    auth maps 1:1 with user_id, we use the wallet_address from the request
    body (the agent knows the wallet, the server knows the user_id).

    Returns a checkout_url the agent presents to the user.
    """
    if body.tier != "pro":
        return CheckoutResponse(
            success=False,
            error_code="INVALID_TIER",
            error_message="Only 'pro' tier is currently available.",
        )

    try:
        svc = StripeService(db)
        checkout_url = await svc.create_checkout_session(
            wallet_address=body.wallet_address,
        )
        return CheckoutResponse(success=True, checkout_url=checkout_url)

    except ValueError as exc:
        logger.error("Checkout configuration error: %s", exc)
        return CheckoutResponse(
            success=False,
            error_code="CONFIG_ERROR",
            error_message=str(exc),
        )
    except Exception as exc:
        logger.error("Checkout session creation failed: %s", exc, exc_info=True)
        return CheckoutResponse(
            success=False,
            error_code="CHECKOUT_FAILED",
            error_message="Failed to create checkout session. Please try again.",
        )


@router.post(
    "/webhook/stripe",
    response_model=WebhookResponse,
    include_in_schema=False,  # Hide from public API docs
)
async def stripe_webhook(
    request: Request,
    db: Database = Depends(get_db),
):
    """
    Stripe webhook receiver.

    This endpoint does NOT require wallet auth -- it is called directly
    by Stripe's servers. Authentication is via Stripe webhook signature
    verification (STRIPE_WEBHOOK_SECRET).
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not sig_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe-Signature header",
        )

    try:
        svc = StripeService(db)
        result = await svc.handle_webhook(payload, sig_header)
        return WebhookResponse(
            success=True,
            event_type=result.get("event_type"),
        )

    except ValueError as exc:
        logger.warning("Webhook validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        logger.error("Webhook processing failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing error",
        )


@router.get("/status", response_model=StatusResponse)
async def get_status(
    wallet_address: str,
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """
    Get the current subscription status for a wallet address.

    Requires wallet auth. The wallet_address is passed as a query parameter.
    """
    try:
        svc = StripeService(db)
        sub_status = await svc.get_subscription_status(wallet_address)
        return StatusResponse(
            success=True,
            wallet_address=sub_status["wallet_address"],
            tier=sub_status["tier"],
            source=sub_status["source"],
            expires_at=sub_status["expires_at"],
            free_writes_used=sub_status["free_writes_used"],
            free_writes_limit=sub_status["free_writes_limit"],
            features=sub_status.get("features"),
        )

    except Exception as exc:
        logger.error("Failed to fetch subscription status: %s", exc, exc_info=True)
        return StatusResponse(
            success=False,
            error_code="STATUS_ERROR",
            error_message="Failed to fetch subscription status.",
        )


# ---------------------------------------------------------------------------
# Coinbase Commerce endpoints
# ---------------------------------------------------------------------------

@router.post("/checkout/crypto", response_model=CryptoCheckoutResponse)
async def create_crypto_checkout(
    body: CheckoutRequest,
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """
    Create a Coinbase Commerce charge for crypto payment.

    Accepts USDC, USDT, and ETH on Base, Ethereum, Solana, Polygon, and Arbitrum.
    Returns a hosted checkout URL the agent presents to the user.
    """
    if body.tier != "pro":
        return CryptoCheckoutResponse(
            success=False,
            error_code="INVALID_TIER",
            error_message="Only 'pro' tier is currently available.",
        )

    try:
        svc = CoinbaseService(db)
        checkout_url = await svc.create_charge(
            wallet_address=body.wallet_address,
        )
        return CryptoCheckoutResponse(success=True, checkout_url=checkout_url)

    except CoinbaseServiceError as exc:
        logger.error("Coinbase checkout failed: %s", exc, exc_info=True)
        return CryptoCheckoutResponse(
            success=False,
            error_code="CHECKOUT_FAILED",
            error_message="Failed to create crypto checkout. Please try again.",
        )


@router.post(
    "/webhook/coinbase",
    response_model=WebhookResponse,
    include_in_schema=False,
)
async def coinbase_webhook(
    request: Request,
    db: Database = Depends(get_db),
):
    """
    Coinbase Commerce webhook receiver.

    No wallet auth -- called by Coinbase servers.
    Authenticated via HMAC-SHA256 signature (X-CC-Webhook-Signature header).
    """
    payload = await request.body()
    sig_header = request.headers.get("x-cc-webhook-signature", "")

    if not sig_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-CC-Webhook-Signature header",
        )

    try:
        svc = CoinbaseService(db)
        result = await svc.handle_webhook(payload, sig_header)
        return WebhookResponse(
            success=True,
            event_type=result.get("event_type"),
            status=result.get("status"),
        )

    except CoinbaseWebhookError as exc:
        logger.warning("Coinbase webhook validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:
        logger.error("Coinbase webhook processing failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing error",
        )
