"""2.4.0rc2 — Tiers + pricing section in Hermes SKILL.md.

Why this file exists
====================

Pedro 2026-05-18 QA finding on 2.4.0rc1: the agent's understanding of
account upgrades, tiers, and pricing was inconsistent (occasionally
inventing "free trial" framing, conflating self-hosted with Pro,
quoting wrong tier caps). Root cause: ``SKILL.md`` only mentioned
pricing in passing in the post-setup confirmation line and had no
canonical table or deny-list. Other surfaces (skill-nanoclaw,
mcp/SKILL.md) carry the canonical copy; Hermes did not.

2.4.0rc2 ships a new ``## Tiers + pricing`` section with:

- A canonical table whose cells cite ``totalreclaw_status`` fields
  (``free_writes_limit`` / ``upgrade_writes_limit``) instead of hardcoded
  caps — Stripe is the source of truth (write-quota source of truth spec,
  R-C), so agent-facing copy carries no quota numbers.
- An automatic quota-signalling spec (>80%, 403 quota exceeded, first
  successful pair).
- An upgrade-flow user-intent → action matrix.
- A forbidden-claims deny-list pinning the seven most common
  hallucinated tier statements the agent must NEVER write.

These tests pin the rc2 contract so a future refactor / branch
synchronization can't silently drop the canonical pricing copy or
the deny-list.
"""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = _REPO_ROOT / "python" / "src" / "totalreclaw" / "hermes" / "SKILL.md"


def _read_skill() -> str:
    assert SKILL_MD.exists(), f"SKILL.md not found at {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def test_skill_includes_tiers_pricing_section():
    body = _read_skill()
    assert "## Tiers + pricing" in body, (
        "Hermes SKILL.md must include a '## Tiers + pricing' section "
        "so the agent has the canonical reference when users ask "
        "about quota / upgrade / pricing."
    )


def test_skill_free_cap_comes_from_status():
    """The Free-tier cell cites ``free_writes_limit`` from
    ``totalreclaw_status`` — no hardcoded cap (R-C: agent-facing copy
    carries no quota numbers; Stripe is the source of truth)."""
    body = _read_skill()
    assert "250 memories" not in body, (
        "SKILL.md must not hardcode the Free cap — quote "
        "`free_writes_limit` from `totalreclaw_status` instead."
    )
    assert "free_writes_limit" in body, (
        "The Free-tier cell must cite `free_writes_limit` from "
        "`totalreclaw_status` so the number is always live."
    )
    # The only permitted chain mention is the deny-list entry telling the
    # agent NOT to name a chain/network to the user.
    assert "Naming the underlying network / chain" in body, (
        "SKILL.md must carry the deny-list entry forbidding the agent from "
        "naming the underlying chain/network to the user."
    )


def test_skill_pro_cap_comes_from_status():
    """The Pro-tier cell cites ``upgrade_writes_limit`` /
    ``free_writes_limit`` — never a hardcoded or stale Pro cap."""
    body = _read_skill()
    assert "3,000" not in body, (
        "SKILL.md must not hardcode the Pro cap — Stripe is the source of "
        "truth; cite `upgrade_writes_limit` / `free_writes_limit` instead."
    )
    assert "1,500" not in body, (
        "The stale 1,500 Pro cap must never come back."
    )
    assert "upgrade_writes_limit" in body, (
        "The Pro-tier cell must cite `upgrade_writes_limit` (the relay "
        "sends it to Free wallets) so the number is always live."
    )
    assert "LLM-guided dedup" in body, (
        "Pro-tier feature delta must mention 'LLM-guided dedup' — that "
        "is what Pro adds at the relay vs. free-tier writes."
    )


def test_skill_points_at_canonical_sources():
    """The section must teach the agent WHERE to get live numbers
    (`totalreclaw_status`) and catalogue prices (pricing URL) rather
    than baking dollar amounts into the skill itself."""
    body = _read_skill()
    assert "totalreclaw.xyz/pricing" in body, (
        "Section must link to https://totalreclaw.xyz/pricing as the "
        "canonical catalogue source — Stripe prices live there, not in "
        "SKILL.md (would go stale instantly)."
    )
    assert "totalreclaw_status" in body, (
        "Section must direct the agent to call ``totalreclaw_status`` "
        "for live tier + used + limit values."
    )


