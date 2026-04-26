"""F3 (rc.24) — packaging integrity regression test.

Background
----------
The rc.23 wheel published to PyPI was MISSING ``retype_setscope.py`` even
though the file existed on ``main`` and PR #135 had merged it. Root
cause: ``pyproject.toml`` had no explicit
``[tool.setuptools.packages.find]`` stanza, so ``setuptools.build_meta``
ran auto-discovery against the ``python/src/`` directory. Auto-discovery
hit a corner case (multiple sibling dirs in the project root + fresh top-
level module added without other build-config touching it) and silently
dropped the new file from the wheel. Result: ``totalreclaw_retype`` and
``totalreclaw_set_scope`` registrations in ``hermes/__init__.py`` failed
at import (``ModuleNotFoundError``), which the plugin loader caught and
swallowed — so the tools just silently disappeared at runtime.

What this test guards
---------------------
For every ``.py`` file under ``python/src/totalreclaw/``, assert that the
file IS present inside the built wheel. We build the wheel into a temp
directory using ``python -m build --wheel`` and then list its contents
via ``zipfile``.

This is intentionally a strict file-by-file check (rather than just
checking import-ability of a few modules), because the rc.23 failure
mode was that a NEW file silently disappeared while the rest of the
package looked healthy. We want every future ``add a new file under
totalreclaw/`` change to either land in the wheel or fail this test
loudly at CI time.

The build step is slow (~20s) so the test is gated on the
``TOTALRECLAW_RUN_PACKAGING_TESTS=1`` env var by default; CI can set it.
For developer-local runs use ``pytest -m packaging`` once the marker
ships, or just set the env var.

Run locally::

    cd python && TOTALRECLAW_RUN_PACKAGING_TESTS=1 pytest \
        tests/test_packaging_integrity.py -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent  # python/
SRC_ROOT = REPO_ROOT / "src" / "totalreclaw"


def _gather_src_py_files() -> list[Path]:
    """Return relative paths of every .py file under src/totalreclaw/.

    ``__pycache__`` directories are excluded — those are byte-compiled
    artifacts and should never appear in the wheel.
    """
    out: list[Path] = []
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        out.append(path.relative_to(SRC_ROOT))
    return sorted(out)


def _build_wheel_into(tmp_dir: Path) -> Path:
    """Build a wheel in ``tmp_dir/dist/`` and return the .whl path.

    Uses ``python -m build --wheel`` so we exercise the SAME path PyPI
    will use. Returns the first .whl in dist (there's exactly one).
    """
    dist_dir = tmp_dir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    # Run from a copy of the source tree so we don't pollute the real
    # python/ working dir with a build/ artifact.
    src_copy = tmp_dir / "src_copy"
    shutil.copytree(
        REPO_ROOT,
        src_copy,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            ".pytest_cache",
            ".venv",
            "dist",
            "build",
            "*.egg-info",
        ),
    )

    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=src_copy,
        check=True,
        # Pipe stdout/stderr; only print on failure to keep test output tidy.
        capture_output=True,
    )

    wheels = list(dist_dir.glob("totalreclaw-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got: {wheels}"
    return wheels[0]


@pytest.mark.skipif(
    os.environ.get("TOTALRECLAW_RUN_PACKAGING_TESTS") != "1",
    reason=(
        "Slow (~20s for `python -m build`). Set "
        "TOTALRECLAW_RUN_PACKAGING_TESTS=1 to enable. CI sets this in the "
        "publish workflow's pre-publish gate."
    ),
)
def test_wheel_contains_every_source_py_file():
    """Every src/totalreclaw/**/*.py file MUST land in the built wheel."""
    src_files = _gather_src_py_files()
    assert src_files, "no .py files found under src/totalreclaw — sanity check"

    # The rc.23 ship-stopper — make sure the regression case is covered.
    assert Path("retype_setscope.py") in src_files, (
        "regression sanity: src/totalreclaw/retype_setscope.py must exist"
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        wheel_path = _build_wheel_into(tmp_path)
        with zipfile.ZipFile(wheel_path) as zf:
            wheel_names = set(zf.namelist())

    # Every src .py should appear at "totalreclaw/<rel>" inside the wheel.
    missing: list[str] = []
    for rel in src_files:
        wheel_member = "totalreclaw/" + rel.as_posix()
        if wheel_member not in wheel_names:
            missing.append(wheel_member)

    assert not missing, (
        f"wheel is missing {len(missing)} expected .py file(s) — this is "
        f"the F3 rc.23 ship-stopper class: setuptools auto-discovery "
        f"dropped some modules. Missing files:\n  "
        + "\n  ".join(missing[:20])
        + ("\n  ..." if len(missing) > 20 else "")
    )


@pytest.mark.skipif(
    os.environ.get("TOTALRECLAW_RUN_PACKAGING_TESTS") != "1",
    reason="Slow — see test_wheel_contains_every_source_py_file",
)
def test_wheel_contains_retype_setscope_specifically():
    """Targeted regression shield for the EXACT rc.23 missing file.

    Even if some bulk packaging-test logic is later refactored away,
    this single-file assertion stays — it pins the very file whose
    absence caused the rc.23 NO-GO so any future regression of the
    same shape fails this specific test name.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        wheel_path = _build_wheel_into(tmp_path)
        with zipfile.ZipFile(wheel_path) as zf:
            names = set(zf.namelist())

    assert "totalreclaw/retype_setscope.py" in names, (
        "rc.23 F3 regression: retype_setscope.py is missing from the wheel "
        "— check pyproject.toml [tool.setuptools.packages.find] config"
    )


def test_pyproject_has_explicit_packages_find():
    """Cheap structural check that the pyproject.toml explicit-find
    stanza is present. Doesn't build a wheel; runs in milliseconds.

    Even without the heavyweight build test, this guards against
    accidentally regressing the pyproject config back to "no explicit
    find stanza" (the rc.23 root cause).
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert "[tool.setuptools.packages.find]" in pyproject, (
        "pyproject.toml must declare [tool.setuptools.packages.find] "
        "explicitly — auto-discovery silently dropped retype_setscope.py "
        "in rc.23 (see F3 in QA-hermes-RC-2.3.1-rc.23-20260426.md)"
    )
    # The where-line + include-line are both required for the fix to
    # work as designed; pin both.
    assert 'where = ["src"]' in pyproject, (
        "[tool.setuptools.packages.find] must set where = [\"src\"] for the "
        "src-layout to be picked up correctly"
    )
    assert 'include = ["totalreclaw*"]' in pyproject, (
        "[tool.setuptools.packages.find] must set include = [\"totalreclaw*\"] "
        "to capture every totalreclaw subpackage"
    )


def test_pyproject_version_no_rc_suffix():
    """The committed pyproject.toml version must NEVER carry an rc<N>
    suffix on main. The publish-python-client.yml workflow appends rcN
    at build time when release-type=rc; an rcN literal in the file
    means the workflow's regex re-applied an off-by-one rc number AND
    a stable build would mis-publish as a pre-release.

    Regression shield for the rc.23 F3 finding (workflow mutation
    leaked back into main as ``version = "2.3.1rc24"``).
    """
    import re

    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, flags=re.MULTILINE)
    assert m is not None, "pyproject.toml has no version= line"
    version = m.group(1)
    assert "rc" not in version, (
        f"pyproject.toml version is {version!r} — must NOT carry an rc "
        f"suffix on main. The publish workflow appends rcN at RC-build "
        f"time; committing the suffix breaks both stable builds and the "
        f"workflow's mutation logic. See F3 in "
        f"QA-hermes-RC-2.3.1-rc.23-20260426.md."
    )
