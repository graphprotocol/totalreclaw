"""Hermes 0.22+ must load the ``totalreclaw`` entry point as a tool plugin.

Hermes classifies an entry-point plugin without importing it, by a plain
substring scan of the target module's first 8 KiB
(``hermes_cli.plugins_manifest._detect_kind_from_source``). Any memory-,
cron- or computer-use-provider marker, comments included, makes the plugin
``exclusive``: the general loader skips it and it only loads once it is the
active ``memory.provider``. A single comment naming the provider base class
did exactly that, so a fresh install had no ``totalreclaw_pair`` tool and
could not reach account setup.

Also covers the websockets requirement: the old ``<14`` cap downgraded
Hermes' own websockets 15 and broke every ``hermes chat``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - requires-python is >=3.11
    tomllib = pytest.importorskip("tomli")

from packaging.requirements import Requirement

_PYTHON_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _PYTHON_ROOT / "pyproject.toml"

# Mirrors the marker tuple in Hermes' _detect_kind_from_source (v0.22.5).
_HERMES_EXCLUSIVE_MARKERS = (
    "register_memory_provider",
    "MemoryProvider",
    "register_cron_scheduler",
    "CronScheduler",
    "register_computer_use_provider",
    "ComputerUseProvider",
)


def _pyproject() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _entry_point_module() -> str:
    eps = _pyproject()["project"]["entry-points"]["hermes_agent.plugins"]
    return eps["totalreclaw"].split(":", 1)[0].strip()


def _module_source(module: str) -> str:
    path = _PYTHON_ROOT / "src" / Path(*module.split("."))
    path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    return path.read_text(encoding="utf-8")


def test_entry_point_targets_the_plugin_package():
    assert _entry_point_module() == "totalreclaw.hermes"


@pytest.mark.parametrize("marker", _HERMES_EXCLUSIVE_MARKERS)
def test_entry_point_module_has_no_exclusive_marker(marker):
    # The whole file, not just the first 8 KiB, so a marker can't creep back
    # in as the file grows past or shrinks under Hermes' scan window.
    source = _module_source(_entry_point_module())
    assert marker not in source, (
        f"{marker!r} appears in the hermes_agent.plugins entry-point module. "
        "Hermes 0.22+ would classify the plugin as an exclusive memory "
        "provider and skip loading its tools until it is activated. Reword "
        "the comment/docstring (e.g. 'memory-provider')."
    )


def _websockets_requirement() -> Requirement:
    for dep in _pyproject()["project"]["dependencies"]:
        req = Requirement(dep)
        if req.name == "websockets":
            return req
    raise AssertionError("websockets is no longer a dependency")


@pytest.mark.parametrize("version", ["12.0", "13.1", "15.0.1", "17.3"])
def test_websockets_requirement_accepts_host_versions(version):
    # 15.0.1 is what Hermes 0.22 pins. Installing totalreclaw must never
    # force a different websockets on the host.
    assert _websockets_requirement().specifier.contains(version)


def test_websockets_has_no_upper_cap():
    assert not any(
        spec.operator in ("<", "<=", "==", "~=")
        for spec in _websockets_requirement().specifier
    )
