"""2.5.3rc4: activation keeps Hermes' built-in memory beside TotalReclaw.

Hermes' MemoryManager always keeps its builtin provider (USER.md + MEMORY.md)
next to the one external provider, and TotalReclaw mirrors the built-in
memory tool's writes (``on_memory_write``). So activation now only sets
``memory.provider: totalreclaw``:

- the default leaves ``memory_enabled`` / ``user_profile_enabled`` alone,
  except that it turns back on what an activation before 2.5.3rc4 turned off
  (both ``false``, ``provider: totalreclaw``, no exclusive marker);
- ``--exclusive`` keeps the old behaviour and marks the switches, so a later
  default activation leaves them off;
- pairing activates (non-exclusive) on both pairing paths, including the
  relay completion sidecar (#614), unless another provider is active.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from totalreclaw.hermes import install_memory_provider as imp


def _write(home: Path, text: str) -> Path:
    cfg = imp.config_path(home)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    return cfg


def _memory(home: Path) -> dict:
    return yaml.safe_load(imp.config_path(home).read_text(encoding="utf-8"))["memory"]


# What `activate-memory-provider` left behind before 2.5.3rc4.
_PRE_RC4_ACTIVATED = (
    "memory:\n"
    "  # Agent's personal notes\n"
    "  memory_enabled: false\n"
    "  # User profile\n"
    "  user_profile_enabled: false\n"
    "  provider: totalreclaw\n"
)


class TestDefaultActivation:
    def test_leaves_builtin_switches_untouched(self, tmp_path: Path):
        cfg = _write(
            tmp_path,
            "memory:\n  memory_enabled: true   # notes\n  user_profile_enabled: true\n",
        )
        result = imp.install_and_activate(hermes_home=tmp_path)
        body = cfg.read_text(encoding="utf-8")
        assert "  memory_enabled: true   # notes\n" in body
        assert "  user_profile_enabled: true\n" in body
        assert _memory(tmp_path)["provider"] == "totalreclaw"
        assert result["activated"] is True
        assert result["exclusive"] is False
        assert result["builtin_disabled"] is False
        assert result["builtin_restored"] is False

    def test_fresh_config_writes_only_the_provider(self, tmp_path: Path):
        imp.install_and_activate(hermes_home=tmp_path)
        assert _memory(tmp_path) == {"provider": "totalreclaw"}

    def test_user_turned_off_builtin_is_respected(self, tmp_path: Path):
        # The user switched the built-in off themselves while another
        # provider was active: not TotalReclaw's write, so not reverted.
        _write(
            tmp_path,
            "memory:\n  provider: none\n  memory_enabled: false\n  user_profile_enabled: false\n",
        )
        result = imp.install_and_activate(hermes_home=tmp_path)
        assert _memory(tmp_path) == {
            "provider": "totalreclaw",
            "memory_enabled": False,
            "user_profile_enabled": False,
        }
        assert result["builtin_disabled"] is True
        assert result["builtin_restored"] is False

    def test_only_one_switch_off_is_not_an_old_activation(self, tmp_path: Path):
        _write(
            tmp_path,
            "memory:\n  provider: totalreclaw\n  memory_enabled: false\n  user_profile_enabled: true\n",
        )
        result = imp.install_and_activate(hermes_home=tmp_path)
        assert _memory(tmp_path)["memory_enabled"] is False
        assert result["builtin_restored"] is False

    def test_restores_what_a_pre_rc4_activation_turned_off(self, tmp_path: Path):
        cfg = _write(tmp_path, _PRE_RC4_ACTIVATED)
        result = imp.install_and_activate(hermes_home=tmp_path)
        assert _memory(tmp_path) == {
            "provider": "totalreclaw",
            "memory_enabled": True,
            "user_profile_enabled": True,
        }
        body = cfg.read_text(encoding="utf-8")
        assert "  # Agent's personal notes\n" in body  # comments survive
        assert result["builtin_restored"] is True
        assert result["builtin_disabled"] is False

    def test_keeps_switches_an_exclusive_activation_turned_off(self, tmp_path: Path):
        imp.install_and_activate(hermes_home=tmp_path, exclusive=True)
        before = imp.config_path(tmp_path).read_text(encoding="utf-8")
        result = imp.install_and_activate(hermes_home=tmp_path)
        assert imp.config_path(tmp_path).read_text(encoding="utf-8") == before
        assert result["builtin_disabled"] is True
        assert result["builtin_restored"] is False

    def test_tools_only_touches_nothing(self, tmp_path: Path):
        original = "memory:\n  provider: honcho\n  memory_enabled: false\n  user_profile_enabled: false\n"
        cfg = _write(tmp_path, original)
        result = imp.install_and_activate(hermes_home=tmp_path, activate=False)
        assert cfg.read_text(encoding="utf-8") == original
        assert result["activated"] is False
        assert result["exclusive"] is False


class TestExclusiveActivation:
    def test_turns_builtin_off_with_marker(self, tmp_path: Path):
        cfg = _write(tmp_path, "memory:\n  memory_enabled: true\n  user_profile_enabled: true\n")
        result = imp.install_and_activate(hermes_home=tmp_path, exclusive=True)
        assert _memory(tmp_path) == {
            "provider": "totalreclaw",
            "memory_enabled": False,
            "user_profile_enabled": False,
        }
        body = cfg.read_text(encoding="utf-8")
        assert body.count(imp._EXCLUSIVE_MARKER) == 2
        assert result["exclusive"] is True
        assert result["builtin_disabled"] is True

    def test_exclusive_over_pre_rc4_config_marks_it(self, tmp_path: Path):
        _write(tmp_path, _PRE_RC4_ACTIVATED)
        imp.install_and_activate(hermes_home=tmp_path, exclusive=True)
        # Now marked: a later default activation keeps it off.
        result = imp.install_and_activate(hermes_home=tmp_path)
        assert result["builtin_disabled"] is True
        assert result["builtin_restored"] is False

    def test_hermes_cli_accepts_exclusive(self, tmp_path: Path, capsys):
        import json

        from totalreclaw.hermes.cli import main

        assert main(["activate-memory-provider", "--hermes-home", str(tmp_path), "--exclusive"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["exclusive"] is True and out["builtin_disabled"] is True

    def test_hermes_cli_default_keeps_builtin(self, tmp_path: Path, capsys):
        import json

        from totalreclaw.hermes.cli import main

        assert main(["activate-memory-provider", "--hermes-home", str(tmp_path)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["exclusive"] is False and out["builtin_disabled"] is False


class TestActivateAfterPairing:
    def test_no_hermes_home_is_skipped(self, tmp_path: Path):
        missing = tmp_path / "no-hermes"
        assert imp.activate_after_pairing(missing) == {
            "activated": False,
            "skipped": "no_hermes_home",
        }
        assert not missing.exists()

    @pytest.mark.parametrize("provider", ["honcho", "builtin"])
    def test_other_provider_is_left_for_the_user(self, tmp_path: Path, provider):
        original = f"memory:\n  provider: {provider}\n"
        cfg = _write(tmp_path, original)
        result = imp.activate_after_pairing(tmp_path)
        assert result["activated"] is False
        assert result["skipped"] == "other_provider"
        assert cfg.read_text(encoding="utf-8") == original
        assert not imp.sidecar_path(tmp_path).exists()

    @pytest.mark.parametrize("config", [None, "memory:\n  provider: none\n", "memory:\n  provider: totalreclaw\n"])
    def test_activates_non_exclusive(self, tmp_path: Path, config):
        tmp_path.mkdir(exist_ok=True)
        if config is not None:
            _write(tmp_path, config)
        result = imp.activate_after_pairing(tmp_path)
        assert result["activated"] is True
        assert result["exclusive"] is False
        assert imp.read_active_provider(tmp_path) == "totalreclaw"
        assert imp.sidecar_path(tmp_path).exists()
        mem = _memory(tmp_path)
        assert "memory_enabled" not in mem and "user_profile_enabled" not in mem

    def test_uses_hermes_home_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = imp.activate_after_pairing()
        assert result["activated"] is True
        assert imp.read_active_provider(tmp_path) == "totalreclaw"


class TestRelaySidecarActivates:
    """#614: the relay completion sidecar now activates the provider too."""

    @staticmethod
    def _run(monkeypatch, tmp_path: Path, outcome: dict, **activate_kwargs) -> MagicMock:
        import asyncio

        from totalreclaw.pair import completion_sidecar as cs

        session = MagicMock()
        session.token = "tok12345abcdef"
        session.url, session.pin, session.expires_at = "https://x", "123456", "later"

        async def fake_open(**kw):
            return session

        async def fake_await(sess, **kw):
            return outcome

        monkeypatch.setattr("totalreclaw.pair.remote_client.open_remote_pair_session", fake_open)
        monkeypatch.setattr("totalreclaw.pair.remote_client.await_phrase_upload", fake_await)
        monkeypatch.setattr(cs, "_handshake_path_for", lambda hid: tmp_path / f"{hid}.json")
        activate_kwargs.setdefault("return_value", {"activated": True})
        with patch(
            "totalreclaw.hermes.install_memory_provider.activate_after_pairing",
            **activate_kwargs,
        ) as act:
            asyncio.run(cs._drive_full_pair_session(handshake_id="h1", mode=None, relay_url=None))
        return act

    def test_activates_after_successful_pairing(self, monkeypatch, tmp_path):
        act = self._run(monkeypatch, tmp_path, {"state": "active", "account_id": "0xabc"})
        act.assert_called_once_with()

    def test_no_activation_when_pairing_failed(self, monkeypatch, tmp_path):
        act = self._run(monkeypatch, tmp_path, {"state": "error", "error": "bad"})
        act.assert_not_called()

    def test_activation_error_does_not_escape(self, monkeypatch, tmp_path):
        act = self._run(
            monkeypatch, tmp_path, {"state": "active"},
            side_effect=RuntimeError("Refusing to overwrite hand-edited sidecar"),
        )
        act.assert_called_once_with()


