"""
TotalReclaw Relay Client.

Async HTTP client for the TotalReclaw relay service.

Event-loop binding
------------------
``httpx.AsyncClient`` binds to the event loop that was running when it was
constructed (via anyio / httpcore primitives). Sharing one instance across
two different loops raises ``RuntimeError: Event loop is closed`` the
moment any I/O is attempted on the "wrong" loop — see
``python/src/totalreclaw/agent/loop_runner.py`` for the root-cause writeup.

The Python client has at least two loop contexts in production:

* The process-wide :class:`_SyncLoopRunner` loop, used by sync Hermes hook
  callbacks (e.g. ``pre_llm_call`` auto-recall).
* Hermes's own async runtime loop, used when it invokes async tool handlers
  like ``totalreclaw_status``.

Historically v2.0.1 cached a single ``httpx.AsyncClient`` on the RelayClient
and returned it from ``_get_http`` regardless of which loop was calling.
That tripped "Event loop is closed" as soon as the second loop tried to
use the client (QA-V1CLEAN-VPS-20260418).

The fix below keeps the convenience of a cached client but keys the cache
by the currently-running event loop, so each loop gets its own, correctly
loop-bound client. Old clients from orphaned loops are dropped.
"""
from __future__ import annotations
import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# 2.3.3-rc.1 — environment-binding rule (PR #165): RC artifacts default to
# STAGING, stable artifacts default to PRODUCTION. The checked-in literal is
# STAGING; the publish-python-client.yml workflow rewrites this single line
# to the production URL when ``release-type=stable`` before
# ``python -m build``. A pre-publish CI guard fails the build if a stable
# wheel still contains ``api-staging`` or an RC wheel contains
# ``api.totalreclaw.xyz`` (sans staging) — see the workflow for the regex.
#
# This is THE canonical default-URL site for the Python package. Every
# other module that resolves a default URL imports
# ``_HARDCODED_DEFAULT_URL`` (or calls ``_default_relay_url()``) from here.
# Adding a second hardcoded URL elsewhere will silently desync the wheel
# from the build-time injection rule. Don't.
_HARDCODED_DEFAULT_URL = "https://api-staging.totalreclaw.xyz"


def _default_relay_url() -> str:
    """Resolve the default relay URL at call time.

    Respects ``TOTALRECLAW_SERVER_URL`` so tests and dev sessions can pin to
    a non-default URL without editing code. Evaluated at every call (not at
    import) so env changes after import take effect.

    The fallback returned when the env var is unset is the value baked into
    ``_HARDCODED_DEFAULT_URL`` at build time — staging for RC artifacts,
    production for stable artifacts. See the module docstring for the
    workflow that performs the substitution.
    """
    return os.environ.get("TOTALRECLAW_SERVER_URL") or _HARDCODED_DEFAULT_URL


