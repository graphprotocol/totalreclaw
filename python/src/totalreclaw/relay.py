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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Optional

import httpx

logger = logging.getLogger(__name__)

# 2.3.12-rc.1 (F flip) — environment-binding rule: BOTH stable and RC
# wheels default to PRODUCTION. The publish-time rewrite that previously
# differentiated stable vs. RC defaults is removed. Staging access is
# opt-in via TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz
# at runtime. A pre-publish CI guard fails the build if any wheel
# contains ``api-staging`` (stranded staging defaults are forbidden).
#
# This is THE canonical default-URL site for the Python package. Every
# other module that resolves a default URL imports
# ``_HARDCODED_DEFAULT_URL`` (or calls ``_default_relay_url()``) from here.
# Adding a second hardcoded URL elsewhere will silently desync the wheel.
_HARDCODED_DEFAULT_URL = "https://api.totalreclaw.xyz"


def _default_relay_url() -> str:
    """Resolve the default relay URL at call time.

    Respects ``TOTALRECLAW_SERVER_URL`` so tests and dev sessions can pin to
    a non-default URL without editing code. Evaluated at every call (not at
    import) so env changes after import take effect.

    The fallback returned when the env var is unset is the value baked into
    ``_HARDCODED_DEFAULT_URL`` — production for both stable and RC builds
    (post-F-flip, 2.3.12-rc.1). Staging access is opt-in via the env var.
    """
    return os.environ.get("TOTALRECLAW_SERVER_URL") or _HARDCODED_DEFAULT_URL


def _detect_client_id() -> str:
    if os.environ.get("HERMES_HOME"):
        return "python-client:hermes-agent"
    return "python-client"


def _client_version() -> str:
    """Installed package version, for the observability suffix on the client
    header. Imported lazily to avoid a circular import
    (``totalreclaw.__init__`` imports client code that imports this module).
    Falls back to ``"unknown"`` if the version can't be resolved."""
    try:
        from totalreclaw import __version__

        return __version__ or "unknown"
    except Exception:
        return "unknown"


def _client_header_value(client_id: str) -> str:
    """Build the ``X-TotalReclaw-Client`` value: ``<client_id>/<version>``
    (e.g. ``python-client/2.4.5``). The relay buckets analytics on the part
    before the first ``/``, so appending the version is observability-only and
    does not fragment client-type aggregation."""
    return f"{client_id}/{_client_version()}"


# ---------------------------------------------------------------------------
# Read-denial surfacing (#662) — typed errors for POST /v1/subgraph, plus the
# client-wide read pause on RelayClient. See
# docs/specs/totalreclaw/read-error-surfacing.md for the full design.
#
# All four classes subclass httpx.HTTPStatusError so existing
# ``except httpx.HTTPStatusError`` / ``.response.status_code`` call sites
# (e.g. ``crystals/recrystallize.py::_is_quota_exhausted_error``) keep
# working unchanged.
# ---------------------------------------------------------------------------


def _truncate_message(message: Optional[str]) -> Optional[str]:
    if not isinstance(message, str):
        return None
    return message[:200]


class RelayReadError(httpx.HTTPStatusError):
    """Non-2xx from ``POST /v1/subgraph``."""

    def __init__(
        self,
        response: httpx.Response,
        *,
        error_code: Optional[str] = None,
        relay_message: Optional[str] = None,
    ):
        self.status_code: int = response.status_code
        self.error_code: Optional[str] = error_code
        self.relay_message: Optional[str] = _truncate_message(relay_message)
        super().__init__(
            self._format_message(), request=response.request, response=response
        )

    def _format_message(self) -> str:
        detail = self.relay_message or self.error_code or "unknown error"
        return f"TotalReclaw relay read failed (HTTP {self.status_code}): {detail}"


class RelayReadBlocked(RelayReadError):
    """Reads are blocked for a window. Triggers the client-wide read pause."""

    kind: ClassVar[str] = "read_blocked"

    def __init__(
        self,
        response: httpx.Response,
        *,
        error_code: Optional[str] = None,
        relay_message: Optional[str] = None,
        retry_after_s: Optional[float] = None,
    ):
        self.retry_after_s: Optional[float] = retry_after_s
        super().__init__(response, error_code=error_code, relay_message=relay_message)


