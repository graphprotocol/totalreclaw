"""TotalReclaw — End-to-end encrypted memory for AI agents."""

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("totalreclaw")
except Exception:
    __version__ = "2.2.4"

from .client import TotalReclaw

__all__ = ["TotalReclaw", "__version__"]
