"""The Hermes plugin manifest version must match the package version.

plugin.yaml once shipped a hardcoded stale ``version: 2.3.1rc26``. The publish
workflow rewrites it to the (RC-suffixed) pyproject version; in source it must
equal the base ``[project].version``.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_plugin_yaml_version_matches_pyproject():
    pyproject = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, flags=re.MULTILINE)
    assert m, "pyproject.toml version not found"
    manifest = (ROOT / "src/totalreclaw/hermes/plugin.yaml").read_text()
    pm = re.search(r"^version:\s*(\S+)\s*$", manifest, flags=re.MULTILINE)
    assert pm, "plugin.yaml top-level version not found"
    assert pm.group(1).strip("'\"") == m.group(1)
