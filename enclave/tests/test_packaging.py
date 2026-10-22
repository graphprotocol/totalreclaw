from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

import totalreclaw_enclave

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_pyproject() -> None:
    data = tomllib.loads(PYPROJECT.read_text())
    assert data["project"]["version"] == totalreclaw_enclave.__version__


def test_dependency_pins() -> None:
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    assert "mcp>=2.2,<3" in deps
    assert "totalreclaw>=2.5.2,<3" in deps
    assert "totalreclaw-core>=2.6.0,<3" in deps


def test_never_uploadable_to_pypi() -> None:
    classifiers = tomllib.loads(PYPROJECT.read_text())["project"]["classifiers"]
    assert "Private :: Do Not Upload" in classifiers


@pytest.mark.parametrize("module", ["mcp.server", "starlette", "uvicorn", "totalreclaw", "totalreclaw_core"])
def test_runtime_dependencies_import(module: str) -> None:
    importlib.import_module(module)
