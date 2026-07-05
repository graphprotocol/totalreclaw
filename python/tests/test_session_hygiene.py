"""Per-conversation session hygiene: honor host session_id + idle rollover.

Guards against parallel conversations (e.g. multiple Telegram chats through one
Hermes process) collapsing into a single session_id and interleaving unrelated
facts into one Crystal. See ``agent/state.py`` + ``hermes/hooks.py``.
"""
import time

import pytest

import totalreclaw.agent.state as state_mod
from totalreclaw.agent.state import (
    AgentState,
    _session_id_from_host,
    _session_idle_seconds,
)
from totalreclaw.hermes import hooks


def _advance_clock(monkeypatch, base: float, delta: float) -> None:
    """Pin state.time.monotonic to base+delta (robust vs fresh-boot clocks)."""
    monkeypatch.setattr(state_mod.time, "monotonic", lambda: base + delta)


# ── honoring a host-provided conversation id ──────────────────────────
def test_start_session_derives_stable_id_from_host():
    s = AgentState()
    a = s.start_session(external_id="telegram:chat=42:topic=7")
    b = s.start_session(external_id="telegram:chat=42:topic=7")
    assert a == b, "same conversation id must map to the same session key"
    assert a == _session_id_from_host("telegram:chat=42:topic=7")


def test_start_session_distinct_hosts_distinct_ids():
    s = AgentState()
    a = s.start_session(external_id="telegram:chat=1")
    b = s.start_session(external_id="telegram:chat=2")
    assert a != b, "distinct conversations must get distinct session keys"


def test_start_session_no_host_mints_uuid7():
    s = AgentState()
    a = s.start_session()
    b = s.start_session()
    assert a != b, "minted sessions are fresh each call"
    assert not s._session_id_from_host


# ── idle-timeout rollover eligibility ─────────────────────────────────
def test_should_roll_only_when_idle_and_self_minted(monkeypatch):
    s = AgentState()
    # no session yet
    assert s.should_roll_idle_session(3600) is False
    # fresh minted session, no gap
    s.start_session()
    assert s.should_roll_idle_session(3600) is False
    # advance the clock past the idle window (real _last_activity, no negatives)
    _advance_clock(monkeypatch, s._last_activity, 10_000)
    assert s.should_roll_idle_session(3600) is True
    # disabled by config
    assert s.should_roll_idle_session(0) is False


def test_host_derived_session_never_idle_rolls(monkeypatch):
    s = AgentState()
    s.start_session(external_id="telegram:chat=9")
    _advance_clock(monkeypatch, s._last_activity, 10_000)
    # the host owns this conversation boundary — never split it on a timer
    assert s.should_roll_idle_session(3600) is False


def test_idle_seconds_env(monkeypatch):
    monkeypatch.delenv("TOTALRECLAW_SESSION_IDLE_MINUTES", raising=False)
    assert _session_idle_seconds() == 3600
    monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "0")
    assert _session_idle_seconds() == 0  # disabled
    monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "30")
    assert _session_idle_seconds() == 1800
    monkeypatch.setenv("TOTALRECLAW_SESSION_IDLE_MINUTES", "nonsense")
    assert _session_idle_seconds() == 3600  # falls back to default


# ── the hook path rolls the session id ────────────────────────────────
def test_maybe_roll_rotates_session_id(monkeypatch):
    s = AgentState()
    # force the cheap unconfigured branch — no network / real vault
    monkeypatch.setattr(s, "is_configured", lambda: False)
    first = s.start_session()
    _advance_clock(monkeypatch, s._last_activity, 10_000)
    hooks._maybe_roll_idle_session(s)
    assert s.session_id is not None
    assert s.session_id != first, "an idle turn must start a fresh session id"
    assert s.turn_count == 0


def test_maybe_roll_noop_when_fresh(monkeypatch):
    s = AgentState()
    monkeypatch.setattr(s, "is_configured", lambda: False)
    first = s.start_session()  # just started → not idle
    hooks._maybe_roll_idle_session(s)
    assert s.session_id == first, "a fresh session must not roll"
