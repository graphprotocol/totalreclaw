"""Hermes plugin manifest and SKILL.md versions must match the package version.

plugin.yaml once shipped a hardcoded stale ``version: 2.3.1rc26`` (SKILL.md had
the same drift). The publish workflow rewrites both to the (RC-suffixed)
pyproject version; in source they must equal the base ``[project].version``.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
HERMES = ROOT / "src/totalreclaw/hermes"


def _pyproject_version() -> str:
    pyproject = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version[ \t]*=[ \t]*"([^"]+)"', pyproject, flags=re.MULTILINE)
    assert m, "pyproject.toml version not found"
    return m.group(1)


def test_plugin_yaml_version_matches_pyproject():
    manifest = (HERMES / "plugin.yaml").read_text()
    pm = re.search(r"^version:[ \t]*(\S+)[ \t]*$", manifest, flags=re.MULTILINE)
    assert pm, "plugin.yaml top-level version not found"
    assert pm.group(1).strip("'\"") == _pyproject_version()


def test_skill_md_frontmatter_version_matches_pyproject():
    text = (HERMES / "SKILL.md").read_text()
    parts = text.split("---\n", 2)
    assert text.startswith("---\n") and len(parts) == 3, "SKILL.md frontmatter not found"
    sm = re.search(r"^version:[ \t]*(\S+)[ \t]*$", parts[1], flags=re.MULTILINE)
    assert sm, "SKILL.md frontmatter version not found"
    assert sm.group(1).strip("'\"") == _pyproject_version()