def test_skill_documents_quota_thresholds():
    """The agent must know that the plugin auto-injects warnings at
    >80% usage + 403 quota exceeded — otherwise it won't surface them
    to the user proactively."""
    body = _read_skill()
    assert ">80%" in body or "80%" in body, (
        "Section must reference the 80% quota threshold — the plugin "
        "auto-injects a warning at that point + the agent must surface "
        "it to the user."
    )
    assert "403" in body, (
        "Section must reference the 403 quota-exceeded response so the "
        "agent knows to call totalreclaw_upgrade after a write fails."
    )


def test_skill_describes_upgrade_flow():
    """The upgrade flow contract: call totalreclaw_upgrade, emit the
    returned Stripe URL verbatim, wait for `done`, then refresh tier
    via totalreclaw_status."""
    body = _read_skill()
    assert "totalreclaw_upgrade" in body, (
        "Section must reference the totalreclaw_upgrade tool."
    )
    assert "Stripe" in body, (
        "Section must clarify that totalreclaw_upgrade returns a "
        "Stripe checkout URL (so the agent knows what kind of URL to "
        "expect + how to frame it for the user)."
    )
    assert "DO NOT paraphrase the URL" in body, (
        "Section must include the verbatim URL invariant — agents tend "
        "to summarize URLs which breaks Stripe checkout."
    )


def test_skill_forbidden_claims_denylist():
    """rc2 ships a deny-list pinning the 7 most common hallucinated
    tier statements. These are the literal failure modes Pedro saw in
    rc1 QA. Pin each one verbatim so a casual refactor can't silently
    drop them."""
    body = _read_skill()
    # Free tier is permanent, not a trial.
    assert "free trial" in body.lower(), (
        "Deny-list must include 'There's a free trial period' or "
        "equivalent — common hallucination: agents invent a trial "
        "period that doesn't exist."
    )
    # Memories don't expire.
    assert "memories expire" in body.lower() or "Memories expire" in body, (
        "Deny-list must address 'memories expire' — they don't, "
        "they're permanent on Gnosis mainnet."
    )
    # Pro is NOT unlimited.
    assert "Pro = unlimited" in body or "Pro is unlimited" in body, (
        "Deny-list MUST pin the 'Pro = unlimited' false claim — Pro "
        "has a monthly cap, NOT unlimited. Saying 'unlimited' silently "
        "breaks user expectations when they hit the cap."
    )
    # Encryption is identical across tiers.
    assert "upgrade to use encryption" in body.lower(), (
        "Deny-list must address 'you need to upgrade to use "
        "encryption' — E2E encryption is identical across tiers."
    )
    # Base Sepolia is dev-only.
    assert "Base Sepolia" in body, (
        "Deny-list must mention Base Sepolia as dev/QA-only — "
        "agents sometimes confuse testnet for production network."
    )


def test_skill_usage_skill_core_sections_present():
    """Post-consolidation (2026-06-05): the usage skill keeps phrase-safety,
    the pair tool reference, and the usage section. The install-flow sections
    (## Silence rules / ## Setup flow) moved OUT to hermes-setup.md, and the
    setup confirmation line (with 'free tier' copy) now lives in that install
    doc — not the usage skill."""
    body = _read_skill()
    assert "## Phrase safety" in body
    assert "totalreclaw_pair" in body
    assert "## Usage (post-setup)" in body
    # Install-flow sections must NOT have leaked back into the usage skill.
    assert "## Silence rules" not in body
    assert "## Setup flow" not in body


def test_upgrade_tool_description_has_no_cap_numbers():
    """R-C: the upgrade tool description is agent-facing copy, so it carries
    no quota numbers — it points at ``totalreclaw_status`` for the live
    figures instead of hardcoding either tier's cap."""
    from totalreclaw.hermes import schemas

    tool = next(
        v
        for v in vars(schemas).values()
        if isinstance(v, dict)
        and isinstance(v.get("description"), str)
        and "upgrade to Pro" in v["description"]
    )
    desc = tool["description"]
    assert "3,000" not in desc, "upgrade description must not hardcode the Pro cap"
    assert "250" not in desc, "upgrade description must not hardcode the Free cap"
    assert "totalreclaw_status" in desc, (
        "upgrade description must point at totalreclaw_status for live caps"
    )
