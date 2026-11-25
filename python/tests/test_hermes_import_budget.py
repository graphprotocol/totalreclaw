"""Importing the Hermes plugin entry point must stay light (internal #726).

Hermes gives import + register() a hard 10 s budget; the write stack
(client, userop, eth_abi, numpy) used to be imported eagerly and blew it on a
cold first import after ``pip install``.
"""
from __future__ import annotations

import subprocess
import sys

_HEAVY = ["totalreclaw.client", "totalreclaw.operations", "totalreclaw.userop",
          "totalreclaw.embedding", "numpy", "eth_abi", "onnxruntime"]


def test_plugin_import_does_not_load_write_stack() -> None:
    code = (
        "import sys, totalreclaw.hermes\n"
        f"print([m for m in {_HEAVY!r} if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout


def test_lazy_public_names_still_resolve() -> None:
    import totalreclaw

    assert totalreclaw.TotalReclaw.__name__ == "TotalReclaw"
    assert issubclass(totalreclaw.RelayReadBlocked, Exception)
    assert "TotalReclaw" in dir(totalreclaw)
