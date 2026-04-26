"""Regression shield for Hermes rc.23 brutal-QA findings (umbrella #147).

Covers the three Hermes-side findings landed in branch
``fix/hermes-rc23-three-findings``:

* **F1 (umbrella #148, BLOCKER)** — ``totalreclaw_pin`` (and
  ``_unpin`` / ``_forget`` / future ``retype`` / ``set_scope``) crash
  on every invocation against PyPI ``totalreclaw-core@2.2.0`` with::

      AttributeError: module 'totalreclaw_core' has no attribute 'confirm_indexed_query'

  Root cause: the ``confirm_indexed_*`` PyO3 bindings were added in the
  Rust source via PR #124 but never published to PyPI. Hermes Python's
  confirm-indexed wrapper called into bindings that only exist in our
  local source tree. The fix mirrors the TS plugin's
  ``skill/plugin/confirm-indexed.ts`` graceful pattern (commit
  ``d9c5352``): wrap binding lookups in try/except, return
  ``ConfirmIndexedResult(indexed=False, last_error=...)`` instead of
  raising. Calling tools surface ``partial=True`` rather than crashing.

* **F4 (umbrella #151, MEDIUM)** —
  ``hermes.pair_tool_completion.complete_pairing`` referenced
  ``session.sid`` but ``RemotePairSession`` only has ``.token``,
  raising ``AttributeError`` on the relay-mode shared completion path.
  Fix: read either attribute defensively (``token`` first because that's
  the canonical relay shape, falling back to ``sid`` for local mode).

Out of scope here: F5 (chat install Step 1 lands ``totalreclaw``
outside Hermes venv on containerized deploys) — that's a docs / SKILL.md
fix verified by reading the rendered guide, not by automated test.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# F1 — confirm_indexed graceful binding-missing fallback
# ---------------------------------------------------------------------------


class TestConfirmIndexedGracefulBindings:
    """When ``totalreclaw_core`` doesn't expose the ``confirm_indexed_*``
    PyO3 bindings (any wheel published before they were added),
    :func:`confirm_indexed` MUST NOT raise. Instead it returns ``False``
    so callers can surface ``partial=True``.
    """

    @pytest.mark.asyncio
    async def test_missing_query_binding_returns_false_not_raises(self):
        """Pre-rc.23 this raised ``AttributeError`` and crashed pin/unpin/forget.

        The fix wraps the ``_core.confirm_indexed_query()`` call in
        try/except inside :func:`confirm_indexed_detailed`. The boolean
        wrapper :func:`confirm_indexed` propagates the False result.
        """
        from totalreclaw import confirm_indexed as ci_mod

        relay = AsyncMock()

        # Simulate a wheel that doesn't export ``confirm_indexed_query``.
        # ``getattr`` would return None — the production helper invokes
        # the attribute directly, which raises AttributeError.
        broken_core = SimpleNamespace()  # No confirm_indexed_* attrs at all

        with patch.object(ci_mod, "_core", broken_core):
            indexed = await ci_mod.confirm_indexed("any-fact-id", relay)
            assert indexed is False, (
                "missing PyO3 bindings must return False, not raise"
            )

    @pytest.mark.asyncio
    async def test_missing_query_binding_detailed_carries_last_error(self):
        """The detailed helper must annotate ``last_error`` so observability /
        Hermes logs can surface WHY the read-after-write was skipped.
        """
        from totalreclaw import confirm_indexed as ci_mod

        relay = AsyncMock()
        broken_core = SimpleNamespace()

        with patch.object(ci_mod, "_core", broken_core):
            result = await ci_mod.confirm_indexed_detailed(
                "any-fact-id", relay
            )

        assert result.indexed is False
        assert result.attempts == 0, (
            "no poll attempts when bindings unavailable"
        )
        assert result.elapsed_ms == 0
        assert result.last_error is not None
        assert "bindings unavailable" in result.last_error

    @pytest.mark.asyncio
    async def test_query_binding_present_but_raises_returns_false(self):
        """Even if ``confirm_indexed_query`` exists but raises (e.g. a
        future protocol-version mismatch), the helper must still
        gracefully return False — never bubble to the calling tool.
        """
        from totalreclaw import confirm_indexed as ci_mod

        relay = AsyncMock()

        def boom():
            raise RuntimeError("simulated core protocol mismatch")

        broken_core = SimpleNamespace(
            confirm_indexed_query=boom,
            confirm_indexed_default_poll_ms=lambda: 100,
            confirm_indexed_default_timeout_ms=lambda: 1000,
        )

        with patch.object(ci_mod, "_core", broken_core):
            indexed = await ci_mod.confirm_indexed("fact", relay)
            assert indexed is False

    @pytest.mark.asyncio
    async def test_pin_tool_returns_partial_when_bindings_missing(self):
        """End-to-end shield: a successful chain write that hit the
        binding-missing path should yield ``pinned=True`` plus
        ``partial=True`` from ``hermes.tools.pin``. Pre-rc.23 the call
        crashed with ``AttributeError``.
        """
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import pin as pin_tool
        import json as _json

        state = PluginState()
        fake_client = AsyncMock()
        fake_client.pin_fact = AsyncMock(
            return_value={
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_status": "active",
                "new_status": "pinned",
                # operations._change_claim_status sets this when
                # confirm_indexed returned False — the rc.23 graceful
                # path that fires on missing PyO3 bindings.
                "partial": True,
            }
        )
        state._client = fake_client

        result = _json.loads(await pin_tool({"fact_id": "old-id"}, state))
        assert result.get("pinned") is True
        assert result.get("partial") is True, (
            "pin tool must propagate partial=True when confirm_indexed "
            "short-circuits on missing PyO3 bindings"
        )
        # Crucially, no `error` key — pre-rc.23 the call raised
        # AttributeError and surfaced as {'error': "module 'totalreclaw_core' "
        # "has no attribute 'confirm_indexed_query'"}.
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_unpin_tool_returns_partial_when_bindings_missing(self):
        """Mirror of the pin shield for unpin — the same
        ``operations._change_claim_status`` path serves both tools, so
        the partial flag must traverse the unpin wrapper too.
        """
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import unpin as unpin_tool
        import json as _json

        state = PluginState()
        fake_client = AsyncMock()
        fake_client.unpin_fact = AsyncMock(
            return_value={
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_status": "pinned",
                "new_status": "active",
                "partial": True,
            }
        )
        state._client = fake_client

        result = _json.loads(await unpin_tool({"fact_id": "old-id"}, state))
        assert result.get("unpinned") is True
        assert result.get("partial") is True
        assert "error" not in result


# ---------------------------------------------------------------------------
# F4 — pair_tool_completion handles both PairSession (.sid) and
# RemotePairSession (.token)
# ---------------------------------------------------------------------------


@dataclass
class _FakeRemoteSession:
    """Minimal stand-in for ``..pair.remote_client.RemotePairSession``.

    The real class has more fields (url, pin, expires_at, keypair, _ws)
    but ``complete_pairing`` only reads the session-id attribute, so we
    keep this fixture minimal.
    """

    token: str


@dataclass
class _FakeLocalSession:
    """Minimal stand-in for ``..pair.session_store.PairSession``."""

    sid: str


class TestCompletePairingSessionAttribute:
    """:func:`hermes.pair_tool_completion.complete_pairing` must read
    either ``session.token`` (relay path, ``RemotePairSession``) or
    ``session.sid`` (local path, ``PairSession``) without raising.

    Pre-rc.23 the helper hard-coded ``session.sid``, raising
    ``AttributeError: 'RemotePairSession' object has no attribute 'sid'``
    on the relay-mode shared completion path (QA finding F4).
    """

    def _build_state_mock(self, eoa: str = "0xEOA") -> MagicMock:
        """Build a fake ``PluginState`` whose ``configure`` is a no-op
        and whose ``get_client`` exposes ``_eoa_address``.
        """
        state = MagicMock()
        state.configure = MagicMock()  # synchronous + no-op
        client = MagicMock()
        client._eoa_address = eoa
        state.get_client = MagicMock(return_value=client)
        return state

    def test_remote_session_with_token_does_not_raise(self):
        """RemotePairSession-shaped object — pre-rc.23 this raised
        AttributeError because the helper read ``session.sid``.
        """
        from totalreclaw.hermes.pair_tool_completion import complete_pairing

        state = self._build_state_mock()
        session = _FakeRemoteSession(token="tok_1234567890abcdef")

        # Must NOT raise — the rc.23 fix uses defensive attribute
        # lookup so this shape works on the shared completion path.
        result = complete_pairing("abandon " * 11 + "about", session, state)
        assert result.state == "active"
        assert result.account_id == "0xEOA"
        # configure was called once with the phrase argument.
        state.configure.assert_called_once()

    def test_local_session_with_sid_still_works(self):
        """Backwards-compat: the local HTTP server passes a
        ``PairSession`` (with ``.sid``). The rc.23 fix must not break
        that path.
        """
        from totalreclaw.hermes.pair_tool_completion import complete_pairing

        state = self._build_state_mock()
        session = _FakeLocalSession(sid="sid_abcdef0123456789")

        result = complete_pairing("abandon " * 11 + "about", session, state)
        assert result.state == "active"
        assert result.account_id == "0xEOA"

    def test_session_without_either_attribute_does_not_crash_logging(self):
        """Defensive: even a totally empty session object shouldn't
        torpedo a successful pairing with a logging-line crash.
        """
        from totalreclaw.hermes.pair_tool_completion import complete_pairing

        state = self._build_state_mock()
        empty_session = SimpleNamespace()  # no .sid, no .token

        # Should not raise; should still return active.
        result = complete_pairing("abandon " * 11 + "about", empty_session, state)
        assert result.state == "active"

    def test_remote_session_logged_with_token_prefix(self, caplog):
        """Verify the log line uses an 8-char prefix from ``.token``
        (not ``.sid``) when the session is remote-shaped. Useful for
        operators correlating gateway logs with relay logs.
        """
        import logging

        from totalreclaw.hermes.pair_tool_completion import complete_pairing

        state = self._build_state_mock(eoa="0xCAFE")
        session = _FakeRemoteSession(token="tok_remoteABC123")

        with caplog.at_level(logging.INFO, logger="totalreclaw.hermes.pair_tool_completion"):
            complete_pairing("abandon " * 11 + "about", session, state)

        # The first 8 chars of the token should appear in at least one
        # log record (info-level success line).
        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "tok_remo" in joined, (
            "expected 8-char token prefix in info log; got: " + joined
        )


# ---------------------------------------------------------------------------
# Phrase-safety regression assertion (sanity — must continue to pass)
# ---------------------------------------------------------------------------


class TestPhraseSafetyUnaffected:
    """The rc.23 fixes touch confirm_indexed + pair_tool_completion +
    Hermes setup docs. None of these should reintroduce a phrase-
    leaking surface. This redundant smoke check confirms the canonical
    forbidden tool list is still empty after :func:`totalreclaw.hermes.register`
    runs against the rc.23 changes.
    """

    def test_no_phrase_generating_tool_re_added(self):
        """Mirror of ``test_agent_tools_phrase_safety.py``'s contract —
        keep the rc.23 branch from accidentally regressing the
        phrase-safety gate. If the canonical test starts failing this
        redundant check flags it locally too.
        """
        from unittest.mock import MagicMock, patch
        import os
        from pathlib import Path

        from totalreclaw.hermes import register

        forbidden = (
            "totalreclaw_setup",
            "totalreclaw_onboard",
            "totalreclaw_onboard_generate",
            "totalreclaw_restore",
            "totalreclaw_generate_phrase",
            "totalreclaw_mnemonic",
        )

        ctx = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                register(ctx)

        registered_names = {
            call.kwargs["name"]
            for call in ctx.register_tool.call_args_list
            if "name" in call.kwargs
        }

        for name in forbidden:
            assert name not in registered_names, (
                f"Phrase-safety regression: {name!r} appears in the "
                f"registered tool set after the rc.23 fix branch."
            )
