"""[memq-3] AgentState.session_id — UUIDv7, session-scoped, in-memory only.

Per memq spec §5 + decomposition item 25:

* ``session_id`` populated on session start via ``uuid7()``.
* Available to hooks throughout session lifetime.
* Cleared on finalize.
* UUIDv7 (time-ordered).
* In-memory only — never persisted to disk (spec §3.1).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AgentState — unit tests
# ---------------------------------------------------------------------------


def _make_state():
    """Build a bare AgentState with no client + no env config."""
    from totalreclaw.agent.state import AgentState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            return AgentState()


def _parse_uuid_hex(sid: str) -> int:
    return int(sid.replace("-", ""), 16)


def test_session_id_defaults_to_none() -> None:
    state = _make_state()
    assert state.session_id is None


def test_start_session_returns_and_stores_uuid7() -> None:
    state = _make_state()
    returned = state.start_session()
    assert returned == state.session_id
    assert isinstance(returned, str)
    # Canonical 8-4-4-4-12 hex layout
    parts = returned.split("-")
    assert [len(p) for p in parts] == [8, 4, 4, 4, 12]


def test_start_session_emits_uuid_version_7_and_rfc_variant() -> None:
    state = _make_state()
    sid = state.start_session()
    val = _parse_uuid_hex(sid)
    # Version nibble is bits 76-79 (counting from the LSB). Pull the
    # 13th hex char (= 4 high bits of the 7th hex group from the left).
    version_nibble = (val >> 76) & 0xF
    assert version_nibble == 0x7, f"expected version 7, got {version_nibble:x}"
    # Variant: top 2 bits of the 9th hex group are 0b10.
    variant_high_bits = (val >> 62) & 0b11
    assert variant_high_bits == 0b10, (
        f"expected RFC variant bits 10, got {variant_high_bits:b}"
    )


def test_start_session_is_time_ordered() -> None:
    """Two consecutive UUIDv7s, separated by a small sleep, compare in
    chronological order when treated as 128-bit ints. UUIDv7's 48 high
    bits encode ms-precision unix time so the second value must be
    strictly greater than the first once at least 1 ms has elapsed."""
    state = _make_state()
    a = state.start_session()
    # Wait > 1ms to guarantee the ms timestamp ticks forward.
    time.sleep(0.005)
    b = state.start_session()
    assert _parse_uuid_hex(b) > _parse_uuid_hex(a)


def test_end_session_clears_id_and_is_idempotent() -> None:
    state = _make_state()
    state.start_session()
    assert state.session_id is not None
    state.end_session()
    assert state.session_id is None
    # Second call must not raise.
    state.end_session()
    assert state.session_id is None


def test_start_session_after_end_produces_fresh_id() -> None:
    state = _make_state()
    first = state.start_session()
    state.end_session()
    second = state.start_session()
    assert second != first


# ---------------------------------------------------------------------------
# Hermes hooks — wiring tests
# ---------------------------------------------------------------------------


def _make_plugin_state(configured: bool):
    """Build a PluginState. ``configured=True`` attaches a MagicMock client
    so ``is_configured()`` returns True without hitting real auto-config."""
    from totalreclaw.hermes.state import PluginState

    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    if configured:
        state._client = MagicMock()
    return state


def test_on_session_start_populates_session_id_when_configured() -> None:
    from totalreclaw.hermes import hooks

    state = _make_plugin_state(configured=True)
    # Stub out the expensive helpers that on_session_start fans into.
    with patch.object(hooks, "_maybe_reconfigure_from_disk"), \
         patch.object(hooks, "_eager_account_register"), \
         patch.object(hooks, "_owner_addresses", return_value=[]), \
         patch.object(hooks, "has_pending", return_value=False):
        hooks.on_session_start(state)
    assert state.session_id is not None


def test_on_session_start_populates_session_id_when_unconfigured() -> None:
    """Hook should set session_id even when the client isn't configured
    yet — useful for log correlation during the pair/setup flow."""
    from totalreclaw.hermes import hooks

    state = _make_plugin_state(configured=False)
    with patch.object(hooks, "_maybe_reconfigure_from_disk"):
        hooks.on_session_start(state)
    assert state.session_id is not None


def test_on_session_finalize_clears_session_id() -> None:
    from totalreclaw.hermes import hooks

    state = _make_plugin_state(configured=True)
    state.start_session()
    assert state.session_id is not None

    with patch.object(hooks, "_auto_extract", return_value=[]), \
         patch.object(hooks, "_session_debrief", return_value=[]), \
         patch.object(hooks, "_get_hermes_llm_config", return_value=None):
        hooks.on_session_finalize(state)
    assert state.session_id is None


def test_on_session_finalize_clears_session_id_when_unconfigured() -> None:
    """The unconfigured early-return path must still clear the id so the
    invariant 'session_id is None outside an active session' holds."""
    from totalreclaw.hermes import hooks

    state = _make_plugin_state(configured=False)
    state.start_session()
    hooks.on_session_finalize(state)
    assert state.session_id is None


def test_session_id_survives_across_turns_within_one_session() -> None:
    """Per the issue's acceptance criteria: ``session_id`` is available
    to hooks throughout session lifetime — i.e. it must NOT change on
    intra-session hook calls (post_llm_call, on_session_end which is
    per-turn, etc.). Only on_session_start should rotate it."""
    from totalreclaw.hermes import hooks

    state = _make_plugin_state(configured=True)
    with patch.object(hooks, "_maybe_reconfigure_from_disk"), \
         patch.object(hooks, "_eager_account_register"), \
         patch.object(hooks, "_owner_addresses", return_value=[]), \
         patch.object(hooks, "has_pending", return_value=False):
        hooks.on_session_start(state)
    sid_at_start = state.session_id

    # on_session_end is per-turn no-op — must not touch session_id.
    hooks.on_session_end(state)
    assert state.session_id == sid_at_start
