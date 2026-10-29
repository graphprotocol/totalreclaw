"""Unit tests for the Hermes MemoryProvider adapter (issue #275).

Covers the 9 unit tests + 3 integration tests from the Path B spec
§"Test plan". The integration tests exercise the install_memory_provider
module (sidecar drop, config write, status read) since Hermes' actual
``MemoryManager.set_active_provider`` is not importable in our test env.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.hermes.state import PluginState
from totalreclaw.hermes import schemas
from totalreclaw.hermes.memory_provider import (
    TotalReclawMemoryProvider,
    _summarize_facts_for_compression,
)
from totalreclaw.hermes import install_memory_provider as imp
from totalreclaw.hermes import memory_provider as mp
from totalreclaw.reranker import RerankerResult


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
    """§5.3 — on_turn_start is a counting no-op; sync_turn owns the counter."""

    def test_does_not_touch_turn_count_with_number(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        provider.on_turn_start(turn_number=5, message="hi")
        # No longer sets the counter — sync_turn (via ingest_turn) owns it.
        assert state.turn_count == 0

    def test_does_not_increment_when_turn_number_missing(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        state.increment_turn()
        state.increment_turn()
        assert state.turn_count == 2

        provider.on_turn_start(turn_number=None, message="hi")

        assert state.turn_count == 2  # unchanged — no double-count vs sync_turn


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
# 5-6. on_memory_write — built-in memory mirror
# ---------------------------------------------------------------------------


def _drain() -> None:
    assert mp._MIRROR_WORKER.drain(timeout=5.0), "mirror worker did not finish"


def _fact(fact_id: str, text: str, cosine: float = 0.0) -> RerankerResult:
    return RerankerResult(id=fact_id, text=text, embedding=None, cosine_sim=cosine)


class _Vault:
    """Records remember/forget calls and serves canned recall results."""

    def __init__(self, state: PluginState, recall_results=None, remember_result=None):
        self.remembered: list[dict] = []
        self.forgotten: list[str] = []
        self.events: list[str] = []
        self.recall_queries: list[str] = []
        self._recall_results = recall_results or []
        self._remember_result = remember_result or {"stored": True, "fact_id": "new"}
        state._client.recall = AsyncMock(side_effect=self._recall)

    async def _recall(self, query, **kw):
        self.recall_queries.append(query)
        self.events.append("recall")
        return list(self._recall_results)

    async def remember(self, args, st, **kw):
        self.remembered.append(args)
        self.events.append("remember")
        return json.dumps(self._remember_result)

    async def forget(self, args, st, **kw):
        self.forgotten.append(args["fact_id"])
        self.events.append("forget")
        return json.dumps({"deleted": True, "fact_id": args["fact_id"]})

    def patches(self):
        return (
            patch("totalreclaw.hermes.tools.remember", side_effect=self.remember),
            patch("totalreclaw.hermes.tools.forget", side_effect=self.forget),
            patch("totalreclaw.embedding.get_embedding", return_value=[0.1] * 8),
        )


def _write(provider, vault: _Vault, *args, **kwargs) -> None:
    p1, p2, p3 = vault.patches()
    with p1, p2, p3:
        provider.on_memory_write(*args, **kwargs)
        _drain()


def _hermes_metadata_mode(provider) -> str:
    """Hermes 0.22.5 ``MemoryManager._provider_memory_write_metadata_mode``."""
    import inspect

    params = inspect.signature(provider.on_memory_write).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()) or "metadata" in params:
        return "keyword"
    accepted = sum(p.kind is not inspect.Parameter.VAR_POSITIONAL for p in params.values())
    return "positional" if accepted >= 4 else "legacy"


class TestOnMemoryWrite:
    """Built-in ``memory`` tool writes are mirrored into the vault."""

    @pytest.fixture(autouse=True)
    def _isolated_state_dirs(self, tmp_path, monkeypatch):
        # Mirror jobs read the reconcile snapshot; keep them off real dirs.
        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    @pytest.mark.parametrize("target", ["user", "memory"])
    def test_add_is_stored_for_both_targets(self, target):
        state = _make_state()
        vault = _Vault(state)
        _write(TotalReclawMemoryProvider(state), vault, "add", target, "User likes mountain biking.")
        assert vault.remembered == [{"text": "User likes mountain biking."}]
        assert vault.forgotten == []

    def test_add_accepts_dict_content_payload(self):
        state = _make_state()
        vault = _Vault(state)
        _write(
            TotalReclawMemoryProvider(state), vault, "add", "user",
            {"text": "User runs marathons.", "type": "claim"},
        )
        assert vault.remembered == [{"text": "User runs marathons.", "type": "claim"}]

    def test_hermes_passes_metadata_by_keyword(self):
        provider = TotalReclawMemoryProvider(_make_state())
        assert _hermes_metadata_mode(provider) == "keyword"

    def test_metadata_accepted_positionally_and_by_keyword(self):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        meta = {"write_origin": "assistant_tool", "session_id": "s1"}
        vault = _Vault(state)
        _write(provider, vault, "add", "user", "Fact A.", dict(meta))
        _write(provider, vault, "add", "user", "Fact B.", metadata=dict(meta))
        _write(provider, vault, "add", "user", "Fact C.")  # legacy 3-arg caller
        assert [a["text"] for a in vault.remembered] == ["Fact A.", "Fact B.", "Fact C."]

    def test_ignores_unknown_actions_and_targets(self):
        state = _make_state()
        vault = _Vault(state)
        provider = TotalReclawMemoryProvider(state)
        for action, target in (
            ("update", "user"), ("delete", "user"), ("read", "memory"),
            ("add", "session"), ("add", "plugin"), ("remove", "skills"),
        ):
            _write(provider, vault, action, target, "x", {"previous_content": "x"})
        assert vault.remembered == [] and vault.forgotten == [] and vault.recall_queries == []

    def test_ignores_unconfigured_state(self):
        state = _make_state(configured=False)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.tools.remember") as remember_mock:
            provider.on_memory_write("add", "user", "x")
            _drain()
        remember_mock.assert_not_called()

    def test_ignores_empty_or_whitespace_content(self):
        state = _make_state()
        vault = _Vault(state)
        provider = TotalReclawMemoryProvider(state)
        for content in ("", "   ", None):
            _write(provider, vault, "add", "user", content)
            _write(provider, vault, "replace", "user", content, {"previous_content": "Old."})
        assert vault.remembered == [] and vault.forgotten == []

    def test_idempotency_lives_in_remember(self):
        # The provider keeps no dedup state of its own; tools.remember dedups.
        state = _make_state()
        vault = _Vault(state)
        provider = TotalReclawMemoryProvider(state)
        _write(provider, vault, "add", "user", "Same fact.")
        _write(provider, vault, "add", "user", "Same fact.")
        assert len(vault.remembered) == 2

    # -- replace / patch ----------------------------------------------------

    @pytest.mark.parametrize("action", ["replace", "patch"])
    def test_replace_supersedes_the_matching_fact(self, action):
        state = _make_state()
        vault = _Vault(state, recall_results=[
            _fact("old-1", "User prefers tea.", 0.99),
            _fact("other", "User lives in Lisbon.", 0.30),
        ])
        _write(
            TotalReclawMemoryProvider(state), vault, action, "user", "User prefers coffee.",
            metadata={"previous_content": "User prefers tea.", "old_text": "tea"},
        )
        assert vault.remembered == [{"text": "User prefers coffee."}]
        assert vault.forgotten == ["old-1"]
        # The old fact is looked up before the new one is stored.
        assert vault.events == ["recall", "remember", "forget"]
        assert vault.recall_queries == ["User prefers tea."]

    def test_replace_with_only_an_old_text_span_keeps_the_old_fact(self):
        # old_text is only the search span a legacy caller sent, never the
        # whole entry: it can't show the edit is the same fact, so keep it.
        state = _make_state()
        vault = _Vault(state, recall_results=[_fact("old-1", "Works at Acme", 0.5)])
        _write(
            TotalReclawMemoryProvider(state), vault, "replace", "memory", "Works at Initech",
            metadata={"old_text": "Acme"},
        )
        assert vault.remembered == [{"text": "Works at Initech"}]
        assert vault.forgotten == []

    def test_merge_keeps_the_old_facts(self):
        # Consolidation folds entries together to make room: not the same
        # fact edited, so nothing is forgotten.
        state = _make_state()
        vault = _Vault(state, recall_results=[_fact("old-1", "User likes tea.", 0.99)])
        _write(
            TotalReclawMemoryProvider(state), vault, "replace", "user",
            "User likes tea and coffee, and avoids dairy.",
            metadata={"previous_content": "User likes tea."},
        )
        assert vault.remembered == [{"text": "User likes tea and coffee, and avoids dairy."}]
        assert vault.forgotten == []
        assert vault.recall_queries == []  # no lookup needed

    def test_same_fact_threshold(self):
        assert mp._same_fact("User prefers tea.", "User prefers green tea.")
        assert not mp._same_fact("User likes tea.", "User likes tea and coffee.")
        assert not mp._same_fact(
            "User has a dog named Rex.",
            "User has a dog named Rex and a cat named Mia; lives in Lisbon.",
        )
        assert not mp._same_fact("", "anything")

    def test_replace_without_a_match_only_stores(self):
        state = _make_state()
        vault = _Vault(state, recall_results=[_fact("x", "Something unrelated.", 0.4)])
        _write(
            TotalReclawMemoryProvider(state), vault, "replace", "user", "New entry.",
            metadata={"previous_content": "Old entry."},
        )
        assert vault.remembered == [{"text": "New entry."}]
        assert vault.forgotten == []

    def test_replace_keeps_old_fact_when_store_fails(self):
        state = _make_state()
        vault = _Vault(
            state,
            recall_results=[_fact("old-1", "Old entry.", 1.0)],
            remember_result={"error": "relay down"},
        )
        _write(
            TotalReclawMemoryProvider(state), vault, "replace", "user", "New entry.",
            metadata={"previous_content": "Old entry."},
        )
        assert vault.forgotten == []

    def test_replace_still_stores_when_lookup_fails(self):
        state = _make_state()
        vault = _Vault(state)
        state._client.recall = AsyncMock(side_effect=RuntimeError("reads paused"))
        _write(
            TotalReclawMemoryProvider(state), vault, "replace", "user", "New entry.",
            metadata={"previous_content": "Old entry."},
        )
        assert vault.remembered == [{"text": "New entry."}]
        assert vault.forgotten == []

    def test_replace_with_same_normalized_text_is_a_no_op(self):
        state = _make_state()
        vault = _Vault(state, recall_results=[_fact("old-1", "User likes tea", 1.0)])
        _write(
            TotalReclawMemoryProvider(state), vault, "replace", "user", "User likes tea.",
            metadata={"previous_content": "user likes  tea"},
        )
        assert vault.remembered == [] and vault.forgotten == []

    # -- remove ---------------------------------------------------------------

    def test_remove_never_forgets(self, caplog):
        # The built-in stores are size-capped; a removal is usually an
        # eviction to make room, not a request to forget.
        caplog.set_level(logging.INFO, logger="totalreclaw.hermes.memory_provider")
        state = _make_state()
        vault = _Vault(state, recall_results=[_fact("f1", "User's dog is called Rex.", 1.0)])
        _write(
            TotalReclawMemoryProvider(state), vault, "remove", "user", "",
            metadata={"previous_content": "User's dog is called Rex.", "old_text": "Rex"},
        )
        assert vault.forgotten == [] and vault.remembered == []
        assert vault.recall_queries == []
        assert "memory mirror: remove target=user removed from Hermes profile; kept in TotalReclaw" in caplog.text

    def test_remove_from_legacy_callers_never_forgets(self):
        state = _make_state()
        vault = _Vault(state, recall_results=[_fact("f1", "Old note", 1.0)])
        _write(TotalReclawMemoryProvider(state), vault, "remove", "memory", "Old note")
        assert vault.forgotten == []

    def test_remove_without_entry_text_does_nothing(self):
        state = _make_state()
        vault = _Vault(state, recall_results=[_fact("f1", "Anything", 1.0)])
        _write(TotalReclawMemoryProvider(state), vault, "remove", "user", "", metadata={})
        assert vault.recall_queries == [] and vault.forgotten == []

    def test_removing_the_last_copy_still_keeps_it(self):
        state = _make_state()
        vault = _Vault(state, recall_results=[_fact("f1", "User prefers dark mode.", 1.0)])
        provider = TotalReclawMemoryProvider(state)
        _write(provider, vault, "add", "user", "User prefers dark mode.")
        _write(provider, vault, "remove", "user", "",
               metadata={"previous_content": "User prefers dark mode."})
        assert vault.forgotten == []

    # -- conservative matching ------------------------------------------------

    @staticmethod
    def _match(results, text, embedding=(0.1,) * 8):
        state = _make_state()
        state._client.recall = AsyncMock(return_value=results)
        with patch("totalreclaw.embedding.get_embedding",
                   return_value=list(embedding) if embedding else None):
            return mp._find_mirrored_facts(state, text)

    def test_match_single_close_semantic(self):
        assert self._match([
            _fact("f1", "The user is vegetarian.", mp.MIRROR_MATCH_THRESHOLD + 0.01),
            _fact("f2", "User likes pasta.", 0.70),
        ], "User is a vegetarian") == ["f1"]

    def test_match_below_threshold_is_none(self):
        assert self._match([
            _fact("f1", "The user is vegetarian.", mp.MIRROR_MATCH_THRESHOLD - 0.01),
        ], "User is a vegetarian") == []

    def test_match_two_close_is_ambiguous(self):
        assert self._match([
            _fact("f1", "User's sister is Ana.", 0.95),
            _fact("f2", "User's sister Ana lives in Porto.", 0.94),
        ], "User has a sister named Ana") == []

    def test_match_identical_copies(self):
        assert self._match([
            _fact("f1", "Uses vim.", 0.99),
            _fact("f2", "uses vim", 0.99),
            _fact("f3", "Uses Emacs sometimes.", 0.93),
        ], "Uses vim.") == ["f1", "f2"]

    def test_match_too_many_identical_copies_is_none(self):
        copies = [_fact(f"f{i}", "Uses vim.", 1.0) for i in range(mp._MIRROR_MAX_FORGETS + 1)]
        assert self._match(copies, "Uses vim.") == []

    def test_match_without_embedding_needs_identical_text(self):
        assert self._match([_fact("f1", "Uses vim a lot.", 0.99)], "Uses vim.", embedding=None) == []

    # -- robustness -------------------------------------------------------------

    def test_returns_before_the_mirror_runs(self):
        import threading

        state = _make_state()
        release = threading.Event()
        started = threading.Event()

        async def slow_remember(args, st, **kw):
            started.set()
            release.wait(5)
            return json.dumps({"stored": True})

        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.tools.remember", side_effect=slow_remember):
            provider.on_memory_write("add", "user", "Slow fact.")
            # Back on the caller's thread while the store is still blocked.
            assert started.wait(5)
            assert mp._MIRROR_WORKER.drain(timeout=0.05) is False
            release.set()
            _drain()

    def test_mirror_runs_writes_in_order(self):
        state = _make_state()
        vault = _Vault(state, recall_results=[_fact("f1", "User prefers tea.", 1.0)])
        provider = TotalReclawMemoryProvider(state)
        p1, p2, p3 = vault.patches()
        with p1, p2, p3:
            provider.on_memory_write("add", "user", "User prefers tea.")
            provider.on_memory_write("replace", "user", "User prefers green tea.",
                                     metadata={"previous_content": "User prefers tea."})
            provider.on_memory_write("add", "user", "Entry two.")
            _drain()
        assert vault.events == ["remember", "recall", "remember", "forget", "remember"]

    def test_no_exception_escapes(self, caplog):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        async def boom(*a, **kw):
            raise RuntimeError("relay exploded")

        state._client.recall = AsyncMock(side_effect=RuntimeError("search exploded"))
        with patch("totalreclaw.hermes.tools.remember", side_effect=boom), \
             patch("totalreclaw.hermes.tools.forget", side_effect=boom):
            provider.on_memory_write("add", "user", "A.")
            provider.on_memory_write("replace", "memory", "B.", {"previous_content": "A."})
            provider.on_memory_write("remove", "user", "", metadata={"previous_content": "B."})
            provider.on_memory_write("add", "user", 12345)  # odd content type
            provider.on_memory_write("add", "user", "C.", metadata="not-a-dict")
            _drain()
        with patch.object(mp._MIRROR_WORKER, "submit", side_effect=RuntimeError("no thread")):
            provider.on_memory_write("add", "user", "D.")
        assert "mirror failed" in caplog.text or "skipped" in caplog.text

    def test_full_queue_drops_with_a_warning(self, caplog):
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)
        with patch.object(mp._MIRROR_WORKER, "submit", return_value=False), \
             patch("totalreclaw.hermes.tools.remember") as remember_mock:
            provider.on_memory_write("add", "user", "Dropped.")
        remember_mock.assert_not_called()
        assert "queue full" in caplog.text

    def test_logs_never_carry_entry_text(self, caplog):
        import logging

        caplog.set_level(logging.DEBUG, logger="totalreclaw.hermes.memory_provider")
        state = _make_state()
        vault = _Vault(state, recall_results=[
            _fact("f1", "secret-ish detail one", 0.95),
            _fact("f2", "secret-ish detail two", 0.95),
        ])
        provider = TotalReclawMemoryProvider(state)
        _write(provider, vault, "remove", "user", "", metadata={"previous_content": "secret-ish detail"})
        _write(provider, vault, "replace", "user", "secret-ish detail two edited",
               metadata={"previous_content": "secret-ish detail two"})
        assert "secret-ish" not in caplog.text


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

    def test_initialize_sets_provider_active_and_delegates_session_start(self):
        # §5.3: initialize sets the single-driver flag and does NOT call
        # on_session_start (the plugin's hook owns session lifecycle — calling
        # it here too would double it).
        state = _make_state()
        provider = TotalReclawMemoryProvider(state)

        with patch("totalreclaw.hermes.hooks.on_session_start") as on_start:
            provider.initialize(session_id="sess-1", platform="cli")

        on_start.assert_not_called()
        assert provider._session_id == "sess-1"
        assert state._provider_active is True

    def test_shutdown_delegates_finalize_to_plugin(self):
        # §5.3: shutdown no longer calls on_session_finalize — the plugin's
        # on_session_finalize hook is the single session-end debrief path.
        state = _make_state(configured=True)
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
        # Roles: the built-in is the short profile, TotalReclaw the long-term
        # store the agent writes to; built-in writes are mirrored (#167).
        assert "USER.md / MEMORY.md" in text
        assert "`totalreclaw_remember`" in text and "`totalreclaw_recall`" in text
        assert "copied to TotalReclaw automatically" in text
        assert "`totalreclaw_forget`" in text  # explicit forgetting
        assert len(text) < 600  # injected every turn

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
        # Fixed 2026-06-10: Hermes scans $HERMES_HOME/plugins/<name>/ for
        # user providers, NOT the nested plugins/memory/<name>/ (bundled-only).
        assert result_path == tmp_path / "plugins" / "totalreclaw" / "__init__.py"

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
        assert "plugins/totalreclaw/__init__.py" in result["sidecar_path"]
        assert "plugins/memory/totalreclaw" not in result["sidecar_path"]
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
        assert path == tmp_path / "plugins" / "totalreclaw" / "__init__.py"

    def test_install_migrates_stale_legacy_sidecar(self, tmp_path: Path):
        # Simulate a pre-fix install: a managed sidecar at the OLD nested path
        # that Hermes never scanned.
        legacy = tmp_path / "plugins" / "memory" / "totalreclaw" / "__init__.py"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(imp._SIDECAR_CONTENT, encoding="utf-8")

        imp.install_sidecar(hermes_home=tmp_path)

        # New (correct) sidecar exists; stale legacy one is gone.
        assert imp.sidecar_path(tmp_path).exists()
        assert imp.sidecar_path(tmp_path) == tmp_path / "plugins" / "totalreclaw" / "__init__.py"
        assert not legacy.exists()

    def test_migrate_leaves_hand_edited_legacy_untouched(self, tmp_path: Path):
        # A hand-edited file at the legacy path must NOT be deleted.
        legacy = tmp_path / "plugins" / "memory" / "totalreclaw" / "__init__.py"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("# hand-written, not managed\n", encoding="utf-8")

        imp.install_sidecar(hermes_home=tmp_path)

        assert legacy.exists()  # untouched — lacks the managed marker


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
    """The ``--exclusive`` mode: silence Hermes' builtin local memory so TR is
    the sole system (the default before 2.5.3rc4)."""

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

    def test_disable_marks_the_switches_as_exclusive(self, tmp_path: Path):
        imp.disable_builtin_memory(hermes_home=tmp_path)
        text = imp.config_path(tmp_path).read_text(encoding="utf-8")
        assert "memory_enabled: false  # totalreclaw: --exclusive\n" in text
        assert "user_profile_enabled: false  # totalreclaw: --exclusive\n" in text

    def test_install_and_activate_exclusive_disables_builtin(self, tmp_path: Path):
        result = imp.install_and_activate(hermes_home=tmp_path, activate=True, exclusive=True)
        assert result["builtin_disabled"] is True
        assert result["exclusive"] is True
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
        rq.assert_called_once_with(state, "who am I?", top_k=state.get_recall_top_k())

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
        # sync_turn now forwards the per-conversation session_id to ingest_turn
        # (the parallel-chat fix) — that forwarding IS the delegation contract.
        it.assert_called_once_with(state, "u", "a", session_id="s1")

    def test_sync_turn_accepts_messages_kwarg(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.hooks.ingest_turn") as it:
            provider.sync_turn(
                "u", "a", session_id="s1", messages=[{"role": "user", "content": "u"}]
            )
        it.assert_called_once_with(state, "u", "a", session_id="s1")

    def test_sync_turn_swallows_errors(self):
        state = _make_state(configured=True)
        provider = TotalReclawMemoryProvider(state)
        with patch("totalreclaw.hermes.hooks.ingest_turn", side_effect=RuntimeError("x")):
            provider.sync_turn("u", "a")  # must not raise
