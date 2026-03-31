"""Tests for TotalReclaw relay client."""
import pytest
from unittest.mock import AsyncMock, patch

from totalreclaw.relay import RelayClient, BillingStatus, BillingFeatures, _detect_client_id


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
        assert headers["X-TotalReclaw-Client"] == "python-client"
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
