"""Install / restart model pins — POST-CONSOLIDATION (2026-06-05).

History
=======
This file originally pinned an *autonomous* `/restart` model in SKILL.md
(agent issues `/restart` itself; deny-list of "Should I /restart?" phrasings;
`/new` as a hot-reload fallback). The 2026-06-05 doc consolidation
(docs/plans/2026-06-04-hermes-skill-consolidation-design.md) moved ALL install
content out of SKILL.md into the single canonical install doc
`docs/guides/hermes-setup.md`, and — crucially — VALIDATED the restart model
against the actual Hermes source (NousResearch/hermes-agent):

* The agent CANNOT self-restart. Hermes parses slash commands from USER input
  only; an agent emitting `/restart` (or `/new`) is inert text, every surface.
* There is NO hot-reload on `hermes plugins install` — it only writes files.
  Plugins are discovered ONCE at gateway boot.
* `/new` does NOT re-scan plugins, so it is NOT a shortcut to bind a freshly
  installed plugin — a FULL gateway restart is required.
* A user-typed `/restart` triggers a graceful SIGUSR1 exit that respawns only
  under a supervisor (systemd/launchd/s6); bare docker / ephemeral `hermes
  chat` need an out-of-band restart.

The old autonomous-`/restart` assertions encoded the *losing* side of a drift
between SKILL.md and hermes-setup.md and are intentionally gone. This file now
pins the VALIDATED model in the install doc, plus the invariant that SKILL.md
carries NO install procedure (only a pointer).
"""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = _REPO_ROOT / "python" / "src" / "totalreclaw" / "hermes" / "SKILL.md"
INSTALL_DOC = _REPO_ROOT / "docs" / "guides" / "hermes-setup.md"


def _read(path: Path) -> str:
    assert path.exists(), f"{path} not found — has it been moved or renamed?"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Section 1 — The validated restart model lives in the install doc
# ---------------------------------------------------------------------------


def test_install_doc_says_agent_cannot_self_restart():
    """The agent must be told it CANNOT issue the restart itself — Hermes
    executes slash commands from user input only (validated against the
    Hermes source). Without this the agent emits an inert `/restart` and
    stalls."""
    body = _read(INSTALL_DOC).lower()
    assert "cannot" in body and "restart" in body
    assert "user input only" in body or "only the user can trigger" in body, (
        "hermes-setup.md must state that Hermes parses slash commands from "
        "USER input only, so an agent-emitted /restart does nothing."
    )


def test_install_doc_requires_full_restart_no_hot_reload():
    """A full gateway restart is the ONLY way to bind a freshly-installed
    plugin — there is no hot-reload. Pin this so a future rewrite doesn't
    re-introduce a phantom hot-reload assumption."""
    body = _read(INSTALL_DOC)
    assert "no hot-reload" in body.lower() or "There is no hot-reload" in body
    assert "once at gateway boot" in body or "once, at gateway startup" in body, (
        "hermes-setup.md must say plugins are discovered once at gateway "
        "boot (so only a full restart binds a new plugin)."
    )


def test_install_doc_forbids_new_as_shortcut():
    """`/new` does NOT re-scan plugins. The doc must NOT present it as a
    shortcut, and should explicitly warn against it (the pre-consolidation
    doc wrongly claimed `/new` reloaded the manifest)."""
    body = _read(INSTALL_DOC)
    assert "Do NOT suggest `/new`" in body or "not a shortcut" in body, (
        "hermes-setup.md must warn that `/new` is NOT a shortcut to bind a "
        "freshly-installed plugin — a new session reuses the boot-time tool "
        "registry."
    )
    # The OLD false claim must be gone.
    assert "without a full gateway restart" not in body, (
        "Stale claim regressed: the doc must NOT say tools bind 'without a "
        "full gateway restart' — that contradicts the validated Hermes "
        "source (plugins discovered once at boot)."
    )
    assert "messaging-platform only" not in body.lower(), (
        "Stale claim regressed: slash-command execution is user-input-only "
        "on EVERY surface, not 'messaging-platform only'."
    )


def test_install_doc_has_per_surface_user_restart_commands():
    """The user (not the agent) restarts the gateway, per surface. Both the
    native and docker restart commands must be present."""
    body = _read(INSTALL_DOC)
    assert "hermes gateway restart" in body
    assert "docker restart" in body


def test_install_doc_supervisor_requirement():
    """The doc must explain that a user-typed /restart only respawns under a
    process supervisor; bare docker / ephemeral CLI need an out-of-band
    restart."""
    body = _read(INSTALL_DOC).lower()
    assert "supervisor" in body
    assert "sigusr1" in body


def test_install_doc_install_order_pip_before_plugin():
    """Install the Python package before enabling the plugin manifest. (The
    old SIGUSR1-reload-race rationale is NOT asserted — the validated source
    shows install writes files with no live reload — but pip-before-plugin
    is kept as the convention so implementations are on disk first.)"""
    body = _read(INSTALL_DOC)
    pip_idx = body.find('"$HERMES_PYTHON" -m pip install')
    plugin_idx = body.find("hermes plugins install p-diogo/totalreclaw-hermes")
    assert pip_idx > 0 and plugin_idx > 0, (
        "hermes-setup.md install procedure must contain both the pip install "
        "and the `hermes plugins install` canonical lines."
    )
    assert pip_idx < plugin_idx, (
        "Install ordering regressed: pip install must precede `hermes plugins "
        "install` so the Python package is on disk before the manifest is "
        "enabled."
    )


# ---------------------------------------------------------------------------
# Section 2 — SKILL.md is usage-only: it must NOT carry the install procedure
# ---------------------------------------------------------------------------


def test_skill_md_has_no_install_commands():
    """Post-consolidation, SKILL.md is the per-turn USAGE skill and must NOT
    mirror the install flow — that lives only in hermes-setup.md. A mirror
    is exactly what drifted before."""
    body = _read(SKILL_MD)
    assert "hermes plugins install" not in body, (
        "SKILL.md must NOT contain the `hermes plugins install` command — "
        "install lives in hermes-setup.md. A mirror re-introduces drift."
    )
    assert "pip install" not in body, (
        "SKILL.md must NOT contain a `pip install` line — install is the "
        "install doc's job."
    )
    # No restart matrix / SIGUSR1 procedure in the usage skill.
    assert "SIGUSR1" not in body
    assert "## Setup flow" not in body and "## Silence rules" not in body


def test_skill_md_points_at_install_doc():
    """SKILL.md must point to the canonical install doc for the
    not-installed / tools-missing case instead of mirroring the steps."""
    body = _read(SKILL_MD)
    assert "docs/guides/hermes-setup.md" in body, (
        "SKILL.md must link the canonical install doc (hermes-setup.md) for "
        "the bootstrap path rather than mirroring install steps."
    )
