"""Every relay subgraph / bundler request carries the index-scheme header (internal#573)."""
from __future__ import annotations

import httpx
import pytest

from totalreclaw import userop
from totalreclaw.relay import INDEX_SCHEME_HEADER, INDEX_SCHEME_VALUE, RelayClient


def test_constants():
    assert INDEX_SCHEME_HEADER == "X-TotalReclaw-Index-Scheme"
    assert INDEX_SCHEME_VALUE == "1"


def test_base_headers_have_scheme():
    rc = RelayClient(auth_key_hex="00" * 32)
    assert rc._base_headers()[INDEX_SCHEME_HEADER] == "1"


def test_make_ctx_headers_have_scheme():
    ctx = userop._make_ctx("0x" + "11" * 20, "https://relay.example", "aa", "0x" + "22" * 20, 100, "cid", None)
    assert ctx.headers[INDEX_SCHEME_HEADER] == "1"


@pytest.mark.asyncio
async def test_query_subgraph_sends_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"data": {"facts": []}})

    rc = RelayClient(relay_url="https://relay.example", auth_key_hex="00" * 32)

    async def _get_http():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    rc._get_http = _get_http  # type: ignore[assignment]
    await rc.query_subgraph("{ facts { id } }", {})
    assert seen["x-totalreclaw-index-scheme"] == "1"


@pytest.mark.asyncio
async def test_real_relay_rpc_sends_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})

    ctx = userop._make_ctx("0x" + "11" * 20, "https://relay.example", "aa", "0x" + "22" * 20, 100, "cid", None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await userop._relay_rpc(http, ctx.relay_url, ctx.headers, "pimlico_getUserOperationGasPrice", [], rpc_id=1)
    assert seen["x-totalreclaw-index-scheme"] == "1"
