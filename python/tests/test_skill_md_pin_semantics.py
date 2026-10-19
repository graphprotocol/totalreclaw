"""PRD-04 F1 / DEP-5 (+ F11): SKILL.md must describe pin behaviour truthfully.

Pinned facts are protected from automatic updates/deletes and rank higher in
recall when relevant; they are NOT injected into every recall (signed-off
F1 design: no "always rank first")."""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = _REPO_ROOT / "python" / "src" / "totalreclaw" / "hermes" / "SKILL.md"


def test_skill_md_does_not_claim_pinned_facts_surface_in_every_recall() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "surface in every subsequent recall" not in text
    assert "Automatic extraction never updates or deletes a pinned fact" in text
