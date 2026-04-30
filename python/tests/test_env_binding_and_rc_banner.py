"""Regression tests for the 2.3.3-rc.1 environment-binding rule and the
RC/staging session-start banner (PR #165 / hermes 2.3.3-rc.1 ship-fix).

Why this file exists
====================

PR #165 codified a hard invariant:

* RC artifacts default to ``https://api-staging.totalreclaw.xyz``.
* Stable artifacts default to ``https://api.totalreclaw.xyz``.

For the Python client this is implemented as:

1. **Single canonical default-URL site** in ``totalreclaw.relay`` —
   ``_HARDCODED_DEFAULT_URL``. Source-of-truth on ``main`` carries the
   staging URL. The publish workflow rewrites that ONE line to the
   production URL when ``release-type=stable`` before
   ``python -m build``.
2. **Pre-publish CI guard** that fails the workflow if the built wheel
   contains the wrong literal for the requested release-type.
3. **Runtime banner**: when an RC wheel is installed AND the user has
   not overridden via ``TOTALRECLAW_SERVER_URL``, the Hermes plugin
   emits a one-shot session-start banner so the user is never surprised
   to find their data in staging.

These tests pin all three behaviors. Skipping any of them risks repeating
the QA regression that prompted PR #165 — every artifact silently
defaulting to staging across both stable + RC for weeks.

The tests are deliberately defensive: they read the literal source code
in ``relay.py`` (not the resolved value) so that even if a future
refactor moves the constant under an indirection layer the test still
fails loudly when the canonical site disappears.
"""
from __future__ import annotations

import importlib
import logging
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Section 1 — Default URL constant: single canonical site
# ---------------------------------------------------------------------------


_RELAY_PY = Path(__file__).resolve().parents[1] / "src" / "totalreclaw" / "relay.py"


def test_canonical_default_url_constant_lives_in_relay_module():
    """``_HARDCODED_DEFAULT_URL`` must exist in ``totalreclaw.relay``.

    The publish workflow's ``sed`` rewrite targets this exact file +
    constant name. Renaming, moving, or losing the constant silently
    breaks the build-time injection — and the production-vs-staging
    bake-in regresses.
    """
    text = _RELAY_PY.read_text()
    assert "_HARDCODED_DEFAULT_URL" in text, (
        "Canonical default-URL constant `_HARDCODED_DEFAULT_URL` not found "
        "in relay.py. The publish workflow's sed rewrite assumes this name "
        "and this file. Update both together if you rename it."
    )


def test_canonical_default_url_is_staging_in_source():
    """On-tree default must be staging — workflow rewrites for stable.

    This is the "source-of-truth on main is staging" half of the
    environment-binding rule. RC builds skip the rewrite entirely and
    publish whatever lives in source; stable builds rewrite to
    production. If the on-tree literal accidentally becomes production,
    RC artifacts would point QA at production and burn real test data
    into production indexes.
    """
    text = _RELAY_PY.read_text()
    match = re.search(
        r'^_HARDCODED_DEFAULT_URL\s*=\s*"([^"]+)"',
        text,
        flags=re.MULTILINE,
    )
    assert match is not None, (
        "Could not parse the `_HARDCODED_DEFAULT_URL = \"...\"` line in "
        "relay.py. The workflow regex assumes this exact assignment shape."
    )
    url = match.group(1)
    assert url == "https://api-staging.totalreclaw.xyz", (
        f"Source-of-truth default URL is {url!r}, expected "
        "https://api-staging.totalreclaw.xyz. The publish-python-client.yml "
        "workflow rewrites this line for stable builds; if the on-tree "
        "literal drifts away from staging, RC artifacts will silently "
        "publish with the wrong default."
    )