class RelayReadQuotaExceeded(RelayReadBlocked):
    kind: ClassVar[str] = "read_quota"

    def __init__(
        self,
        response: httpx.Response,
        *,
        error_code: Optional[str] = None,
        relay_message: Optional[str] = None,
        retry_after_s: Optional[float] = None,
        upgrade_url: Optional[str] = None,
        tier: Optional[str] = None,
        limit: Optional[int] = None,
        used: Optional[int] = None,
        resets_at: Optional[datetime] = None,
        legacy: bool = False,
    ):
        self.upgrade_url = upgrade_url
        self.tier = tier
        self.limit = limit
        self.used = used
        self.resets_at = resets_at
        self.legacy = legacy
        super().__init__(
            response,
            error_code=error_code,
            relay_message=relay_message,
            retry_after_s=retry_after_s,
        )

    def _format_message(self) -> str:
        return "TotalReclaw memory reads are paused: monthly read limit reached."


class RelayRateLimited(RelayReadBlocked):
    kind: ClassVar[str] = "rate_limited"

    def __init__(
        self,
        response: httpx.Response,
        *,
        error_code: Optional[str] = None,
        relay_message: Optional[str] = None,
        retry_after_s: Optional[float] = None,
        limit_scope: Optional[str] = None,
    ):
        self.limit_scope = limit_scope
        super().__init__(
            response,
            error_code=error_code,
            relay_message=relay_message,
            retry_after_s=retry_after_s,
        )

    def _format_message(self) -> str:
        if self.retry_after_s is not None:
            minutes = max(1, round(self.retry_after_s / 60))
            return (
                "TotalReclaw memory reads are paused: too many requests "
                f"(retry in ~{minutes} min)."
            )
        return "TotalReclaw memory reads are paused: too many requests."


