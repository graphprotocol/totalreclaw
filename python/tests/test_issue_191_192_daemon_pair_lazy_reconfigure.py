"""Regression tests for issues #191 (auto-extraction not firing in
daemon mode) and #192 (lazy relay account creation).

Both bugs surfaced during user manual QA on stable 2.3.1 (2026-04-27)
running Hermes in DAEMON mode (gateway-served chat via Telegram). The
common root cause: the gateway's plugin singleton is constructed ONCE
at boot, and in rc.24+ the pair-completion sidecar runs in a SEPARATE
PROCESS that writes ``~/.totalreclaw/credentials.json`` without any
in-process notification to the gateway-side ``PluginState``.

Pre-2.3.2 effect:

* ``state.is_configured()`` stays ``False`` for the gateway's full
  lifetime (or until the user manually restarted the gateway, per the
  ``totalreclaw_pair`` instructions). Every ``post_llm_call`` hook
  early-returns at the ``if not state.is_configured(): return``
  guard, so no auto-extraction ever runs. → bug #191.
* The relay only learns the account exists when an authenticated,
  wallet-keyed request hits it (e.g. ``totalreclaw_status``). Until
  the user explicitly probed (`"what's my quota?"`), the staging
  relay had no account record. → bug #192.

The 2.3.2-rc.1 fix adds two helpers in ``totalreclaw.hermes.hooks``:

* ``_maybe_reconfigure_from_disk(state)`` re-reads creds.json on every
  ``on_session_start`` / ``pre_llm_call`` / ``post_llm_call`` entry.
  Cheap on the configured-state hot path (early return on
  ``is_configured()``), and idempotent on disk.
* ``_eager_account_register(state)`` issues a one-shot
  ``client.status()`` once per state-configure event so the relay
  side gets an authenticated wallet-keyed request before any
  extraction batch fires.

These tests pin the contract:

1. ``_maybe_reconfigure_from_disk`` reconfigures when creds appeared
   after PluginState construction; otherwise it's a no-op.
2. ``post_llm_call`` in daemon mode picks up creds written mid-flight
   and starts firing extraction on the next per-N-turns boundary.
3. ``on_session_start`` calls ``client.status()`` once per state-
   configure event for eager relay-side account creation.
4. ``state.configure()`` clears the eager-register latch so re-pairing
   with a different mnemonic re-registers the new SA.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_home(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a fresh temp dir for the test."""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setenv("HOME", tmpdir)
    monkeypatch.setattr(Path, "home", lambda: Path(tmpdir))
    return Path(tmpdir)


def _write_creds(home: Path, mnemonic: str) -> None:
    """Materialize a canonical credentials.json under ``home``."""
    creds_dir = home / ".totalreclaw"
    creds_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (creds_dir / "credentials.json").write_text(
        json.dumps({"mnemonic": mnemonic})
    )


def _stub_client(*_, **__):
    """Drop-in stub for ``totalreclaw.client.TotalReclaw``."""
    c = MagicMock()
    c._eoa_address = "0xeoatest"
    c._sa_address = None
    c.smart_account_address = None
    c.wallet_address = "0xeoatest"
    return c


_VALID_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


# ---------------------------------------------------------------------------
# 1. _maybe_reconfigure_from_disk contract
# ---------------------------------------------------------------------------


