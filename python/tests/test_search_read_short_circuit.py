"""Read-denial short-circuit behaviour across every ``query_subgraph`` call
site (#662). See docs/specs/totalreclaw/read-error-surfacing.md §4.4.
"""
from __future__ import annotations

import base64
import logging
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from totalreclaw.crypto import derive_keys_from_mnemonic, encrypt, generate_blind_indices
from totalreclaw.operations import (
    DEFAULT_TRAPDOOR_BATCH_SIZE,
    search_facts,
    export_facts,
    find_existing_content_fps,
)
from totalreclaw.relay import (
    RelayClient,
    RelayReadError,
    RelayReadQuotaExceeded,
)
from totalreclaw.confirm_indexed import confirm_indexed

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


def _resp(status: int, json_body) -> httpx.Response:
    req = httpx.Request("POST", "https://api-staging.totalreclaw.xyz/v1/subgraph")
    return httpx.Response(status, json=json_body, request=req)


def _quota_error() -> RelayReadQuotaExceeded:
    return RelayReadQuotaExceeded(
        _resp(403, {"error": "quota_exceeded"}), legacy=True
    )


# A long-enough query to guarantee >=3 trapdoor chunks so the short-circuit
# tests below actually prove the fan-out stopped (a 1- or 2-chunk query
# would pass even without the fix, since there'd be nothing left to fan out
# into). Verified as a precondition in each test, not just asserted in a
# comment — see the spec's E2E/test §5 group 1.
_MANY_CHUNKS_QUERY = "Pedro prefers dark mode and likes strong coffee in the morning meetings"


