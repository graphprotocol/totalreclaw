"""2.4.4 — SKILL.md ``## Usage (post-setup)`` section must cover the
five gaps the prior version had:

1. Auto-extraction awareness (post_llm_call hook fires; don't double-call).
2. What NOT to store (transient noise / commands / banter).
3. Memory taxonomy types — name all 6 (claim / preference / directive /
   commitment / episode / summary) so the agent knows what `type=` to
   pass.
4. Scopes — name the 8 (work / personal / health / family / creative /
   finance / misc / unspecified).
5. Summaries — when to call `totalreclaw_debrief` vs let the
   on_session_end hook auto-fire.

Plus: tool surface line must include ``totalreclaw_retype`` and
``totalreclaw_set_scope`` (regression-fix for 2.3.5rc1 → 2.4.4: those
two tools existed but were absent from the tool-surface line, so
agents didn't know to call them for re-tagging mis-classified facts).

Companion to:
- ``test_skill_md_tiers_pricing_2_4_0.py`` (2.4.0 tier section)
- ``test_skill_md_restart_hardening_2_3_4.py`` (2.3.4 deny-list)
- ``test_skill_md_includes_disable_memory_step.py`` (rc.26 disable-built-in)
"""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = _REPO_ROOT / "python" / "src" / "totalreclaw" / "hermes" / "SKILL.md"


def _read() -> str:
    assert SKILL_MD.exists(), f"SKILL.md not found at {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Auto-extraction awareness
# ---------------------------------------------------------------------------


def test_usage_section_warns_about_auto_extraction():
    """The agent must know auto-extraction exists so it doesn't double-call
    `totalreclaw_remember` on content the hook already captured."""
    body = _read()
    # Pin the awareness signal — at least one of these phrases.
    has_signal = (
        "auto-extraction" in body.lower()
        or "post_llm_call" in body
        or "auto-extract" in body.lower()
    )
    assert has_signal, (
        "Usage section must reference auto-extraction by name so the "
        "agent knows the post_llm_call hook fires every ~3 turns and "
        "doesn't manually re-store facts that the hook already captured."
    )


def test_usage_section_explicit_dont_double_call():
    """Explicit instruction not to double-call (auto + manual on same content)."""
    body = _read().lower()
    assert "don't double-call" in body or "do not double-call" in body or "skip the manual" in body, (
        "Usage section must explicitly tell the agent NOT to manually "
        "remember content that auto-extraction has already captured."
    )


# ---------------------------------------------------------------------------
# What NOT to store
# ---------------------------------------------------------------------------


def test_usage_section_lists_what_not_to_store():
    """The agent needs explicit negative examples — what NOT to send to
    totalreclaw_remember — so it doesn't fill the user's quota with
    transient session noise."""
    body = _read().lower()
    # At least 2 of these negative-example signals must be present.
    signals = [
        "casual greetings",
        "transient",
        "banter",
        "don't store",
        "skip totalreclaw_remember",
        "transient noise",
        "tool-output paste-back",
        "commands / instructions the user issued to you",
    ]
    hits = sum(1 for s in signals if s in body)
    assert hits >= 2, (
        f"Usage section must list at least 2 negative-example signals for "
        f"what NOT to store. Found {hits}. Looked for: {signals}"
    )


# ---------------------------------------------------------------------------
# Taxonomy types
# ---------------------------------------------------------------------------


def test_usage_section_names_all_six_memory_types():
    """All 6 taxonomy v1 types must be named in the Usage section so the
    agent can pass `type=` correctly on manual `totalreclaw_remember`
    calls. Without this, the agent either omits `type=` (extractor
    auto-tags inconsistently) or guesses.
    """
    body = _read().lower()
    required_types = ["claim", "preference", "directive", "commitment", "episode", "summary"]
    missing = [t for t in required_types if f"`{t}`" not in body]
    assert not missing, (
        f"Usage section must name all 6 memory taxonomy types in "
        f"backticks. Missing: {missing}"
    )


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


