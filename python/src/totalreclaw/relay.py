"""
TotalReclaw Relay Client.

Async HTTP client for the TotalReclaw relay service.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

_HARDCODED_PRODUCTION_URL = "https://api.totalreclaw.xyz"


def _default_relay_url() -> str:
    """Resolve the default relay URL at call time.

    Respects ``TOTALRECLAW_SERVER_URL`` so tests and dev sessions can pin to
    staging without editing code. Evaluated at every call (not at import) so
    env changes after import take effect.
    """
    return os.environ.get("TOTALRECLAW_SERVER_URL") or _HARDCODED_PRODUCTION_URL


# Backward-compat: preserve the old module attribute as a property-like
# access via __getattr__ at import would complicate consumers, so we keep
# it as a constant for direct reads but the client should prefer
# _default_relay_url(). The constant itself is production to keep existing
# behavior when no env var is set.
DEFAULT_RELAY_URL = _default_relay_url()


def _detect_client_id() -> str:
    if os.environ.get("HERMES_HOME"):
        return "python-client:hermes-agent"
    return "python-client"


@dataclass
class BillingFeatures:
    llm_dedup: bool = False
    custom_extract_interval: bool = False
    min_extract_interval: Optional[int] = None
    extraction_interval: Optional[int] = None
    max_facts_per_extraction: Optional[int] = None
    max_candidate_pool: Optional[int] = None


@dataclass
class BillingStatus:
    tier: str
    free_writes_used: int
    free_writes_limit: int
    expires_at: Optional[str] = None
    features: Optional[BillingFeatures] = None


@dataclass
class CheckoutResponse:
    checkout_url: str
    session_id: str


class RelayClient:
    def __init__(
        self,
        relay_url: str = DEFAULT_RELAY_URL,
        auth_key_hex: Optional[str] = None,
        wallet_address: Optional[str] = None,
        is_test: bool = False,
    ):
        self._relay_url = relay_url.rstrip("/")
        self._auth_key_hex = auth_key_hex
        self._wallet_address = wallet_address
        self._client_id = _detect_client_id()
        self._is_test = is_test or os.environ.get("TOTALRECLAW_TEST", "").lower() == "true"
        self._http: Optional[httpx.AsyncClient] = None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-TotalReclaw-Client": self._client_id,
        }
        if self._is_test:
            headers["X-TotalReclaw-Test"] = "true"
        if self._auth_key_hex:
            headers["Authorization"] = f"Bearer {self._auth_key_hex}"
        return headers

    async def register(self, auth_key_hash: str, salt_hex: str) -> str:
        http = await self._get_http()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-TotalReclaw-Client": self._client_id,
        }
        if self._is_test:
            headers["X-TotalReclaw-Test"] = "true"
        resp = await http.post(
            f"{self._relay_url}/v1/register",
            headers=headers,
            json={"auth_key_hash": auth_key_hash, "salt": salt_hex},
        )
        resp.raise_for_status()
        return resp.json()["user_id"]

    async def query_subgraph(
        self, query: str, variables: dict[str, Any], chain: Optional[str] = None,
    ) -> dict[str, Any]:
        http = await self._get_http()
        params = {}
        if chain:
            params["chain"] = chain
        resp = await http.post(
            f"{self._relay_url}/v1/subgraph",
            headers=self._base_headers(),
            json={"query": query, "variables": variables},
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def submit_userop(self, json_rpc_body: dict[str, Any]) -> dict[str, Any]:
        http = await self._get_http()
        headers = self._base_headers()
        if self._wallet_address:
            headers["X-Wallet-Address"] = self._wallet_address
        resp = await http.post(
            f"{self._relay_url}/v1/bundler",
            headers=headers,
            json=json_rpc_body,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_billing_status(self) -> BillingStatus:
        http = await self._get_http()
        params = {}
        if self._wallet_address:
            params["wallet_address"] = self._wallet_address
        resp = await http.get(
            f"{self._relay_url}/v1/billing/status",
            headers=self._base_headers(),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        features = None
        if data.get("features"):
            f = data["features"]
            features = BillingFeatures(
                llm_dedup=f.get("llm_dedup", False),
                custom_extract_interval=f.get("custom_extract_interval", False),
                min_extract_interval=f.get("min_extract_interval"),
                extraction_interval=f.get("extraction_interval"),
                max_facts_per_extraction=f.get("max_facts_per_extraction"),
                max_candidate_pool=f.get("max_candidate_pool"),
            )
        return BillingStatus(
            tier=data["tier"],
            free_writes_used=data.get("free_writes_used", 0),
            free_writes_limit=data.get("free_writes_limit", 0),
            expires_at=data.get("expires_at"),
            features=features,
        )

    async def create_checkout(self) -> CheckoutResponse:
        http = await self._get_http()
        resp = await http.post(
            f"{self._relay_url}/v1/billing/checkout",
            headers=self._base_headers(),
            json={"wallet_address": self._wallet_address, "tier": "pro"},
        )
        resp.raise_for_status()
        data = resp.json()
        return CheckoutResponse(checkout_url=data["checkout_url"], session_id=data["session_id"])

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
