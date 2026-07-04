"""Regression: agent must route "buy a one-time top-up pack" to
``totalreclaw_top_up`` — not ``totalreclaw_upgrade`` (issue #405).

Why this file exists
====================

rc.2.4.5rc10 auto-QA (Hermes, umbrella #403 / finding #F2 / #405): a fresh
session got the verbatim user message

    "I am about to run a big import and might blow past my quota.
     Can I buy a top-up pack of 5000 extra memories?"

The agent replied "The only paid option I can initiate from here is the Pro
upgrade", called ``totalreclaw_status`` + ``totalreclaw_upgrade``, and never
called ``totalreclaw_top_up`` — despite the tool being registered.

Root cause (two compounding gaps):

1. The agent-facing runtime prompt ``SKILL.md`` steered every quota / pay /
   "more memories" intent to ``totalreclaw_upgrade`` and never mentioned
   ``totalreclaw_top_up``. Its "Tool surface" roster omitted ``_top_up``
   entirely, so the tool was effectively invisible to the agent's routing.
2. The ``TOPUP`` schema description over-gated on the quota being *exhausted*
   ("Only call when ... quota + grace are exhausted"), excluding exactly the
   proactive / pre-import case the user asked about — while ``UPGRADE`` had no
   such gate and grabbed "pay for more memories".

These tests pin the fix: SKILL.md surfaces + routes top-up, the schema accepts
proactive intent, and both artifacts disambiguate top-up (one-time pack) from
upgrade (recurring Pro).
"""
from __future__ import annotations

from pathlib import Path

from totalreclaw.hermes import schemas as _schemas


_REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = _REPO_ROOT / "python" / "src" / "totalreclaw" / "hermes" / "SKILL.md"
HERMES_GUIDE = _REPO_ROOT / "docs" / "guides" / "hermes-setup.md"


def _read(path: Path) -> str:
    assert path.exists(), f"not found: {path}"
    return path.read_text(encoding="utf-8")


# --- SKILL.md (runtime prompt) --------------------------------------------


def test_skill_tool_surface_lists_top_up():
    """The 'Tool surface' roster the agent reads must include ``_top_up`` —
    if the tool isn't in the inventory, the agent won't route to it."""
    body = _read(SKILL_MD)
    assert "_top_up" in body or "totalreclaw_top_up" in body, (
        "SKILL.md must surface totalreclaw_top_up so the agent knows the "
        "tool exists (it was omitted from the Tool surface line — #405)."
    )


def test_skill_routes_proactive_topup_intent():
    """SKILL.md must teach the agent to offer top-up for a proactive
    'more memories now' request, distinct from the recurring Pro upgrade."""
    body = _read(SKILL_MD)
    assert "totalreclaw_top_up" in body, (
        "SKILL.md must name totalreclaw_top_up in its routing guidance."
    )
    lowered = body.lower()
    # Must contrast top-up (one-time pack) vs upgrade (recurring), so the
    # agent stops defaulting every pay intent to totalreclaw_upgrade.
    assert "one-time" in lowered, (
        "SKILL.md must describe top-up as a one-time pack to disambiguate "
        "it from the recurring Pro upgrade."
    )
    # Proactive trigger — the exact failure mode from #405.
    assert "big import" in lowered or "blow past" in lowered or "proactive" in lowered, (
        "SKILL.md must tell the agent that a proactive 'might blow past my "
        "quota' request is a valid top-up trigger (don't wait for the quota "
        "to be exhausted)."
    )


# --- schema description (LLM routing source of truth) ---------------------


def test_topup_schema_accepts_proactive_intent():
    """The TOPUP description must no longer gate exclusively on the quota
    being exhausted — a proactive / pre-import buy is a valid trigger."""
    desc = _schemas.TOPUP["description"]
    lowered = desc.lower()
    assert "totalreclaw_top_up" == _schemas.TOPUP["name"]
    # The pre-fix description said "Only call when ... quota + grace are
    # exhausted" as the sole gate; the fix must explicitly allow proactive
    # requests.
    assert "do not wait" in lowered or "proactive" in lowered, (
        "TOPUP description must allow proactive top-up requests (not only "
        "post-exhaustion) — that was the #405 mis-gate."
    )


def test_topup_schema_disambiguates_from_upgrade():
    """Both billing tools must cross-reference each other so the LLM can
    pick the right one at routing time."""
    topup = _schemas.TOPUP["description"]
    upgrade = _schemas.UPGRADE["description"]
    assert "totalreclaw_upgrade" in topup, (
        "TOPUP description must reference totalreclaw_upgrade to steer "
        "recurring-subscription intent away from the one-time pack."
    )
    assert "totalreclaw_top_up" in upgrade, (
        "UPGRADE description must point one-time-pack intent at "
        "totalreclaw_top_up instead of grabbing it."
    )


# --- setup guide (human + install-time doc) -------------------------------


def test_hermes_guide_tools_table_lists_top_up():
    body = _read(HERMES_GUIDE)
    assert "totalreclaw_top_up" in body, (
        "hermes-setup.md Tools table must list totalreclaw_top_up (#405 — "
        "it was omitted, hiding the tool from the guide's inventory)."
    )
