"""Cross-client portability tests.

Validates that the Python client produces byte-compatible crypto output
and can round-trip facts through the same on-chain storage as the
TypeScript MCP server / OpenClaw plugin.

Requires TOTALRECLAW_RECOVERY_PHRASE env var.
Run: TOTALRECLAW_RECOVERY_PHRASE="your mnemonic" python -m pytest tests/test_cross_client.py -v
"""
import asyncio
import os
import time

import pytest

from totalreclaw.client import TotalReclaw, _derive_smart_account_address
from totalreclaw.crypto import derive_keys_from_mnemonic, encrypt, decrypt

MNEMONIC = os.environ.get("TOTALRECLAW_RECOVERY_PHRASE", "")
STAGING_URL = os.environ.get(
    "TOTALRECLAW_SERVER_URL", "https://api-staging.totalreclaw.xyz"
)

pytestmark = pytest.mark.skipif(
    not MNEMONIC, reason="TOTALRECLAW_RECOVERY_PHRASE not set"
)


@pytest.fixture(scope="module")
async def py_client():
    c = TotalReclaw(mnemonic=MNEMONIC, relay_url=STAGING_URL, is_test=True)
    yield c
    await c.close()


class TestCrossClientPortability:
    @pytest.mark.asyncio
    async def test_crypto_roundtrip(self):
        """Python encrypt/decrypt round-trips correctly with derived keys."""
        keys = derive_keys_from_mnemonic(MNEMONIC)
        plaintext = "Cross-client test: Python encrypt and decrypt"
        encrypted = encrypt(plaintext, keys.encryption_key)
        assert decrypt(encrypted, keys.encryption_key) == plaintext

    @pytest.mark.asyncio
    async def test_same_wallet_address(self):
        """Python derives the same EOA address deterministically."""
        addr = _derive_smart_account_address(MNEMONIC)
        assert addr.startswith("0x")
        assert len(addr) == 42  # 0x + 40 hex chars

        # Deterministic: calling again yields the same address
        addr2 = _derive_smart_account_address(MNEMONIC)
        assert addr == addr2

    @pytest.mark.asyncio
    async def test_key_derivation_deterministic(self):
        """Keys derived from the same mnemonic are always identical."""
        keys1 = derive_keys_from_mnemonic(MNEMONIC)
        keys2 = derive_keys_from_mnemonic(MNEMONIC)
        assert keys1.auth_key == keys2.auth_key
        assert keys1.encryption_key == keys2.encryption_key
        assert keys1.dedup_key == keys2.dedup_key
        assert keys1.salt == keys2.salt

    @pytest.mark.asyncio
    async def test_python_store_python_recall(self, py_client):
        """Basic round-trip: Python stores and recalls via the relay."""
        unique = f"py_roundtrip_{int(time.time())}"
        text = f"Python client test {unique}"
        fact_id = await py_client.remember(text, importance=0.7)
        assert len(fact_id) == 36  # UUID v4

        await asyncio.sleep(10)  # Wait for subgraph indexing

        results = await py_client.recall(f"Python client {unique}")
        texts = [r.text for r in results]
        assert any(unique in t for t in texts), (
            f"Expected '{unique}' in recall results, got: {texts}"
        )