def _parse_resets_at(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _retry_after_seconds(resp: httpx.Response, body: Optional[dict]) -> Optional[float]:
    """Body ``retry_after`` (a number) first, then the ``Retry-After`` header
    (delta-seconds or an HTTP-date). Negative or unparseable -> ``None``.

    Mirrors ``agent/llm_client.py::_parse_retry_after`` — not imported from
    there, because ``relay.py`` must not import ``agent/``.
    """
    if isinstance(body, dict):
        raw = body.get("retry_after")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
            return float(raw)

    raw_header = resp.headers.get("retry-after")
    if not raw_header:
        return None
    raw_header = raw_header.strip()
    if not raw_header:
        return None

    try:
        secs = float(raw_header)
        return secs if secs >= 0 else None
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(raw_header)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return delta if delta >= 0 else None
    except (TypeError, ValueError):
        return None


def _classify_subgraph_error(resp: httpx.Response) -> RelayReadError:
    """Classify a non-2xx ``/v1/subgraph`` response.

    Matches on ``error_code`` first, then ``error``, by **string equality**
    (never substring — ``read_quota_exceeded`` contains the substring
    ``quota_exceeded``). Tolerates a non-JSON body (e.g. a Cloudflare HTML
    error page). First match wins:

      1. ``code == "read_quota_exceeded"`` (any status) -> quota, new contract
      2. ``status == 403 and code == "quota_exceeded"`` -> quota, legacy contract
      3. ``status == 429`` -> rate limited (legacy text body or
         ``error_code == "rate_limited"``)
      4. otherwise -> plain ``RelayReadError``
    """
    try:
        body: Any = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = None

    code: Optional[str] = None
    message: Optional[str] = None
    if body is not None:
        raw_code = body.get("error_code") or body.get("error")
        if isinstance(raw_code, str):
            code = raw_code
        raw_message = body.get("message")
        if isinstance(raw_message, str):
            message = raw_message

    status = resp.status_code
    retry_after_s = _retry_after_seconds(resp, body)

    if code == "read_quota_exceeded":
        return RelayReadQuotaExceeded(
            resp,
            error_code=code,
            relay_message=message,
            retry_after_s=retry_after_s,
            upgrade_url=(body.get("upgrade_url") if body else None),
            tier=(body.get("tier") if body else None),
            limit=(body.get("limit") if body else None),
            used=(body.get("used") if body else None),
            resets_at=_parse_resets_at(body.get("resets_at")) if body else None,
            legacy=False,
        )
    if status == 403 and code == "quota_exceeded":
        return RelayReadQuotaExceeded(
            resp,
            error_code=code,
            relay_message=message,
            retry_after_s=retry_after_s,
            upgrade_url=(body.get("upgrade_url") if body else None),
            legacy=True,
        )
    if status == 429:
        limit_scope = body.get("limit_scope") if body else None
        return RelayRateLimited(
            resp,
            error_code=code,
            relay_message=message,
            retry_after_s=retry_after_s,
            limit_scope=(limit_scope if isinstance(limit_scope, str) else None),
        )
    return RelayReadError(resp, error_code=code, relay_message=message)


# Floor + default for the read-pause re-probe cadence. The floor exists so a
# test/E2E env override can shorten the window without allowing a hot loop.
_REPROBE_FLOOR_S = 5.0
_REPROBE_DEFAULT_S = 900.0


def _reprobe_seconds() -> float:
    raw = os.environ.get("TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS")
    if raw:
        try:
            return max(_REPROBE_FLOOR_S, float(raw))
        except ValueError:
            pass
    return _REPROBE_DEFAULT_S


def read_pause_reprobe_seconds() -> float:
    """Public accessor for the re-probe cadence (env-configurable via
    ``TOTALRECLAW_READ_PAUSE_REPROBE_SECONDS``). Used by the user-facing
    read-pause notice text (``agent/read_block.py``) to tell the user how
    often TotalReclaw retries."""
    return _reprobe_seconds()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _pause_seconds(err: RelayReadBlocked) -> float:
    """How long to pause reads for *err*. See §4.3 of the read-error-surfacing
    spec for the re-probe rationale (a fixed pause-until-``resets_at`` would
    stay stuck past a mid-episode cap raise, upgrade, or transient DB blip)."""
    if isinstance(err, RelayRateLimited):
        base = err.retry_after_s if err.retry_after_s is not None else 300.0
        return _clamp(base, 30.0, 3600.0)
    if isinstance(err, RelayReadQuotaExceeded):
        reprobe = _reprobe_seconds()
        if err.resets_at is not None:
            remaining = (err.resets_at - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                return min(reprobe, remaining)
        return reprobe
    return _reprobe_seconds()


@dataclass
class ReadBlockState:
    """A client-wide read-pause episode (see ``RelayClient.read_block``)."""

    error: RelayReadBlocked
    paused_until: float  # time.monotonic() deadline
    paused_until_utc: datetime  # for display — aware UTC
    since_utc: datetime  # start of the episode — aware UTC
    episode: int  # monotonically increasing per RelayClient


def ephemeral_read_block_state(err: RelayReadBlocked) -> ReadBlockState:
    """Build a throwaway :class:`ReadBlockState` for *err* without touching
    any ``RelayClient``.

    Defensive fallback for a caller that catches ``RelayReadBlocked`` but
    finds ``relay.read_block()`` already ``None`` (e.g. a very short pause
    window expired between the raise and the catch) — it still needs
    *something* to format a user-facing notice from. ``episode=-1`` marks
    it as not a real ``RelayClient``-tracked episode.
    """
    now_utc = datetime.now(timezone.utc)
    pause_s = _pause_seconds(err)
    return ReadBlockState(
        error=err,
        paused_until=time.monotonic() + pause_s,
        paused_until_utc=now_utc + timedelta(seconds=pause_s),
        since_utc=now_utc,
        episode=-1,
    )


@dataclass
class BillingFeatures:
    llm_dedup: bool = False
    custom_extract_interval: bool = False
    min_extract_interval: Optional[int] = None
    extraction_interval: Optional[int] = None
    max_facts_per_extraction: Optional[int] = None
    max_candidate_pool: Optional[int] = None
    recall_top_k: Optional[int] = None
    # Latest published stable Python-client version (e.g. "2.4.5"), when the
    # relay advertises one. None ⇒ the relay has not opted into update-notices
    # (or is an older build) ⇒ the client never nudges. See
    # ``totalreclaw.update_notice`` for the compare + rate-limit logic.
    latest_stable_python: Optional[str] = None


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
    # The relay's POST /v1/billing/checkout success payload is
    # ``{success, checkout_url}`` — it does NOT return a session id (the
    # Stripe Session id stays server-side). The client never uses it, so it
    # is optional; hard-reading ``data["session_id"]`` here is what raised
    # ``KeyError: 'session_id'`` and surfaced as "Failed to create checkout
    # session: 'session_id'" on every upgrade attempt.
    session_id: Optional[str] = None


class RelayClient:
    def __init__(
        self,
        relay_url: Optional[str] = None,
        auth_key_hex: Optional[str] = None,
        wallet_address: Optional[str] = None,
        is_test: bool = False,
        session_id: Optional[str] = None,
    ):
        # Resolve the default at construction (not import) so a runtime change
        # to ``TOTALRECLAW_SERVER_URL`` after this module is imported takes
        # effect. An import-time snapshot as a default param would silently pin
        # a stale URL.
        self._relay_url = (relay_url or _default_relay_url()).rstrip("/")
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
        # Client-wide read pause (#662) — see ``read_block`` / ``_set_read_block``.
        # In-memory only; not persisted across a daemon restart (Decision #2,
        # read-error-surfacing.md).
        self._read_block: Optional[ReadBlockState] = None
        self._read_block_episode_counter: int = 0

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

    # ------------------------------------------------------------------
    # Public seam for the high-level client.
    #
    # ``TotalReclaw`` used to reach into these private attributes directly
    # (reading ``_relay_url`` / ``_client_id`` / ``_session_id`` and
    # *mutating* ``_wallet_address`` after lazy SA resolution). Exposing a
    # small read API + an explicit ``set_wallet_address`` keeps that state
    # transition inside ``RelayClient`` rather than smeared across the seam.
    # ------------------------------------------------------------------
    @property
    def relay_url(self) -> str:
        """The resolved relay base URL (trailing slash stripped)."""
        return self._relay_url

    @property
    def client_id(self) -> str:
        """The ``X-TotalReclaw-Client`` identity for this client."""
        return self._client_id

    @property
    def session_id(self) -> Optional[str]:
        """The optional Axiom session tag, or ``None`` if unset."""
        return self._session_id

    @property
    def auth_key_hex(self) -> Optional[str]:
        """The hex-encoded auth key used for the ``Authorization`` header."""
        return self._auth_key_hex

    @property
    def wallet_address(self) -> Optional[str]:
        """The Smart Account address the relay tags outgoing writes with."""
        return self._wallet_address

    # ------------------------------------------------------------------
    # Client-wide read pause (#662)
    #
    # Owned here (not on the higher-level ``TotalReclaw`` client) because a
    # ``RelayClient`` lives as long as the process's configured client —
    # shared by the sync-loop hooks and the Hermes async tools — making it
    # the right place for state that must be visible from every call site
    # that shares one relay connection. Plain attribute writes are enough;
    # no lock is needed (single-threaded asyncio).
    # ------------------------------------------------------------------
    def read_block(self) -> Optional[ReadBlockState]:
        """The active read-pause episode, or ``None`` once its deadline has
        passed.

        The underlying episode record is *kept* past the deadline (not
        cleared) so a re-probe that blocks again reuses the same episode id
        — only a successful response (``_clear_read_block``) starts a fresh
        episode. See ``_set_read_block``.
        """
        blk = self._read_block
        if blk is None:
            return None
        if time.monotonic() >= blk.paused_until:
            return None
        return blk

    def _set_read_block(self, err: RelayReadBlocked) -> None:
        prev = self._read_block
        now_utc = datetime.now(timezone.utc)
        pause_s = _pause_seconds(err)
        paused_until_utc = now_utc + timedelta(seconds=pause_s)
        if prev is not None:
            # Re-block within the same episode (the prior episode was never
            # cleared by a success) — log at DEBUG to avoid errors.log spam.
            episode = prev.episode
            since_utc = prev.since_utc
            logger.debug(
                "TotalReclaw: relay reads re-blocked (%s) within episode %d, "
                "until %s UTC",
                err.kind,
                episode,
                paused_until_utc.strftime("%Y-%m-%d %H:%M:%S"),
            )
        else:
            self._read_block_episode_counter += 1
            episode = self._read_block_episode_counter
            since_utc = now_utc
            logger.warning(
                "TotalReclaw: relay reads paused (%s) until %s UTC; recall "
                "will show a notice",
                err.kind,
                paused_until_utc.strftime("%Y-%m-%d %H:%M:%S"),
            )
        self._read_block = ReadBlockState(
            error=err,
            paused_until=time.monotonic() + pause_s,
            paused_until_utc=paused_until_utc,
            since_utc=since_utc,
            episode=episode,
        )

    def _clear_read_block(self) -> None:
        """Called on any 2xx ``/v1/subgraph`` response. The next block (if
        any) starts a new episode."""
        self._read_block = None

    def set_wallet_address(self, wallet_address: Optional[str]) -> None:
        """Update the Smart Account address forwarded on writes.

        Called by :class:`~totalreclaw.client.TotalReclaw` once the CREATE2
        Smart Account address is resolved lazily (it is unknown at relay
        construction time). Kept as an explicit method so the client does
        not mutate ``self._relay._wallet_address`` across the seam.
        """
        self._wallet_address = wallet_address

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-TotalReclaw-Client": _client_header_value(self._client_id),
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
            "X-TotalReclaw-Client": _client_header_value(self._client_id),
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
        # Read-denial surfacing (#662) — an active pause means NO HTTP call.
        # Once one call is blocked, every ``query_subgraph`` call site gets a
        # zero-HTTP typed-error failure until the pause window expires: this
        # is what stops a recall's trapdoor-chunk fan-out (plus dedup /
        # contradiction / confirm-indexed) from re-hitting an already-denied
        # relay. See docs/specs/totalreclaw/read-error-surfacing.md §4.2-4.3.
        blk = self.read_block()
        if blk is not None:
            logger.debug(
                "subgraph read short-circuited (%s paused until %s UTC)",
                blk.error.kind,
                blk.paused_until_utc.strftime("%Y-%m-%d %H:%M:%S"),
            )
            # ``with_traceback(None)`` — this same exception instance can be
            # raised many times across an episode; reset the traceback each
            # time so it doesn't grow unbounded.
            raise blk.error.with_traceback(None)

        http = await self._get_http()
        params = {}
        if chain:
            params["chain"] = chain
        # The relay routes /v1/subgraph to the correct chain by looking up
        # this wallet's tier (Pro → Gnosis, Free → Base Sepolia). It reads the
        # wallet from the ``X-Wallet-Address`` header and *fails open to free*
        # when the header is absent. Without it, Pro-tier reads land on the
        # Base Sepolia subgraph and return 0 rows even though the facts live
        # on Gnosis — the recall-returns-nothing bug (issue #486). Mirror the
        # write path (``submit_userop``) and always send the wallet so reads
        # track the same chain as writes.
        headers = self._base_headers()
        if self._wallet_address:
            headers["X-Wallet-Address"] = self._wallet_address
        resp = await http.post(
            f"{self._relay_url}/v1/subgraph",
            headers=headers,
            json={"query": query, "variables": variables},
            params=params,
        )
        if resp.is_success:
            self._clear_read_block()
            return resp.json()
        err = _classify_subgraph_error(resp)
        if isinstance(err, RelayReadBlocked):
            self._set_read_block(err)
        raise err

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
                recall_top_k=f.get("recall_top_k"),
                latest_stable_python=f.get("latest_stable_python"),
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
        # The relay returns HTTP 200 even on its own error path, with
        # ``{success: false, error_code, error_message}`` and NO
        # ``checkout_url`` (see relay src/routes/billing.ts). Detect that and
        # raise a readable error instead of a bare ``KeyError: 'checkout_url'``.
        checkout_url = data.get("checkout_url")
        if not checkout_url:
            detail = data.get("error_message") or data.get("error_code") or "relay returned no checkout_url"
            raise RuntimeError(f"relay checkout failed: {detail}")
        # ``session_id`` is optional — the relay does not send it.
        return CheckoutResponse(checkout_url=checkout_url, session_id=data.get("session_id"))

    async def create_topup(self, pack: str) -> CheckoutResponse:
        """Create a one-time top-up Checkout session (#392).

        ``pack`` is the facts count (``"1000"`` | ``"5000"`` | ``"10000"``).
        Mirrors :meth:`create_checkout` but hits ``/v1/billing/topup``
        (mode='payment'); the relay credits ``topup_writes_purchased`` via the
        Stripe webhook on completion.
        """
        http = await self._get_http()
        resp = await http.post(
            f"{self._relay_url}/v1/billing/topup",
            headers=self._base_headers(),
            json={"wallet_address": self._wallet_address, "pack": pack},
        )
        resp.raise_for_status()
        data = resp.json()
        checkout_url = data.get("checkout_url")
        if not checkout_url:
            detail = data.get("error_message") or data.get("error_code") or "relay returned no checkout_url"
            raise RuntimeError(f"relay top-up failed: {detail}")
        return CheckoutResponse(checkout_url=checkout_url, session_id=data.get("session_id"))

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
