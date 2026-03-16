"""
Integration tests for the TotalReclaw billing module.

Tests cover:
- Stripe Checkout session creation
- Stripe webhook processing (checkout.session.completed)
- Stripe webhook signature validation
- Coinbase Commerce charge creation
- Coinbase Commerce webhook processing (charge:confirmed)
- Coinbase Commerce webhook signature validation
- Coinbase Commerce idempotency
- Subscription status (free / pro)
- Free-tier usage tracking and limits
- Auth enforcement on billing endpoints

All external services (Stripe API, Coinbase Commerce API) are mocked.
Database is mocked via the conftest.py mock_db pattern.

Run with:
    cd server && python -m pytest tests/test_billing.py -v
"""
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A valid 32-byte auth key (hex) for test requests
TEST_AUTH_KEY = os.urandom(32)
TEST_AUTH_KEY_HEX = TEST_AUTH_KEY.hex()
TEST_AUTH_HEADERS = {"Authorization": f"Bearer {TEST_AUTH_KEY_HEX}"}

TEST_WALLET = "0x1234567890abcdef1234567890abcdef12345678"


def _make_user_mock():
    """Return a mock user object that satisfies get_current_user."""
    return type("User", (), {
        "user_id": "test-user-001",
        "auth_key_hash": hashlib.sha256(TEST_AUTH_KEY).digest(),
    })()