def _assert_at_least_n_chunks(query: str, n: int = 3) -> None:
    word_trapdoors = generate_blind_indices(query)
    chunk_count = -(-len(word_trapdoors) // DEFAULT_TRAPDOOR_BATCH_SIZE)  # ceil div
    assert chunk_count >= n, (
        f"test query only yields {chunk_count} trapdoor chunk(s) "
        f"({len(word_trapdoors)} word trapdoors / batch size "
        f"{DEFAULT_TRAPDOOR_BATCH_SIZE}) — need >= {n} for this test to "
        f"actually prove the fan-out stopped rather than trivially having "
        f"nothing left to fan out into."
    )


class TestSearchFactsShortCircuit:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    async def test_mocked_relay_raises_and_stops_after_first_chunk(self, keys):
        _assert_at_least_n_chunks(_MANY_CHUNKS_QUERY)

        relay = AsyncMock(spec=RelayClient)
        relay.query_subgraph = AsyncMock(side_effect=_quota_error())

        with pytest.raises(RelayReadQuotaExceeded):
            await search_facts(
                query=_MANY_CHUNKS_QUERY,
                keys=keys,
                owner="0x1234",
                relay=relay,
            )
        assert relay.query_subgraph.await_count == 1

    @pytest.mark.asyncio
    async def test_real_relay_mocktransport_short_circuits(self, keys):
        _assert_at_least_n_chunks(_MANY_CHUNKS_QUERY)

        count = {"n": 0}

        def handler(request):
            count["n"] += 1
            return httpx.Response(403, json={"error": "quota_exceeded"})

        transport = httpx.MockTransport(handler)
        rc = RelayClient(relay_url="https://api-staging.totalreclaw.xyz")

        async def _mock_get_http():
            return httpx.AsyncClient(transport=transport, timeout=10.0)

        rc._get_http = _mock_get_http  # type: ignore[assignment]

        with pytest.raises(RelayReadQuotaExceeded):
            await search_facts(
                query=_MANY_CHUNKS_QUERY,
                keys=keys,
                owner="0x1234",
                relay=rc,
            )
        assert count["n"] == 1

        with pytest.raises(RelayReadQuotaExceeded):
            await search_facts(
                query="Pedro prefers dark mode and likes strong coffee in the morning meetings",
                keys=keys,
                owner="0x1234",
                relay=rc,
            )
        assert count["n"] == 1  # still one — the pause short-circuited it

    @pytest.mark.asyncio
    async def test_total_generic_outage_raises_instead_of_empty(self, keys):
        relay = AsyncMock(spec=RelayClient)
        relay.query_subgraph = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "502", request=httpx.Request("POST", "https://x"),
                response=httpx.Response(502, request=httpx.Request("POST", "https://x")),
            )
        )
        with pytest.raises(httpx.HTTPStatusError):
            await search_facts(
                query="Pedro prefers dark mode and likes strong coffee in the morning meetings",
                keys=keys,
                owner="0x1234",
                relay=relay,
            )

    @pytest.mark.asyncio
    async def test_partial_degrade_preserved_when_some_queries_succeed(self, keys):
        encrypted_b64 = encrypt("Pedro prefers dark mode", keys.encryption_key)
        encrypted_hex = "0x" + base64.b64decode(encrypted_b64).hex()
        ok_payload = {
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
        req = httpx.Request("POST", "https://x")
        transport_err = httpx.HTTPStatusError(
            "502", request=req, response=httpx.Response(502, request=req)
        )

        relay = AsyncMock(spec=RelayClient)
        # First call 502 (degrade), every subsequent call succeeds.
        relay.query_subgraph = AsyncMock(
            side_effect=[transport_err] + [ok_payload] * 20
        )
        results = await search_facts(
            query="Pedro prefers dark mode and likes strong coffee in the morning meetings",
            keys=keys,
            owner="0x1234",
            relay=relay,
        )
        assert len(results) >= 1
        assert any("Pedro prefers dark mode" in r.text for r in results)


class TestExportFactsPropagates:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    async def test_blocked_relay_raises_not_empty_list(self, keys):
        relay = AsyncMock(spec=RelayClient)
        relay.query_subgraph = AsyncMock(side_effect=_quota_error())
        with pytest.raises(RelayReadQuotaExceeded):
            await export_facts(keys, "0x1234", relay)


class TestFindExistingContentFpsDegrades:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    async def test_returns_empty_set_no_warning(self, keys, caplog):
        relay = AsyncMock(spec=RelayClient)
        relay.query_subgraph = AsyncMock(side_effect=_quota_error())
        with caplog.at_level(logging.DEBUG, logger="totalreclaw.operations"):
            result = await find_existing_content_fps(
                keys, "0x1234", relay, ["fp1", "fp2"]
            )
        assert result == set()
        warn_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warn_records == []


class TestConfirmIndexedDegradesFast:
    @pytest.mark.asyncio
    async def test_returns_false_fast_one_call(self):
        relay = AsyncMock(spec=RelayClient)
        relay.query_subgraph = AsyncMock(side_effect=_quota_error())
        start = time.monotonic()
        result = await confirm_indexed(
            "fact-id", relay, expect="active", timeout_ms=10_000, poll_interval_ms=100
        )
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 1.0
        assert relay.query_subgraph.await_count == 1


class TestContradictionDegrades:
    @pytest.mark.asyncio
    async def test_read_blocked_keeps_all_remaining_facts_recall_once(self):
        from totalreclaw.agent.contradiction import detect_and_resolve_contradictions
        from totalreclaw.agent.extraction import ExtractedFact, ExtractedEntity

        facts = [
            ExtractedFact(
                text=f"fact {i}",
                type="claim",
                importance=5,
                action="ADD",
                entities=[ExtractedEntity(name=f"entity{i}", type="concept")],
                source="user",
            )
            for i in range(3)
        ]

        client = AsyncMock()
        client.recall = AsyncMock(side_effect=_quota_error())

        with patch(
            "totalreclaw.embedding.get_embedding", return_value=[0.1] * 8
        ):
            kept = await detect_and_resolve_contradictions(facts, client)

        assert len(kept) == 3
        assert client.recall.await_count == 1


class TestFetchRecentMemoriesDegrades:
    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        from totalreclaw.agent.lifecycle import _fetch_recent_memories

        state = AsyncMock()
        client = AsyncMock()
        client.recall = AsyncMock(side_effect=_quota_error())
        state.get_client = lambda: client

        result = _fetch_recent_memories(state)
        assert result == []
