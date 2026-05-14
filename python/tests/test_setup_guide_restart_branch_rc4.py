"""2.3.7rc4 — restart-branch table in ``docs/guides/hermes-setup.md``.

Context — Pedro 2026-05-14 QA on 2.3.7rc3
------------------------------------------

The previous restart instruction (rc.7+) emitted a single line:

    > Send `/restart` in chat now. Reply `done` once you see
    > `Gateway restarted successfully`.

That works in messaging-platform surfaces (Telegram, Discord, Slack,
Matrix, Feishu, WhatsApp) where the gateway intercepts the slash
command BEFORE the agent sees it. It does NOT work in ``hermes
chat`` (CLI/TUI) — the ``/restart`` string flows through to the LLM
as plain text, the agent has no tool to execute it, and the chat
session can't restart the gateway it's living inside. In docker,
even ``hermes gateway restart`` fails because there's no
systemd/launchd service supervisor to respawn the process after a
SIGUSR1 exit.

rc4 fixes the instruction with:

1. A probe + per-surface emit matrix. Messaging adapters → ``/restart``
   in chat. Docker CLI → ``docker restart <container>`` from host
   shell. Native CLI → ``hermes gateway restart`` in a second
   terminal. Detection-fails → ask the user which surface they're on
   and branch on the answer.

2. Continuation-phrase reply instead of bare ``done``. After a
   gateway restart the agent's session memory is wiped; the rc3
   proactive setup nudge fires on the first turn of every
   unconfigured session, but for belt-and-suspenders the user is
   asked to reply ``"Done — continue setting up TotalReclaw"`` (or
   equivalent resumption language) so the agent's next turn has both
   a system-injected nudge AND an unambiguous user message.

3. Diagnostics entries documenting the failure modes the user might
   hit if they ignore (or don't read) the per-surface emit.

These tests pin the rc4 contract so a future refactor can't silently
drop the multi-surface branch.
"""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE_MD = _REPO_ROOT / "docs" / "guides" / "hermes-setup.md"


def _read_guide() -> str:
    assert GUIDE_MD.exists(), f"hermes-setup.md not found at {GUIDE_MD}"
    return GUIDE_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Section 1 — Surface detection probe
# ---------------------------------------------------------------------------


def test_guide_has_surface_detection_probe():
    """The Install procedure step 4 must include a small shell probe
    the agent runs to detect the user's surface before emitting the
    restart instruction. Without the probe the agent emits the
    messaging-only ``/restart`` line for every user.
    """
    body = _read_guide()
    # Indicators that a detection probe exists.
    assert "IN_DOCKER=" in body, (
        "hermes-setup.md must include a docker-cgroup probe so the "
        "agent can detect when it's running inside a container."
    )
    assert "HAS_MSG_BOT" in body or "HAS_TELEGRAM" in body, (
        "Probe must check for messaging-bot env vars so the agent can "
        "detect when ``/restart`` would actually be intercepted by a "
        "gateway adapter."
    )


# ---------------------------------------------------------------------------
# Section 2 — Per-surface emit matrix
# ---------------------------------------------------------------------------


def test_guide_lists_all_messaging_platforms():
    """rc4 extends the table to every messaging platform Hermes
    supports. Per ``gateway/platforms/`` in upstream Hermes: Telegram,
    Discord, Slack, Matrix, Feishu, WhatsApp. All six must appear in
    the restart-branch table so the agent doesn't gate on a hardcoded
    Telegram/Discord-only check.
    """
    body = _read_guide()
    for platform in ("Telegram", "Discord", "Slack", "Matrix", "Feishu", "WhatsApp"):
        assert platform in body, (
            f"hermes-setup.md restart matrix must list {platform!r} "
            f"as a messaging platform that intercepts ``/restart``. "
            f"Without it the agent will emit the wrong restart "
            f"instruction for {platform} users."
        )


def test_guide_documents_docker_restart_branch():
    """When the agent detects it's running inside docker AND there's
    no messaging-bot env, it must emit a ``docker restart
    <container>`` instruction (run from the host shell, NOT from
    inside the container) — that's the only restart path that works
    without a service supervisor inside the container."""
    body = _read_guide()
    assert "docker restart" in body, (
        "hermes-setup.md must reference ``docker restart <container>`` "
        "as the restart path for docker-CLI users."
    )
    # Must explicitly note the command runs from the host (a common
    # mistake is to run docker restart from inside the container).
    assert "host shell" in body or "outside the container" in body, (
        "hermes-setup.md must clarify that ``docker restart`` runs "
        "from the HOST shell, not from inside the container — "
        "otherwise users try ``docker restart`` from inside and hit "
        "permission errors."
    )


