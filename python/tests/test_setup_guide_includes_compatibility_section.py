"""2.3.6rc5: ``docs/guides/hermes-setup.md`` Compatibility section
must honestly document Hermes's non-disable-able built-in memory.

Context — rc5 finding (2026-05-12 Pop-OS QA)
---------------------------------------------

Pre-rc5: the guide claimed step 3 (``hermes tools disable memory``)
"disables Hermes built-in memory". Verified false: that command only
blocks the agent-callable ``memory`` tool. Hermes the gateway STILL
writes ``~/.hermes/memories/USER.md`` autonomously. Upstream Hermes
explicitly says built-in is "always active" per ``hermes memory --help``.

rc5 drops the false promise:
- ``hermes tools disable memory`` is no longer in the install procedure
  (kept as an optional aside in the Compatibility section since it
  still has value blocking the agent-callable tool, but it's NOT
  framed as a disable for the gateway-side writes).
- The Compatibility section honestly states USER.md cannot be disabled.
- A new ``## Recall behaviour`` section requires the agent to call
  ``totalreclaw_recall`` for user-facing recall queries so the
  canonical TotalReclaw read path is exercised even though USER.md is
  also in context.

What this test enforces (rc5 update)
------------------------------------

1. Compatibility section exists and honestly documents the non-disable.
2. Recall-behaviour section exists and tells the agent to call
   ``totalreclaw_recall`` for recall queries.
3. Phrase-safety + tool reference still present (preserved invariants).
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


def test_guide_does_not_falsely_claim_to_disable_built_in_memory():
    """rc5 honesty: the guide must NOT claim that running
    `hermes tools disable memory` actually disables Hermes built-in
    memory writes. Upstream-verified false claim from rc.26.

    The phrase "disable Hermes built-in memory" or "auto-disables"
    must not appear as a claim that the gateway USER.md writes stop.
    Mentions are allowed only in the Compatibility section's honest
    framing (which acknowledges the gateway side cannot be disabled)."""
    body = _read_guide()
    # The false-claim phrases from rc.26 must not appear.
    assert "I've disabled Hermes' built-in `memory` tool so TotalReclaw is your primary memory" not in body, (
        "rc5 dropped the false claim that `hermes tools disable memory` "
        "makes TotalReclaw primary. Hermes built-in is non-disable-able "
        "per upstream — the gateway writes USER.md regardless."
    )
    assert "(CRITICAL)" not in body, (
        "rc5 removed the (CRITICAL) marker on the disable-memory step "
        "since that step was based on a false claim."
    )


def test_guide_includes_honest_compatibility_section():
    """The Compatibility section exists and says TotalReclaw runs alongside
    Hermes' built-in memory (2.5.3rc4). Turning the built-in off is the
    explicit ``--exclusive`` opt-out, through the ``memory_enabled`` /
    ``user_profile_enabled`` settings, NOT ``hermes tools disable memory``,
    which also hides the memory provider's tools (rc3 QA, #602)."""
    body = _read_guide()
    assert "## Compatibility with Hermes built-in memory" in body, (
        "hermes-setup.md must include a `## Compatibility with Hermes "
        "built-in memory` section explaining the dual-mode reality."
    )
    section = body.split("## Compatibility with Hermes built-in memory", 1)[1]
    assert "TotalReclaw runs alongside Hermes' built-in memory" in section, (
        "Compatibility section must say the built-in memory stays on beside "
        "TotalReclaw by default (2.5.3rc4)."
    )
    assert "New and edited built-in entries are copied into TotalReclaw; removals are not" in section, (
        "Compatibility section must say adds/edits are mirrored but removals "
        "are not (they are usually evictions for space)."
    )
    assert "A removal never deletes anything from TotalReclaw" in section
    assert "the old memories are kept" in section  # merges keep old facts
    assert "To forget something, the agent calls `totalreclaw_forget`" in section
    assert "Changes Hermes doesn't report are caught at session start and end" in section, (
        "Compatibility section must say the reconcile pass catches the "
        "background review's writes, which Hermes never reports."
    )
    assert "TOTALRECLAW_HERMES_MEMORY_RECONCILE=0" in section
    assert "activate-memory-provider --exclusive" in section, (
        "Compatibility section must name the opt-out that turns the "
        "built-in off."
    )
    assert "memory_enabled: false" in section and "user_profile_enabled: false" in section, (
        "Compatibility section must name the two settings `--exclusive` "
        "writes to turn the built-in stores off on Hermes 0.22+."
    )
    assert "always active" in section, (
        "Compatibility section must address Hermes' own 'always active' "
        "wording so readers aren't misled by `hermes memory --help`."
    )
    assert "Do **not** also run `hermes tools disable memory`" in section, (
        "Compatibility section must warn against `hermes tools disable "
        "memory`: on Hermes 0.22+ it gates off the provider's tools."
    )
    assert "USER.md" in section, (
        "Compatibility section must name the actual file Hermes writes "
        "to (~/.hermes/memories/USER.md) so users / readers can trace it."
    )


