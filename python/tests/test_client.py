"""Tests for TotalReclaw high-level client."""
import pytest
from unittest.mock import AsyncMock, patch

from totalreclaw.client import TotalReclaw, _derive_smart_account_address, _get_eoa_address


TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


class TestDeriveSmartAccountAddress:
    def test_eoa_returns_hex_address(self):
        addr = _get_eoa_address(TEST_MNEMONIC)
        assert addr.startswith("0x")
        assert len(addr) == 42

    def test_eoa_deterministic(self):
        addr1 = _get_eoa_address(TEST_MNEMONIC)
        addr2 = _get_eoa_address(TEST_MNEMONIC)
        assert addr1 == addr2

    @pytest.mark.asyncio
    async def test_smart_account_address(self):
        addr = await _derive_smart_account_address(TEST_MNEMONIC)
        assert addr.startswith("0x")
        assert len(addr) == 42
        # Known CREATE2 address for the test mnemonic
        assert addr == "0x2c0cf74b2b76110708ca431796367779e3738250"

    @pytest.mark.asyncio
    async def test_smart_account_deterministic(self):
        addr1 = await _derive_smart_account_address(TEST_MNEMONIC)
        addr2 = await _derive_smart_account_address(TEST_MNEMONIC)
        assert addr1 == addr2


class TestTotalReclawClient:
    @pytest.fixture
    def client(self):
        # Provide wallet_address to skip RPC call in unit tests.
        # Set _registered=True so _ensure_registered never POSTs to the relay —
        # this fixture is strictly for unit tests and must not make network calls.
        c = TotalReclaw(mnemonic=TEST_MNEMONIC, wallet_address="0x2c0cf74b2b76110708ca431796367779e3738250")
        c._registered = True
        return c

    def test_wallet_address(self, client):
        assert client.wallet_address.startswith("0x")
        assert client.wallet_address == "0x2c0cf74b2b76110708ca431796367779e3738250"

    def test_keys_derived(self, client):
        assert len(client.keys.auth_key) == 32
        assert len(client.keys.encryption_key) == 32

    def test_eoa_credentials_derived(self, client):
        assert client._eoa_address.startswith("0x")
        assert len(client._eoa_address) == 42
        assert len(client._eoa_private_key) == 32

    @pytest.mark.asyncio
    async def test_close(self, client):
        await client.close()

    @pytest.mark.asyncio
    async def test_status(self, client):
        with patch.object(
            client._relay, "get_billing_status", new_callable=AsyncMock
        ) as mock:
            from totalreclaw.relay import BillingStatus
            mock.return_value = BillingStatus(
                tier="free", free_writes_used=5, free_writes_limit=500
            )
            status = await client.status()
            assert status.tier == "free"

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock)
    async def test_remember(self, mock_send, client):
        mock_send.return_value = "0xabc123"
        fact_id = await client.remember("Test memory")
        assert len(fact_id) == 36
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock)
    async def test_forget(self, mock_send, client):
        mock_send.return_value = "0xabc123"
        result = await client.forget("fact-123")
        assert result is True
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolve_address(self):
        """Test that resolve_address queries the factory."""
        client = TotalReclaw(mnemonic=TEST_MNEMONIC)
        addr = await client.resolve_address()
        assert addr == "0x2c0cf74b2b76110708ca431796367779e3738250"
        await client.close()
