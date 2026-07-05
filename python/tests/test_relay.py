"""Tests for TotalReclaw relay client."""
import httpx
import pytest
from unittest.mock import AsyncMock, patch

from totalreclaw.relay import (
    RelayClient,
    BillingStatus,
    BillingFeatures,
    _detect_client_id,
    _client_header_value,
)


class TestDetectClientId:
    def test_default(self):
        with patch.dict("os.environ", {}, clear=True):
            assert _detect_client_id() == "python-client"

    def test_hermes(self):
        with patch.dict("os.environ", {"HERMES_HOME": "/path"}):
            assert _detect_client_id() == "python-client:hermes-agent"


class TestRelayClient:
    @pytest.fixture
    def client(self):
        return RelayClient(
            relay_url="https://api.example.com",
            auth_key_hex="abc123",
            wallet_address="0x1234",
        )

    def test_base_headers(self, client):
        headers = client._base_headers()
        assert headers["Authorization"] == "Bearer abc123"
        # Header now carries an observability version suffix:
        # `python-client/<version>`. The relay buckets on the part before
        # the first `/`, so we assert the prefix, not an exact match.
        assert headers["X-TotalReclaw-Client"].startswith("python-client")
        assert "/" in headers["X-TotalReclaw-Client"]
        assert headers["X-TotalReclaw-Client"].split("/", 1)[0] == "python-client"
        assert headers["Content-Type"] == "application/json"

    def test_url_strip_trailing_slash(self):
        c = RelayClient(relay_url="https://api.example.com/")
        assert c._relay_url == "https://api.example.com"

    def test_no_auth(self):
        c = RelayClient()
        headers = c._base_headers()
        assert "Authorization" not in headers

    def test_is_test_header_present(self):
        c = RelayClient(is_test=True)
        headers = c._base_headers()
        assert headers["X-TotalReclaw-Test"] == "true"

    def test_is_test_header_absent_by_default(self):
        c = RelayClient()
        headers = c._base_headers()
        assert "X-TotalReclaw-Test" not in headers

    def test_is_test_from_env(self):
        with patch.dict("os.environ", {"TOTALRECLAW_TEST": "true"}):
            c = RelayClient()
            assert c._is_test is True
            headers = c._base_headers()
            assert headers["X-TotalReclaw-Test"] == "true"

    @pytest.mark.asyncio
    async def test_close_no_http(self):
        c = RelayClient()
        await c.close()  # Should not raise


class TestClientHeaderValue:
    def test_appends_version(self):
        val = _client_header_value("python-client")
        assert val.startswith("python-client/")
        # bucket (before first '/') is preserved for relay analytics
        assert val.split("/", 1)[0] == "python-client"

    def test_preserves_hermes_bucket(self):
        val = _client_header_value("python-client:hermes-agent")
        assert val.split("/", 1)[0] == "python-client:hermes-agent"

    def test_version_is_nonempty(self):
        from totalreclaw.relay import _client_version

        # Resolves to the installed __version__ (or "unknown" on failure),
        # never empty.
        assert _client_version()
        # And the composed header always has exactly one bucket + version.
        val = _client_header_value("python-client")
        assert val.count("/") >= 1


class TestBillingStatusParsing:
    """get_billing_status must parse latest_stable_python from features."""

    def _relay_with_response(self, payload: dict) -> RelayClient:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(_handler)
        rc = RelayClient(
            relay_url="https://api-staging.totalreclaw.xyz",
            auth_key_hex="00" * 32,
            wallet_address="0x" + "ab" * 20,
        )

        async def _mock_get_http() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=transport, timeout=10.0)

        rc._get_http = _mock_get_http  # type: ignore[assignment]
        return rc

    @pytest.mark.asyncio
    async def test_parses_latest_stable_python(self):
        rc = self._relay_with_response(
            {
                "tier": "free",
                "free_writes_used": 1,
                "free_writes_limit": 250,
                "features": {"recall_top_k": 16, "latest_stable_python": "2.4.5"},
            }
        )
        status = await rc.get_billing_status()
        assert status.features is not None
        assert status.features.latest_stable_python == "2.4.5"

    @pytest.mark.asyncio
    async def test_absent_latest_stable_python_is_none(self):
        rc = self._relay_with_response(
            {
                "tier": "free",
                "free_writes_used": 1,
                "free_writes_limit": 250,
                "features": {"recall_top_k": 16},
            }
        )
        status = await rc.get_billing_status()
        assert status.features is not None
        assert status.features.latest_stable_python is None