def test_guide_includes_canonical_cross_session_framing():
    """rc5: the guide must clearly frame TotalReclaw as the canonical
    cross-session store, with Hermes USER.md framed as a local
    per-container context cache (not a memory store).
    """
    body = _read_guide()
    assert "canonical cross-session store" in body, (
        "Compatibility section must label TotalReclaw as the canonical "
        "cross-session store so the dual-mode reality has a clear "
        "value-prop anchor."
    )
    # USER.md is framed as a CACHE / context layer, not a memory store.
    assert "context cache" in body or "context layer" in body, (
        "Compatibility section must frame USER.md as a local per-"
        "container context cache, not a memory store."
    )


def test_guide_requires_totalreclaw_recall_for_recall_queries():
    """rc5: a new ``## Recall behaviour`` section requires the agent to
    call ``totalreclaw_recall`` for user-facing recall queries so the
    canonical read path is exercised even when USER.md has the answer
    in context."""
    body = _read_guide()
    assert "## Recall behaviour" in body, (
        "hermes-setup.md must include a `## Recall behaviour` section "
        "telling the agent how to handle user recall queries."
    )
    assert "totalreclaw_recall" in body, (
        "Recall-behaviour section must explicitly direct the agent to "
        "call the `totalreclaw_recall` tool."
    )


def test_guide_points_users_to_issue_tracker_for_dual_mode_feedback():
    """rc5: users who want fully-disable-able built-in memory must be
    pointed at Hermes upstream (since that's where the fix would land)
    AND our own tracker for scope-of-demand signal."""
    body = _read_guide()
    # rc5 points at BOTH the upstream Hermes repo (where the actual fix
    # would have to land) AND our own tracker.
    assert "github.com/NousResearch/hermes-agent/issues" in body or "NousResearch/hermes-agent" in body, (
        "Compatibility section must link to Hermes upstream issue "
        "tracker since the fix has to land there, not in totalreclaw."
    )
    assert "github.com/graphprotocol/totalreclaw/issues" in body, (
        "Compatibility section must also link to our own tracker so we "
        "can scope demand independently."
    )


def test_guide_says_pairing_activates_the_provider():
    """2.5.3rc4 (#614): both pairing paths activate the provider, so the
    guide no longer tells the agent pairing doesn't, and activation no
    longer claims to turn the built-in memory off."""
    body = _read_guide()
    assert "Pairing does NOT make TotalReclaw" not in body
    assert "**Pairing makes TotalReclaw Hermes' memory provider**" in body
    assert "REQUIRED: pairing does not do this" not in body
    assert "takes over from Hermes' built-in memory" not in body
    assert "so Hermes starts using TotalReclaw as its memory provider." in body


def test_guide_post_install_steps_renumbered():
    """2.3.6rc4 agent-only rewrite collapsed the numbered "Step N" headings
    into procedure subsections. The structural invariants the prior shape
    enforced (credentials-check exists; pair flow exists; verify-and-confirm
    exists) are preserved by content keywords rather than heading shape.
    """
    body = _read_guide()
    # Credentials check still present (in the Account setup section).
    assert "credentials.json" in body and "already set up" in body, (
        "Guide must include the credentials-check + already-set-up early-exit."
    )
    # Pair flow still present.
    assert "totalreclaw_pair" in body and "Enter PIN" in body, (
        "Guide must include the totalreclaw_pair call + the user-facing "
        "Enter-PIN instruction."
    )
    # Verify-and-confirm step.
    assert "Reply `done`" in body and "✓ TotalReclaw set up." in body, (
        "Guide must include the verify-and-confirm flow: user replies "
        "`done` → re-check credentials → confirm with the sealed marker."
    )
