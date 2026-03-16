"""
Stripe integration service for TotalReclaw billing.

Handles:
- Creating Stripe Checkout sessions for subscription upgrades
- Processing Stripe webhooks (checkout.session.completed, subscription events)
- Querying subscription status
- Free-tier usage tracking and enforcement
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import stripe
from sqlalchemy import text

from ..config import get_settings
from ..db.database import Database

logger = logging.getLogger(__name__)


def derive_features(tier: str) -> dict:
    """Derive feature flags from subscription tier.

    Client-side features gated by tier. Self-hosters can override
    since they control their own server.
    """
    is_pro = tier == "pro"
    return {
        "llm_dedup": is_pro,
        "custom_extract_interval": is_pro,
        "min_extract_interval": 2 if is_pro else 5,
    }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Stripe keys — loaded from environment at import time.
# The service methods validate they are set before using them.

STRIPE_SECRET_KEY: str = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET: str = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID: str = os.environ.get("STRIPE_PRICE_ID", "")


def _configure_stripe() -> None:
    """Set the Stripe API key on the stripe module. Idempotent."""
    if STRIPE_SECRET_KEY:
        stripe.api_key = STRIPE_SECRET_KEY


class StripeService:
    """Stateless service that wraps Stripe API calls and subscription DB ops."""

    def __init__(self, db: Database) -> None:
        self.db = db
        _configure_stripe()

    # ------------------------------------------------------------------
    # Checkout
    # ------------------------------------------------------------------

    async def create_checkout_session(
        self,
        wallet_address: str,
        price_id: Optional[str] = None,
    ) -> str:
        """
        Create a Stripe Checkout session for the given wallet.

        If a Stripe Customer already exists for this wallet, reuse it.
        Otherwise, create a new Customer so future sessions can prefill.

        Args:
            wallet_address: The user's ERC-4337 smart-account address.
            price_id: Stripe Price ID to use. Falls back to STRIPE_PRICE_ID env.

        Returns:
            The Checkout session URL the user should be redirected to.

        Raises:
            ValueError: If Stripe keys are not configured.
            stripe.error.StripeError: On Stripe API failures.
        """
        if not STRIPE_SECRET_KEY:
            raise ValueError(
                "STRIPE_SECRET_KEY is not configured. "
                "Set the environment variable before calling billing endpoints."
            )

        effective_price_id = price_id or STRIPE_PRICE_ID
        if not effective_price_id:
            raise ValueError(
                "No Stripe Price ID provided and STRIPE_PRICE_ID env var is not set."
            )

        # Look up or create Stripe Customer for this wallet
        customer_id = await self._get_or_create_stripe_customer(wallet_address)

        session = stripe.checkout.Session.create(
            customer=customer_id,
            client_reference_id=wallet_address,
            payment_method_types=["card"],
            mode="subscription",
            line_items=[
                {
                    "price": effective_price_id,
                    "quantity": 1,
                },
            ],
            success_url=(
                os.environ.get(
                    "STRIPE_SUCCESS_URL",
                    "https://totalreclaw.xyz/billing/success"
                    "?session_id={CHECKOUT_SESSION_ID}",
                )
            ),
            cancel_url=(
                os.environ.get(
                    "STRIPE_CANCEL_URL",
                    "https://totalreclaw.xyz/billing/cancel",
                )
            ),
        )

        logger.info(
            "Stripe Checkout session created",
            extra={
                "wallet_address": wallet_address,
                "session_id": session.id,
            },
        )

        if not session.url:
            raise ValueError("Stripe returned no checkout URL")
        return session.url

    # ------------------------------------------------------------------
    # Webhook handling
    # ------------------------------------------------------------------

    async def handle_webhook(
        self,
        payload: bytes,
        sig_header: str,
    ) -> dict:
        """
        Verify and process a Stripe webhook event.

        Supported events:
        - checkout.session.completed — activate pro subscription
        - customer.subscription.updated — sync expiry / cancellation
        - customer.subscription.deleted — downgrade to free
        - invoice.payment_succeeded — extend subscription period

        Args:
            payload: Raw request body bytes.
            sig_header: Value of the Stripe-Signature header.

        Returns:
            Dict with processing result, e.g. {"status": "ok", "event_type": "..."}.

        Raises:
            ValueError: If webhook secret is missing or signature is invalid.
        """
        if not STRIPE_WEBHOOK_SECRET:
            raise ValueError("STRIPE_WEBHOOK_SECRET is not configured.")

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except stripe.error.SignatureVerificationError as exc:
            logger.warning("Stripe webhook signature verification failed: %s", exc)
            raise ValueError("Invalid Stripe webhook signature") from exc

        event_type = event["type"]
        logger.info("Stripe webhook received", extra={"event_type": event_type})

        handler = {
            "checkout.session.completed": self._handle_checkout_completed,
            "customer.subscription.updated": self._handle_subscription_updated,
            "customer.subscription.deleted": self._handle_subscription_deleted,
            "invoice.payment_succeeded": self._handle_invoice_paid,
        }.get(event_type)

        if handler:
            await handler(event["data"]["object"])
        else:
            logger.debug("Unhandled Stripe event type: %s", event_type)

        return {"status": "ok", "event_type": event_type}

    # ------------------------------------------------------------------
    # Subscription status
    # ------------------------------------------------------------------

    async def get_subscription_status(self, wallet_address: str) -> dict:
        """
        Return the current subscription status for a wallet.

        If no subscription row exists, the wallet is on the free tier.

        Returns:
            {
                "wallet_address": str,
                "tier": "free" | "pro",
                "source": str | None,
                "expires_at": ISO-8601 str | None,
                "free_writes_used": int,
                "free_writes_limit": int,
            }
        """
        settings = get_settings()
        row = await self._get_subscription(wallet_address)

        if row is None:
            return {
                "wallet_address": wallet_address,
                "tier": "free",
                "source": None,
                "expires_at": None,
                "free_writes_used": 0,
                "free_writes_limit": settings.free_tier_writes_per_month,
                "features": derive_features("free"),
            }

        # Check if pro subscription has expired
        tier = row["tier"]
        if tier == "pro" and row["expires_at"]:
            if row["expires_at"] < datetime.now(timezone.utc):
                # Expired — treat as free
                tier = "free"

        return {
            "wallet_address": wallet_address,
            "tier": tier,
            "source": row["source"],
            "expires_at": (
                row["expires_at"].isoformat() if row["expires_at"] else None
            ),
            "free_writes_used": row["free_writes_used"],
            "free_writes_limit": settings.free_tier_writes_per_month,
            "features": derive_features(tier),
        }

    # ------------------------------------------------------------------
    # Free-tier usage gating
    # ------------------------------------------------------------------

    async def check_and_increment_free_usage(
        self,
        wallet_address: str,
    ) -> bool:
        """
        Check whether a free-tier wallet may perform another write.

        If allowed, atomically increments the counter and returns True.
        If the wallet is on the pro tier, always returns True (no counter bump).
        If the free-tier limit is reached, returns False.

        Monthly reset: if *free_writes_reset_at* is older than the start of
        the current calendar month (UTC), the counter is reset to 0 first.

        Args:
            wallet_address: The user's wallet address.

        Returns:
            True if the write is allowed, False otherwise.
        """
        settings = get_settings()

        async with self.db.session() as session:
            # Upsert the subscription row if it doesn't exist yet
            await session.execute(
                text("""
                    INSERT INTO subscriptions (wallet_address, tier, free_writes_used)
                    VALUES (:wallet, 'free', 0)
                    ON CONFLICT (wallet_address) DO NOTHING
                """),
                {"wallet": wallet_address},
            )

            # Fetch current state
            result = await session.execute(
                text("""
                    SELECT tier, free_writes_used, free_writes_reset_at, expires_at
                    FROM subscriptions
                    WHERE wallet_address = :wallet
                    FOR UPDATE
                """),
                {"wallet": wallet_address},
            )
            row = result.fetchone()

            # Pro tier — always allowed
            if row.tier == "pro":
                # But only if not expired
                if row.expires_at and row.expires_at < datetime.now(timezone.utc):
                    pass  # fall through to free-tier logic
                else:
                    return True

            # Monthly reset check
            now = datetime.now(timezone.utc)
            month_start = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            if row.free_writes_reset_at is None or row.free_writes_reset_at < month_start:
                await session.execute(
                    text("""
                        UPDATE subscriptions
                        SET free_writes_used = 0,
                            free_writes_reset_at = :month_start,
                            updated_at = NOW()
                        WHERE wallet_address = :wallet
                    """),
                    {"wallet": wallet_address, "month_start": month_start},
                )
                current_usage = 0
            else:
                current_usage = row.free_writes_used

            if current_usage >= settings.free_tier_writes_per_month:
                return False

            # Increment
            await session.execute(
                text("""
                    UPDATE subscriptions
                    SET free_writes_used = free_writes_used + 1,
                        updated_at = NOW()
                    WHERE wallet_address = :wallet
                """),
                {"wallet": wallet_address},
            )

            return True

    async def check_and_increment_free_read_usage(
        self,
        wallet_address: str,
    ) -> bool:
        """
        Check whether a free-tier wallet may perform another read.

        If allowed, atomically increments the counter and returns True.
        If the wallet is on the pro tier, checks pro read limits.
        If the tier limit is reached, returns False.

        Monthly reset: if *free_reads_reset_at* is older than the start of
        the current calendar month (UTC), the counter is reset to 0 first.

        Args:
            wallet_address: The user's wallet address.

        Returns:
            True if the read is allowed, False otherwise.
        """
        settings = get_settings()

        async with self.db.session() as session:
            # Upsert the subscription row if it doesn't exist yet
            await session.execute(
                text("""
                    INSERT INTO subscriptions (wallet_address, tier, free_reads_used)
                    VALUES (:wallet, 'free', 0)
                    ON CONFLICT (wallet_address) DO NOTHING
                """),
                {"wallet": wallet_address},
            )

            # Fetch current state
            result = await session.execute(
                text("""
                    SELECT tier, free_reads_used, free_reads_reset_at, expires_at
                    FROM subscriptions
                    WHERE wallet_address = :wallet
                    FOR UPDATE
                """),
                {"wallet": wallet_address},
            )
            row = result.fetchone()

            # Determine tier and limit
            is_pro = False
            if row.tier == "pro":
                if row.expires_at and row.expires_at < datetime.now(timezone.utc):
                    pass  # expired — treat as free
                else:
                    is_pro = True

            limit = (
                settings.pro_tier_reads_per_month if is_pro
                else settings.free_tier_reads_per_month
            )

            # Monthly reset check
            now = datetime.now(timezone.utc)
            month_start = now.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            if row.free_reads_reset_at is None or row.free_reads_reset_at < month_start:
                await session.execute(
                    text("""
                        UPDATE subscriptions
                        SET free_reads_used = 0,
                            free_reads_reset_at = :month_start,
                            updated_at = NOW()
                        WHERE wallet_address = :wallet
                    """),
                    {"wallet": wallet_address, "month_start": month_start},
                )
                current_usage = 0
            else:
                current_usage = row.free_reads_used

            if current_usage >= limit:
                return False

            # Increment
            await session.execute(
                text("""
                    UPDATE subscriptions
                    SET free_reads_used = free_reads_used + 1,
                        updated_at = NOW()
                    WHERE wallet_address = :wallet
                """),
                {"wallet": wallet_address},
            )

            return True

    # ==================================================================
    # Private helpers
    # ==================================================================

    async def _get_subscription(self, wallet_address: str) -> Optional[dict]:
        """Fetch the subscription row as a dict, or None."""
        async with self.db.session() as session:
            result = await session.execute(
                text("""
                    SELECT wallet_address, tier, source, stripe_id,
                           stripe_customer_id, coinbase_id, expires_at,
                           free_writes_used, free_writes_reset_at,
                           free_reads_used, free_reads_reset_at,
                           created_at, updated_at
                    FROM subscriptions
                    WHERE wallet_address = :wallet
                """),
                {"wallet": wallet_address},
            )
            row = result.fetchone()
            if row is None:
                return None
            return {
                "wallet_address": row.wallet_address,
                "tier": row.tier,
                "source": row.source,
                "stripe_id": row.stripe_id,
                "stripe_customer_id": row.stripe_customer_id,
                "coinbase_id": row.coinbase_id,
                "expires_at": row.expires_at,
                "free_writes_used": row.free_writes_used,
                "free_writes_reset_at": row.free_writes_reset_at,
                "free_reads_used": row.free_reads_used,
                "free_reads_reset_at": row.free_reads_reset_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }

    async def _get_or_create_stripe_customer(
        self,
        wallet_address: str,
    ) -> str:
        """
        Return an existing Stripe Customer ID for the wallet, or create one.

        The Customer ID is persisted in the subscriptions table so that
        subsequent checkout sessions reuse the same customer record.
        """
        sub = await self._get_subscription(wallet_address)
        if sub and sub.get("stripe_customer_id"):
            return sub["stripe_customer_id"]

        # Create a new Stripe Customer
        customer = stripe.Customer.create(
            metadata={"wallet_address": wallet_address},
        )
        customer_id = customer["id"]

        # Upsert subscription row with the new customer ID
        async with self.db.session() as session:
            await session.execute(
                text("""
                    INSERT INTO subscriptions
                        (wallet_address, tier, stripe_customer_id, free_writes_used)
                    VALUES (:wallet, 'free', :cus_id, 0)
                    ON CONFLICT (wallet_address)
                    DO UPDATE SET stripe_customer_id = :cus_id,
                                  updated_at = NOW()
                """),
                {"wallet": wallet_address, "cus_id": customer_id},
            )

        logger.info(
            "Stripe Customer created",
            extra={
                "wallet_address": wallet_address,
                "customer_id": customer_id,
            },
        )

        return customer_id

    # ------------------------------------------------------------------
    # Webhook event handlers
    # ------------------------------------------------------------------

    async def _handle_checkout_completed(self, session_obj: dict) -> None:
        """
        Handle checkout.session.completed — activate pro subscription.

        Extracts the wallet address from client_reference_id and the
        Stripe Subscription ID from the session object.
        """
        wallet_address = session_obj.get("client_reference_id")
        if not wallet_address:
            logger.error(
                "checkout.session.completed missing client_reference_id"
            )
            return

        stripe_subscription_id = session_obj.get("subscription")
        stripe_customer_id = session_obj.get("customer")

        # Fetch subscription details from Stripe to get current_period_end
        expires_at = None
        if stripe_subscription_id:
            try:
                sub = stripe.Subscription.retrieve(stripe_subscription_id)
                expires_at = datetime.fromtimestamp(
                    sub["current_period_end"], tz=timezone.utc
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch Stripe subscription details: %s", exc
                )

        async with self.db.session() as db_session:
            await db_session.execute(
                text("""
                    INSERT INTO subscriptions
                        (wallet_address, tier, source, stripe_id,
                         stripe_customer_id, expires_at, free_writes_used)
                    VALUES (:wallet, 'pro', 'stripe', :stripe_id,
                            :cus_id, :expires, 0)
                    ON CONFLICT (wallet_address)
                    DO UPDATE SET tier = 'pro',
                                  source = 'stripe',
                                  stripe_id = :stripe_id,
                                  stripe_customer_id = :cus_id,
                                  expires_at = :expires,
                                  updated_at = NOW()
                """),
                {
                    "wallet": wallet_address,
                    "stripe_id": stripe_subscription_id,
                    "cus_id": stripe_customer_id,
                    "expires": expires_at,
                },
            )

        logger.info(
            "Subscription activated via checkout",
            extra={
                "wallet_address": wallet_address,
                "stripe_id": stripe_subscription_id,
            },
        )

    async def _handle_subscription_updated(self, sub_obj: dict) -> None:
        """
        Handle customer.subscription.updated — sync status and expiry.

        Covers cases like plan changes, trial→active, and cancellation scheduling.
        """
        stripe_subscription_id = sub_obj.get("id")
        status = sub_obj.get("status")  # active, past_due, canceled, etc.
        current_period_end = sub_obj.get("current_period_end")

        # Find the wallet by stripe_id
        wallet_address = await self._wallet_for_stripe_id(stripe_subscription_id)
        if not wallet_address:
            logger.warning(
                "subscription.updated for unknown stripe_id: %s",
                stripe_subscription_id,
            )
            return

        # Map Stripe status to our tier
        tier = "pro" if status in ("active", "trialing", "past_due") else "free"
        expires_at = (
            datetime.fromtimestamp(current_period_end, tz=timezone.utc)
            if current_period_end
            else None
        )

        async with self.db.session() as db_session:
            await db_session.execute(
                text("""
                    UPDATE subscriptions
                    SET tier = :tier,
                        expires_at = :expires,
                        updated_at = NOW()
                    WHERE wallet_address = :wallet
                """),
                {
                    "tier": tier,
                    "expires": expires_at,
                    "wallet": wallet_address,
                },
            )

        logger.info(
            "Subscription updated",
            extra={
                "wallet_address": wallet_address,
                "stripe_status": status,
                "tier": tier,
            },
        )

    async def _handle_subscription_deleted(self, sub_obj: dict) -> None:
        """
        Handle customer.subscription.deleted — downgrade to free tier.
        """
        stripe_subscription_id = sub_obj.get("id")
        wallet_address = await self._wallet_for_stripe_id(stripe_subscription_id)
        if not wallet_address:
            logger.warning(
                "subscription.deleted for unknown stripe_id: %s",
                stripe_subscription_id,
            )
            return

        async with self.db.session() as db_session:
            await db_session.execute(
                text("""
                    UPDATE subscriptions
                    SET tier = 'free',
                        expires_at = NULL,
                        stripe_id = NULL,
                        updated_at = NOW()
                    WHERE wallet_address = :wallet
                """),
                {"wallet": wallet_address},
            )

        logger.info(
            "Subscription deleted — downgraded to free",
            extra={"wallet_address": wallet_address},
        )

    async def _handle_invoice_paid(self, invoice_obj: dict) -> None:
        """
        Handle invoice.payment_succeeded — extend the subscription period.

        This fires on each successful renewal payment.
        """
        stripe_subscription_id = invoice_obj.get("subscription")
        if not stripe_subscription_id:
            return

        wallet_address = await self._wallet_for_stripe_id(stripe_subscription_id)
        if not wallet_address:
            return

        # Fetch updated period from Stripe
        try:
            sub = stripe.Subscription.retrieve(stripe_subscription_id)
            expires_at = datetime.fromtimestamp(
                sub["current_period_end"], tz=timezone.utc
            )
        except Exception as exc:
            logger.warning("Failed to fetch subscription for renewal: %s", exc)
            return

        async with self.db.session() as db_session:
            await db_session.execute(
                text("""
                    UPDATE subscriptions
                    SET tier = 'pro',
                        expires_at = :expires,
                        updated_at = NOW()
                    WHERE wallet_address = :wallet
                """),
                {"wallet": wallet_address, "expires": expires_at},
            )

        logger.info(
            "Subscription renewed",
            extra={
                "wallet_address": wallet_address,
                "new_expires_at": expires_at.isoformat(),
            },
        )

    async def _wallet_for_stripe_id(
        self,
        stripe_subscription_id: str,
    ) -> Optional[str]:
        """Look up the wallet address that owns a given Stripe Subscription ID."""
        async with self.db.session() as session:
            result = await session.execute(
                text("""
                    SELECT wallet_address
                    FROM subscriptions
                    WHERE stripe_id = :sid
                    LIMIT 1
                """),
                {"sid": stripe_subscription_id},
            )
            row = result.fetchone()
            return row.wallet_address if row else None