def test_no_other_hardcoded_default_url_sites():
    """Other modules MUST NOT bake their own default URL.

    Historically ``agent/state.py`` and ``cli.py`` each had their own
    ``"https://api.totalreclaw.xyz"`` literal as a fallback. That left
    three independent sources of truth and made the build-time injection
    rule impossible to enforce: a stable build could rewrite ``relay.py``
    correctly and still publish a wheel where ``state.py`` defaulted to
    production for unconfigured agents.

    This test scans every Python source file under ``src/totalreclaw``
    for hardcoded ``totalreclaw.xyz`` URLs that aren't pure docstring /
    comment references AND don't sit on the canonical line. Catches
    drift before it reaches the wheel.
    """
    src_root = Path(__file__).resolve().parents[1] / "src" / "totalreclaw"
    bad_lines: list[str] = []
    # Allow this file (test infra) and anything under tests/. Allow the
    # canonical site itself. Allow the pair WS default URL — it's a
    # different host class (wss://) governed by its own env var
    # ``TOTALRECLAW_PAIR_RELAY_URL``; PR #165 explicitly does not change
    # the pair-flow default in 2.3.3-rc.1 (out of scope, separate
    # follow-up).
    canonical_marker = "_HARDCODED_DEFAULT_URL"
    pair_default_marker = 'DEFAULT_RELAY_URL = "wss://api-staging.totalreclaw.xyz"'
    for py in src_root.rglob("*.py"):
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            if "totalreclaw.xyz" not in line:
                continue
            stripped = line.strip()
            # Skip docstring / comment lines that just MENTION the URL.
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
                continue
            # Skip the canonical site itself.
            if canonical_marker in line:
                continue
            # Skip the pair flow's separate WS default URL.
            if pair_default_marker in line:
                continue
            # Skip ``relay_url=`` usages that just forward an already-
            # resolved URL (param wiring, not a default literal).
            if "relay_url=" in line and "https://api" not in line:
                continue
            # Anything else with a bare http(s)://api{,-staging}.totalreclaw.xyz
            # literal in code is a drift candidate.
            if re.search(r'"https?://api[a-z\-]*\.totalreclaw\.xyz', line):
                bad_lines.append(f"{py.relative_to(src_root)}:{lineno}: {stripped}")
    assert not bad_lines, (
        "Found hardcoded URL literals outside the canonical "
        "`_HARDCODED_DEFAULT_URL` site:\n"
        + "\n".join(bad_lines)
        + "\n\nConsolidate to `totalreclaw.relay._default_relay_url()` so "
        "the build-time injection rule has a single rewrite target."
    )


# ---------------------------------------------------------------------------
# Section 2 — Default resolver respects env override
# ---------------------------------------------------------------------------


def test_default_relay_url_resolves_to_hardcoded_when_env_unset(monkeypatch):
    """Without ``TOTALRECLAW_SERVER_URL`` the resolver returns the bake-in.

    RC wheels: ``_HARDCODED_DEFAULT_URL == staging`` -> resolver returns
    staging. Stable wheels (post build-time rewrite):
    ``_HARDCODED_DEFAULT_URL == production`` -> resolver returns
    production. The test asserts the contract — whichever literal lives
    in the module at runtime is what the resolver hands back.
    """
    monkeypatch.delenv("TOTALRECLAW_SERVER_URL", raising=False)
    from totalreclaw import relay

    importlib.reload(relay)
    assert relay._default_relay_url() == relay._HARDCODED_DEFAULT_URL


def test_default_relay_url_env_override_wins(monkeypatch):
    """User env override beats the build-time default in every release.

    This is the "user env always wins" invariant from PR #165. Even
    after a stable wheel bakes production into ``_HARDCODED_DEFAULT_URL``,
    a user (or QA harness) that exports ``TOTALRECLAW_SERVER_URL=...``
    must hit that URL — never the bake-in.
    """
    monkeypatch.setenv("TOTALRECLAW_SERVER_URL", "https://example.invalid")
    from totalreclaw import relay

    importlib.reload(relay)
    assert relay._default_relay_url() == "https://example.invalid"


# ---------------------------------------------------------------------------
# Section 3 — RC/staging session-start banner
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_state():
    """Build a vanilla ``AgentState`` with the eager-configure path
    short-circuited so banner tests don't depend on credentials/relay
    network calls. The banner check fires BEFORE the configured-only
    return in ``on_session_start`` — that's the whole point of running
    it for unconfigured fresh installs.
    """
    from totalreclaw.agent.state import AgentState

    s = AgentState.__new__(AgentState)  # bypass __init__
    s._client = None
    s._turn_count = 0
    s._messages = []
    s._last_processed_idx = 0
    s._billing_cache = None
    s._billing_cache_time = 0.0
    s._extraction_interval = 3
    s._max_facts = 15
    s._min_importance = 6
    s._quota_warning = None
    s._server_url = None
    s._env_interval_override = False
    s._env_importance_override = False
    return s


