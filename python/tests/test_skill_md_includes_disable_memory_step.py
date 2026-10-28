"""Disable-built-in-memory coverage — POST-CONSOLIDATION (2026-06-05).

History
=======
rc.26 (issue #167) shipped a `(CRITICAL)` "disable built-in memory" install
step in SKILL.md, on the theory that Hermes' built-in `memory` tool steals
"remember/recall" intents from TotalReclaw. The 2026-06-05 consolidation
(docs/plans/2026-06-04-hermes-skill-consolidation-design.md) moved install +
compatibility content into the canonical install doc `hermes-setup.md`, which
already carried the *accurate* stance — validated against Hermes upstream:

    Hermes built-in memory (USER.md / MEMORY.md) CANNOT be fully disabled. The
    built-in layer is always active; `hermes tools disable memory` only blocks
    the AGENT from calling the `memory` tool — the gateway keeps writing
    USER.md. So disabling is an OPTIONAL partial mitigation, not a `(CRITICAL)`
    on/off switch.

2026-10-28 (rc3 QA, #602): on Hermes 0.22+ the stance changed again. The
``memory_enabled`` / ``user_profile_enabled`` settings DO turn the built-in
stores off, and ``hermes tools disable memory`` became harmful (it also hides
the memory provider's tools). The install doc now says so.

The `(CRITICAL)` framing in SKILL.md was the overstated side of a drift. These
tests now pin the accurate coverage in hermes-setup.md, and assert SKILL.md no
longer carries the install-time disable step (it's not usage). The real
intent-stealing mitigation is the tool-description bias in `tools.py` +
`totalreclaw_recall`'s "call me first" steering, which the usage skill keeps.
"""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = _REPO_ROOT / "python" / "src" / "totalreclaw" / "hermes" / "SKILL.md"
INSTALL_DOC = _REPO_ROOT / "docs" / "guides" / "hermes-setup.md"


def _read(path: Path) -> str:
    assert path.exists(), f"{path} not found at {path}"
    return path.read_text(encoding="utf-8")


def test_install_doc_documents_disable_memory_command():
    """hermes-setup.md must mention the `hermes tools disable memory`
    command so the agent knows the (partial) mitigation exists."""
    body = _read(INSTALL_DOC)
    assert "hermes tools disable memory" in body, (
        "hermes-setup.md must document the `hermes tools disable memory` "
        "command (issue #167 mitigation)."
    )


def test_install_doc_states_how_builtin_memory_is_really_turned_off():
    """Hermes 0.22+ (rc3 QA, #602): the built-in stores are turned off by the
    ``memory_enabled`` / ``user_profile_enabled`` settings that
    ``activate-memory-provider`` writes. ``hermes tools disable memory`` is
    NOT the switch: it only gates the agent's ``memory`` toolset, and on 0.22+
    that also hides the memory provider's tools and system-prompt block. Pin
    both so a rewrite doesn't bring the disable step back."""
    body = _read(INSTALL_DOC)
    assert "memory_enabled: false" in body and "user_profile_enabled: false" in body, (
        "hermes-setup.md must name the settings that turn the built-in "
        "stores off on Hermes 0.22+."
    )
    assert "Do **not** also run `hermes tools disable memory`" in body, (
        "hermes-setup.md must warn against `hermes tools disable memory`: "
        "on Hermes 0.22+ it gates off the provider's tools."
    )


def test_install_doc_names_the_failure_mode_files():
    """The failure-mode files (USER.md / MEMORY.md) must be named so the
    agent + a future contributor understand where data wrongly lands."""
    body = _read(INSTALL_DOC)
    assert "MEMORY.md" in body
    assert "USER.md" in body


def test_skill_md_does_not_carry_install_disable_step():
    """Post-consolidation, the usage skill must NOT carry the install-time
    disable-memory step — that's the install doc's job. The intent-stealing
    mitigation in the usage skill is `totalreclaw_recall`-first steering, not
    a CLI install command."""
    body = _read(SKILL_MD)
    assert "hermes tools disable memory" not in body, (
        "SKILL.md (usage-only) must NOT contain the install-time `hermes "
        "tools disable memory` command — it lives in hermes-setup.md."
    )
    assert "(CRITICAL)" not in body, (
        "The overstated '(CRITICAL) disable memory' install framing must not "
        "be in the usage skill."
    )
