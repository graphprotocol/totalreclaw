"""2.3.4-rc.1 SKILL.md hardening pins.

Why this file exists
====================

Plugin-side QA against 3.3.3-rc.1 on 2026-04-30 surfaced three
skill-instruction gaps:

1. The agent (zai/glm-5-turbo) asked the user *"Should I /restart?"*
   instead of issuing the slash command autonomously — PR #162's
   restart-step prose was too soft for a less-capable model to follow
   reliably.
2. When `/restart` returned *"You are not authorized to use this
   command"* (the OpenClaw-side `tier: power` Telegram `allowFrom`
   auth gate), the agent didn't know how to recover and looped.
3. A gateway-config-driven SIGUSR1 reload can race a plugin install:
   if the Python package isn't on disk by the time the manifest
   registers, the gateway binds the manifest with no implementations.

Hermes mirrors the same skill instructions, so the same gaps apply.
2.3.4-rc.1 hardens both ``SKILL.md`` and ``docs/guides/hermes-setup.md``
with:

* A forbidden-vocabulary deny-list at the restart step ("Should I
  /restart?" etc.) — the agent must act autonomously and announce.
* An ``/restart`` unauthorized fallback chain: try ``/new`` once (fresh
  session may pick up freshly-bound tools), then escalate to a
  single-line user-prompted restart. Do NOT loop on ``/restart``.
* Install order: Python package FIRST, plugin manifest SECOND. The
  manifest registration is the reload-trigger; landing the Python
  package first means the implementations are present when the reload
  fires.

This test file pins those pieces against silent regressions in future
SKILL.md / guide refactors. The assertions are deliberately literal —
they compare against the exact phrases the plan asked for so that a
contributor reading the test sees the contract.

Companion to ``test_skill_md_includes_disable_memory_step.py`` (rc.26
disable-memory step) and ``test_setup_guide_includes_compatibility_section.py``
(rc.26 user-guide compatibility section). Together those three test
files cover the load-bearing prose in the install + setup flow.
"""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = (
    _REPO_ROOT / "python" / "src" / "totalreclaw" / "hermes" / "SKILL.md"
)
USER_GUIDE = _REPO_ROOT / "docs" / "guides" / "hermes-setup.md"


def _read(path: Path) -> str:
    assert path.exists(), f"{path} not found — has it been moved or renamed?"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Section 1 — Forbidden-vocabulary deny-list at the restart step
# ---------------------------------------------------------------------------


def test_skill_md_has_forbidden_phrases_section_for_restart():
    """SKILL.md must include a deny-list section that spells out the
    anti-pattern phrases the agent is forbidden from writing in chat
    when tools fail to bind post-install. Without the explicit list,
    a soft "issue autonomously" instruction is not enough — plugin-side
    QA on 2026-04-30 showed less-capable models drafting "Should I
    /restart?" despite the existing prose.
    """
    body = _read(SKILL_MD)
    assert "Forbidden phrases when tools fail to bind" in body, (
        "SKILL.md must contain a 'Forbidden phrases when tools fail to "
        "bind' deny-list section. The 2.3.4-rc.1 hardening codifies "
        "the deny-list explicitly so soft 'issue autonomously' prose "
        "isn't the only signal to the agent."
    )


def test_skill_md_denies_should_i_restart_phrasing():
    """The exact 'Should I /restart?' phrase must be in the deny-list.

    Plugin-side QA on 2026-04-30 caught zai/glm-5-turbo writing exactly
    this line when tools didn't bind after install. The fix is to make
    the phrase a literal item in the deny-list so the model has a
    direct match-and-avoid signal.
    """
    body = _read(SKILL_MD)
    assert '"Should I /restart' in body, (
        'SKILL.md deny-list must include the literal "Should I /restart" '
        "phrase — that is the anti-pattern the 2.3.4-rc.1 fix was "
        "scoped to eliminate."
    )


def test_skill_md_denies_url_question_phrasing():
    """'Do you have a public URL?' must be in the deny-list.

    The install is local; there is no URL the agent should be asking
    the user for at restart-time. This question came up in the same
    QA run and indicates the model has lost the thread of what step
    it's on.
    """
    body = _read(SKILL_MD)
    assert '"Do you have a public URL' in body, (
        'SKILL.md deny-list must include "Do you have a public URL" — '
        "agents that drift to that phrase have lost track of the install "
        "step they are on and need a direct match-and-avoid signal."
    )


def test_skill_md_denies_let_me_check_phrasing():
    """'Let me check if the tool is bound.' must be in the deny-list.

    Verification of tool-binding is a silent shell call (the existing
    silence rule), not a chat line. Narrating it breaks the silence
    rule and adds noise.
    """
    body = _read(SKILL_MD)
    assert '"Let me check if the tool is bound' in body, (
        'SKILL.md deny-list must include "Let me check if the tool is '
        'bound" — verification is silent, not a chat line.'
    )


# ---------------------------------------------------------------------------
# Section 2 — `/restart` unauthorized fallback (try `/new`, then user)
# ---------------------------------------------------------------------------


