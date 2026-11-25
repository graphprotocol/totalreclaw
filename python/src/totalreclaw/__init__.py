"""TotalReclaw — End-to-end encrypted memory for AI agents."""

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("totalreclaw")
except Exception:
    # Fallback string used in editable / source-tree installs where the
    # installed-package metadata is absent. Must match pyproject.toml —
    # test_version.py enforces a semver-shape string; the live value in
    # a pip-installed wheel comes from importlib.metadata above.
    __version__ = "2.3.5"

# Heavy submodules (client -> operations -> userop/eth_abi/numpy/...) are
# imported lazily on first attribute access (PEP 562). Importing a light
# submodule such as ``totalreclaw.hermes`` must not pay ~2 s of import cost
# (Hermes gives plugin load a hard 10 s budget). ``from totalreclaw import
# TotalReclaw`` and friends keep working unchanged.
_LAZY = {
    "TotalReclaw": ".client",
    "RelayReadError": ".relay",
    "RelayReadBlocked": ".relay",
    "RelayReadQuotaExceeded": ".relay",
    "RelayRateLimited": ".relay",
}


def __getattr__(name):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(mod, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(list(globals()) + list(_LAZY))


__all__ = [
    "TotalReclaw",
    "__version__",
    "RelayReadError",
    "RelayReadBlocked",
    "RelayReadQuotaExceeded",
    "RelayRateLimited",
]
