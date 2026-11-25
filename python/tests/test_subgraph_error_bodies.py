"""internal#559 — a subgraph HTTP 2xx with a GraphQL error body must not
read as an empty result.

Covers ``parse_subgraph_data`` and each call site routed through it.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from totalreclaw.crypto import derive_keys_from_mnemonic
from totalreclaw.operations import (
    _fetch_fact_by_id,
    _recall_rows,
    export_facts,
    fetch_recent_facts,
    find_existing_content_fps,
)
from totalreclaw.relay import SubgraphQueryError, parse_subgraph_data
from totalreclaw.retype_setscope import execute_retype

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon about"
)
OWNER = "0x0000000000000000000000000000000000001234"
ERR_BODY = {"errors": [{"message": "indexer unavailable"}]}
PARTIAL_BODY = {"data": {"facts": [{"id": "1"}]}, "errors": [{"message": "boom"}]}
NULL_BODY = {"data": None}
OK_BODY = {"data": {"facts": []}}
BAD_BODIES = [ERR_BODY, PARTIAL_BODY, NULL_BODY, {}, []]


def _relay(*bodies):
    r = MagicMock()
    r.query_subgraph = AsyncMock(side_effect=list(bodies))
    return r


class TestHelper:
    def test_normal_returns_data(self):
        assert parse_subgraph_data(OK_BODY) == {"facts": []}

    @pytest.mark.parametrize("body", BAD_BODIES)
    def test_bad_bodies_raise(self, body):
        with pytest.raises(SubgraphQueryError):
            parse_subgraph_data(body)

    def test_message_truncated_and_no_variables(self):
        body = {"errors": [{"message": "x" * 5000, "extensions": {"vars": "SECRET"}}]}
        with pytest.raises(SubgraphQueryError) as ei:
            parse_subgraph_data(body)
        assert len(str(ei.value)) < 400
        assert "SECRET" not in str(ei.value)


class TestExport:
    @pytest.mark.parametrize("body", BAD_BODIES)
    async def test_error_on_first_page_fails_export(self, body):
        keys = derive_keys_from_mnemonic(MNEMONIC)
        with pytest.raises(SubgraphQueryError):
            await export_facts(keys, OWNER, _relay(body))

    async def test_error_mid_pagination_fails_export(self):
        keys = derive_keys_from_mnemonic(MNEMONIC)
        page = {"data": {"facts": [{"id": "1", "encryptedBlob": "zz"}]}}
        with pytest.raises(SubgraphQueryError):
            await export_facts(keys, OWNER, _relay(page, ERR_BODY), page_size=1)

    async def test_normal_empty_export(self):
        keys = derive_keys_from_mnemonic(MNEMONIC)
        assert await export_facts(keys, OWNER, _relay(OK_BODY)) == []


class TestFetchFactById:
    @pytest.mark.parametrize("strict", [True, False])
    @pytest.mark.parametrize("body", BAD_BODIES)
    async def test_error_raises(self, body, strict):
        with pytest.raises(RuntimeError):
            await _fetch_fact_by_id("f1", OWNER, _relay(body), strict=strict)

    async def test_missing_fact_is_none_when_lenient(self):
        assert await _fetch_fact_by_id("f1", OWNER, _relay({"data": {"fact": None}})) is None


class TestRetype:
    @pytest.mark.parametrize("body", [ERR_BODY, PARTIAL_BODY, NULL_BODY])
    async def test_error_is_not_fact_not_found(self, body):
        keys = derive_keys_from_mnemonic(MNEMONIC)
        with pytest.raises(SubgraphQueryError):
            await execute_retype(
                "f1", "claim", keys, OWNER, _relay(body),
                eoa_private_key=b"\x01" * 32, eoa_address=OWNER,
            )

    async def test_genuinely_missing_fact_still_reported(self):
        keys = derive_keys_from_mnemonic(MNEMONIC)
        res = await execute_retype(
            "f1", "claim", keys, OWNER, _relay({"data": {"fact": None}}),
            eoa_private_key=b"\x01" * 32, eoa_address=OWNER,
        )
        assert res["success"] is False and "Fact not found" in res["error"]


class TestOtherSites:
    @pytest.mark.parametrize("body", [ERR_BODY, PARTIAL_BODY, NULL_BODY])
    async def test_recent_facts_raises(self, body):
        keys = derive_keys_from_mnemonic(MNEMONIC)
        with pytest.raises(SubgraphQueryError):
            await fetch_recent_facts(keys, OWNER, _relay(body), 5)

    async def test_prewrite_dedup_fails_open_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert await find_existing_content_fps(derive_keys_from_mnemonic(MNEMONIC), OWNER, _relay(ERR_BODY), ["a"]) == set()
        assert any("fail-open" in r.message for r in caplog.records)

    async def test_prewrite_dedup_normal(self):
        body = {"data": {"facts": [{"contentFp": "a"}]}}
        assert await find_existing_content_fps(derive_keys_from_mnemonic(MNEMONIC), OWNER, _relay(body), ["a"]) == {"a"}


class TestRecallLenient:
    @pytest.mark.parametrize("body", [ERR_BODY, PARTIAL_BODY, NULL_BODY])
    def test_empty_with_warning_not_raise(self, body, caplog):
        with caplog.at_level(logging.WARNING):
            assert _recall_rows(body, "facts") == []
        assert any("SubgraphQueryError" in r.message for r in caplog.records)

    def test_normal(self):
        assert _recall_rows({"data": {"facts": [{"id": "1"}]}}, "facts") == [{"id": "1"}]
