"""SKILL.md post-consolidation structure invariants (2026-06-05).

Pins the Option-B consolidation contract
(docs/plans/2026-06-04-hermes-skill-consolidation-design.md):

* SKILL.md is the per-turn USAGE skill — usage sections present, install flow
  absent (only a pointer to hermes-setup.md).
* No chain/network name appears in user-facing copy (docs name no chain;
  `totalreclaw_status` / billing is canonical). The single allowed exception is
  the deny-list entry that tells the agent NOT to name a chain.
* The always-injected frontmatter `description` is trigger-complete — it covers
  the whole memory-intent surface (forget / pin / status / import / …), not
  just remember/recall, so the skill actually loads on those turns.

The chain-name guard here also serves as the CI drift-guard (it runs in the
python-tests job): a future edit that reintroduces "Gnosis mainnet" into
user-facing copy fails the build.
"""
from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = _REPO_ROOT / "python" / "src" / "totalreclaw" / "hermes" / "SKILL.md"


def _read() -> str:
    assert SKILL_MD.exists(), f"SKILL.md not found at {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def _frontmatter_description() -> str:
    body = _read()
    # Frontmatter is the first --- ... --- block.
    m = re.search(r"^---\s*\n(.*?)\n---\s*\n", body, re.DOTALL)
    assert m, "SKILL.md must start with a YAML frontmatter block."
    fm = m.group(1)
    dm = re.search(r'description:\s*"(.*?)"\s*$', fm, re.DOTALL | re.MULTILINE)
    assert dm, "SKILL.md frontmatter must have a quoted `description:` field."
    return dm.group(1)


def test_usage_sections_present():
    body = _read()
    for heading in ("## Phrase safety", "## Usage (post-setup)", "## Tool surface"):
        assert heading in body, f"SKILL.md must keep the '{heading}' section."


def test_install_flow_absent_only_pointer():
    """The install procedure must NOT live in the usage skill — only a pointer
    to the canonical install doc. (Drift-guard: install headings reappearing
    here is exactly the mirror that drifted before.)"""
    body = _read()
    for forbidden in ("## Setup flow", "## Silence rules", "hermes plugins install", "pip install"):
        assert forbidden not in body, (
            f"SKILL.md (usage-only) must not contain '{forbidden}' — install "
            "lives in docs/guides/hermes-setup.md."
        )
    assert "docs/guides/hermes-setup.md" in body, (
        "SKILL.md must point at the canonical install doc for the bootstrap "
        "path."
    )


def test_no_user_facing_chain_names():
    """Docs name no chain. The ONLY line allowed to mention a chain/network is
    the deny-list entry instructing the agent not to name one."""
    body = _read()
    chain_terms = ("Gnosis", "Base Sepolia", "mainnet", "testnet")
    offenders = []
    for lineno, line in enumerate(body.splitlines(), 1):
        if any(term in line for term in chain_terms):
            if "Naming the underlying network" not in line:
                offenders.append((lineno, line.strip()))
    assert not offenders, (
        "User-facing chain/network names found in SKILL.md (only the "
        "'Naming the underlying network' deny-list entry may reference a "
        f"chain): {offenders}"
    )


def test_description_is_trigger_complete():
    """The always-injected description must cover the whole memory-intent
    surface so the skill loads on forget/pin/status/import turns too — not
    just remember/recall. A narrow description = skill never loads on those
    turns = agent flies blind (the reliability bug this consolidation fixes)."""
    desc = _frontmatter_description().lower()
    for term in ("remember", "recall", "forget", "status", "import"):
        assert term in desc, (
            f"Frontmatter description must mention '{term}' so the skill "
            "triggers on that intent. Description is injected every turn and "
            "is the load trigger; omitting an intent means the skill never "
            "loads for it."
        )
    # Must signal the surface is broader than the two headline tools.
    assert "pin" in desc or "manage" in desc, (
        "Description should signal the management tools (pin / re-type / "
        "re-scope) so the agent doesn't treat remember/recall as the whole "
        "surface."
    )


def test_tool_surface_lists_full_set():
    """The canonical full enumeration lives in ## Tool surface — keep the
    management tools the old main-branch SKILL.md was missing."""
    body = _read()
    for tool in ("_pin", "_unpin", "_retype", "_set_scope", "_status", "_import_from"):
        assert tool in body, f"## Tool surface must list `totalreclaw{tool}`."
