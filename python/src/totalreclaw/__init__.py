"""TotalReclaw — End-to-end encrypted memory for AI agents."""

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("totalreclaw")
except Exception:
    # Fallback string used in editable / source-tree installs where the
    # installed-package metadata is absent. Must match pyproject.toml —
    # test_version.py enforces a semver-shape string; the live value in
    # a pip-installed wheel comes from importlib.metadata above.
    __version__ = "2.3.1rc4"

from .client import TotalReclaw

__all__ = ["TotalReclaw", "__version__"]
