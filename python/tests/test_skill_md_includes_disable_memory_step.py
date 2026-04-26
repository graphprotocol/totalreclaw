"""rc.26: SKILL.md must include the ``hermes tools disable memory`` step.

Context — rc.24 NO-GO finding (issue #167)
-------------------------------------------

rc.24 chat-flow QA showed that Hermes' built-in ``memory`` tool steals
"remember X" / "recall X" intents from ``totalreclaw_remember`` during
natural conversation, causing memories to land in MEMORY.md instead of
TotalReclaw's encrypted vault. rc.25 shipped path B (tool-description
bias). rc.26 ships path A (hard-coded exclusivity): the install flow
auto-disables the built-in memory tool, and the dual-mode setup is
documented as unsupported.

What this test enforces
-----------------------

The shipped ``SKILL.md`` MUST contain:

1. The ``hermes tools disable memory`` invocation.
2. The user-verbatim warning text exactly as decided in the rc.26 plan
   (re-enable instructions + ``hermes memory reset`` pointer).

Test source: SKILL.md is the canonical spec for what the agent does at
install time. If a future refactor breaks this block, the rc.26 fix
silently regresses and we re-introduce the rc.24 bug.
"""
from __future__ import annotations

from pathlib import Path


SKILL_MD = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "totalreclaw"
    / "hermes"
    / "SKILL.md"
)


def _read_skill_md() -> str:
    assert SKILL_MD.exists(), f"SKILL.md not found at {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def test_skill_md_invokes_hermes_tools_disable_memory():
    """SKILL.md must instruct the agent to run ``hermes tools disable memory``."""
    body = _read_skill_md()
    assert "hermes tools disable memory" in body, (
        "SKILL.md must include the literal `hermes tools disable memory` "
        "command — the rc.26 fix for issue #167 (Hermes built-in memory "
        "steals memory intents from TotalReclaw)."
    )


def test_skill_md_marks_disable_step_critical():
    """The disable-memory step must be marked CRITICAL so install
    flows that skim the doc don't treat it as optional polish."""
    body = _read_skill_md()
    assert "Disable Hermes built-in memory" in body, (
        "SKILL.md must contain a step heading mentioning 'Disable Hermes "
        "built-in memory' so the agent doesn't skip past it."
    )
    # The plan asked for a CRITICAL marker on the step heading.
    assert "(CRITICAL)" in body, (
        "Disable-memory step must be marked '(CRITICAL)' in the heading so "
        "the agent treats it as non-optional during install."
    )


def test_skill_md_includes_user_verbatim_warning_text():
    """The user-verbatim block must include the re-enable + reset
    pointers exactly as the rc.26 plan dictates."""
    body = _read_skill_md()
    # Mention that built-in memory is disabled.
    assert "disabled Hermes' built-in `memory` tool" in body, (
        "SKILL.md must include the user-verbatim warning that TotalReclaw "
        "disabled the built-in memory tool."
    )
    # Re-enable instruction.
    assert "hermes tools enable memory" in body, (
        "SKILL.md must instruct the user how to re-enable Hermes built-in "
        "memory (and warn it's NOT recommended while TotalReclaw is "
        "installed)."
    )
    assert "NOT recommended" in body, (
        "User-verbatim warning must explicitly say re-enabling is NOT "
        "recommended while TotalReclaw is installed."
    )
    # Wipe-files pointer.
    assert "hermes memory reset" in body, (
        "SKILL.md must reference `hermes memory reset` as the optional "
        "wipe step for orphaned MEMORY.md / USER.md files."
    )


def test_skill_md_explains_why_dual_mode_is_anti_pattern():
    """The step must explain WHY built-in memory must be disabled —
    so a future refactor doesn't accidentally drop the reasoning + the
    instruction together."""
    body = _read_skill_md()
    # Match the explanatory clause about intent-stealing / silent bug.
    assert "competes" in body.lower() or "steal" in body.lower(), (
        "Disable-memory step must explain that the built-in memory tool "
        "competes / steals intents from TotalReclaw — preserves the why "
        "if the doc gets refactored."
    )
    # The "MEMORY.md instead of vault" failure mode must be called out
    # by name so a future contributor reading only the install flow
    # understands the data-loss surface.
    assert "MEMORY.md" in body, (
        "Disable-memory step must reference MEMORY.md by name so the "
        "failure mode (data lands in the wrong file) is explicit."
    )


def test_skill_md_keeps_post_install_steps_renumbered():
    """The disable-memory step inserted itself between restart-gateway
    and verify-tool-bound, so subsequent steps re-number. SKIP-install
    fast-path pointer must point at the credential-check step (now
    step 5), not step 4 as in rc.25."""
    body = _read_skill_md()
    assert "SKIP install and jump to step 5" in body, (
        "After inserting the disable-memory step, the fast-path pointer "
        "for already-installed plugins MUST jump to step 5 (credential "
        "check), not step 4 — otherwise the agent mis-routes for "
        "returning users."
    )