class TestRestoreAfterUpgrade:
    """Plugin load turns the built-in back on once, for exactly the pre-rc4
    pattern, so existing users don't have to re-pair."""

    @pytest.fixture(autouse=True)
    def _state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path / "state"))

    def test_restores_pre_rc4_pattern_once(self, tmp_path: Path, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="totalreclaw.hermes.install_memory_provider")
        home = tmp_path / "hermes"
        cfg = _write(home, _PRE_RC4_ACTIVATED)
        assert imp.restore_builtin_after_upgrade(home) is True
        assert _memory(home) == {
            "provider": "totalreclaw",
            "memory_enabled": True,
            "user_profile_enabled": True,
        }
        assert "  # Agent's personal notes\n" in cfg.read_text(encoding="utf-8")
        assert "Takes effect at the next gateway restart" in caplog.text
        marker = next((tmp_path / "state").glob("hermes-builtin-restore-*.done"))
        assert stat_mode(marker) == 0o600

        # The user later turns it off by hand: not overridden.
        _write(home, _PRE_RC4_ACTIVATED)
        assert imp.restore_builtin_after_upgrade(home) is False
        assert _memory(home)["memory_enabled"] is False

    @pytest.mark.parametrize("config", [
        "memory:\n  provider: totalreclaw\n  memory_enabled: true\n  user_profile_enabled: true\n",
        "memory:\n  provider: honcho\n  memory_enabled: false\n  user_profile_enabled: false\n",
        "memory:\n  provider: totalreclaw\n  memory_enabled: false\n  user_profile_enabled: true\n",
        "memory:\n  provider: totalreclaw\n"
        "  memory_enabled: false  # totalreclaw: --exclusive\n"
        "  user_profile_enabled: false  # totalreclaw: --exclusive\n",
    ])
    def test_other_configs_untouched(self, tmp_path: Path, config):
        home = tmp_path / "hermes"
        cfg = _write(home, config)
        assert imp.restore_builtin_after_upgrade(home) is False
        assert cfg.read_text(encoding="utf-8") == config

    def test_no_hermes_home_does_nothing(self, tmp_path: Path):
        assert imp.restore_builtin_after_upgrade(tmp_path / "missing") is False
        assert not (tmp_path / "state").exists()

    def test_never_raises(self, tmp_path: Path, monkeypatch):
        home = tmp_path / "hermes"
        _write(home, _PRE_RC4_ACTIVATED)
        monkeypatch.setattr(imp, "_update_memory_keys", lambda *a, **k: (_ for _ in ()).throw(OSError("ro fs")))
        assert imp.restore_builtin_after_upgrade(home) is False

    def test_plugin_load_calls_it_only_inside_hermes(self, monkeypatch):
        import sys

        import totalreclaw.hermes as hermes_pkg

        with patch.object(imp, "restore_builtin_after_upgrade") as restore:
            monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)
            hermes_pkg._restore_builtin_memory_after_upgrade()
            restore.assert_not_called()
            monkeypatch.setitem(sys.modules, "hermes_cli", MagicMock())
            hermes_pkg._restore_builtin_memory_after_upgrade()
            restore.assert_called_once_with()


def stat_mode(path: Path) -> int:
    import stat

    return stat.S_IMODE(path.stat().st_mode)