def test_usage_section_names_all_eight_scopes():
    """All 8 scopes must appear so the agent can pass `scope=` correctly
    + so the agent knows to call `totalreclaw_set_scope` for mis-scoped
    facts."""
    body = _read().lower()
    required_scopes = [
        "work", "personal", "health", "family",
        "creative", "finance", "misc", "unspecified",
    ]
    # Look in the "Scopes." paragraph specifically.
    scope_para_match = (
        "scope" in body
        and all(s in body for s in required_scopes)
    )
    assert scope_para_match, (
        f"Usage section must name all 8 scopes (work / personal / health / "
        f"family / creative / finance / misc / unspecified)."
    )


# ---------------------------------------------------------------------------
# Summaries / debrief
# ---------------------------------------------------------------------------


def test_usage_section_explains_debrief_auto_fire_vs_manual():
    """The agent must know:
    - `totalreclaw_debrief` auto-fires via the `on_session_end` hook
    - Manual calls only when user explicitly asks for a mid-session recap
    Without this, agents either skip debrief entirely (it's not in their
    write playbook) or double-call (one auto + one manual)."""
    body = _read().lower()
    has_auto_signal = (
        "on_session_end" in body
        or "auto-fires" in body
        or "auto-fire" in body
        or "auto-summarize" in body
    )
    has_manual_signal = (
        "manual call" in body
        or "manually call" in body
        or "manual `totalreclaw_debrief`" in body.lower()
        or "explicitly ask" in body
    )
    assert has_auto_signal, (
        "Usage section must explain that totalreclaw_debrief auto-fires "
        "via the on_session_end hook."
    )
    assert has_manual_signal, (
        "Usage section must explain WHEN the agent should manually call "
        "totalreclaw_debrief (only on explicit user request for a "
        "mid-session recap)."
    )


# ---------------------------------------------------------------------------
# Tool surface line includes retype + set_scope
# ---------------------------------------------------------------------------


def test_tool_surface_line_includes_retype_and_set_scope():
    """The ``## Tool surface`` line under SKILL.md was missing
    ``totalreclaw_retype`` + ``totalreclaw_set_scope`` until 2.4.4 even
    though those tools have shipped since rc.23 (2026-04-15). Agents
    couldn't surface them because they weren't in the visible tool list.
    """
    body = _read()
    # Pull the Tool surface paragraph specifically (between the section
    # header and the next section / EOF).
    tool_surface_idx = body.find("## Tool surface")
    assert tool_surface_idx >= 0, "SKILL.md must include a `## Tool surface` section"
    tail = body[tool_surface_idx:]
    next_section = tail.find("\n## ", 1)
    tool_surface_block = tail[:next_section] if next_section > 0 else tail
    assert "_retype" in tool_surface_block, (
        "`## Tool surface` line must include `_retype` (= totalreclaw_retype). "
        "Existed since rc.23, was missing from the surface line until 2.4.4."
    )
    assert "_set_scope" in tool_surface_block, (
        "`## Tool surface` line must include `_set_scope` (= totalreclaw_set_scope). "
        "Same omission as _retype."
    )


# ---------------------------------------------------------------------------
# Mutation pattern preserved (recall first → mutate)
# ---------------------------------------------------------------------------


def test_usage_section_preserves_recall_first_mutation_pattern():
    """Pre-2.4.4 rule: forget / pin / unpin all need fact_id, so the
    pattern is `recall first, mutate second`. Must survive the
    rewrite — the new section adds retype + set_scope but the
    recall-first principle still applies."""
    body = _read().lower()
    assert "recall first" in body, (
        "Usage section must preserve the 'recall first → mutate second' "
        "pattern for forget / pin / unpin / retype / set_scope."
    )


# ---------------------------------------------------------------------------
# Recall-behaviour rule pinned (rc.5)
# ---------------------------------------------------------------------------


def test_usage_section_pins_rc5_recall_behaviour_rule():
    """The rc.5 rule: agents MUST call totalreclaw_recall even when the
    answer appears to be in the current context window. Hermes' USER.md
    cache is local; TotalReclaw on chain is canonical."""
    body = _read().lower()
    has_signal = (
        "rc.5 recall-behaviour" in body
        or "even when the answer appears to be in the current context" in body
        or ("must call" in body and "totalreclaw_recall" in body)
    )
    assert has_signal, (
        "Usage section must pin the rc.5 recall-behaviour rule: agents "
        "MUST call totalreclaw_recall for first-person factual queries "
        "EVEN when context appears to hold the answer. USER.md is local; "
        "on-chain TR is canonical."
    )
