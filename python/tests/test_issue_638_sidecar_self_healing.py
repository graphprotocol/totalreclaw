"""Issue #638 — the MemoryProvider sidecar must be a full self-healing
plugin file, not a bare re-export stub.

The old ``_SIDECAR_CONTENT`` was a 3-line re-export. Two defects followed:
no ``register()`` (tools/hooks silently dropped on the next plugin reload
once the sidecar replaced a git-plugin bootstrap), and no bootstrap (a
Hermes runtime regeneration that recreated the venv without the pip
package killed the import and the plugin vanished with no error
anywhere). The resilient edition self-heals the import and, failing
that, registers a loud degraded ``totalreclaw_status`` tool.

Three layers of coverage here:

* static — the template carries the marker, the provider re-export, a
  ``register()`` delegation, the ``ImportError`` self-heal, and the
  degraded tool.
* compile — the template is syntactically valid Python.
* exec — the template actually RUNS in three modes: healthy (package
  importable), self-heal success (import fails once, pip reinstall
  succeeds, retry import succeeds), and degraded (import fails, reinstall
  fails → loud diagnostic tool instead of a silent skip).
"""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from totalreclaw.hermes import install_memory_provider as imp

# Imported at module scope so the exec-sandbox retry path finds it cached
# in sys.modules once the self-heal fake "installs" it.
import totalreclaw.hermes.memory_provider  # noqa: F401


class TestSidecarStaticGuards:
    def test_marker_preserved(self):
        assert imp._SIDECAR_CONTENT.startswith(imp._SIDECAR_MARKER)

    def test_carries_provider_reexport(self):
        assert "TotalReclawMemoryProvider" in imp._SIDECAR_CONTENT

    def test_carries_register_delegation(self):
        # Defect #1: the bare stub had no register() — tools/hooks died
        # on the next reload wherever the sidecar replaced a git-plugin
        # bootstrap file.
        assert "def register(ctx):" in imp._SIDECAR_CONTENT
        assert "from totalreclaw.hermes import register as _register" in (
            imp._SIDECAR_CONTENT
        )

    def test_carries_importerror_self_heal(self):
        # Defect #2: regeneration wipes the venv package — the sidecar
        # must attempt a bounded reinstall into the RUNNING interpreter.
        body = imp._SIDECAR_CONTENT
        assert "except ImportError" in body
        assert "sys.executable" in body
        assert '"-m"' in body and '"pip"' in body and '"install"' in body
        assert "totalreclaw" in body

    def test_carries_degraded_status_tool(self):
        # Loud failure: a diagnostic tool instead of a silent plugin skip.
        body = imp._SIDECAR_CONTENT
        assert "_DEGRADED_STATUS_SCHEMA" in body
        assert '"totalreclaw_status"' in body
        assert "degraded" in body

    def test_no_phrase_material(self):
        # Phrase-safety invariant: the sidecar template must never carry
        # credential material or key handling.
        lowered = imp._SIDECAR_CONTENT.lower()
        assert "mnemonic" not in lowered
        assert "recovery_phrase" not in lowered
        assert "phrase" not in lowered


class TestSidecarCompiles:
    def test_template_is_valid_python(self):
        compile(imp._SIDECAR_CONTENT, "<totalreclaw-sidecar>", "exec")


class _FakeCtx:
    """Records register_tool calls; stands in for the Hermes plugin ctx."""

    def __init__(self):
        self.tools = {}

    def register_tool(self, *, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}


def _exec_sidecar():
    ns = {"__name__": "totalreclaw_sidecar_under_test"}
    exec(imp._SIDECAR_CONTENT, ns)
    return ns


