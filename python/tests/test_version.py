"""Tests for top-level totalreclaw.__version__ export (added in 2.2.4)."""

import re


def test_version_string_exists():
    """totalreclaw.__version__ must be importable and non-empty."""
    import totalreclaw

    assert hasattr(totalreclaw, "__version__"), "__version__ missing from totalreclaw package"
    v = totalreclaw.__version__
    assert isinstance(v, str) and v.strip(), "__version__ must be a non-empty string"


def test_version_semver_shape():
    """__version__ must look like a semver string (MAJOR.MINOR.PATCH[...])."""
    import totalreclaw

    # Accept PEP 440 / semver — just requires at least X.Y.Z prefix.
    assert re.match(r"^\d+\.\d+\.\d+", totalreclaw.__version__), (
        f"__version__ does not look like a version string: {totalreclaw.__version__!r}"
    )


def test_version_in_all():
    """__version__ must appear in totalreclaw.__all__."""
    import totalreclaw

    assert "__version__" in totalreclaw.__all__, "__version__ not listed in totalreclaw.__all__"


def test_version_accessible_via_import_alias():
    """Callers can do `from totalreclaw import __version__` without ImportError."""
    from totalreclaw import __version__  # noqa: F401

    assert isinstance(__version__, str)
