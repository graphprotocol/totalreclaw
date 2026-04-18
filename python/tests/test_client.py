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


class TestWalletAddressContract:
    """Regression tests for DIAG-PYTHON-V2-20260418.md.

    Pre-2.0.1 the `wallet_address` property silently returned the EOA placeholder
    until `resolve_address()` ran. That misled QA tooling into querying the
    subgraph with the EOA and seeing "0 facts" — but the facts were written
    under the correct Smart Account address all along.

    The fix: `wallet_address` raises `RuntimeError` if accessed before the
    Smart Account address is resolved. The new async `get_wallet_address()`
    resolves-then-returns for callers that want the lazy behaviour.
    """

    def test_wallet_address_raises_before_resolve(self):
        """Accessing `wallet_address` pre-resolve must fail loud, not return EOA."""
        client = TotalReclaw(recovery_phrase=TEST_MNEMONIC)
        # No async calls here — the relay http client is lazy and is never
        # instantiated just by reading `.wallet_address`.
        with pytest.raises(RuntimeError, match="not yet resolved"):
            _ = client.wallet_address

    def test_wallet_address_works_when_provided_at_construction(self):
        """If `wallet_address=` is passed, the property returns it immediately."""
        sa = "0x2c0cf74b2b76110708ca431796367779e3738250"
        client = TotalReclaw(recovery_phrase=TEST_MNEMONIC, wallet_address=sa)
        assert client.wallet_address == sa.lower()

    def test_wallet_address_does_not_leak_eoa(self):
        """The EOA must never be returned by `.wallet_address` as a placeholder.

        Historical bug: constructor stored EOA in `_wallet_address` and the
        property returned it until `resolve_address` ran. That leaked the wrong
        address to introspection callers.
        """
        client = TotalReclaw(recovery_phrase=TEST_MNEMONIC)
        eoa = client._eoa_address
        # `_wallet_address` must NOT be initialized to the EOA. It must be None
        # so any accidental direct read fails rather than silently pointing at
        # the wrong address.
        assert client._wallet_address is None
        assert client._address_resolved is False
        # And the property must refuse to return anything.
        with pytest.raises(RuntimeError):
            _ = client.wallet_address
        # Sanity: we still have access to the EOA via the private attribute,
        # just not via the public `wallet_address` property.
        assert eoa.startswith("0x")

    @pytest.mark.asyncio
    async def test_wallet_address_after_resolve(self):
        """After `resolve_address()`, the property returns the SA address."""
        client = TotalReclaw(recovery_phrase=TEST_MNEMONIC)
        try:
            sa = await client.resolve_address()
            assert client.wallet_address == sa
            # SA differs from EOA (this is the whole point of a Smart Account).
            assert client.wallet_address != client._eoa_address.lower()
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_get_wallet_address_resolves_lazily(self):
        """`await client.get_wallet_address()` resolves if not yet resolved."""
        client = TotalReclaw(recovery_phrase=TEST_MNEMONIC)
        try:
            assert client._address_resolved is False
            sa = await client.get_wallet_address()
            assert sa.startswith("0x") and len(sa) == 42
            assert client._address_resolved is True
            # Second call is cached.
            sa2 = await client.get_wallet_address()
            assert sa2 == sa
        finally:
            await client.close()
