"""``docs/guides/install.md`` must route a Hermes 0.22+ agent to the Hermes guide.

The universal dispatcher (what ``https://totalreclaw.xyz/install`` points at)
used to detect Hermes with ``command -v hermes && command -v pip`` and say
"Install is ``pip install totalreclaw``". Hermes 0.22+ runs on its own bundled
Python with a managed venv, so a working Hermes usually has no bare ``pip`` on
PATH: the probe failed and the agent fell through to the MCP branch. A bare
``pip install`` also lands where Hermes doesn't import from (hermes-setup.md
Install procedure steps 1-2 are the supported path).
"""
from __future__ import annotations

import re
from pathlib import Path

GUIDES = Path(__file__).resolve().parents[2] / "docs" / "guides"


def _hermes_branch() -> str:
    text = (GUIDES / "install.md").read_text(encoding="utf-8")
    m = re.search(r"### You are a Hermes agent\n(.*?)\n### ", text, re.DOTALL)
    assert m, "install.md lost its '### You are a Hermes agent' branch"
    return m.group(1)


def test_hermes_probe_does_not_require_pip_on_path():
    branch = _hermes_branch()
    assert "command -v hermes" in branch
    assert "command -v pip" not in branch


def test_hermes_branch_routes_to_hermes_setup_without_bare_pip_install():
    branch = _hermes_branch()
    assert "hermes-setup.md" in branch
    assert not re.search(r"Install is `pip install totalreclaw`", branch)


def test_openclaw_guide_runtime_probe_does_not_require_pip():
    text = (GUIDES / "openclaw-setup.md").read_text(encoding="utf-8")
    assert "command -v hermes && command -v pip" not in text
