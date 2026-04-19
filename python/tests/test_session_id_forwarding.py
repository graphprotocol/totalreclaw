"""Bug #4 regression tests — TOTALRECLAW_SESSION_ID is honored again.

v2.0.1 accidentally rejected ``TOTALRECLAW_SESSION_ID`` as a "removed env
var" and emitted a cryptic warning line, breaking Axiom session-scoped log
queries (QA-V1CLEAN-VPS-20260418 Bug #1). The Python client needs to:

1. NOT warn on ``TOTALRECLAW_SESSION_ID`` (it is a SUPPORTED env var
   again in v2.0.2).
2. Forward the value to the relay as the ``X-TotalReclaw-Session`` header
   on every HTTP call — subgraph queries, bundler submissions, billing
   status, and the ``/v1/register`` handshake.
3. Also accept an explicit ``session_id=`` constructor arg as a
   programmatic override (takes precedence over the env var).

The relay (``totalreclaw-relay/src/routes/proxy.ts``) reads the header
and emits it as the ``sessionId`` field on Axiom log lines.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# RelayClient — accepts env + constructor override, forwards header
# ---------------------------------------------------------------------------


def test_relay_client_picks_up_env_session_id() -> None:
    """``TOTALRECLAW_SESSION_ID`` env var is read at construction."""
    from totalreclaw.relay import RelayClient

    with patch.dict(os.environ, {"TOTALRECLAW_SESSION_ID": "qa-py202-1"}):
        rc = RelayClient(auth_key_hex="00" * 32)
    assert rc._session_id == "qa-py202-1"


def test_relay_client_constructor_arg_overrides_env() -> None:
    """Explicit ``session_id=`` kwarg wins over the env var."""
    from totalreclaw.relay import RelayClient

    with patch.dict(os.environ, {"TOTALRECLAW_SESSION_ID": "env-value"}):
        rc = RelayClient(auth_key_hex="00" * 32, session_id="override-value")
    assert rc._session_id == "override-value"


def test_relay_client_no_session_id_omits_header() -> None:
    """No env, no kwarg → no ``X-TotalReclaw-Session`` header."""
    from totalreclaw.relay import RelayClient

    with patch.dict(os.environ, {}, clear=True):
        rc = RelayClient(auth_key_hex="00" * 32)
    assert rc._session_id is None
    headers = rc._base_headers()
    assert "X-TotalReclaw-Session" not in headers


def test_relay_client_base_headers_include_session() -> None:
    """With a session set, the base headers include it."""
    from totalreclaw.relay import RelayClient

    rc = RelayClient(auth_key_hex="00" * 32, session_id="qa-py202-2")
    headers = rc._base_headers()
    assert headers["X-TotalReclaw-Session"] == "qa-py202-2"


def test_relay_client_empty_env_is_treated_as_unset() -> None:
    """Empty string in env must not set a header with an empty value."""
    from totalreclaw.relay import RelayClient

    with patch.dict(os.environ, {"TOTALRECLAW_SESSION_ID": ""}):
        rc = RelayClient(auth_key_hex="00" * 32)
    assert rc._session_id is None
    assert "X-TotalReclaw-Session" not in rc._base_headers()


# ---------------------------------------------------------------------------
# End-to-end: the header actually makes it on the wire on subgraph + register
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subgraph_query_sends_session_header() -> None:
    """A subgraph call from RelayClient carries the session header."""
    from totalreclaw.relay import RelayClient

    observed: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        observed.update(dict(request.headers))
        return httpx.Response(200, json={"data": {"facts": []}})

    transport = httpx.MockTransport(_handler)
    rc = RelayClient(
        relay_url="https://api-staging.totalreclaw.xyz",
        auth_key_hex="00" * 32,
        session_id="qa-subgraph-1",
    )

    # Install mock transport on the current loop.
    async def _get_http():
        return httpx.AsyncClient(transport=transport, timeout=10.0)

    rc._get_http = _get_http  # type: ignore[assignment]

    await rc.query_subgraph("{ facts { id } }", {})
    assert observed.get("x-totalreclaw-session") == "qa-subgraph-1"


@pytest.mark.asyncio
async def test_register_sends_session_header() -> None:
    """The ``/v1/register`` call (which uses its own headers dict, not
    ``_base_headers``) must still include the session header."""
    from totalreclaw.relay import RelayClient

    observed: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        observed.update(dict(request.headers))
        return httpx.Response(200, json={"user_id": "abc"})

    transport = httpx.MockTransport(_handler)
    rc = RelayClient(
        relay_url="https://api-staging.totalreclaw.xyz",
        auth_key_hex="00" * 32,
        session_id="qa-register-1",
    )

    async def _get_http():
        return httpx.AsyncClient(transport=transport, timeout=10.0)

    rc._get_http = _get_http  # type: ignore[assignment]

    uid = await rc.register("hash" * 8, "salt" * 8)
    assert uid == "abc"
    assert observed.get("x-totalreclaw-session") == "qa-register-1"


# ---------------------------------------------------------------------------
# TotalReclaw client threads it through to the RelayClient
# ---------------------------------------------------------------------------


TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def test_client_forwards_session_id_to_relay() -> None:
    """Passing ``session_id=`` on ``TotalReclaw(...)`` sets the header."""
    from totalreclaw.client import TotalReclaw

    with patch.dict(os.environ, {}, clear=True):
        c = TotalReclaw(recovery_phrase=TEST_MNEMONIC, session_id="qa-tr-1")
    assert c._relay._session_id == "qa-tr-1"


def test_client_reads_env_session_id_when_no_override() -> None:
    """Without ``session_id=``, ``TOTALRECLAW_SESSION_ID`` is used."""
    from totalreclaw.client import TotalReclaw

    with patch.dict(os.environ, {"TOTALRECLAW_SESSION_ID": "qa-env-1"}):
        c = TotalReclaw(recovery_phrase=TEST_MNEMONIC)
    assert c._relay._session_id == "qa-env-1"


# ---------------------------------------------------------------------------
# Agent state: the "removed env var" warning no longer fires on SESSION_ID.
# ---------------------------------------------------------------------------


def test_session_id_is_not_in_removed_env_vars_list() -> None:
    """``TOTALRECLAW_SESSION_ID`` must NOT be in the removed-env list.

    The state module logs a warning at import time for any of these env
    vars when set; including SESSION_ID emitted a confusing "ignoring
    removed env var" line and caused Bug #1 in the v2.0.1 QA.
    """
    from totalreclaw.agent import state

    assert "TOTALRECLAW_SESSION_ID" not in state._REMOVED_ENV_VARS, (
        "TOTALRECLAW_SESSION_ID was restored in v2.0.2; keep it OUT of "
        "_REMOVED_ENV_VARS so the agent state module does not warn about it."
    )


def test_session_id_env_does_not_trigger_removed_warning(caplog) -> None:
    """Setting the env var must not produce a 'removed env var' warning."""
    from totalreclaw.agent.state import (
        _REMOVED_ENV_VARS,
        _warn_removed_env_vars_once,
    )
    import totalreclaw.agent.state as state_mod

    # Reset the module's once-flag so the warning path runs again.
    state_mod._warned_removed_env_vars = False
    with patch.dict(os.environ, {"TOTALRECLAW_SESSION_ID": "qa-state-1"}, clear=True):
        with caplog.at_level("WARNING", logger="totalreclaw.agent.state"):
            _warn_removed_env_vars_once()
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "TOTALRECLAW_SESSION_ID" not in joined, (
        "Setting TOTALRECLAW_SESSION_ID now-supported env var must not "
        "produce the 'ignoring removed env var' warning."
    )


# ---------------------------------------------------------------------------
# UserOp path — session_id threads through build_and_send_userop
# ---------------------------------------------------------------------------


def test_build_and_send_userop_accepts_session_id_kwarg() -> None:
    """The userop helper must accept ``session_id=`` — no TypeError.

    We don't actually send — just verify the signature accepts the kwarg,
    so that the calls from ``operations.py`` don't throw.
    """
    import inspect
    from totalreclaw.userop import build_and_send_userop

    sig = inspect.signature(build_and_send_userop)
    assert "session_id" in sig.parameters
    # Default must be None so existing callers keep working.
    assert sig.parameters["session_id"].default is None