def _coinbase_webhook_signature(payload: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature the same way CoinbaseService does."""
    return hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Mock DB that supports billing operations
# ---------------------------------------------------------------------------

class BillingMockDB:
    """
    A mock Database instance with an async context-manager session()
    that stores subscription rows in memory.

    This allows the StripeService and CoinbaseService to execute their
    raw SQL via ``session.execute(text(...), params)`` and have the
    results be consistent across calls within a test.
    """

    def __init__(self):
        # In-memory subscription store: wallet_address -> dict
        self.subscriptions: dict = {}
        # Track auth lookups
        self._user = _make_user_mock()

    # -- Auth helpers expected by conftest / dependencies --
    async def get_user_by_auth_hash(self, auth_hash):
        return self._user

    async def update_last_seen(self, user_id):
        pass

    async def health_check(self):
        return {"status": "connected"}

    # -- The billing services use db.session() as an async context manager --
    class _MockSession:
        """Minimal async session that intercepts execute() calls."""

        def __init__(self, db: "BillingMockDB"):
            self._db = db

        async def execute(self, stmt, params=None):
            """Route SQL statements to in-memory store."""
            sql = str(stmt.text if hasattr(stmt, "text") else stmt).strip()
            params = params or {}

            # ----- INSERT INTO subscriptions ... ON CONFLICT ... -----
            if sql.upper().startswith("INSERT INTO SUBSCRIPTIONS"):
                wallet = params.get("wallet") or params.get("addr")

                # Detect tier from SQL literal VALUES (e.g. VALUES(:wallet, 'pro', ...))
                detected_tier = "free"
                if "'pro'" in sql:
                    detected_tier = "pro"
                # Params override if present
                if "tier" in params:
                    detected_tier = params["tier"]

                # Detect source from SQL literal VALUES
                detected_source = None
                if "'stripe'" in sql and "source" in sql.lower():
                    detected_source = "stripe"
                elif "'coinbase_commerce'" in sql and "source" in sql.lower():
                    detected_source = "coinbase_commerce"
                if "source" in params:
                    detected_source = params["source"]

                if wallet and wallet not in self._db.subscriptions:
                    self._db.subscriptions[wallet] = {
                        "wallet_address": wallet,
                        "tier": detected_tier,
                        "source": detected_source,
                        "stripe_id": params.get("stripe_id") if "stripe_id" in params else None,
                        "stripe_customer_id": params.get("cus_id") if "cus_id" in params else None,
                        "coinbase_id": params.get("cid") if "cid" in params else None,
                        "expires_at": params.get("expires") or params.get("exp"),
                        "free_writes_used": 0,
                        "free_writes_reset_at": None,
                        "free_reads_used": 0,
                        "free_reads_reset_at": None,
                        "created_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                elif wallet and wallet in self._db.subscriptions:
                    # ON CONFLICT ... DO UPDATE
                    row = self._db.subscriptions[wallet]
                    if "DO UPDATE" in sql.upper():
                        row["tier"] = detected_tier
                        if detected_source:
                            row["source"] = detected_source
                        for key in ("stripe_id", "cus_id"):
                            if key in params:
                                mapped = key if key != "cus_id" else "stripe_customer_id"
                                row[mapped] = params[key]
                        if "cid" in params:
                            row["coinbase_id"] = params["cid"]
                        if "expires" in params:
                            row["expires_at"] = params["expires"]
                        if "exp" in params:
                            row["expires_at"] = params["exp"]
                    row["updated_at"] = datetime.now(timezone.utc)
                return _EmptyResult()

            # ----- UPDATE subscriptions SET ... -----
            if sql.upper().startswith("UPDATE SUBSCRIPTIONS"):
                wallet = params.get("wallet") or params.get("addr")
                if wallet and wallet in self._db.subscriptions:
                    row = self._db.subscriptions[wallet]
                    if "tier" in params:
                        row["tier"] = params["tier"]
                    if "'pro'" in sql and "tier" in sql.lower():
                        row["tier"] = "pro"
                    if "'free'" in sql and "tier" in sql.lower():
                        row["tier"] = "free"
                    if "expires" in params:
                        row["expires_at"] = params["expires"]
                    if "exp" in params:
                        row["expires_at"] = params["exp"]
                    if "cid" in params:
                        row["coinbase_id"] = params["cid"]
                    if "'coinbase_commerce'" in sql and "source" in sql.lower():
                        row["source"] = "coinbase_commerce"
                    if "'stripe'" in sql and "source" in sql.lower():
                        row["source"] = "stripe"
                    if "stripe_id" in params:
                        row["stripe_id"] = params["stripe_id"]
                    if "free_writes_used = 0" in sql:
                        row["free_writes_used"] = 0
                    if "free_reads_used = 0" in sql:
                        row["free_reads_used"] = 0
                    if "month_start" in params:
                        # Determine which reset field to update based on SQL
                        if "free_reads_reset_at" in sql:
                            row["free_reads_reset_at"] = params["month_start"]
                        else:
                            row["free_writes_reset_at"] = params["month_start"]
                    if "free_writes_used = free_writes_used + 1" in sql:
                        row["free_writes_used"] = row.get("free_writes_used", 0) + 1
                    if "free_reads_used = free_reads_used + 1" in sql:
                        row["free_reads_used"] = row.get("free_reads_used", 0) + 1
                    row["updated_at"] = datetime.now(timezone.utc)
                return _EmptyResult()

            # ----- SELECT ... FROM subscriptions WHERE wallet_address = :wallet -----
            if "FROM SUBSCRIPTIONS" in sql.upper().replace("\n", " "):
                wallet = params.get("wallet") or params.get("addr") or params.get("sid")
                # Look up by stripe_id if that's the parameter
                if "stripe_id = :sid" in sql:
                    sid = params.get("sid")
                    for w, row in self._db.subscriptions.items():
                        if row.get("stripe_id") == sid:
                            return _SingleRowResult(row)
                    return _EmptyResult()
                if wallet and wallet in self._db.subscriptions:
                    return _SingleRowResult(self._db.subscriptions[wallet])
                return _EmptyResult()

            return _EmptyResult()

        async def commit(self):
            pass

        async def rollback(self):
            pass

    class _SessionCtx:
        """Async context manager returned by db.session()."""

        def __init__(self, db: "BillingMockDB"):
            self._session = BillingMockDB._MockSession(db)

        async def __aenter__(self):
            return self._session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type:
                await self._session.rollback()
            else:
                await self._session.commit()
            return False

    def session(self):
        return self._SessionCtx(self)


class _EmptyResult:
    """Mock query result with no rows."""

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _SingleRowResult:
    """Mock query result that returns a single dict-backed row."""

    def __init__(self, data: dict):
        self._data = data

    def fetchone(self):
        return _Row(self._data)

    def fetchall(self):
        return [_Row(self._data)]


class _Row:
    """A row object with attribute-based and dict-based access."""

    def __init__(self, data: dict):
        self._data = data
        for k, v in data.items():
            setattr(self, k, v)

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def billing_db(monkeypatch):
    """
    Set up a BillingMockDB and patch it into all the places FastAPI resolves get_db.
    Also patches get_current_user so authenticated endpoints pass auth.
    """
    from src.db import database as db_module

    mock = BillingMockDB()

    # Patch get_db everywhere
    modules = [
        "src.db.database",
        "src.db",
        "src.dependencies",
        "src.billing.routes",
    ]
    for mod in modules:
        try:
            monkeypatch.setattr(f"{mod}.get_db", lambda: mock)
        except AttributeError:
            pass

    monkeypatch.setattr(db_module, "_db", mock)
    return mock


@pytest.fixture
def billing_client(monkeypatch, billing_db):
    """
    A FastAPI TestClient with billing mock DB installed and
    init_db/close_db patched to no-ops.
    """
    from src import main as main_module
    from src.db import database as db_module
    from fastapi.testclient import TestClient

    async def _noop_init(url=None):
        pass

    async def _noop_close():
        pass

    # Ensure get_db returns our billing mock
    monkeypatch.setattr(db_module, "_db", billing_db)

    with patch.object(main_module, "init_db", side_effect=_noop_init), \
         patch.object(main_module, "close_db", side_effect=_noop_close):
        with TestClient(main_module.app) as c:
            yield c


# ---------------------------------------------------------------------------
# Stripe: Checkout tests
# ---------------------------------------------------------------------------

class TestStripeCheckout:
    """Tests for POST /v1/billing/checkout."""

    @patch("src.billing.stripe_service.stripe.Customer.create")
    @patch("src.billing.stripe_service.stripe.checkout.Session.create")
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    @patch("src.billing.stripe_service.STRIPE_PRICE_ID", "price_test_fake")
    def test_checkout_creates_session(
        self, mock_session_create, mock_customer_create, billing_client, billing_db
    ):
        """POST /v1/billing/checkout with valid wallet returns checkout_url."""
        mock_customer_create.return_value = {"id": "cus_test_123"}
        mock_session_create.return_value = MagicMock(
            id="cs_test_session_001",
            url="https://checkout.stripe.com/pay/cs_test_session_001",
        )

        resp = billing_client.post(
            "/v1/billing/checkout",
            json={"wallet_address": TEST_WALLET, "tier": "pro"},
            headers=TEST_AUTH_HEADERS,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["checkout_url"] == "https://checkout.stripe.com/pay/cs_test_session_001"

    def test_checkout_invalid_tier(self, billing_client):
        """POST /v1/billing/checkout with tier='invalid' returns an error."""
        resp = billing_client.post(
            "/v1/billing/checkout",
            json={"wallet_address": TEST_WALLET, "tier": "invalid"},
            headers=TEST_AUTH_HEADERS,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error_code"] == "INVALID_TIER"

    def test_checkout_requires_auth(self, billing_client):
        """POST /v1/billing/checkout without auth header returns 401."""
        resp = billing_client.post(
            "/v1/billing/checkout",
            json={"wallet_address": TEST_WALLET, "tier": "pro"},
        )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Stripe: Webhook tests
# ---------------------------------------------------------------------------

class TestStripeWebhook:
    """Tests for POST /v1/billing/webhook/stripe."""

    @patch("src.billing.stripe_service.stripe.Subscription.retrieve")
    @patch("src.billing.stripe_service.stripe.Webhook.construct_event")
    @patch("src.billing.stripe_service.STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    def test_stripe_webhook_activates_subscription(
        self, mock_construct, mock_sub_retrieve, billing_client, billing_db
    ):
        """
        POST /v1/billing/webhook/stripe with a valid checkout.session.completed
        event creates/updates a subscription in the DB.
        """
        future_ts = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        mock_sub_retrieve.return_value = {"current_period_end": future_ts}

        mock_construct.return_value = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": TEST_WALLET,
                    "subscription": "sub_test_001",
                    "customer": "cus_test_001",
                }
            },
        }

        payload = b'{"type":"checkout.session.completed"}'
        resp = billing_client.post(
            "/v1/billing/webhook/stripe",
            content=payload,
            headers={"stripe-signature": "t=123,v1=abc123"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["event_type"] == "checkout.session.completed"

        # Verify subscription was created in mock DB
        sub = billing_db.subscriptions.get(TEST_WALLET)
        assert sub is not None
        assert sub["tier"] == "pro"
        assert sub["source"] == "stripe"

    @patch("src.billing.stripe_service.stripe.Webhook.construct_event")
    @patch("src.billing.stripe_service.STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    def test_stripe_webhook_invalid_signature(
        self, mock_construct, billing_client
    ):
        """
        POST /v1/billing/webhook/stripe with an invalid signature returns 400.
        """
        import stripe
        mock_construct.side_effect = stripe.error.SignatureVerificationError(
            "Invalid signature", "sig_header_value"
        )

        payload = b'{"type":"checkout.session.completed"}'
        resp = billing_client.post(
            "/v1/billing/webhook/stripe",
            content=payload,
            headers={"stripe-signature": "t=123,v1=bad_signature"},
        )

        assert resp.status_code == 400

    def test_stripe_webhook_missing_signature_header(self, billing_client):
        """
        POST /v1/billing/webhook/stripe without Stripe-Signature header returns 400.
        """
        payload = b'{"type":"checkout.session.completed"}'
        resp = billing_client.post(
            "/v1/billing/webhook/stripe",
            content=payload,
            # No stripe-signature header
        )

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Stripe: Subscription status tests
# ---------------------------------------------------------------------------

class TestSubscriptionStatus:
    """Tests for GET /v1/billing/status."""

    def test_status_returns_free_tier(self, billing_client, billing_db):
        """GET /v1/billing/status for a wallet with no subscription returns tier=free."""
        resp = billing_client.get(
            "/v1/billing/status",
            params={"wallet_address": TEST_WALLET},
            headers=TEST_AUTH_HEADERS,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["tier"] == "free"
        assert data["wallet_address"] == TEST_WALLET
        assert data["free_writes_used"] == 0

    @patch("src.billing.stripe_service.stripe.Subscription.retrieve")
    @patch("src.billing.stripe_service.stripe.Webhook.construct_event")
    @patch("src.billing.stripe_service.STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    def test_status_returns_pro_after_payment(
        self, mock_construct, mock_sub_retrieve, billing_client, billing_db
    ):
        """
        GET /v1/billing/status after a successful Stripe webhook returns tier=pro.
        """
        future_ts = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        mock_sub_retrieve.return_value = {"current_period_end": future_ts}

        # First, simulate the webhook
        mock_construct.return_value = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": TEST_WALLET,
                    "subscription": "sub_test_002",
                    "customer": "cus_test_002",
                }
            },
        }

        billing_client.post(
            "/v1/billing/webhook/stripe",
            content=b'{"type":"checkout.session.completed"}',
            headers={"stripe-signature": "t=123,v1=abc"},
        )

        # Now check status
        resp = billing_client.get(
            "/v1/billing/status",
            params={"wallet_address": TEST_WALLET},
            headers=TEST_AUTH_HEADERS,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["tier"] == "pro"
        assert data["source"] == "stripe"


# ---------------------------------------------------------------------------
# Coinbase Commerce: Checkout tests
# ---------------------------------------------------------------------------

class TestCoinbaseCheckout:
    """Tests for POST /v1/billing/checkout/crypto."""

    @patch("src.billing.coinbase_service.httpx.AsyncClient")
    def test_crypto_checkout_creates_charge(
        self, mock_httpx_cls, billing_client, billing_db, monkeypatch
    ):
        """
        POST /v1/billing/checkout/crypto with a valid wallet returns a checkout_url.
        """
        # Patch the settings to provide an API key
        from src.config import Settings
        monkeypatch.setattr(
            "src.billing.coinbase_service.get_settings",
            lambda: type("S", (), {
                "coinbase_commerce_api_key": "test_api_key_123",
                "coinbase_commerce_webhook_secret": "test_webhook_secret",
            })(),
        )

        # Mock the httpx AsyncClient context manager and response
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "data": {
                "id": "charge_test_001",
                "hosted_url": "https://commerce.coinbase.com/charges/charge_test_001",
            }
        }

        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls.return_value = mock_client_instance

        resp = billing_client.post(
            "/v1/billing/checkout/crypto",
            json={"wallet_address": TEST_WALLET, "tier": "pro"},
            headers=TEST_AUTH_HEADERS,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["checkout_url"] == "https://commerce.coinbase.com/charges/charge_test_001"

    def test_crypto_checkout_invalid_tier(self, billing_client):
        """POST /v1/billing/checkout/crypto with tier='invalid' returns error."""
        resp = billing_client.post(
            "/v1/billing/checkout/crypto",
            json={"wallet_address": TEST_WALLET, "tier": "invalid"},
            headers=TEST_AUTH_HEADERS,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error_code"] == "INVALID_TIER"


# ---------------------------------------------------------------------------
# Coinbase Commerce: Webhook tests
# ---------------------------------------------------------------------------

class TestCoinbaseWebhook:
    """Tests for POST /v1/billing/webhook/coinbase."""

    WEBHOOK_SECRET = "test_coinbase_webhook_secret"

    def _make_coinbase_event(self, event_type: str, charge_id: str, wallet: str) -> bytes:
        """Build a Coinbase Commerce webhook event payload."""
        return json.dumps({
            "event": {
                "type": event_type,
                "data": {
                    "id": charge_id,
                    "code": charge_id,
                    "metadata": {
                        "wallet_address": wallet,
                    },
                },
            },
        }).encode("utf-8")

    def _patch_settings(self, monkeypatch):
        """Patch get_settings to provide Coinbase keys."""
        monkeypatch.setattr(
            "src.billing.coinbase_service.get_settings",
            lambda: type("S", (), {
                "coinbase_commerce_api_key": "test_api_key",
                "coinbase_commerce_webhook_secret": self.WEBHOOK_SECRET,
            })(),
        )

    def test_coinbase_webhook_activates_subscription(
        self, billing_client, billing_db, monkeypatch
    ):
        """
        POST /v1/billing/webhook/coinbase with a valid charge:confirmed event
        creates a pro subscription in the DB.
        """
        self._patch_settings(monkeypatch)

        payload = self._make_coinbase_event(
            "charge:confirmed", "cb_charge_001", TEST_WALLET
        )
        signature = _coinbase_webhook_signature(payload, self.WEBHOOK_SECRET)

        resp = billing_client.post(
            "/v1/billing/webhook/coinbase",
            content=payload,
            headers={"x-cc-webhook-signature": signature},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        # Verify subscription was created
        sub = billing_db.subscriptions.get(TEST_WALLET)
        assert sub is not None
        assert sub["tier"] == "pro"
        assert sub["coinbase_id"] == "cb_charge_001"

    def test_coinbase_webhook_invalid_signature(
        self, billing_client, billing_db, monkeypatch
    ):
        """
        POST /v1/billing/webhook/coinbase with an invalid signature returns 400.
        """
        self._patch_settings(monkeypatch)

        payload = self._make_coinbase_event(
            "charge:confirmed", "cb_charge_002", TEST_WALLET
        )

        resp = billing_client.post(
            "/v1/billing/webhook/coinbase",
            content=payload,
            headers={"x-cc-webhook-signature": "bad_signature_value"},
        )

        assert resp.status_code == 400

    def test_coinbase_webhook_missing_signature_header(self, billing_client):
        """
        POST /v1/billing/webhook/coinbase without X-CC-Webhook-Signature returns 400.
        """
        payload = b'{"event":{"type":"charge:confirmed"}}'
        resp = billing_client.post(
            "/v1/billing/webhook/coinbase",
            content=payload,
            # No signature header
        )

        assert resp.status_code == 400

    def test_coinbase_webhook_idempotent(
        self, billing_client, billing_db, monkeypatch
    ):
        """
        Sending the same charge_id twice does not extend the subscription.
        The second call should be a no-op (idempotent).
        """
        self._patch_settings(monkeypatch)

        charge_id = "cb_charge_idempotent"
        payload = self._make_coinbase_event(
            "charge:confirmed", charge_id, TEST_WALLET
        )
        signature = _coinbase_webhook_signature(payload, self.WEBHOOK_SECRET)

        # First webhook call
        resp1 = billing_client.post(
            "/v1/billing/webhook/coinbase",
            content=payload,
            headers={"x-cc-webhook-signature": signature},
        )
        assert resp1.status_code == 200

        # Record the expiry after first activation
        sub = billing_db.subscriptions.get(TEST_WALLET)
        assert sub is not None
        first_expires = sub["expires_at"]

        # Second webhook call with the same charge_id
        resp2 = billing_client.post(
            "/v1/billing/webhook/coinbase",
            content=payload,
            headers={"x-cc-webhook-signature": signature},
        )
        assert resp2.status_code == 200

        # Expiry should NOT have changed (idempotent)
        sub_after = billing_db.subscriptions.get(TEST_WALLET)
        assert sub_after["expires_at"] == first_expires


# ---------------------------------------------------------------------------
# Free-tier usage tests
# ---------------------------------------------------------------------------

class TestFreeTierUsage:
    """Tests for StripeService.check_and_increment_free_usage."""

    @pytest.mark.asyncio
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    async def test_free_tier_usage_increments(self):
        """check_and_increment_free_usage increments the free_writes_used counter."""
        db = BillingMockDB()
        svc = _make_stripe_service(db)

        # First call: should succeed and create the subscription row
        result = await svc.check_and_increment_free_usage(TEST_WALLET)
        assert result is True

        sub = db.subscriptions.get(TEST_WALLET)
        assert sub is not None
        assert sub["free_writes_used"] == 1

        # Second call: counter goes to 2
        result2 = await svc.check_and_increment_free_usage(TEST_WALLET)
        assert result2 is True
        assert db.subscriptions[TEST_WALLET]["free_writes_used"] == 2

    @pytest.mark.asyncio
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    async def test_free_tier_limit_reached(self):
        """After free_tier_writes_per_month writes, check_and_increment_free_usage returns False."""
        db = BillingMockDB()
        svc = _make_stripe_service(db)

        # Patch get_settings to return a low write limit
        mock_settings = _make_mock_settings(free_tier_writes_per_month=3)
        with patch("src.billing.stripe_service.get_settings", return_value=mock_settings):
            # Use up all 3 free writes
            for _ in range(3):
                result = await svc.check_and_increment_free_usage(TEST_WALLET)
                assert result is True

            # The 4th call should be denied
            result = await svc.check_and_increment_free_usage(TEST_WALLET)
            assert result is False

    @pytest.mark.asyncio
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    async def test_pro_tier_bypasses_limit(self):
        """Pro-tier users always pass the free-usage check."""
        db = BillingMockDB()
        # Pre-seed a pro subscription
        db.subscriptions[TEST_WALLET] = {
            "wallet_address": TEST_WALLET,
            "tier": "pro",
            "source": "stripe",
            "stripe_id": "sub_test_pro",
            "stripe_customer_id": "cus_test_pro",
            "coinbase_id": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            "free_writes_used": 9999,  # Way over limit
            "free_writes_reset_at": datetime.now(timezone.utc),
            "free_reads_used": 0,
            "free_reads_reset_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        svc = _make_stripe_service(db)
        result = await svc.check_and_increment_free_usage(TEST_WALLET)
        assert result is True


# ---------------------------------------------------------------------------
# Free-tier read usage tests
# ---------------------------------------------------------------------------

class TestFreeReadUsage:
    """Tests for StripeService.check_and_increment_free_read_usage."""

    @pytest.mark.asyncio
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    async def test_free_read_usage_increments(self):
        """check_and_increment_free_read_usage increments the free_reads_used counter."""
        db = BillingMockDB()
        svc = _make_stripe_service(db)

        # First call: should succeed and create the subscription row
        result = await svc.check_and_increment_free_read_usage(TEST_WALLET)
        assert result is True

        sub = db.subscriptions.get(TEST_WALLET)
        assert sub is not None
        assert sub["free_reads_used"] == 1

        # Second call: counter goes to 2
        result2 = await svc.check_and_increment_free_read_usage(TEST_WALLET)
        assert result2 is True
        assert db.subscriptions[TEST_WALLET]["free_reads_used"] == 2

    @pytest.mark.asyncio
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    async def test_free_read_limit_reached(self):
        """After free_tier_reads_per_month reads, check returns False."""
        db = BillingMockDB()
        svc = _make_stripe_service(db)

        # Patch get_settings to return a low read limit
        mock_settings = _make_mock_settings(free_tier_reads_per_month=3)
        with patch("src.billing.stripe_service.get_settings", return_value=mock_settings):
            # Use up all 3 free reads
            for _ in range(3):
                result = await svc.check_and_increment_free_read_usage(TEST_WALLET)
                assert result is True

            # The 4th call should be denied
            result = await svc.check_and_increment_free_read_usage(TEST_WALLET)
            assert result is False

    @pytest.mark.asyncio
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    async def test_pro_tier_has_higher_read_limit(self):
        """Pro-tier users get pro_tier_reads_per_month limit."""
        db = BillingMockDB()
        # Pre-seed a pro subscription with reads near the free limit
        db.subscriptions[TEST_WALLET] = {
            "wallet_address": TEST_WALLET,
            "tier": "pro",
            "source": "stripe",
            "stripe_id": "sub_test_pro",
            "stripe_customer_id": "cus_test_pro",
            "coinbase_id": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            "free_writes_used": 0,
            "free_writes_reset_at": datetime.now(timezone.utc),
            "free_reads_used": 5,  # Over a hypothetical free limit of 3
            "free_reads_reset_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        svc = _make_stripe_service(db)

        # Pro tier with high limit should still allow reads
        mock_settings = _make_mock_settings(
            free_tier_reads_per_month=3,
            pro_tier_reads_per_month=100000,
        )
        with patch("src.billing.stripe_service.get_settings", return_value=mock_settings):
            result = await svc.check_and_increment_free_read_usage(TEST_WALLET)
            assert result is True
            assert db.subscriptions[TEST_WALLET]["free_reads_used"] == 6

    @pytest.mark.asyncio
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    async def test_read_write_counters_independent(self):
        """Read and write counters are tracked independently."""
        db = BillingMockDB()
        svc = _make_stripe_service(db)

        # Do some writes
        await svc.check_and_increment_free_usage(TEST_WALLET)
        await svc.check_and_increment_free_usage(TEST_WALLET)

        # Do some reads
        await svc.check_and_increment_free_read_usage(TEST_WALLET)

        sub = db.subscriptions[TEST_WALLET]
        assert sub["free_writes_used"] == 2
        assert sub["free_reads_used"] == 1


# ---------------------------------------------------------------------------
# Subscription status with settings tests
# ---------------------------------------------------------------------------

class TestSubscriptionStatusSettings:
    """Tests that subscription status uses get_settings() for limits."""

    @pytest.mark.asyncio
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    async def test_status_uses_settings_for_limit(self):
        """get_subscription_status returns the limit from get_settings()."""
        db = BillingMockDB()
        svc = _make_stripe_service(db)

        mock_settings = _make_mock_settings(free_tier_writes_per_month=200)
        with patch("src.billing.stripe_service.get_settings", return_value=mock_settings):
            status = await svc.get_subscription_status(TEST_WALLET)
            assert status["free_writes_limit"] == 200
            assert status["tier"] == "free"

    @pytest.mark.asyncio
    @patch("src.billing.stripe_service.STRIPE_SECRET_KEY", "sk_test_fake")
    async def test_status_with_different_settings_value(self):
        """Changing settings value changes the returned limit."""
        db = BillingMockDB()
        svc = _make_stripe_service(db)

        mock_settings = _make_mock_settings(free_tier_writes_per_month=500)
        with patch("src.billing.stripe_service.get_settings", return_value=mock_settings):
            status = await svc.get_subscription_status(TEST_WALLET)
            assert status["free_writes_limit"] == 500


# ---------------------------------------------------------------------------
# Helpers for unit-level service tests
# ---------------------------------------------------------------------------

def _make_stripe_service(db: BillingMockDB):
    """Instantiate a StripeService with a mock DB, bypassing Stripe key config."""
    from src.billing.stripe_service import StripeService
    return StripeService(db)


def _make_mock_settings(**overrides):
    """Create a mock Settings object with configurable tier limits.

    Default values match the real config.py defaults. Override any
    setting by passing it as a keyword argument.
    """
    defaults = {
        "free_tier_writes_per_month": 250,
        "free_tier_reads_per_month": 1000,
        "pro_tier_writes_per_month": 10000,
        "pro_tier_reads_per_month": 100000,
    }
    defaults.update(overrides)
    return type("MockSettings", (), defaults)()


# ---------------------------------------------------------------------------
# Module-level execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
