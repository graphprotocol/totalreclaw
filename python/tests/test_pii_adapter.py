"""PRD-04 DEP-4 — Python adapter for the core PII / credential filter.

Covers ``totalreclaw.pii`` (decision decoding, strict ``allow_sensitive``
parsing, block-and-tell shape, log redaction, fail-closed fallback) and its
call sites:

* Hermes ``totalreclaw_remember`` — explicit: ``block`` → block-and-tell
  unless ``allow_sensitive=true``; ``flag`` and ``allow`` are stored.
* ``agent.lifecycle.auto_extract`` / ``session_debrief`` and
  ``MemoryProvider.on_memory_write`` — automatic: only ``allow`` is stored.

Uses the real ``totalreclaw_core.pii_filter`` binding (CI builds the wheel
from local source). Sensitive values are public test vectors (PSP test card)
or fake-shaped; the log assertions prove none reaches a log line.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import totalreclaw_core
from totalreclaw import pii
from totalreclaw.agent.debrief import Crystal
from totalreclaw.agent.extraction import ExtractedFact
from totalreclaw.hermes.memory_provider import TotalReclawMemoryProvider
from totalreclaw.hermes.state import PluginState

CARD = "4111 1111 1111 1111"  # public PSP test card (Luhn-valid Visa)
CARD_TEXT = f"My card number is {CARD}"
FLAG_TEXT = "number 123-45-6789 on the form"  # SSN shape, no keyword -> flag
CLEAN_TEXT = "My doctor is Dr. Ana Sousa"


def _plugin_state(client: MagicMock) -> PluginState:
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            state = PluginState()
    state._client = client
    state.get_client = MagicMock(return_value=client)
    return state


def _remember_client() -> MagicMock:
    client = MagicMock()
    client.remember = AsyncMock(return_value="fact-id-stub-1")
    return client


def _batch_client() -> MagicMock:
    client = MagicMock()
    client.recall = AsyncMock(return_value=[])
    client.forget = AsyncMock(return_value=True)
    client.remember = AsyncMock(return_value="crystal-id-1")

    async def _batch(facts, source="python-client"):
        return [f"fact-id-{i}" for i in range(len(facts))]

    client.remember_batch = AsyncMock(side_effect=_batch)
    return client


def _fact(text: str, action: str = "ADD", existing: str | None = None) -> ExtractedFact:
    return ExtractedFact(
        text=text, type="claim", importance=7, action=action,
        existing_fact_id=existing, confidence=0.9, source="user", scope="unspecified",
    )


# ---------------------------------------------------------------------------
# totalreclaw.pii
# ---------------------------------------------------------------------------


class TestPiiModule:
    def test_check_allow(self) -> None:
        result = pii.check(CLEAN_TEXT)
        assert result == pii.PiiResult(decision="allow", category=None, message=None)
        assert result.is_allow

    def test_check_block_carries_core_message(self) -> None:
        result = pii.check(CARD_TEXT)
        assert (result.decision, result.category) == ("block", "payment_card")
        assert result.message.startswith("Not saved: ")

    def test_check_flag(self) -> None:
        result = pii.check(FLAG_TEXT)
        assert (result.decision, result.category) == ("flag", "government_id")

    def test_check_fails_closed_when_core_raises(self, monkeypatch) -> None:
        def boom(_text: str) -> str:
            raise RuntimeError("binding exploded")

        monkeypatch.setattr(totalreclaw_core, "pii_filter", boom, raising=False)
        result = pii.check(CLEAN_TEXT)
        assert (result.decision, result.category) == ("flag", pii.FILTER_ERROR_CATEGORY)
        assert pii.auto_storable(CLEAN_TEXT) is False

    @pytest.mark.parametrize(
        "value,expected",
        [(True, True), ("true", True), (" TRUE ", True), (False, False), ("false", False),
         ("False", False), (1, False), ("yes", False), (None, False)],
    )
    def test_parse_allow_sensitive_is_strict(self, value, expected) -> None:
        assert pii.parse_allow_sensitive(value) is expected

    def test_block_notice_shape_matches_mcp_contract(self) -> None:
        notice = pii.block_notice(pii.check(CARD_TEXT))
        assert list(notice) == ["stored", "blocked", "category", "message", "override"]
        assert notice["stored"] is False
        assert notice["blocked"] == "sensitive"
        assert notice["category"] == "payment_card"
        assert notice["message"] == pii.check(CARD_TEXT).message
        assert notice["override"] == "allow_sensitive"

    def test_log_label_redacts_non_allow_text(self) -> None:
        assert pii.log_label(CARD_TEXT, pii.check(CARD_TEXT)) == "<redacted:payment_card>"
        assert pii.log_label(FLAG_TEXT, pii.check(FLAG_TEXT)) == "<redacted:government_id>"
        assert pii.log_label(CLEAN_TEXT, pii.check(CLEAN_TEXT)) == CLEAN_TEXT

    def test_drop_sensitive_facts_keeps_allow_and_non_storing_actions(self) -> None:
        facts = [
            _fact(CLEAN_TEXT),
            _fact(CARD_TEXT),
            _fact(FLAG_TEXT, action="UPDATE", existing="old-1"),
            _fact(CARD_TEXT, action="DELETE", existing="old-2"),
        ]
        kept = pii.drop_sensitive_facts(facts)
        assert [(f.text, f.action) for f in kept] == [(CLEAN_TEXT, "ADD"), (CARD_TEXT, "DELETE")]


# ---------------------------------------------------------------------------
# Hermes totalreclaw_remember (explicit)
# ---------------------------------------------------------------------------


class TestRememberBlockAndTell:
    async def test_block_returns_notice_and_does_not_store(self, caplog) -> None:
        from totalreclaw.hermes.tools import remember

        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        result = json.loads(await remember({"text": CARD_TEXT}, state))
        assert result == pii.block_notice(pii.check(CARD_TEXT))
        client.remember.assert_not_called()
        assert "4111" not in caplog.text

    async def test_allow_sensitive_true_stores_blocked_text(self) -> None:
        from totalreclaw.hermes.tools import remember

        client = _remember_client()
        state = _plugin_state(client)
        with patch("totalreclaw.embedding.get_embedding", return_value=None):
            result = json.loads(await remember({"text": CARD_TEXT, "allow_sensitive": True}, state))
        assert result["stored"] is True
        client.remember.assert_called_once()
        assert client.remember.call_args.args[0] == CARD_TEXT

    async def test_allow_sensitive_string_false_does_not_override(self) -> None:
        from totalreclaw.hermes.tools import remember

        client = _remember_client()
        state = _plugin_state(client)
        result = json.loads(await remember({"text": CARD_TEXT, "allow_sensitive": "false"}, state))
        assert result["blocked"] == "sensitive"
        client.remember.assert_not_called()

    async def test_flag_is_stored_on_explicit_remember(self, caplog) -> None:
        from totalreclaw.hermes.tools import remember

        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        with patch("totalreclaw.embedding.get_embedding", return_value=None):
            result = json.loads(await remember({"text": FLAG_TEXT}, state))
        assert result["stored"] is True
        client.remember.assert_called_once()
        assert "123-45-6789" not in caplog.text

    async def test_doctor_name_is_stored(self) -> None:
        from totalreclaw.hermes.tools import remember

        client = _remember_client()
        state = _plugin_state(client)
        with patch("totalreclaw.embedding.get_embedding", return_value=None):
            result = json.loads(await remember({"text": CLEAN_TEXT}, state))
        assert result["stored"] is True

    async def test_sensitive_gate_runs_before_self_directive_log(self, caplog) -> None:
        from totalreclaw.hermes.tools import remember

        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        text = f"I'll always use totalreclaw_remember for card {CARD}"
        result = json.loads(await remember({"text": text}, state))
        assert result["blocked"] == "sensitive"
        assert "4111" not in caplog.text

    def test_schema_declares_allow_sensitive_boolean_default_false(self) -> None:
        from totalreclaw.hermes.schemas import REMEMBER

        prop = REMEMBER["parameters"]["properties"]["allow_sensitive"]
        assert prop["type"] == "boolean"
        assert prop["default"] is False
        assert "allow_sensitive" not in REMEMBER["parameters"]["required"]
        assert "allow_sensitive" in REMEMBER["description"]


# ---------------------------------------------------------------------------
# Automatic paths: auto_extract, session_debrief, on_memory_write
# ---------------------------------------------------------------------------


class TestAutomaticPathsStoreOnlyAllow:
    def _run_auto_extract(self, facts: list[ExtractedFact]):
        from totalreclaw.agent.lifecycle import auto_extract

        client = _batch_client()
        state = _plugin_state(client)

        async def fake_extract(*args, **kwargs):
            return facts

        async def passthrough(fs, *args, **kwargs):
            return fs

        with patch("totalreclaw.agent.lifecycle.extract_facts_llm", new=fake_extract), \
             patch("totalreclaw.agent.lifecycle.detect_and_resolve_contradictions", new=passthrough), \
             patch("totalreclaw.agent.lifecycle._fetch_recent_memories", return_value=[]), \
             patch("totalreclaw.agent.lifecycle._is_near_duplicate", return_value=False), \
             patch("totalreclaw.embedding.get_embedding", return_value=None):
            state.add_message("user", "hello there")
            state.add_message("assistant", "hi")
            stored = auto_extract(state)
        return stored, client, state

    def test_auto_extract_drops_block_and_flag_facts(self, caplog) -> None:
        caplog.set_level(logging.DEBUG)
        stored, client, _ = self._run_auto_extract(
            [_fact(CLEAN_TEXT), _fact(CARD_TEXT), _fact(FLAG_TEXT)]
        )
        assert stored == [CLEAN_TEXT]
        sent = client.remember_batch.call_args.args[0]
        assert [d["text"] for d in sent] == [CLEAN_TEXT]
        assert "4111" not in caplog.text
        assert "123-45-6789" not in caplog.text

    def test_auto_extract_all_sensitive_stores_nothing_and_marks_processed(self) -> None:
        stored, client, state = self._run_auto_extract([_fact(CARD_TEXT)])
        assert stored == []
        assert client.remember_batch.call_count == 0
        assert state.get_unprocessed_messages() == []

    def test_session_debrief_skips_crystal_with_sensitive_content(self) -> None:
        from totalreclaw.agent.lifecycle import session_debrief

        client = _batch_client()
        state = _plugin_state(client)
        for i in range(8):
            state.add_message("user" if i % 2 == 0 else "assistant", f"trip planning turn {i}")
        crystal = Crystal(narrative="Planned the Porto trip.", key_outcomes=[f"Paid with {CARD}"])

        async def fake_generate(*args, **kwargs):
            return crystal

        with patch("totalreclaw.agent.lifecycle.generate_crystal", new=fake_generate):
            ids = session_debrief(state, stored_fact_texts=[])
        assert ids == []
        client.remember.assert_not_called()

    def test_session_debrief_stores_clean_crystal(self) -> None:
        from totalreclaw.agent.lifecycle import session_debrief

        client = _batch_client()
        state = _plugin_state(client)
        for i in range(8):
            state.add_message("user" if i % 2 == 0 else "assistant", f"trip planning turn {i}")
        crystal = Crystal(narrative="Planned the Porto trip.", key_outcomes=["train booked"])

        async def fake_generate(*args, **kwargs):
            return crystal

        with patch("totalreclaw.agent.lifecycle.generate_crystal", new=fake_generate):
            ids = session_debrief(state, stored_fact_texts=[])
        assert ids == ["crystal-id-1"]

    @pytest.fixture
    def mirror_dirs(self, tmp_path, monkeypatch):
        # Mirror jobs read the reconcile snapshot; keep them off real dirs.
        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    def test_on_memory_write_mirror_skips_sensitive_content(self, mirror_dirs, caplog) -> None:
        from totalreclaw.hermes import memory_provider as mp

        client = MagicMock()
        client.recall = AsyncMock(return_value=[])
        provider = TotalReclawMemoryProvider(_plugin_state(client))
        caplog.set_level(logging.DEBUG)
        with patch("totalreclaw.hermes.tools.remember") as remember_mock, \
             patch("totalreclaw.hermes.tools.forget") as forget_mock:
            provider.on_memory_write("add", "user", CARD_TEXT)
            provider.on_memory_write("add", "user", FLAG_TEXT)
            provider.on_memory_write("replace", "user", CARD_TEXT, {"previous_content": CLEAN_TEXT})
            assert mp._MIRROR_WORKER.drain(timeout=5.0)
        remember_mock.assert_not_called()
        forget_mock.assert_not_called()
        assert "4111" not in caplog.text
        assert "123-45-6789" not in caplog.text

    def test_on_memory_write_mirror_keeps_clean_content(self, mirror_dirs) -> None:
        from totalreclaw.hermes import memory_provider as mp

        client = MagicMock()
        client.recall = AsyncMock(return_value=[])
        provider = TotalReclawMemoryProvider(_plugin_state(client))
        called: list[dict] = []

        async def fake_remember(args, st, **kw):
            called.append(args)
            return json.dumps({"stored": True, "fact_id": "new"})

        with patch("totalreclaw.hermes.tools.remember", side_effect=fake_remember), \
             patch("totalreclaw.embedding.get_embedding", return_value=[0.1] * 8):
            provider.on_memory_write("add", "user", CLEAN_TEXT)
            assert mp._MIRROR_WORKER.drain(timeout=5.0)
        assert [a["text"] for a in called] == [CLEAN_TEXT]