class TestSidecarExecHealthy:
    def test_success_path_exports_provider_and_delegates_register(self, monkeypatch):
        # Redirect the delegation target so the REAL register() (heavy:
        # shared state, welcome, auto-migrate, ~15 tools) never runs.
        calls = []
        monkeypatch.setattr(
            "totalreclaw.hermes.register", lambda ctx: calls.append(ctx)
        )
        ns = _exec_sidecar()

        assert ns["_LOAD_ERROR"] is None
        from totalreclaw.hermes.memory_provider import TotalReclawMemoryProvider
        assert ns["TotalReclawMemoryProvider"] is TotalReclawMemoryProvider

        ctx = _FakeCtx()
        ns["register"](ctx)
        assert calls == [ctx]
        # Healthy path registers NO degraded tool of its own.
        assert ctx.tools == {}


class TestSidecarExecSelfHeal:
    def test_reinstall_recovers_the_import(self, monkeypatch):
        # Simulate the regenerated venv by blocking the FULL dotted name
        # the template imports — setting only the parent entry to None is
        # NOT enough: `from a.b.c import X` consults sys.modules for
        # "a.b.c" first and never touches the parent when cached. The
        # fake pip "install" then restores the real modules so the
        # template's retry import succeeds.
        real_totalreclaw = sys.modules["totalreclaw"]
        real_provider_mod = sys.modules["totalreclaw.hermes.memory_provider"]
        monkeypatch.setitem(sys.modules, "totalreclaw", None)
        monkeypatch.setitem(
            sys.modules, "totalreclaw.hermes.memory_provider", None
        )

        heal_calls = []

        def fake_run(cmd, **kw):
            assert "pip" in cmd and "install" in cmd  # self-heal, not something else
            heal_calls.append(cmd)
            sys.modules["totalreclaw"] = real_totalreclaw
            sys.modules["totalreclaw.hermes.memory_provider"] = real_provider_mod
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        ns = _exec_sidecar()

        # The heal must actually have run (guards against the import
        # succeeding via cache and the test passing vacuously).
        assert len(heal_calls) == 1
        assert ns["_LOAD_ERROR"] is None
        assert (
            ns["TotalReclawMemoryProvider"]
            is real_provider_mod.TotalReclawMemoryProvider
        )


class TestSidecarExecDegraded:
    def test_failed_reinstall_registers_loud_status_tool(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "totalreclaw", None)
        monkeypatch.setitem(
            sys.modules, "totalreclaw.hermes.memory_provider", None
        )

        def fake_run(cmd, **kw):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="Network is unreachable"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        ns = _exec_sidecar()

        # Degraded mode: no provider class, but the module loaded and the
        # failure is recorded loudly.
        assert ns["TotalReclawMemoryProvider"] is None
        assert ns["_LOAD_ERROR"] is not None
        assert "Network is unreachable" in ns["_LOAD_ERROR"]
        assert "exited 1" in ns["_LOAD_ERROR"]

        ctx = _FakeCtx()
        ns["register"](ctx)
        assert list(ctx.tools) == ["totalreclaw_status"]
        assert ctx.tools["totalreclaw_status"]["toolset"] == "totalreclaw"

        result = ctx.tools["totalreclaw_status"]["handler"]({})
        assert result["ok"] is False
        assert result["degraded"] is True
        assert "missing from this Hermes venv" in result["error"]
        assert "pip install" in result["fix"]
        assert result["detail"] == ns["_LOAD_ERROR"]

    def test_degraded_register_survives_broken_ctx(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "totalreclaw", None)
        monkeypatch.setitem(
            sys.modules, "totalreclaw.hermes.memory_provider", None
        )

        def fake_run(cmd, **kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="no network")

        monkeypatch.setattr(subprocess, "run", fake_run)
        ns = _exec_sidecar()

        class BrokenCtx:
            def register_tool(self, **kw):
                raise TypeError("ctx shape changed upstream")

        # Must not raise — the degraded path logs instead. (Guarded by the
        # full-name block above: with the import genuinely failing, the
        # template takes the DEGRADED register, never the real one.)
        with caplog.at_level("ERROR"):
            ns["register"](BrokenCtx())
        assert any(
            "degraded status tool registration failed" in r.message
            for r in caplog.records
        )
