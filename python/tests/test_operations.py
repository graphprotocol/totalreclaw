"""Tests for TotalReclaw store/search operations."""
import pytest
import base64
from unittest.mock import AsyncMock, patch

from totalreclaw.crypto import derive_keys_from_mnemonic, encrypt
from totalreclaw.operations import store_fact, search_facts, forget_fact, export_facts
from totalreclaw.relay import RelayClient

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"

# Derive EOA credentials for tests
from eth_account import Account as _Account
_Account.enable_unaudited_hdwallet_features()
_EOA = _Account.from_mnemonic(TEST_MNEMONIC, account_path="m/44'/60'/0'/0/0")
EOA_ADDRESS = _EOA.address
EOA_PRIVATE_KEY = bytes(_EOA.key)


class TestStoreFact:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock)
    async def test_store_returns_uuid(self, mock_send, keys):
        mock_send.return_value = "0xabc123"
        relay = AsyncMock(spec=RelayClient)
        relay._relay_url = "https://api.totalreclaw.xyz"
        relay._auth_key_hex = "deadbeef"
        relay._client_id = "test"
        fact_id = await store_fact(
            text="Test fact",
            keys=keys,
            owner="0x1234",
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender="0x1234",
        )
        assert len(fact_id) == 36  # UUID format
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock)
    async def test_store_with_embedding(self, mock_send, keys):
        mock_send.return_value = "0xabc123"
        relay = AsyncMock(spec=RelayClient)
        relay._relay_url = "https://api.totalreclaw.xyz"
        relay._auth_key_hex = "deadbeef"
        relay._client_id = "test"
        from totalreclaw.lsh import LSHHasher
        from totalreclaw.crypto import derive_lsh_seed

        lsh_seed = derive_lsh_seed(TEST_MNEMONIC, keys.salt)
        lsh = LSHHasher(lsh_seed, 4)  # Small dims for test
        embedding = [0.5, 0.5, 0.5, 0.5]

        fact_id = await store_fact(
            text="Test fact",
            keys=keys,
            owner="0x1234",
            relay=relay,
            lsh_hasher=lsh,
            embedding=embedding,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender="0x1234",
        )
        assert len(fact_id) == 36

    @pytest.mark.asyncio
    async def test_store_requires_eoa_key(self, keys):
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="eoa_private_key"):
            await store_fact(
                text="Test fact",
                keys=keys,
                owner="0x1234",
                relay=relay,
            )


class TestSearchFacts:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    async def test_search_empty_query(self, keys):
        relay = AsyncMock(spec=RelayClient)
        relay.query_subgraph = AsyncMock(
            return_value={"data": {"blindIndexes": []}}
        )
        results = await search_facts(
            query="ab",  # Very short query, few trapdoors
            keys=keys,
            owner="0x1234",
            relay=relay,
        )
        # With such a short query we may get trapdoors, but empty results from relay
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_with_results(self, keys):
        # Encrypt a test fact
        encrypted_b64 = encrypt("Pedro prefers dark mode", keys.encryption_key)
        encrypted_hex = "0x" + base64.b64decode(encrypted_b64).hex()

        relay = AsyncMock(spec=RelayClient)
        relay.query_subgraph = AsyncMock(
            return_value={
                "data": {
                    "blindIndexes": [
                        {
                            "id": "idx-1",
                            "fact": {
                                "id": "fact-1",
                                "encryptedBlob": encrypted_hex,
                                "encryptedEmbedding": None,
                                "decayScore": "0.5",
                                "timestamp": "2026-03-29T10:00:00.000Z",
                                "isActive": True,
                                "contentFp": "abc",
                            },
                        }
                    ]
                }
            }
        )

        results = await search_facts(
            query="Pedro dark mode preferences",
            keys=keys,
            owner="0x1234",
            relay=relay,
        )
        assert len(results) >= 1
        assert "Pedro prefers dark mode" in results[0].text


class TestForgetFact:
    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock)
    async def test_forget_success(self, mock_send):
        mock_send.return_value = "0xabc123"
        relay = AsyncMock(spec=RelayClient)
        relay._relay_url = "https://api.totalreclaw.xyz"
        relay._auth_key_hex = "deadbeef"
        relay._client_id = "test"
        result = await forget_fact(
            "fact-id-123",
            "0x1234",
            relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender="0x1234",
        )
        assert result is True

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock)
    async def test_forget_failure(self, mock_send):
        mock_send.side_effect = Exception("network error")
        relay = AsyncMock(spec=RelayClient)
        relay._relay_url = "https://api.totalreclaw.xyz"
        relay._auth_key_hex = "deadbeef"
        relay._client_id = "test"
        result = await forget_fact(
            "fact-id-123",
            "0x1234",
            relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender="0x1234",
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_forget_requires_eoa_key(self):
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="eoa_private_key"):
            await forget_fact("fact-id-123", "0x1234", relay)


class TestExportFacts:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    async def test_export_empty(self, keys):
        relay = AsyncMock(spec=RelayClient)
        relay.query_subgraph = AsyncMock(return_value={"data": {"facts": []}})
        results = await export_facts(keys, "0x1234", relay)
        assert results == []

    @pytest.mark.asyncio
    async def test_export_with_facts(self, keys):
        encrypted_b64 = encrypt("Test memory", keys.encryption_key)
        encrypted_hex = "0x" + base64.b64decode(encrypted_b64).hex()

        relay = AsyncMock(spec=RelayClient)
        relay.query_subgraph = AsyncMock(
            return_value={
                "data": {
                    "facts": [
                        {
                            "id": "fact-1",
                            "encryptedBlob": encrypted_hex,
                            "encryptedEmbedding": None,
                            "decayScore": "0.7",
                            "timestamp": "2026-03-29T10:00:00.000Z",
                            "isActive": True,
                            "contentFp": "abc",
                        }
                    ]
                }
            }
        )
        results = await export_facts(keys, "0x1234", relay)
        assert len(results) == 1
        assert results[0]["text"] == "Test memory"
        assert results[0]["importance"] == 0.7