# Backward-compat: preserve the old module attribute as a property-like
# access via __getattr__ at import would complicate consumers, so we keep
# it as a constant for direct reads but the client should prefer
# _default_relay_url(). The constant resolves to whichever default the
# build-time rewrite landed on.
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
        session_id: Optional[str] = None,
    ):
        self._relay_url = relay_url.rstrip("/")
        self._auth_key_hex = auth_key_hex
        self._wallet_address = wallet_address
        self._client_id = _detect_client_id()
        self._is_test = is_test or os.environ.get("TOTALRECLAW_TEST", "").lower() == "true"
        # Optional session tag forwarded to the relay as ``X-TotalReclaw-Session``
        # for Axiom log tracing. Resolves at construction in this priority:
        #
        #   1. Explicit ``session_id=`` constructor argument.
        #   2. ``TOTALRECLAW_SESSION_ID`` env var.
        #   3. None (header omitted).
        #
        # The v1 env cleanup accidentally removed this in v2.0.1 and broke
        # Axiom session-scoped log queries — QA-V1CLEAN-VPS-20260418 Bug #1.
        # Restored in v2.0.2.
        resolved_session = session_id
        if resolved_session is None:
            env_session = os.environ.get("TOTALRECLAW_SESSION_ID")
            resolved_session = env_session if env_session else None
        self._session_id: Optional[str] = resolved_session or None
        # Cache httpx.AsyncClient per event loop. Sharing one client across
        # loops is the root cause of "Event loop is closed" in v2.0.1 — see
        # the module docstring. Keying by ``id(loop)`` means each loop gets
        # its own client, correctly bound, and orphaned clients (from
        # short-lived sync loops) are dropped transparently.
        self._http_per_loop: dict[int, httpx.AsyncClient] = {}

    async def _get_http(self) -> httpx.AsyncClient:
        """Return an ``httpx.AsyncClient`` bound to the current event loop.

        If the loop we're running on already has a cached client, reuse it
        (connection pooling is preserved within a loop). Otherwise build a
        fresh one. We never try to carry an httpx client across loops —
        that's the bug we're fixing.
        """
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        cached = self._http_per_loop.get(loop_id)
        if cached is not None and not cached.is_closed:
            return cached
        if cached is not None and cached.is_closed:
            # Best-effort cleanup of the stale entry — the client already
            # released its own sockets when it closed, but we still want
            # the dict key gone so it doesn't grow unbounded.
            self._http_per_loop.pop(loop_id, None)
        client = httpx.AsyncClient(timeout=30.0)
        self._http_per_loop[loop_id] = client
        return client

    # Backward-compat shim: some legacy callers and tests read / write
    # ``self._http`` directly (particularly tests that monkeypatch the
    # transport). Expose it as a property mapped to the current loop's
    # cached client so the old API keeps working.
    @property
    def _http(self) -> Optional[httpx.AsyncClient]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — return whichever entry exists (usually a
            # test setting up state before a call). ``None`` if empty.
            if self._http_per_loop:
                return next(iter(self._http_per_loop.values()))
            return None
        return self._http_per_loop.get(id(loop))

    @_http.setter
    def _http(self, value: Optional[httpx.AsyncClient]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Outside any loop — apply to all cache entries so tests that
            # assign a mock transport before making an async call see the
            # replacement from whichever loop their call runs on.
            if value is None:
                self._http_per_loop.clear()
            else:
                # Can't bind without a loop, but the caller explicitly set
                # a value — trust it for the next .get on any loop.
                # This path is only really used by tests that assign a
                # MockTransport-backed client; using ``0`` as a sentinel
                # key means ``_get_http`` will find+reuse it on first call
                # inside a loop as long as it isn't closed.
                self._http_per_loop[0] = value
            return
        if value is None:
            self._http_per_loop.pop(id(loop), None)
        else:
            self._http_per_loop[id(loop)] = value

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-TotalReclaw-Client": self._client_id,
        }
        if self._is_test:
            headers["X-TotalReclaw-Test"] = "true"
        if self._auth_key_hex:
            headers["Authorization"] = f"Bearer {self._auth_key_hex}"
        # Forward QA-scoped session tag for Axiom log tracing (if set).
        # Matches the relay's expected header name and the semantic the
        # plugin used before the v1 env cleanup.
        if self._session_id:
            headers["X-TotalReclaw-Session"] = self._session_id
        return headers

    async def register(self, auth_key_hash: str, salt_hex: str) -> str:
        http = await self._get_http()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-TotalReclaw-Client": self._client_id,
        }
        if self._is_test:
            headers["X-TotalReclaw-Test"] = "true"
        if self._session_id:
            headers["X-TotalReclaw-Session"] = self._session_id
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
        """Close any cached httpx clients across all loops we've touched.

        Best-effort: clients from other loops cannot be closed from here
        (aclose is loop-bound), so we only close the one for the
        currently-running loop and drop references to the rest so they
        get garbage-collected. In practice tests call ``close()`` on the
        same loop they used to create the client, so this works.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        current_id = id(loop) if loop is not None else None
        for loop_id, client in list(self._http_per_loop.items()):
            if loop_id == current_id and not client.is_closed:
                try:
                    await client.aclose()
                except Exception:  # pragma: no cover — best-effort teardown
                    pass
            self._http_per_loop.pop(loop_id, None)