def test_skill_md_documents_restart_unauthorized_fallback():
    """SKILL.md must describe what to do when `/restart` returns
    'not authorized'. Without this, the agent loops on `/restart`
    forever — exactly what plugin-side QA caught.
    """
    body = _read(SKILL_MD)
    assert "not authorized" in body, (
        "SKILL.md must describe the `/restart` 'not authorized' "
        "failure mode — managed Hermes deployments that gate "
        "session-scope slash commands behind RBAC return this string "
        "and the agent must know how to recover."
    )
    assert "do NOT loop on `/restart`" in body or "Do NOT keep retrying `/restart`" in body, (
        "SKILL.md must explicitly forbid looping on `/restart` after "
        "an unauthorized response — the gate isn't going to flip "
        "mid-session and the loop just spams the user."
    )


def test_skill_md_uses_slash_new_as_first_unauthorized_fallback():
    """When `/restart` is gated, the agent should try `/new` once
    before asking the user to restart. `/new` opens a fresh session
    in the same gateway, which may pick up freshly-bound tools without
    requiring a full reload — cheaper than a user-prompted restart.
    """
    body = _read(SKILL_MD)
    assert "/new" in body, (
        "SKILL.md must mention the `/new` slash command as the next "
        "fallback after `/restart` returns unauthorized. `/new` opens "
        "a fresh session in the same gateway and may pick up freshly-"
        "bound tools without requiring a full reload."
    )


def test_skill_md_user_prompted_restart_is_last_resort():
    """The unauthorized chain must end at a single-line user-prompted
    restart message, NOT at another `/restart` retry. The whole point
    is to break out of the slash-command path when the gate is closed.
    """
    body = _read(SKILL_MD)
    # The user-prompted fallback must reference at least one external
    # restart command so the user has actionable guidance.
    assert "hermes gateway restart" in body
    assert "docker restart" in body


# ---------------------------------------------------------------------------
# Section 3 — Step 4 retry chain matches Step 2 (no "should I /restart again?")
# ---------------------------------------------------------------------------


def test_skill_md_step_4_retry_is_autonomous():
    """Step 4 (verify-bound) must re-issue `/restart` autonomously —
    the same autonomy contract Step 2 has. The 2.3.3-rc.1 prose said
    "re-issue /restart once" but did not explicitly forbid the
    "should I?" prefix; some models added it anyway.
    """
    body = _read(SKILL_MD)
    # Locate Step 4 ("Verify ``totalreclaw_pair`` is bound") prose.
    assert "re-issue `/restart` once **autonomously**" in body, (
        "Step 4 must say `/restart` is re-issued autonomously — match "
        "Step 2's autonomy contract so the deny-list applies at "
        "verify-bound retry time too."
    )


# ---------------------------------------------------------------------------
# Section 4 — Install ordering: Python package FIRST, manifest SECOND
# ---------------------------------------------------------------------------


def test_skill_md_install_order_python_first_then_manifest():
    """SKILL.md Step 1b must show the pip install line BEFORE the
    `hermes plugins install` line. The reverse order races a
    config-driven SIGUSR1 reload — the manifest registers, the reload
    fires, the gateway finds no implementations, binding fails.
    """
    body = _read(SKILL_MD)
    pip_idx = body.find('"$HERMES_PYTHON" -m pip install --pre totalreclaw')
    plugin_idx = body.find("hermes plugins install p-diogo/totalreclaw-hermes")
    assert pip_idx > 0, (
        "SKILL.md must contain the canonical `$HERMES_PYTHON -m pip "
        "install --pre totalreclaw` line in Step 1b."
    )
    assert plugin_idx > 0, (
        "SKILL.md must contain the canonical `hermes plugins install "
        "p-diogo/totalreclaw-hermes` line in Step 1b."
    )
    assert pip_idx < plugin_idx, (
        "SKILL.md Step 1b ordering regressed: pip install must precede "
        "`hermes plugins install` so the Python package is on disk "
        "before the manifest registration triggers a reload. See the "
        "2.3.4-rc.1 install-race finding from plugin-side QA."
    )


def test_skill_md_install_order_has_explanatory_comment():
    """The order isn't obvious — without an inline comment a future
    refactor will swap it back to plugin-then-pip. Pin the comment.
    """
    body = _read(SKILL_MD)
    assert "Order matters" in body, (
        "SKILL.md must explain why pip install precedes `hermes "
        "plugins install` — without the comment a future refactor "
        "will reorder them and silently re-introduce the install race."
    )
    assert "SIGUSR1" in body, (
        "SKILL.md ordering comment must reference SIGUSR1 / config-"
        "driven reload — that is the specific failure mode and naming "
        "it makes the comment self-explanatory to a future contributor."
    )


# ---------------------------------------------------------------------------
# Section 5 — User-guide mirror: same hardening lands in hermes-setup.md
# ---------------------------------------------------------------------------


