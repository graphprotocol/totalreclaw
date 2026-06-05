"""Regression tests for issue #373 (umbrella #368 finding F5).

The Hermes auto-QA on rc.2.4.4rc6 (§9c) observed the agent's final prose
leaking chain names ("Base Sepolia (testnet)", "Gnosis mainnet") into a
status-tool response even though the underlying tool output was clean.
The root cause is lexicon-seeding: chain-name tokens appearing in any
string that the Hermes runtime injects into the LLM context — tool
descriptions, hook-injected priority nudges, quota-warning nudges, and
the negative-example clause in ``SKILL.md`` — give the LLM the
vocabulary it then paraphrases into prose. The doc-consolidation goal
#300 stripped chain names from user-visible guides but missed these
runtime-injected strings.

This test pins, at the module level, that the LLM-facing surfaces
(tool-description constants, hook-injected nudges, the Hermes SKILL.md
prompt) do not contain literal chain-name vocabulary that could seed
the LLM's lexicon. It is a structural guard against re-introduction;
it does not (and cannot) prove the LLM never emits chain names —
that is what the auto-QA scenario verifies post-merge.
"""
from __future__ import annotations

import re
from pathlib import Path

from totalreclaw.hermes import hooks, schemas

_FORBIDDEN = re.compile(
    r"\b(base\s+sepolia|gnosis|testnet|mainnet)\b",
    re.IGNORECASE,
)


def _assert_clean(name: str, text: str) -> None:
    match = _FORBIDDEN.search(text)
    assert match is None, (
        f"{name} contains a chain-name token ({match.group(0)!r}) that "
        f"seeds the LLM lexicon — see umbrella #368 finding F5. "
        f"Excerpt: ...{text[max(match.start()-40, 0):match.end()+40]}..."
    )


def test_remember_tool_description_has_no_chain_names() -> None:
    _assert_clean("schemas.REMEMBER.description", schemas.REMEMBER["description"])


def test_upgrade_tool_description_has_no_chain_names() -> None:
    _assert_clean("schemas.UPGRADE.description", schemas.UPGRADE["description"])


def test_tool_priority_nudge_has_no_chain_names() -> None:
    _assert_clean("hooks._TOOL_PRIORITY_NUDGE", hooks._TOOL_PRIORITY_NUDGE)


def test_all_tool_descriptions_have_no_chain_names() -> None:
    tools = [
        ("REMEMBER", schemas.REMEMBER),
        ("RECALL", schemas.RECALL),
        ("FORGET", schemas.FORGET),
        ("PIN", schemas.PIN),
        ("STATUS", schemas.STATUS),
        ("UPGRADE", schemas.UPGRADE),
    ]
    for name, spec in tools:
        _assert_clean(f"schemas.{name}.description", spec["description"])
        for param_name, param_spec in spec["parameters"].get("properties", {}).items():
            desc = param_spec.get("description", "")
            if desc:
                _assert_clean(f"schemas.{name}.{param_name}.description", desc)


def test_skill_md_has_no_chain_names() -> None:
    skill_path = Path(__file__).resolve().parents[1] / "src" / "totalreclaw" / "hermes" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    _assert_clean("hermes/SKILL.md", text)
