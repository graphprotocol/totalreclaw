"""The usage skill handles "update TotalReclaw" and the restart that follows.

Found on a real Telegram install (2026-11-01): asked "Can you upgrade
TotalReclaw to rc6?", the agent had only this skill, where "upgrade" meant
buying Pro. It installed the package, tried to restart the gateway itself
(Hermes blocks that from agent sessions), wrote a restart script and told the
user to SSH in, when sending /restart in the chat would have done it.
"""
from __future__ import annotations

from pathlib import Path

SKILL_MD = Path(__file__).resolve().parents[2] / "python" / "src" / "totalreclaw" / "hermes" / "SKILL.md"


def _skill() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def test_version_update_is_not_routed_to_pro_checkout():
    s = _skill()
    assert "software update, not Pro" in s
    assert "## Updating TotalReclaw" in s
    assert "hermes-setup.md" in s.split("## Updating TotalReclaw", 1)[1].split("## ", 1)[0]


def test_chat_users_are_asked_to_send_restart():
    section = _skill().split("## Restarting Hermes", 1)[1].split("\n## ", 1)[0]
    assert "You cannot restart Hermes yourself" in section
    assert "Send `/restart` here" in section
    assert "Never give a chat-app user shell commands" in section
    assert "Don't write restart scripts" in section
    assert "hermes gateway restart" in section  # terminal CLI only