def test_guide_documents_native_cli_restart_branch():
    """When the agent detects native install (no docker, has
    systemctl/launchctl, no messaging bot), it must emit a
    ``hermes gateway restart`` instruction in a second terminal."""
    body = _read_guide()
    assert "hermes gateway restart" in body, (
        "hermes-setup.md must reference ``hermes gateway restart`` "
        "as the restart path for native CLI users (where a "
        "systemd/launchd supervisor respawns the gateway)."
    )


def test_guide_documents_detection_failure_fallback():
    """When the detection probe is inconclusive, the agent must ask
    the user which surface they're on (not silently pick a default,
    not loop on ``/restart``). The ask must enumerate the same set
    of surfaces as the matrix."""
    body = _read_guide()
    # Existence of an explicit fallback path.
    assert "Ask the user" in body or "ask the user how" in body, (
        "hermes-setup.md must instruct the agent to ASK the user "
        "when surface detection fails — silent default-picking is "
        "worse than asking."
    )


# ---------------------------------------------------------------------------
# Section 3 — Continuation-phrase reply (replaces bare ``done``)
# ---------------------------------------------------------------------------


def test_guide_uses_continuation_phrase_instead_of_bare_done():
    """rc4 replaces ``Reply done`` with a continuation-phrase reply
    (``Done — continue setting up TotalReclaw`` or equivalent
    resumption language). Combined with the rc3 proactive setup
    nudge, this maximises the chance that the agent picks up
    mid-setup after a gateway restart wipes chat history.

    The exact phrase ``Continue setting up TotalReclaw`` (case-
    insensitive) MUST appear in the user-visible emit so the user
    has a verbatim suggestion to type."""
    body = _read_guide().lower()
    assert "continue setting up totalreclaw" in body, (
        "hermes-setup.md must include the literal ``Continue setting "
        "up TotalReclaw`` resumption phrase as the suggested reply "
        "after restart. Bare ``done`` is insufficient post-restart — "
        "the agent's session memory is wiped + needs an unambiguous "
        "trigger to call totalreclaw_pair."
    )


def test_guide_still_references_rc3_nudge_safety_net():
    """The continuation phrase pairs with the rc3 proactive setup
    nudge that fires on the first turn of every unconfigured session.
    The guide must surface this so the agent doesn't panic when the
    user replies with something other than the verbatim phrase —
    the nudge will still fire."""
    body = _read_guide()
    # Reference to the proactive nudge / unconfigured-session
    # auto-prompt mechanism.
    assert "proactive setup nudge" in body or "proactive" in body and "unconfigured" in body, (
        "hermes-setup.md must mention the rc3 proactive setup nudge "
        "so the agent (and human readers) know the nudge is the "
        "primary belt; the continuation phrase is just the secondary "
        "suspenders."
    )


# ---------------------------------------------------------------------------
# Section 4 — Diagnostics for the failure modes
# ---------------------------------------------------------------------------


def test_diagnostics_documents_cli_cant_do_that_failure():
    """The Diagnostics section must document the symptom Pedro hit on
    2026-05-14: agent in ``hermes chat`` (CLI) refuses to execute
    ``/restart`` because it doesn't have that tool. The fix is to
    switch to the per-surface restart command from step 4."""
    body = _read_guide().lower()
    # Phrase shape — flexible match.
    assert "can't do that here" in body or "i'm not able to" in body or "cli/tui" in body, (
        "Diagnostics must document the 'agent says /restart can't do "
        "that here in CLI/TUI' failure mode that Pedro hit 2026-05-14. "
        "Without this entry, future users + their agents won't know "
        "what to do when /restart silently no-ops in hermes chat."
    )


def test_diagnostics_documents_docker_supervisor_failure():
    """``hermes gateway restart`` from INSIDE a docker container
    fails because the container has no systemd/launchd service
    supervisor to respawn the gateway after SIGUSR1 exit. The
    Diagnostics section must document this so users don't loop on
    ``hermes gateway restart`` inside docker."""
    body = _read_guide()
    # Must reference the supervisor / respawn concept.
    assert "service supervisor" in body or "systemd" in body or "launchd" in body, (
        "Diagnostics must explain why ``hermes gateway restart`` "
        "fails inside docker (no service supervisor to respawn)."
    )


# ---------------------------------------------------------------------------
# Section 5 — Backwards-compat: existing rc.7 user-issued model preserved
# ---------------------------------------------------------------------------


def test_guide_still_requires_user_issued_restart_not_agent_issued():
    """rc.7 invariant — agent does NOT issue ``/restart`` itself.
    rc4 extends the surface coverage but must preserve the rc.7
    user-issued model. Otherwise the rc.7 double-restart 502 bug
    (sidecar killed by overlapping restarts) re-surfaces."""
    body = _read_guide()
    assert "agent does NOT issue" in body or "Do NOT issue" in body, (
        "hermes-setup.md must preserve the rc.7 invariant: agent "
        "does NOT issue ``/restart`` itself — the user does. rc.7 "
        "fixed a double-restart-kills-pair-sidecar 502 bug; rc4 "
        "must not regress that."
    )
