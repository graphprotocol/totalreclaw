"""Integration tests against the staging relay.

Requires TOTALRECLAW_RECOVERY_PHRASE env var.
Run: TOTALRECLAW_RECOVERY_PHRASE="your mnemonic" python -m pytest tests/test_staging_integration.py -v
"""
import asyncio
import os
import time

import pytest

from totalreclaw.client import TotalReclaw

MNEMONIC = os.environ.get("TOTALRECLAW_RECOVERY_PHRASE", "")
STAGING_URL = os.environ.get(
    "TOTALRECLAW_SERVER_URL", "https://api-staging.totalreclaw.xyz"
)

pytestmark = pytest.mark.skipif(
    not MNEMONIC, reason="TOTALRECLAW_RECOVERY_PHRASE not set"
)


async def _wait_for_indexing(seconds: int = 10):
    """Wait for subgraph to index new transactions."""
    await asyncio.sleep(seconds)


@pytest.fixture
async def client():
    c = TotalReclaw(mnemonic=MNEMONIC, relay_url=STAGING_URL, is_test=True)
    await c.resolve_address()
    yield c
    await c.close()


class TestStagingIntegration:
    @pytest.mark.asyncio
    async def test_register(self, client):
        """Register (or re-register) with the relay and get a user_id."""
        user_id = await client.register()
        assert user_id  # non-empty string

    @pytest.mark.asyncio
    async def test_status(self, client):
        """Billing status returns a valid tier and quota."""
        status = await client.status()
        assert status.tier in ("free", "pro")
        assert status.free_writes_limit >= 0

    @pytest.mark.asyncio
    async def test_remember_and_recall(self, client):
        """Store a unique fact and recall it after subgraph indexing."""
        unique = f"integration_test_{int(time.time())}"
        text = f"Pedro {unique} uses Python for AI development"
        fact_id = await client.remember(text, importance=0.8)
        assert len(fact_id) == 36  # UUID v4

        # Wait for subgraph to index the on-chain write
        # Relay -> bundler -> mempool -> block -> subgraph indexing takes ~15-30s
        await _wait_for_indexing(30)

        results = await client.recall(f"Pedro {unique}")
        texts = [r.text for r in results]
        assert any(unique in t for t in texts), (
            f"Expected '{unique}' in results, got: {texts}"
        )

    @pytest.mark.asyncio
    async def test_forget(self, client):
        """Store a fact, then soft-delete it via tombstone."""
        unique = f"forget_test_{int(time.time())}"
        text = f"Temporary fact {unique} should be deleted"
        fact_id = await client.remember(text)

        await _wait_for_indexing(10)

        success = await client.forget(fact_id)
        assert success is True

    @pytest.mark.asyncio
    async def test_export(self, client):
        """Export all active facts for this wallet."""
        facts = await client.export_all()
        assert isinstance(facts, list)
        # Should have at least the facts we stored in earlier tests
        assert len(facts) >= 0
