"""Unit tests for the Hermes MemoryProvider adapter (issue #275).

Covers the 9 unit tests + 3 integration tests from the Path B spec
§"Test plan". The integration tests exercise the install_memory_provider
module (sidecar drop, config write, status read) since Hermes' actual
``MemoryManager.set_active_provider`` is not importable in our test env.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from totalreclaw.hermes.state import PluginState
from totalreclaw.hermes import schemas
from totalreclaw.hermes.memory_provider import (
    TotalReclawMemoryProvider,
    _summarize_facts_for_compression,
)
from totalreclaw.hermes import install_memory_provider as imp


def _make_state(configured: bool = True) -> PluginState:
    """Build a PluginState with a stubbed-out client when ``configured``.

    No phrase is ever generated or stored — we set the ``_client``
    attribute directly to a MagicMock so ``is_configured()`` returns
    True without touching crypto / network / disk.
    """
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    if configured:
        state._client = MagicMock()
    return state


# ---------------------------------------------------------------------------
# 1. on_turn_start
# ---------------------------------------------------------------------------


class TestOnTurnStart:
    """Test 1 — on_turn_start increments / overrides turn counter when fired."""

    def test_overrides_to_supplied_turn_number(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        provider.on_turn_start(turn_number=5, message="hi")

        assert state.turn_count == 5

    def test_increments_when_turn_number_missing(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        # baseline
        state.increment_turn()
        state.increment_turn()
        assert state.turn_count == 2

        provider.on_turn_start(turn_number=None, message="hi")

        assert state.turn_count == 3


# ---------------------------------------------------------------------------
# 2-4. on_pre_compress
# ---------------------------------------------------------------------------


class TestOnPreCompress:
    """Tests 2-4 — pre-compaction extraction + summary semantics."""

    def test_empty_string_when_no_unprocessed_messages(self):
        """Test 4 — returns '' when nothing pending."""
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        # State buffer is empty by construction; nothing to extract.

        result = provider.on_pre_compress(messages=[{"role": "user", "content": "hi"}])

        assert result == ""

    def test_empty_string_when_not_configured(self):
        """Unconfigured users return '' (no creds → can't store)."""
        state = _make_state(configured=False)
        provider = TotalReclawMemoryProvider(state)
        # Even if there are unprocessed messages in state, no creds.
        state.add_message("user", "I work as a software engineer at Acme.")

        result = provider.on_pre_compress(messages=[])

        assert result == ""

    def test_extracts_only_from_unprocessed_messages(self):
        """Test 2 — dedup guard: extraction skips already-processed messages."""
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        state.add_message("user", "old message — already extracted")
        state.add_message("assistant", "ok")
        state.mark_messages_processed()  # mark first two processed
        state.add_message("user", "new message — fresh")

        captured: dict = {}

        def fake_auto_extract(passed_state, mode, llm_config):
            captured["messages"] = passed_state.get_unprocessed_messages()
            return []

        with patch(
            "totalreclaw.agent.lifecycle.auto_extract",
            side_effect=fake_auto_extract,
        ):
            provider.on_pre_compress(messages=[])

        assert len(captured["messages"]) == 1
        assert captured["messages"][0]["content"] == "new message — fresh"

    def test_returns_summary_when_facts_extracted(self):
        """Test 3 — formatted bullet summary returned when facts produced."""
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        state.add_message("user", "I prefer dark mode.")

        with patch(
            "totalreclaw.agent.lifecycle.auto_extract",
            return_value=["User prefers dark mode."],
        ):
            result = provider.on_pre_compress(messages=[])

        assert "TotalReclaw" in result
        assert "User prefers dark mode." in result
        assert "- " in result  # bullet list shape

    def test_swallows_extraction_errors(self):
        """Extraction failure → '' return, never raises."""
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        state.add_message("user", "anything")

        with patch(
            "totalreclaw.agent.lifecycle.auto_extract",
            side_effect=RuntimeError("relay down"),
        ):
            result = provider.on_pre_compress(messages=[])

        assert result == ""


# ---------------------------------------------------------------------------
# 5-6. on_memory_write
# ---------------------------------------------------------------------------


class TestOnMemoryWrite:
    """Tests 5-6 — Background-Review write capture + dedup."""

    def test_mirrors_add_user_writes(self):
        """Test 5a — action=add target=user gets routed through remember()."""
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        called_args: list[dict] = []

        async def fake_remember(args, st, **kw):
            called_args.append(args)
            return {"ok": True}

        with patch("totalreclaw.hermes.tools.remember", side_effect=fake_remember):
            provider.on_memory_write(
                action="add",
                target="user",
                content="User likes mountain biking.",
            )

        assert len(called_args) == 1
        assert called_args[0]["text"] == "User likes mountain biking."

    def test_ignores_non_add_actions(self):
        """Test 5b — action != add is ignored."""
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        with patch("totalreclaw.hermes.tools.remember") as remember_mock:
            provider.on_memory_write(action="update", target="user", content="x")
            provider.on_memory_write(action="delete", target="user", content="x")

        remember_mock.assert_not_called()

    def test_ignores_non_user_targets(self):
        """Test 5c — target != user is ignored (session-state, plugin writes)."""
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        with patch("totalreclaw.hermes.tools.remember") as remember_mock:
            provider.on_memory_write(action="add", target="session", content="x")
            provider.on_memory_write(action="add", target="plugin", content="x")

        remember_mock.assert_not_called()

    def test_ignores_unconfigured_state(self):
        """No creds → no mirror (early return)."""
        state = _make_state(configured=False)
        provider = TotalReclawMemoryProvider(state)

        with patch("totalreclaw.hermes.tools.remember") as remember_mock:
            provider.on_memory_write(action="add", target="user", content="x")

        remember_mock.assert_not_called()

    def test_ignores_empty_or_whitespace_content(self):
        """Empty / whitespace content → no mirror."""
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        with patch("totalreclaw.hermes.tools.remember") as remember_mock:
            provider.on_memory_write(action="add", target="user", content="")
            provider.on_memory_write(action="add", target="user", content="   ")
            provider.on_memory_write(action="add", target="user", content=None)

        remember_mock.assert_not_called()

    def test_accepts_dict_content_payload(self):
        """Test 6 — content can arrive as a dict; we extract text + meta."""
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        called_args: list[dict] = []

        async def fake_remember(args, st, **kw):
            called_args.append(args)
            return {"ok": True}

        with patch("totalreclaw.hermes.tools.remember", side_effect=fake_remember):
            provider.on_memory_write(
                action="add",
                target="user",
                content={"text": "User runs marathons.", "type": "claim"},
            )

        assert len(called_args) == 1
        assert called_args[0]["text"] == "User runs marathons."
        assert called_args[0]["type"] == "claim"

    def test_idempotency_via_remember_dedup_path(self):
        """Test 6 — repeated mirror calls call remember() each time; dedup
        responsibility lives inside remember() via embedding similarity.

        We verify the contract: provider doesn't track its own dedup
        state; it delegates to the same path foreground tools use.
        Same content twice → remember() called twice; the second is
        dropped at the embedding-dedup layer (covered by lifecycle tests).
        """
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        call_count = {"n": 0}

        async def fake_remember(args, st, **kw):
            call_count["n"] += 1
            return {"ok": True}

        with patch("totalreclaw.hermes.tools.remember", side_effect=fake_remember):
            provider.on_memory_write(action="add", target="user", content="Same fact.")
            provider.on_memory_write(action="add", target="user", content="Same fact.")

        assert call_count["n"] == 2  # provider doesn't dedup; remember() does


# ---------------------------------------------------------------------------
# 7. Core lifecycle — is_available / initialize / shutdown / system_prompt
# ---------------------------------------------------------------------------


class TestCoreLifecycle:
    """Tests for the ABC's abstract core lifecycle.

    The spec listed ``on_session_switch`` for test 7, but the upstream
    ABC at ``agent.memory_provider.MemoryProvider`` has no such hook.
    Replaced with coverage of the four abstract lifecycle methods that
    Hermes actually calls.
    """

    def test_is_available_true_when_configured(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        assert provider.is_available() is True

    def test_is_available_false_when_unconfigured(self):
        state = _make_state(configured=False)
        provider = TotalReclawMemoryProvider(state)
        assert provider.is_available() is False

    def test_initialize_invokes_session_start_hook(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        with patch("totalreclaw.hermes.hooks.on_session_start") as on_start:
            provider.initialize(session_id="sess-1", platform="cli")

        on_start.assert_called_once()
        assert provider._session_id == "sess-1"

    def test_shutdown_runs_session_finalize(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)

        with patch("totalreclaw.hermes.hooks.on_session_finalize") as finalize:
            provider.shutdown()

        finalize.assert_called_once()

    def test_shutdown_skipped_when_unconfigured(self):
        state = _make_state(configured=False)
        provider = TotalReclawMemoryProvider(state)

        with patch("totalreclaw.hermes.hooks.on_session_finalize") as finalize:
            provider.shutdown()

        finalize.assert_not_called()

    def test_name_property(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        assert provider.name == "totalreclaw"

    def test_system_prompt_block_configured(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        text = provider.system_prompt_block()
        assert "TotalReclaw" in text
        assert "active memory provider" in text

    def test_system_prompt_block_unconfigured(self):
        state = _make_state(configured=False)
        provider = TotalReclawMemoryProvider(state)
        text = provider.system_prompt_block()
        assert "setup pending" in text
        assert "totalreclaw_pair" in text


# ---------------------------------------------------------------------------
# 8. get_tool_schemas
# ---------------------------------------------------------------------------


class TestGetToolSchemas:
    """Test 8 — schema accessor returns the canonical TR tool list."""

    def test_returns_full_tr_tool_set(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        names = {s["name"] for s in provider.get_tool_schemas()}

        # Mirror plugin.yaml::provides_tools — keep this list as the
        # source-of-truth check; if a new tool ships in the manifest, it
        # must also appear here (and in get_tool_schemas).
        expected = {
            "totalreclaw_remember",
            "totalreclaw_recall",
            "totalreclaw_forget",
            "totalreclaw_export",
            "totalreclaw_status",
            "totalreclaw_pair",
            "totalreclaw_pin",
            "totalreclaw_unpin",
            "totalreclaw_retype",
            "totalreclaw_set_scope",
            "totalreclaw_import_from",
            "totalreclaw_import_batch",
            "totalreclaw_import_status",
            "totalreclaw_import_abort",
            "totalreclaw_upgrade",
            "totalreclaw_debrief",
        }
        assert expected.issubset(names), f"missing: {expected - names}"

    def test_schemas_share_source_of_truth_with_register(self):
        """No duplication — provider schemas are the SAME dicts the
        generic plugin's ``register()`` passes to ``ctx.register_tool``.
        """
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        provider_remember = next(
            s for s in provider.get_tool_schemas() if s["name"] == "totalreclaw_remember"
        )
        assert provider_remember is schemas.REMEMBER


# ---------------------------------------------------------------------------
# 9. get_config_schema
# ---------------------------------------------------------------------------


class TestGetConfigSchema:
    """Test 9 — ``hermes memory setup`` integration descriptor.

    Per the ABC, ``get_config_schema`` returns a LIST of field dicts.
    TotalReclaw needs no setup fields (pairing happens via the
    ``totalreclaw_pair`` chat tool, browser-side crypto). Return [].
    """

    def test_returns_empty_list(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        cfg = provider.get_config_schema()

        assert isinstance(cfg, list)
        assert cfg == []


# ---------------------------------------------------------------------------
# Other safety + helper tests
# ---------------------------------------------------------------------------


class TestHandleToolCall:
    """Dispatching tool calls through the MemoryProvider path."""

    def test_dispatches_remember(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        async def fake_remember(args, st, **kw):
            return {"ok": True, "fact_id": "0xabc"}

        with patch("totalreclaw.hermes.tools.remember", side_effect=fake_remember):
            out = provider.handle_tool_call("totalreclaw_remember", {"text": "hi"})

        data = json.loads(out)
        assert data == {"ok": True, "fact_id": "0xabc"}

    def test_unknown_tool_raises_not_implemented(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        with pytest.raises(NotImplementedError):
            provider.handle_tool_call("not_a_tr_tool", {})


class TestOnDelegationNoop:
    """v1 stub — must not raise + return None."""

    def test_noop_returns_none(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        result = provider.on_delegation(task={}, result={}, child_session_id="x")
        assert result is None


class TestSummaryHelper:
    def test_empty_facts(self):
        assert _summarize_facts_for_compression([]) == ""

    def test_non_empty_facts(self):
        out = _summarize_facts_for_compression(["A", "B"])
        assert "- A" in out
        assert "- B" in out


# ---------------------------------------------------------------------------
# Integration — install_memory_provider module (tests 10-12 surrogate)
# ---------------------------------------------------------------------------


class TestInstallMemoryProvider:
    """Tests 10-12 — sidecar drop + config write + status read.

    Hermes' real ``MemoryManager.set_active_provider`` is not importable
    here; the file-system contract these tests check IS the contract
    Hermes' loader uses (scan plugins/memory/<name>/, read config.yaml).
    """

    def test_install_sidecar_writes_managed_shim(self, tmp_path: Path):
        result_path = imp.install_sidecar(hermes_home=tmp_path)

        assert result_path.exists()
        body = result_path.read_text(encoding="utf-8")
        assert "managed-by: totalreclaw.hermes.install_memory_provider" in body
        assert "TotalReclawMemoryProvider" in body
        assert result_path == tmp_path / "plugins" / "memory" / "totalreclaw" / "__init__.py"

    def test_install_sidecar_idempotent_on_managed_file(self, tmp_path: Path):
        imp.install_sidecar(hermes_home=tmp_path)
        # Second call must not raise — managed marker is present.
        imp.install_sidecar(hermes_home=tmp_path)

    def test_install_sidecar_refuses_to_clobber_hand_edited(self, tmp_path: Path):
        path = imp.sidecar_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# hand-written, do not touch\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="Refusing to overwrite"):
            imp.install_sidecar(hermes_home=tmp_path)

    def test_install_sidecar_force_overrides_hand_edit_refusal(self, tmp_path: Path):
        path = imp.sidecar_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# hand-written, do not touch\n", encoding="utf-8")

        imp.install_sidecar(hermes_home=tmp_path, force=True)
        body = path.read_text(encoding="utf-8")
        assert "managed-by:" in body

    def test_read_active_provider_returns_empty_when_no_config(self, tmp_path: Path):
        assert imp.read_active_provider(tmp_path) == ""

    def test_read_active_provider_parses_honcho(self, tmp_path: Path):
        cfg = imp.config_path(tmp_path)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            "memory:\n  provider: honcho\n  flag: true\n", encoding="utf-8"
        )
        assert imp.read_active_provider(tmp_path) == "honcho"

    def test_set_active_provider_creates_block(self, tmp_path: Path):
        imp.set_active_provider("totalreclaw", hermes_home=tmp_path)
        cfg = imp.config_path(tmp_path)
        body = cfg.read_text(encoding="utf-8")
        assert "memory:" in body
        assert "provider: totalreclaw" in body

    def test_set_active_provider_updates_existing_block(self, tmp_path: Path):
        cfg = imp.config_path(tmp_path)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            "other: keep_me\nmemory:\n  provider: honcho\n  flag: true\n",
            encoding="utf-8",
        )
        imp.set_active_provider("totalreclaw", hermes_home=tmp_path)
        body = cfg.read_text(encoding="utf-8")
        assert "provider: totalreclaw" in body
        assert "provider: honcho" not in body
        assert "other: keep_me" in body
        assert "flag: true" in body

    def test_issue_351_provider_last_in_block_preserves_yaml(self, tmp_path: Path):
        # Pre-state: 'provider:' is the LAST line of the memory block and a
        # top-level key (delegation:) follows on the next line. Pre-fix, the
        # provider-line regex's trailing \s*$ consumed the line-terminating
        # newline and the replacement smashed `delegation:` onto the same
        # line as the new provider value, producing invalid YAML.
        import yaml as _yaml
        cfg = imp.config_path(tmp_path)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            "memory:\n"
            "  memory_enabled: true\n"
            "  provider: none\n"
            "delegation:\n"
            "  enabled: false\n",
            encoding="utf-8",
        )
        imp.set_active_provider("totalreclaw", hermes_home=tmp_path)
        body = cfg.read_text(encoding="utf-8")
        assert "totalreclawdelegation" not in body
        assert "provider: totalreclaw\ndelegation:" in body
        parsed = _yaml.safe_load(body)
        assert parsed["memory"]["provider"] == "totalreclaw"
        assert parsed["memory"]["memory_enabled"] is True
        assert "delegation" in parsed

    def test_issue_351_provider_at_eof_preserves_yaml(self, tmp_path: Path):
        import yaml as _yaml
        cfg = imp.config_path(tmp_path)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            "other: keep_me\n"
            "memory:\n"
            "  provider: none\n",
            encoding="utf-8",
        )
        imp.set_active_provider("totalreclaw", hermes_home=tmp_path)
        body = cfg.read_text(encoding="utf-8")
        parsed = _yaml.safe_load(body)
        assert parsed["other"] == "keep_me"
        assert parsed["memory"]["provider"] == "totalreclaw"

    def test_install_and_activate_combines_steps(self, tmp_path: Path):
        result = imp.install_and_activate(hermes_home=tmp_path, activate=True)

        assert result["activated"] is True
        assert result["active_provider"] == "totalreclaw"
        assert "plugins/memory/totalreclaw/__init__.py" in result["sidecar_path"]
        assert imp.read_active_provider(tmp_path) == "totalreclaw"

    def test_install_and_activate_tools_only_branch(self, tmp_path: Path):
        # Pre-existing Honcho config.
        cfg = imp.config_path(tmp_path)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("memory:\n  provider: honcho\n", encoding="utf-8")

        result = imp.install_and_activate(hermes_home=tmp_path, activate=False)

        assert result["activated"] is False
        assert result["previous_provider"] == "honcho"
        assert result["active_provider"] == "honcho"
        assert imp.read_active_provider(tmp_path) == "honcho"
        # Sidecar still installed — tools available even though TR isn't active.
        assert Path(result["sidecar_path"]).exists()

    def test_uninstall_sidecar_removes_managed_file(self, tmp_path: Path):
        imp.install_sidecar(hermes_home=tmp_path)
        assert imp.sidecar_path(tmp_path).exists()
        assert imp.uninstall_sidecar(tmp_path) is True
        assert not imp.sidecar_path(tmp_path).exists()

    def test_hermes_home_env_override(self, tmp_path: Path):
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            path = imp.sidecar_path()
        assert path == tmp_path / "plugins" / "memory" / "totalreclaw" / "__init__.py"


# ---------------------------------------------------------------------------
# CLI surface — memory-status / install / activate
# ---------------------------------------------------------------------------


class TestCliMemoryProvider:
    def test_memory_status_emits_json(self, tmp_path: Path, capsys):
        from totalreclaw.hermes.cli import main

        cfg = imp.config_path(tmp_path)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("memory:\n  provider: byterover\n", encoding="utf-8")

        rc = main(["memory-status", "--hermes-home", str(tmp_path)])
        out = capsys.readouterr().out
        data = json.loads(out)

        assert rc == 0
        assert data == {"provider": "byterover"}

    def test_memory_status_returns_none_when_unset(self, tmp_path: Path, capsys):
        from totalreclaw.hermes.cli import main
        rc = main(["memory-status", "--hermes-home", str(tmp_path)])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert rc == 0
        assert data == {"provider": "none"}

    def test_install_memory_provider_writes_sidecar(self, tmp_path: Path, capsys):
        from totalreclaw.hermes.cli import main

        rc = main(["install-memory-provider", "--hermes-home", str(tmp_path)])
        out = capsys.readouterr().out
        data = json.loads(out)

        assert rc == 0
        assert data["activated"] is False
        assert Path(data["sidecar_path"]).exists()
        assert imp.read_active_provider(tmp_path) == ""

    def test_activate_memory_provider_writes_sidecar_and_activates(self, tmp_path: Path, capsys):
        from totalreclaw.hermes.cli import main

        rc = main(["activate-memory-provider", "--hermes-home", str(tmp_path)])
        out = capsys.readouterr().out
        data = json.loads(out)

        assert rc == 0
        assert data["activated"] is True
        assert imp.read_active_provider(tmp_path) == "totalreclaw"

    def test_install_with_activate_flag(self, tmp_path: Path, capsys):
        from totalreclaw.hermes.cli import main

        rc = main([
            "install-memory-provider",
            "--hermes-home", str(tmp_path),
            "--activate",
        ])
        out = capsys.readouterr().out
        data = json.loads(out)

        assert rc == 0
        assert data["activated"] is True

    def test_install_refuses_hand_edited_without_force(self, tmp_path: Path, capsys):
        from totalreclaw.hermes.cli import main

        path = imp.sidecar_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# hand-written\n", encoding="utf-8")

        rc = main(["install-memory-provider", "--hermes-home", str(tmp_path)])

        captured = capsys.readouterr()
        assert rc == 2
        assert "Refusing to overwrite" in captured.err


class TestDisableBuiltinMemory:
    """Strategy 1: silence Hermes' builtin local memory so TR is the sole system."""

    def test_disable_sets_both_flags_fresh_config(self, tmp_path: Path):
        imp.disable_builtin_memory(hermes_home=tmp_path)
        text = imp.config_path(tmp_path).read_text(encoding="utf-8")
        import yaml
        cfg = yaml.safe_load(text)
        assert cfg["memory"]["memory_enabled"] is False
        assert cfg["memory"]["user_profile_enabled"] is False

    def test_disable_flips_existing_true_flags_and_preserves_keys(self, tmp_path: Path):
        cfg = imp.config_path(tmp_path)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            "memory:\n"
            "  memory_enabled: true\n"
            "  user_profile_enabled: true\n"
            "  memory_char_limit: 2200\n"
            "  provider: totalreclaw\n"
            "delegation:\n  foo: bar\n",
            encoding="utf-8",
        )
        imp.disable_builtin_memory(hermes_home=tmp_path)
        import yaml
        parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert parsed["memory"]["memory_enabled"] is False
        assert parsed["memory"]["user_profile_enabled"] is False
        # Unrelated keys preserved.
        assert parsed["memory"]["memory_char_limit"] == 2200
        assert parsed["memory"]["provider"] == "totalreclaw"
        assert parsed["delegation"]["foo"] == "bar"

    def test_disable_is_idempotent(self, tmp_path: Path):
        imp.disable_builtin_memory(hermes_home=tmp_path)
        first = imp.config_path(tmp_path).read_text(encoding="utf-8")
        imp.disable_builtin_memory(hermes_home=tmp_path)
        second = imp.config_path(tmp_path).read_text(encoding="utf-8")
        assert first == second

    def test_disable_inserts_into_block_without_those_keys(self, tmp_path: Path):
        cfg = imp.config_path(tmp_path)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("memory:\n  provider: totalreclaw\n", encoding="utf-8")
        imp.disable_builtin_memory(hermes_home=tmp_path)
        import yaml
        parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert parsed["memory"]["provider"] == "totalreclaw"
        assert parsed["memory"]["memory_enabled"] is False
        assert parsed["memory"]["user_profile_enabled"] is False

    def test_install_and_activate_disables_builtin(self, tmp_path: Path):
        result = imp.install_and_activate(hermes_home=tmp_path, activate=True)
        assert result["builtin_disabled"] is True
        import yaml
        parsed = yaml.safe_load(imp.config_path(tmp_path).read_text(encoding="utf-8"))
        assert parsed["memory"]["provider"] == "totalreclaw"
        assert parsed["memory"]["memory_enabled"] is False
        assert parsed["memory"]["user_profile_enabled"] is False

    def test_tools_only_does_not_disable_builtin(self, tmp_path: Path):
        # Another provider is active → tools-only branch must NOT touch builtin.
        cfg = imp.config_path(tmp_path)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            "memory:\n  provider: honcho\n  memory_enabled: true\n", encoding="utf-8"
        )
        result = imp.install_and_activate(hermes_home=tmp_path, activate=False)
        assert result["builtin_disabled"] is False
        import yaml
        parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert parsed["memory"]["memory_enabled"] is True  # untouched
        assert parsed["memory"]["provider"] == "honcho"

    def test_set_memory_key_replace_and_insert(self):
        # Replace existing.
        t = imp._set_memory_key("memory:\n  memory_enabled: true\n", "memory_enabled", "false")
        assert "memory_enabled: false" in t and "true" not in t
        # Insert when key missing but block present.
        t2 = imp._set_memory_key("memory:\n  provider: x\n", "memory_enabled", "false")
        assert "provider: x" in t2 and "memory_enabled: false" in t2
        # Create block when absent.
        t3 = imp._set_memory_key("", "memory_enabled", "false")
        assert t3.startswith("memory:") and "memory_enabled: false" in t3


class TestNativeAutoMemory:
    """§5.2 (#351) — prefetch/sync_turn delegate to the shared §5.1 entry points."""

    def test_prefetch_delegates_to_recall_for_query(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.hooks.recall_for_query", return_value="CTX") as rq:
            out = provider.prefetch("who am I?", session_id="s1")
        assert out == "CTX"
        rq.assert_called_once_with(state, "who am I?", top_k=8)

    def test_prefetch_empty_when_unconfigured(self):
        state = _make_state(configured=False)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.hooks.recall_for_query") as rq:
            assert provider.prefetch("q") == ""
        rq.assert_not_called()

    def test_prefetch_swallows_errors(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.hooks.recall_for_query", side_effect=RuntimeError("x")):
            assert provider.prefetch("q") == ""

    def test_prefetch_none_becomes_empty(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.hooks.recall_for_query", return_value=None):
            assert provider.prefetch("q") == ""

    def test_sync_turn_delegates_to_ingest_turn(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.hooks.ingest_turn") as it:
            provider.sync_turn("u", "a", session_id="s1")
        it.assert_called_once_with(state, "u", "a")

    def test_sync_turn_accepts_messages_kwarg(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.hooks.ingest_turn") as it:
            provider.sync_turn(
                "u", "a", session_id="s1", messages=[{"role": "user", "content": "u"}]
            )
        it.assert_called_once_with(state, "u", "a")

    def test_sync_turn_swallows_errors(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.hooks.ingest_turn", side_effect=RuntimeError("x")):
            provider.sync_turn("u", "a")  # must not raise
