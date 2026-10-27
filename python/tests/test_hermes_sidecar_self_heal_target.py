"""The sidecar's self-heal must install the right thing in the right place.

On Hermes 0.22+ the gateway runs on Hermes' bundled Python but imports its
dependencies from a separate managed venv. The old self-heal ran
``sys.executable -m pip install --pre totalreclaw``, which:

* installed into the bundled Python's own site-packages, not the venv the
  process imports from, and (with PYTHONPATH pointing at that venv) skipped
  dependencies such as httpx as "already installed";
* resolved the newest pre-release, putting an RC (staging relay) on a
  production install;
* reported success while ``import totalreclaw`` still failed, and the
  failure only reached the logs.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import totalreclaw
from totalreclaw.hermes import install_memory_provider as imp

# Cached in sys.modules so the exec'd sidecar can find it after a fake heal.
import totalreclaw.hermes.memory_provider  # noqa: F401


class _Ctx:
    def __init__(self):
        self.tools = {}
        self.hooks = {}

    def register_tool(self, *, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def _exec(content: str) -> dict:
    ns = {"__name__": "totalreclaw_sidecar_under_test"}
    exec(content, ns)
    return ns


def _block_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "totalreclaw", None)
    monkeypatch.setitem(sys.modules, "totalreclaw.hermes.memory_provider", None)


def _fake_venv(tmp_path: Path) -> tuple[Path, Path]:
    venv = tmp_path / "installs" / "env" / "venv"
    site = venv / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /opt/hermes-python/bin\n")
    (venv / "bin").mkdir()
    py = venv / "bin" / "python"
    py.write_text("")
    return site, py


def test_written_sidecar_pins_the_installed_version():
    content = imp._sidecar_content()
    assert f"_SIDECAR_VERSION = {totalreclaw.__version__!r}" in content
    assert imp._VERSION_PLACEHOLDER not in content.split("_SIDECAR_VERSION = ", 1)[1].split("\n", 1)[0]
    compile(content, "<sidecar>", "exec")


@pytest.mark.parametrize("version,expected", [
    ("2.5.2", "totalreclaw==2.5.2"),
    ("2.5.3rc3", "totalreclaw==2.5.3rc3"),
    ("", "totalreclaw"),
])
def test_requirement_follows_the_version_that_wrote_the_sidecar(version, expected):
    content = imp._SIDECAR_CONTENT.replace(
        f'_SIDECAR_VERSION = "{imp._VERSION_PLACEHOLDER}"',
        f"_SIDECAR_VERSION = {version!r}",
    )
    ns = _exec(content)  # healthy import path: nothing installed
    assert ns["_requirement"]() == expected


def test_self_heal_targets_the_venv_on_sys_path_with_deps_and_no_pre(
    tmp_path, monkeypatch
):
    site, venv_python = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path / "hermes-agent"), str(site), *sys.path])
    monkeypatch.setenv("PYTHONPATH", str(site))
    monkeypatch.setenv("PIP_PRE", "1")
    monkeypatch.setenv("PIP_NO_DEPS", "1")
    real = sys.modules["totalreclaw"], sys.modules["totalreclaw.hermes.memory_provider"]
    _block_import(monkeypatch)

    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("env") or {}))
        if "install" in cmd:
            sys.modules["totalreclaw"], sys.modules["totalreclaw.hermes.memory_provider"] = real
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    content = imp._SIDECAR_CONTENT.replace(
        f'_SIDECAR_VERSION = "{imp._VERSION_PLACEHOLDER}"', "_SIDECAR_VERSION = '2.5.2'"
    )
    ns = _exec(content)

    assert ns["_LOAD_ERROR"] is None
    installs = [(cmd, env) for cmd, env in calls if "install" in cmd]
    assert len(installs) == 1
    cmd, env = installs[0]
    assert cmd[:5] == [sys.executable, "-m", "pip", "--python", str(venv_python)]
    assert cmd[-1] == "totalreclaw==2.5.2"
    assert "--pre" not in cmd and "--no-deps" not in cmd
    for key in ("PYTHONPATH", "PIP_PRE", "PIP_NO_DEPS"):
        assert key not in env


def test_without_pip_in_the_runtime_it_bootstraps_the_target(tmp_path, monkeypatch):
    site, venv_python = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "path", [str(site), *sys.path])
    _block_import(monkeypatch)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        rc = 1 if cmd[-1] == "--version" else 0  # no pip anywhere yet
        return SimpleNamespace(returncode=rc, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ns = _exec(imp._SIDECAR_CONTENT)
    assert [str(venv_python), "-m", "ensurepip", "--upgrade"] in calls
    install = [c for c in calls if "install" in c][-1]
    assert install[:4] == [str(venv_python), "-m", "pip", "install"]
    # The fake install restored nothing, so the import still fails and the
    # sidecar says so instead of reporting success.
    assert "still fails" in ns["_LOAD_ERROR"]


def test_import_still_failing_after_install_is_degraded_and_loud(monkeypatch):
    _block_import(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    ns = _exec(imp._SIDECAR_CONTENT)
    assert ns["TotalReclawMemoryProvider"] is None
    assert "still fails" in ns["_LOAD_ERROR"]

    ctx = _Ctx()
    ns["register"](ctx)
    status = ctx.tools["totalreclaw_status"]({})
    assert status["degraded"] is True
    assert "pip" in status["fix"] and "totalreclaw" in status["fix"]
    notice = ctx.hooks["pre_llm_call"](is_first_turn=True)
    assert "OFFLINE" in notice["context"] and "Tell the user" in notice["context"]


def _install_fake_plugin_manager(monkeypatch, plugins: dict):
    mod = ModuleType("hermes_cli.plugins")
    mod.get_plugin_manager = lambda: SimpleNamespace(_plugins=plugins)
    monkeypatch.setitem(sys.modules, "hermes_cli", ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", mod)


def test_skips_registration_when_entry_point_plugin_is_live(monkeypatch):
    calls = []
    monkeypatch.setattr("totalreclaw.hermes.register", lambda ctx: calls.append(ctx))
    _install_fake_plugin_manager(monkeypatch, {
        "totalreclaw": SimpleNamespace(
            enabled=True,
            manifest=SimpleNamespace(source="entrypoint"),
            tools_registered=["totalreclaw_pair"],
        ),
    })
    ns = _exec(imp._SIDECAR_CONTENT)
    assert ns["register"](_Ctx()) is None
    assert calls == []  # hooks would otherwise fire twice


@pytest.mark.parametrize("plugins", [
    {},
    {"totalreclaw": SimpleNamespace(enabled=False, manifest=SimpleNamespace(source="entrypoint"), tools_registered=[])},
])
def test_registers_everything_when_entry_point_plugin_is_not_live(monkeypatch, plugins):
    calls = []
    monkeypatch.setattr("totalreclaw.hermes.register", lambda ctx: calls.append(ctx))
    _install_fake_plugin_manager(monkeypatch, plugins)
    ns = _exec(imp._SIDECAR_CONTENT)
    ctx = _Ctx()
    ns["register"](ctx)
    assert calls == [ctx]


def test_refresh_rewrites_outdated_managed_sidecar(tmp_path):
    path = tmp_path / "plugins" / "totalreclaw" / "__init__.py"
    path.parent.mkdir(parents=True)
    path.write_text(imp._SIDECAR_MARKER + "\n# old content\n")
    assert imp.refresh_sidecar(path) is True
    assert path.read_text() == imp._sidecar_content()
    assert imp.refresh_sidecar(path) is False  # already current


def test_refresh_leaves_hand_edited_file_alone(tmp_path):
    path = tmp_path / "__init__.py"
    path.write_text("# my own plugin\n")
    assert imp.refresh_sidecar(path) is False
    assert path.read_text() == "# my own plugin\n"


def test_install_sidecar_writes_pinned_content(tmp_path):
    path = imp.install_sidecar(hermes_home=tmp_path)
    assert path.read_text(encoding="utf-8") == imp._sidecar_content()


def test_classic_venv_uses_the_running_interpreter(monkeypatch):
    # Older Hermes runs from its own venv: the first site-packages on
    # sys.path belongs to sys.prefix, so pip runs plainly under it.
    site = Path(sys.prefix) / "lib" / "python3.x" / "site-packages"
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    monkeypatch.setattr(Path, "is_file", lambda self: self.name == "pyvenv.cfg")
    monkeypatch.setattr(sys, "path", [str(site)])
    ns = _exec(imp._SIDECAR_CONTENT)
    assert ns["_target_python"]() == sys.executable
