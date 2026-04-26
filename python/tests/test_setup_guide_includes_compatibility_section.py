"""rc.26: ``docs/guides/hermes-setup.md`` must include the disable-memory
agent-imperative step + a Compatibility section.

Context — rc.24 NO-GO finding (issue #167)
-------------------------------------------

The public guide at ``docs/guides/hermes-setup.md`` is the canonical
URL the user pastes into Hermes chat. The agent fetches it and follows
its agent-instructions block. rc.26 ships path A (auto-disable
built-in memory) and the doc MUST mirror SKILL.md so the agent does
the same thing whether it loaded the local skill or fetched the URL.

What this test enforces
-----------------------

The shipped ``hermes-setup.md`` MUST contain:

1. The ``hermes tools disable memory`` invocation in the agent-imperative
   block (Step 3 in rc.26).
2. The user-verbatim warning text mirroring SKILL.md.
3. A ``## Compatibility with Hermes built-in memory`` section near the
   bottom that documents dual-mode as unsupported.
"""
from __future__ import annotations

from pathlib import Path


GUIDE_MD = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "guides"
    / "hermes-setup.md"
)


def _read_guide() -> str:
    assert GUIDE_MD.exists(), f"hermes-setup.md not found at {GUIDE_MD}"
    return GUIDE_MD.read_text(encoding="utf-8")


def test_guide_includes_disable_memory_agent_step():
    """The agent-instructions block must include the disable-memory step."""
    body = _read_guide()
    # Step heading + invocation (CRITICAL marker per the rc.26 plan).
    assert "Disable Hermes built-in memory" in body, (
        "hermes-setup.md must include a step heading 'Disable Hermes "
        "built-in memory' in the agent-instructions block."
    )
    assert "(CRITICAL)" in body, (
        "Disable-memory step must be marked '(CRITICAL)' so the agent "
        "treats it as non-optional during install."
    )
    assert "hermes tools disable memory" in body, (
        "Agent-instructions block must invoke `hermes tools disable memory`."
    )


def test_guide_includes_user_verbatim_warning():
    """Mirrors SKILL.md user-verbatim text."""
    body = _read_guide()
    assert "disabled Hermes' built-in `memory` tool" in body, (
        "hermes-setup.md must mirror the SKILL.md user-verbatim warning."
    )
    assert "hermes tools enable memory" in body, (
        "User-verbatim warning must include the re-enable command + "
        "the NOT-recommended caveat."
    )
    assert "NOT recommended" in body, (
        "Re-enable instruction must be explicitly NOT recommended while "
        "TotalReclaw is installed."
    )
    assert "hermes memory reset" in body, (
        "User-verbatim warning must include the optional `hermes memory "
        "reset` pointer for wiping orphaned MEMORY.md / USER.md files."
    )


def test_guide_has_compatibility_section():
    """The bottom-of-doc Compatibility section explains dual-mode."""
    body = _read_guide()
    assert "## Compatibility with Hermes built-in memory" in body, (
        "hermes-setup.md must include a `## Compatibility with Hermes "
        "built-in memory` section near the bottom — this is the durable "
        "human-reader documentation of why dual-mode is unsupported."
    )
    # Compatibility content must explain the auto-disable + recommend
    # against re-enabling.
    assert "auto-disables" in body, (
        "Compatibility section must state that the install flow "
        "auto-disables Hermes built-in memory."
    )
    assert "DO NOT recommend re-enabling" in body, (
        "Compatibility section must explicitly say DO NOT recommend "
        "re-enabling while TotalReclaw is installed."
    )


def test_guide_compatibility_section_documents_switch_back_path():
    """If the user wants to switch back to Hermes built-in memory,
    the doc must show the canonical commands (enable + clear + uninstall)."""
    body = _read_guide()
    # Switch-back recipe — the canonical 3-step flow.
    assert "totalreclaw forget --all" in body, (
        "Compatibility section must document the optional "
        "`totalreclaw forget --all` command for users switching back "
        "to Hermes built-in memory."
    )
    assert "pip uninstall totalreclaw" in body, (
        "Compatibility section must document the optional "
        "`pip uninstall totalreclaw` command for users switching back."
    )


def test_guide_compatibility_section_points_users_to_issue_tracker():
    """Users who want dual-mode must be pointed at GitHub issues so we
    can scope demand without committing to support upfront."""
    body = _read_guide()
    # rc.26 plan asked for a link to the GitHub discussion / issue
    # tracker; we use the issues page.
    assert "github.com/p-diogo/totalreclaw/issues" in body, (
        "Compatibility section must link to the GitHub issue tracker so "
        "users wanting dual-mode can flag demand."
    )


def test_guide_post_install_steps_renumbered():
    """Inserting the disable-memory step at position 3 shifts the rest
    of the agent-instructions block. Verify the renumbering landed
    consistently — Step 4 is credentials, Step 5 is pair, Step 6 is
    verify-and-confirm."""
    body = _read_guide()
    assert "### Step 4 — Check for existing credentials" in body, (
        "Step 4 must be the credentials check (was Step 3 in rc.25)."
    )
    assert "### Step 5 — Pair" in body, (
        "Step 5 must be the pair flow (was Step 4 in rc.25)."
    )
    assert "### Step 6 — Verify and confirm" in body, (
        "Step 6 must be the verify-and-confirm flow (was Step 5 in rc.25)."
    )