def test_user_guide_mirrors_unauthorized_fallback_prose():
    """`docs/guides/hermes-setup.md` must mirror the SKILL.md
    unauthorized fallback so a user reading the guide learns the same
    contract. Drift between SKILL.md (agent-facing) and the user
    guide (human-facing) was a recurring rc.x finding pre-rc.20.
    """
    body = _read(USER_GUIDE)
    assert "not authorized" in body, (
        "hermes-setup.md must describe the `/restart` 'not authorized' "
        "failure mode and recovery path so users on managed Hermes "
        "deployments understand what they are seeing."
    )
    assert "/new" in body, (
        "hermes-setup.md must reference `/new` as the unauthorized "
        "fallback hop, mirroring SKILL.md."
    )


def test_user_guide_mirrors_install_ordering():
    """The pip-then-manifest order must also be in the user guide so
    a user copy-pasting from the manual section doesn't race the
    reload. Drift between SKILL.md and the user guide is a known
    failure mode.

    The check is scoped to the Step 1b code block (the canonical
    copy-paste target), NOT the agent-instructions overview list at
    the top of the guide which simply describes what each command
    does and is not a copy-paste source.
    """
    body = _read(USER_GUIDE)
    # Locate the Step 1b code block — that's the user-facing canonical
    # copy-paste target; the agent-instructions overview list at the
    # top of the guide just describes commands and is not the source
    # of truth for ordering.
    step_1b_marker = "#### Step 1b"
    step_1b_idx = body.find(step_1b_marker)
    assert step_1b_idx > 0, (
        "hermes-setup.md must contain a 'Step 1b' subsection with the "
        "canonical install commands. Search for '#### Step 1b' returned "
        "no match — has the section heading been renamed?"
    )
    # Also bound the search at the next top-level step heading so we
    # don't accidentally pick up the manual-CLI block lower in the doc.
    step_1c_idx = body.find("#### Step 1c", step_1b_idx)
    step_1b_block = (
        body[step_1b_idx:step_1c_idx] if step_1c_idx > 0 else body[step_1b_idx:]
    )

    pip_idx = step_1b_block.find('"$HERMES_PYTHON" -m pip install --pre totalreclaw')
    plugin_idx = step_1b_block.find("hermes plugins install p-diogo/totalreclaw-hermes")
    assert pip_idx > 0 and plugin_idx > 0, (
        "hermes-setup.md Step 1b must contain both the pip install and "
        "the `hermes plugins install` canonical lines."
    )
    assert pip_idx < plugin_idx, (
        "hermes-setup.md Step 1b ordering regressed: pip install must "
        "precede `hermes plugins install`. See the 2.3.4-rc.1 install-"
        "race finding from plugin-side QA."
    )

    # Also pin ordering in the 'Fully manual (CLI only)' section — that
    # block is the other copy-paste target and the original 2.3.3 prose
    # had it in plugin-then-pip order. The 2.3.4-rc.1 fix re-ordered it.
    manual_marker = "## Fully manual (CLI only"
    manual_idx = body.find(manual_marker)
    assert manual_idx > 0, (
        "hermes-setup.md must retain the 'Fully manual (CLI only)' "
        "section — that is the second copy-paste target users hit."
    )
    # Bound the search so we don't run off into Upgrading / Targeting.
    next_section_idx = body.find("\n---\n", manual_idx)
    manual_block = (
        body[manual_idx:next_section_idx] if next_section_idx > 0 else body[manual_idx:]
    )
    manual_pip = manual_block.find("pip install --pre totalreclaw")
    manual_plugin = manual_block.find("hermes plugins install p-diogo/totalreclaw-hermes")
    assert manual_pip > 0 and manual_plugin > 0
    assert manual_pip < manual_plugin, (
        "'Fully manual (CLI only)' section must show pip install BEFORE "
        "`hermes plugins install` — same install-race rationale as "
        "Step 1b."
    )


def test_user_guide_calls_out_order_change_with_version_marker():
    """The order change is part of 2.3.4-rc.1 hardening — the marker
    helps a user reading the guide trace why the order is what it is
    if they remember a different order from earlier RCs."""
    body = _read(USER_GUIDE)
    assert "2.3.4-rc.1" in body, (
        "hermes-setup.md must mention '2.3.4-rc.1' as the version "
        "anchor for the install-order change so a user with stale "
        "memory of pre-rc.X order can trace why it changed."
    )


# ---------------------------------------------------------------------------
# Section 6 — Sanity: rc.26 disable-memory step is unchanged
# ---------------------------------------------------------------------------


def test_skill_md_still_disables_built_in_memory():
    """Defensive cross-check — the 2.3.4-rc.1 hardening MUST NOT
    delete the rc.26 disable-memory step. Belt-and-suspenders next to
    `test_skill_md_includes_disable_memory_step.py` because the
    edit-by-large-blocks pattern of this PR could have accidentally
    truncated the disable-memory section.
    """
    body = _read(SKILL_MD)
    assert "hermes tools disable memory" in body
    assert "Disable Hermes built-in memory" in body
    assert "(CRITICAL)" in body