def _patch_default_url(monkeypatch, value: str) -> None:
    """Force ``_HARDCODED_DEFAULT_URL`` so tests can simulate RC vs
    stable wheels without actually rebuilding. The hooks module captures
    the constant at import time, so we patch BOTH locations."""
    from totalreclaw import relay
    from totalreclaw.hermes import hooks

    monkeypatch.setattr(relay, "_HARDCODED_DEFAULT_URL", value)
    monkeypatch.setattr(hooks, "_HARDCODED_DEFAULT_URL", value)


def test_banner_emitted_when_rc_default_is_staging_and_env_unset(
    monkeypatch, fresh_state
):
    """RC wheel + no env override -> banner queued on session start.

    The banner uses the existing ``set_quota_warning`` channel so the
    next ``pre_llm_call`` injects it as ``context``. We assert (a) the
    quota warning is now set, (b) the message contains the canonical
    warning string, and (c) the latch attribute was set to gate
    subsequent sessions.
    """
    monkeypatch.delenv("TOTALRECLAW_SERVER_URL", raising=False)
    _patch_default_url(monkeypatch, "https://api-staging.totalreclaw.xyz")

    from totalreclaw.hermes import hooks

    hooks.on_session_start(fresh_state, session_id="s1")

    warning = fresh_state.get_quota_warning()
    assert warning is not None, (
        "RC wheel default points at staging and no env override is set, "
        "but on_session_start did not queue the RC/staging banner. Users "
        "would silently store data in staging without warning."
    )
    assert "RC/staging" in warning
    assert "api-staging.totalreclaw.xyz" in warning
    assert "no SLA" in warning
    assert "pip install totalreclaw" in warning
    assert getattr(fresh_state, "_totalreclaw_rc_banner_shown", False) is True


def test_banner_not_emitted_when_default_is_production(
    monkeypatch, fresh_state
):
    """Stable wheel (production default) -> banner stays silent.

    Real users on stable builds must NEVER see this warning — it would
    look like a setup bug from their side. Asserting the negative path
    keeps the banner from leaking into stable installs if the bake-in
    goes wrong.
    """
    monkeypatch.delenv("TOTALRECLAW_SERVER_URL", raising=False)
    _patch_default_url(monkeypatch, "https://api.totalreclaw.xyz")

    from totalreclaw.hermes import hooks

    hooks.on_session_start(fresh_state, session_id="s1")

    assert fresh_state.get_quota_warning() is None
    assert getattr(fresh_state, "_totalreclaw_rc_banner_shown", False) is False


def test_banner_not_emitted_when_env_override_set(monkeypatch, fresh_state):
    """RC wheel + env override -> banner stays silent.

    A maintainer who explicitly pinned ``TOTALRECLAW_SERVER_URL`` to
    staging (or anywhere else) has already opted in knowingly. Spamming
    the banner on top of an explicit override is noise, not signal.
    """
    monkeypatch.setenv(
        "TOTALRECLAW_SERVER_URL", "https://api-staging.totalreclaw.xyz"
    )
    _patch_default_url(monkeypatch, "https://api-staging.totalreclaw.xyz")

    from totalreclaw.hermes import hooks

    hooks.on_session_start(fresh_state, session_id="s1")

    assert fresh_state.get_quota_warning() is None
    assert getattr(fresh_state, "_totalreclaw_rc_banner_shown", False) is False


def test_banner_one_shot_per_state_instance(monkeypatch, fresh_state):
    """Banner fires exactly once per ``PluginState`` lifetime.

    Hermes daemon mode keeps the same plugin singleton across many
    sessions in one process. The banner is a "first launch" signal —
    re-emitting on every ``on_session_start`` would be spam, and the
    LLM would either start ignoring it or surface confusing repeat
    warnings to the user. Latch lives on the state instance for the
    life of the process.
    """
    monkeypatch.delenv("TOTALRECLAW_SERVER_URL", raising=False)
    _patch_default_url(monkeypatch, "https://api-staging.totalreclaw.xyz")

    from totalreclaw.hermes import hooks

    hooks.on_session_start(fresh_state, session_id="s1")
    first_warning = fresh_state.get_quota_warning()
    assert first_warning is not None  # sanity

    # Simulate a downstream consumer (pre_llm_call) clearing the warning
    # after surfacing it once — same path the real plugin runs.
    fresh_state.clear_quota_warning()

    hooks.on_session_start(fresh_state, session_id="s2")
    second_warning = fresh_state.get_quota_warning()
    assert second_warning is None, (
        "Banner re-emitted on the second on_session_start call. The "
        "_totalreclaw_rc_banner_shown latch must keep this one-shot "
        "across same-process sessions."
    )
