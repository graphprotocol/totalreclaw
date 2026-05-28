"""rc.2.4.5: SKILL.md must keep ``hermes plugins install`` as a non-skippable step.

Context — issue #341 (auto-QA F2 on rc.2.4.4rc1)
------------------------------------------------

QA-hermes-RC-2.4.4rc1-20260528.md Finding #2 showed glm-5-turbo ran
``pip install totalreclaw`` but skipped
``hermes plugins install p-diogo/totalreclaw-hermes --enable``. Hermes
v0.10.0 Python-plugin auto-discovery still binds ``totalreclaw_*`` tools
from the installed package, so the agent rationalised the manifest step
as unnecessary. The manifest under ``~/.hermes/plugins/`` is still load-
bearing for ``hermes plugins list`` visibility, upgrade management, and
the gateway SIGUSR1 reload race documented in step 1's comment block.

What this test enforces
-----------------------

The Step 1 install block in SKILL.md MUST keep the two required commands
together AND carry a load-bearing anchor that prevents an LLM from
rationalising the manifest command away. If a future refactor removes
the "TWO ... REQUIRED" anchor or drops the second command, the rc.2.4.4
regression silently returns.
"""
from __future__ import annotations

from pathlib import Path


SKILL_MD = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "totalreclaw"
    / "hermes"
    / "SKILL.md"
)


def _read_skill_md() -> str:
    assert SKILL_MD.exists(), f"SKILL.md not found at {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def test_skill_md_invokes_hermes_plugins_install():
    """SKILL.md must instruct the agent to run the literal manifest command."""
    body = _read_skill_md()
    assert "hermes plugins install p-diogo/totalreclaw-hermes --enable" in body, (
        "SKILL.md must include the literal "
        "`hermes plugins install p-diogo/totalreclaw-hermes --enable` "
        "command — see issue #341 (auto-QA F2 rc.2.4.4rc1)."
    )


def test_skill_md_marks_plugin_manifest_step_required():
    """The Step 1 header must contain a load-bearing anchor identifying
    the manifest install as REQUIRED, not optional. Without this, glm-
    5-turbo (and similar) rationalise the manifest command as skippable
    because v0.10.0 auto-discovery binds tools from pip install alone."""
    body = _read_skill_md()
    # Anchor: the words "TWO" + "REQUIRED" must appear close together in
    # the install step header. We check the substring as a single phrase
    # so accidental reflows that split them across a paragraph break get
    # flagged.
    assert "TWO shell commands are REQUIRED" in body, (
        "Step 1 header must carry the 'TWO shell commands are REQUIRED' "
        "anchor so the agent does not rationalise skipping "
        "`hermes plugins install` when pip install alone seems to bind "
        "the tools (Hermes v0.10.0+ Python auto-discovery)."
    )


def test_skill_md_calls_out_v0_10_auto_discovery_pitfall():
    """The Step 1 header must explicitly address the v0.10.0 auto-
    discovery rationalisation. Without naming the failure mode, a future
    refactor may drop the warning and re-introduce the rc.2.4.4 skip."""
    body = _read_skill_md()
    assert "auto-discovery" in body or "auto-bind" in body, (
        "Step 1 must call out that v0.10.0+ auto-discovery binds tools "
        "from pip install alone — this is the exact rationalisation that "
        "led the agent to skip the manifest step in rc.2.4.4rc1."
    )
    assert "hermes plugins list" in body, (
        "Step 1 must name `hermes plugins list` as one of the surfaces "
        "the manifest step exists to satisfy — preserves the why if the "
        "doc gets refactored."
    )


def test_skill_md_keeps_pip_install_before_plugin_manifest():
    """The order pip-install → plugins-install matters (SIGUSR1 reload
    race documented in the step comment block). Test the ordering of the
    actual shell-command block, which is the line pair the agent executes.
    The header anchor may also name both commands as prose — that's fine
    and doesn't satisfy this ordering check."""
    body = _read_skill_md()
    pip_line = '"$HERMES_PYTHON" -m pip install --pre totalreclaw'
    manifest_line = "hermes plugins install p-diogo/totalreclaw-hermes --enable"
    pip_idx = body.find(pip_line)
    assert pip_idx != -1, "pip install command missing from SKILL.md"
    # Look for the manifest command that appears AFTER the pip command —
    # this is the shell-block pair the agent actually runs.
    manifest_idx = body.find(manifest_line, pip_idx)
    assert manifest_idx != -1, (
        "`hermes plugins install p-diogo/totalreclaw-hermes --enable` must "
        "appear in the shell block AFTER `pip install` — pip install must "
        "come FIRST so the Python implementations are on disk before the "
        "manifest triggers a SIGUSR1 reload on deploys that drive plugin "
        "discovery from manifest-registration. Inverting risks silent bind "
        "failure."
    )
    # Sanity: the two should be on neighbouring lines (no extra commands
    # interleaved in the shell block).
    between = body[pip_idx + len(pip_line) : manifest_idx]
    assert between.count("\n") <= 2, (
        "pip install and `hermes plugins install` should sit on adjacent "
        "lines in the shell block — extra commands between them suggest "
        "a refactor split that may have broken the install pair."
    )