def test_maybe_reconfigure_picks_up_creds_written_after_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the fix: when creds.json appears AFTER the
    plugin constructed its state (rc.24+ sidecar pair flow), the next
    hook call must reconfigure without a gateway restart.
    """
    home = _tmp_home(monkeypatch)
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)

    from totalreclaw.hermes.state import PluginState
    from totalreclaw.hermes.hooks import _maybe_reconfigure_from_disk

    with patch("totalreclaw.client.TotalReclaw", side_effect=_stub_client):
        state = PluginState()
        assert not state.is_configured(), "no creds yet on init"

        # Sidecar writes creds mid-session.
        _write_creds(home, _VALID_MNEMONIC)

        # The reconfigure helper must pick them up.
        did = _maybe_reconfigure_from_disk(state)
        assert did is True
        assert state.is_configured()


def test_maybe_reconfigure_is_no_op_when_already_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _tmp_home(monkeypatch)
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)
    _write_creds(home, _VALID_MNEMONIC)

    from totalreclaw.hermes.state import PluginState
    from totalreclaw.hermes.hooks import _maybe_reconfigure_from_disk

    with patch("totalreclaw.client.TotalReclaw", side_effect=_stub_client):
        state = PluginState()
        assert state.is_configured()
        # Second call must not re-trigger; returns False.
        assert _maybe_reconfigure_from_disk(state) is False


def test_maybe_reconfigure_no_op_when_creds_still_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tmp_home(monkeypatch)
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)

    from totalreclaw.hermes.state import PluginState
    from totalreclaw.hermes.hooks import _maybe_reconfigure_from_disk

    with patch("totalreclaw.client.TotalReclaw", side_effect=_stub_client):
        state = PluginState()
        assert not state.is_configured()
        assert _maybe_reconfigure_from_disk(state) is False
        assert not state.is_configured()


# ---------------------------------------------------------------------------
# 2. Daemon-mode hook flow — post_llm_call must trigger extraction once
#    creds appear mid-conversation. This is the headline #191 regression.
# ---------------------------------------------------------------------------


def test_post_llm_call_fires_extraction_after_mid_session_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end daemon-mode simulation:

    1. Gateway boots, plugin loads — no creds yet.
    2. ``on_session_start`` fires for the user's first session.
    3. User runs ``totalreclaw_pair`` from chat. The sidecar
       subprocess writes creds.json (we simulate by writing the file).
    4. Subsequent ``post_llm_call`` invocations must reconfigure the
       in-memory state AND start firing ``_auto_extract`` every N
       turns.

    Pre-2.3.2 this test would fail at ``mock_extract.call_count >= 1``
    because the unconfigured state suppressed every extraction.
    """
    home = _tmp_home(monkeypatch)
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)

    from totalreclaw.hermes.state import PluginState
    from totalreclaw.hermes import hooks

    with patch("totalreclaw.client.TotalReclaw", side_effect=_stub_client):
        # Step 1: plugin loads, no creds yet.
        state = PluginState()
        assert not state.is_configured()

        # Step 2: first session_start.
        hooks.on_session_start(state, session_id="s1")
        assert not state.is_configured()

        # Step 3: simulate sidecar writing creds.json mid-session.
        _write_creds(home, _VALID_MNEMONIC)

        # Step 4: 4 more post_llm_call invocations. With the default
        # extraction interval of 3 turns, we expect ≥1 extraction call.
        with patch.object(hooks, "_auto_extract") as mock_extract, patch.object(
            hooks, "_get_hermes_llm_config", return_value=MagicMock()
        ), patch.object(hooks, "_eager_account_register"):
            for i in range(1, 5):
                hooks.post_llm_call(
                    state,
                    user_message=f"user turn {i}",
                    assistant_response=f"assistant turn {i}",
                )

        # Lazy reconfigure should have fired on turn 1 and the
        # extraction interval should have tripped on turn 3.
        assert state.is_configured(), (
            "post_llm_call must lazy-reconfigure when creds appear "
            "after plugin load (issue #191)"
        )
        assert mock_extract.call_count >= 1, (
            f"expected ≥1 extraction in 4 turns at interval=3, "
            f"got {mock_extract.call_count}"
        )


# ---------------------------------------------------------------------------
# 3. Eager account register (#192)
# ---------------------------------------------------------------------------


def test_eager_account_register_calls_status_once_per_configure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_eager_account_register`` must call ``client.status()`` once
    when the state is configured, latch a flag so subsequent calls
    are no-ops, and clear that latch when ``state.configure()`` runs
    again (re-pair flow).
    """
    home = _tmp_home(monkeypatch)
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)
    _write_creds(home, _VALID_MNEMONIC)

    # Track status() calls on the stubbed client.
    status_calls: list[int] = []

    def stub_with_status(*_, **__):
        c = _stub_client()

        async def _status():
            status_calls.append(1)
            return MagicMock()

        c.status = _status
        return c

    from totalreclaw.hermes.state import PluginState
    from totalreclaw.hermes.hooks import _eager_account_register

    with patch("totalreclaw.client.TotalReclaw", side_effect=stub_with_status):
        state = PluginState()
        assert state.is_configured()

        # First call: should invoke status().
        _eager_account_register(state)
        assert len(status_calls) == 1, "status() must be called once"

        # Second call: latched, no-op.
        _eager_account_register(state)
        assert len(status_calls) == 1, "second call must short-circuit"

        # Re-pair flow: configure() should clear the latch.
        state.configure(_VALID_MNEMONIC)
        _eager_account_register(state)
        assert len(status_calls) == 2, (
            "state.configure() must clear the eager-register latch so "
            "the new SA gets its own first-contact request to the relay"
        )


def test_on_session_start_triggers_eager_account_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_session_start`` is the canonical fire point for the eager
    register — must invoke ``_eager_account_register`` when the state
    is configured (creds existed at boot OR were just lazy-reconfigured).
    """
    home = _tmp_home(monkeypatch)
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)
    _write_creds(home, _VALID_MNEMONIC)

    from totalreclaw.hermes.state import PluginState
    from totalreclaw.hermes import hooks

    with patch("totalreclaw.client.TotalReclaw", side_effect=_stub_client):
        state = PluginState()
        with patch.object(hooks, "_eager_account_register") as mock_eager:
            hooks.on_session_start(state, session_id="s1")
            mock_eager.assert_called_once_with(state)


def test_on_session_start_skips_eager_register_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If creds still missing after the lazy reconfigure attempt,
    don't try to register — the client doesn't exist yet.
    """
    _tmp_home(monkeypatch)
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)

    from totalreclaw.hermes.state import PluginState
    from totalreclaw.hermes import hooks

    with patch("totalreclaw.client.TotalReclaw", side_effect=_stub_client):
        state = PluginState()
        assert not state.is_configured()
        with patch.object(hooks, "_eager_account_register") as mock_eager:
            hooks.on_session_start(state, session_id="s1")
            mock_eager.assert_not_called()
