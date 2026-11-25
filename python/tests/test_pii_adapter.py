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
        with_token = pii.block_notice(pii.check(CARD_TEXT), "abc")
        assert list(with_token) == [
            "stored", "blocked", "category", "message", "override", "confirm_token", "next_step",
        ]
        assert with_token["confirm_token"] == "abc"
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
        assert result.pop("confirm_token")
        assert result.pop("next_step")
        assert result == pii.block_notice(pii.check(CARD_TEXT))
        client.remember.assert_not_called()
        assert "4111" not in caplog.text

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

    def test_schema_declares_confirm_token(self) -> None:
        from totalreclaw.hermes.schemas import REMEMBER

        props = REMEMBER["parameters"]["properties"]
        assert props["confirm_token"]["type"] == "string"
        assert "confirm_token" not in REMEMBER["parameters"]["required"]
        assert "confirm_token" in props["allow_sensitive"]["description"]


IBAN_TEXT = "My IBAN is GB82 WEST 1234 5698 7654 32"  # public documentation IBAN


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def store(monkeypatch):
    clock = _Clock()
    s = pii.ConfirmationStore(clock=clock)
    s.clock = clock
    monkeypatch.setattr(pii, "confirmations", s)
    return s


async def _remember(args, state):
    from totalreclaw.hermes.tools import remember

    with patch("totalreclaw.embedding.get_embedding", return_value=None):
        return json.loads(await remember(args, state))


class TestConfirmToken:
    async def test_first_call_with_allow_sensitive_is_rejected(self, store, caplog) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        res = await _remember({"text": CARD_TEXT, "allow_sensitive": True}, state)
        assert res["blocked"] == "sensitive" and res["confirm_token"]
        client.remember.assert_not_called()
        assert "reason=no_token, category=payment_card" in caplog.text
        assert "4111" not in caplog.text

    async def test_same_turn_retry_rejected_with_new_token(self, store, caplog) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        first = await _remember({"text": CARD_TEXT}, state)
        again = await _remember(
            {"text": CARD_TEXT, "allow_sensitive": True, "confirm_token": first["confirm_token"]}, state
        )
        assert again["blocked"] == "sensitive"
        assert again["confirm_token"] != first["confirm_token"]
        client.remember.assert_not_called()
        assert "reason=same_turn" in caplog.text

    async def test_retry_after_turn_stores_and_consumes_token(self, store, caplog) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        first = await _remember({"text": CARD_TEXT}, state)
        store.note_turn_completed()
        args = {"text": CARD_TEXT, "allow_sensitive": True, "confirm_token": first["confirm_token"]}
        res = await _remember(args, state)
        assert res["stored"] is True
        assert client.remember.call_args.args[0] == CARD_TEXT
        assert "remember storing sensitive text on explicit request" in caplog.text
        again = await _remember(args, state)
        assert again["blocked"] == "sensitive"
        assert "reason=unknown_token" in caplog.text
        assert client.remember.call_count == 1
        assert "4111" not in caplog.text

    async def test_expired_token(self, store, caplog) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        first = await _remember({"text": CARD_TEXT}, state)
        store.note_turn_completed()
        store.clock.t += 30 * 60  # expired at exactly 30:00
        res = await _remember(
            {"text": CARD_TEXT, "allow_sensitive": True, "confirm_token": first["confirm_token"]}, state
        )
        assert res["blocked"] == "sensitive"
        assert "reason=expired" in caplog.text
        client.remember.assert_not_called()

    async def test_category_mismatch(self, store, caplog) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        first = await _remember({"text": CARD_TEXT}, state)
        assert first["category"] == "payment_card"
        store.note_turn_completed()
        res = await _remember(
            {"text": IBAN_TEXT, "allow_sensitive": True, "confirm_token": first["confirm_token"]}, state
        )
        assert res["blocked"] == "sensitive" and res["category"] == "bank_account"
        assert "reason=category_mismatch, category=bank_account" in caplog.text
        assert "GB82" not in caplog.text
        client.remember.assert_not_called()

    async def test_rephrased_allow_text_stores_normally(self, store) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        first = await _remember({"text": CARD_TEXT}, state)
        store.note_turn_completed()
        res = await _remember(
            {"text": CLEAN_TEXT, "allow_sensitive": True, "confirm_token": first["confirm_token"]}, state
        )
        assert res["stored"] is True

    async def test_string_false_with_valid_token_still_blocks(self, store) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        first = await _remember({"text": CARD_TEXT}, state)
        store.note_turn_completed()
        res = await _remember(
            {"text": CARD_TEXT, "allow_sensitive": "false", "confirm_token": first["confirm_token"]}, state
        )
        assert res["blocked"] == "sensitive"
        client.remember.assert_not_called()

    def test_live_token_cap(self, store) -> None:
        tokens = [store.issue("payment_card") for _ in range(40)]
        assert len(store._tokens) == pii.CONFIRM_TOKEN_MAX_LIVE
        assert tokens[0] not in store._tokens and tokens[-1] in store._tokens

    def test_non_string_confirm_token_is_no_token(self, store, caplog) -> None:
        import asyncio

        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        for bad in (123, ["x"]):
            res = asyncio.run(
                _remember({"text": CARD_TEXT, "allow_sensitive": True, "confirm_token": bad}, state)
            )
            assert res["blocked"] == "sensitive"
        assert caplog.text.count("reason=no_token") == 2
        client.remember.assert_not_called()

    async def test_store_error_fails_closed(self, store, caplog) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        first = await _remember({"text": CARD_TEXT}, state)
        store.note_turn_completed()

        def boom(*a, **k):
            raise KeyError("x")

        store.redeem = boom
        res = await _remember(
            {"text": CARD_TEXT, "allow_sensitive": True, "confirm_token": first["confirm_token"]}, state
        )
        assert res["blocked"] == "sensitive"
        assert "remember blocked: confirmation store error (KeyError)" in caplog.text
        client.remember.assert_not_called()

        store.issue = boom
        res = await _remember({"text": CARD_TEXT, "allow_sensitive": True, "confirm_token": "t"}, state)
        assert res["blocked"] == "sensitive" and "confirm_token" not in res
        client.remember.assert_not_called()


def _hook_state() -> PluginState:
    return _plugin_state(_remember_client())


class TestTurnCounterWiring:
    def test_ingest_turn_increments_once(self, store) -> None:
        from totalreclaw.hermes import hooks

        state = _hook_state()
        before = store.turn_seq
        hooks.ingest_turn(state, "u", "a")
        assert store.turn_seq == before + 1

    def test_provider_sync_turn_increments_once_no_double_count(self, store) -> None:
        from totalreclaw.hermes import hooks

        state = _hook_state()
        provider = TotalReclawMemoryProvider(state)
        state._provider_active = True  # single-driver gate: provider drives the turn
        before = store.turn_seq
        provider.sync_turn("u", "a", session_id="s1")
        hooks.post_llm_call(state, user_message="u", assistant_response="a", session_id="s1")
        assert store.turn_seq == before + 1

    def test_hook_drives_turn_when_provider_inactive(self, store) -> None:
        from totalreclaw.hermes import hooks

        state = _hook_state()
        state._provider_active = False
        before = store.turn_seq
        hooks.post_llm_call(state, user_message="u", assistant_response="a", session_id="s1")
        assert store.turn_seq == before + 1


# ---------------------------------------------------------------------------
# Import engine: sensitive facts are dropped before any store call
# ---------------------------------------------------------------------------


def _import_client() -> MagicMock:
    client = MagicMock()
    client.remember = AsyncMock(return_value="id-1")
    client._ensure_chain_id = AsyncMock(return_value=1)  # per-fact path
    return client


class TestImportDropsSensitive:
    @pytest.fixture(autouse=True)
    def _emb(self, monkeypatch):
        import totalreclaw.embedding as emb

        monkeypatch.setattr(emb, "get_embedding", lambda _t: [0.0] * 640)

    async def test_mem0_batch_drops_card_fact(self, caplog) -> None:
        from totalreclaw.import_engine import ImportEngine

        client = _import_client()
        caplog.set_level(logging.DEBUG)
        content = json.dumps({"memories": [
            {"memory": CARD_TEXT}, {"memory": CLEAN_TEXT},
        ]})
        result = await ImportEngine(client=client).process_batch(source="mem0", content=content)
        texts = [c.args[0] for c in client.remember.call_args_list]
        assert texts == [CLEAN_TEXT]
        assert result.facts_stored == 1
        assert "import: dropped 1 sensitive fact(s) (categories=payment_card)" in caplog.text
        assert "4111" not in caplog.text

    async def test_chunk_extraction_path_drops_card_fact(self, caplog) -> None:
        from tests.test_import_crystals import _GEMINI_ONE_CONVO
        from totalreclaw.import_engine import ImportEngine

        client = _import_client()
        caplog.set_level(logging.DEBUG)

        async def fake_extract(messages, timestamp):
            return [
                {"text": CARD_TEXT, "type": "claim", "importance": 8},
                {"text": CLEAN_TEXT, "type": "claim", "importance": 8},
            ]

        result = await ImportEngine(client=client, llm_extract=fake_extract).process_batch(
            source="gemini", content=_GEMINI_ONE_CONVO
        )
        texts = [c.args[0] for c in client.remember.call_args_list]
        assert CLEAN_TEXT in texts and not any("4111" in t for t in texts)
        assert result.facts_stored == len(texts)
        assert "sensitive fact(s) (categories=payment_card)" in caplog.text
        assert "4111" not in caplog.text


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

    def test_session_debrief_skips_crystal_with_sensitive_content(self, caplog) -> None:
        from totalreclaw.agent.lifecycle import session_debrief

        client = _batch_client()
        state = _plugin_state(client)
        for i in range(8):
            state.add_message("user" if i % 2 == 0 else "assistant", f"trip planning turn {i}")
        crystal = Crystal(narrative="Planned the Porto trip.", key_outcomes=[f"Paid with {CARD}"])

        async def fake_generate(*args, **kwargs):
            return crystal

        with patch("totalreclaw.agent.lifecycle.generate_crystal", new=fake_generate):
            with caplog.at_level("INFO", logger="totalreclaw.agent.lifecycle"):
                ids = session_debrief(state, stored_fact_texts=[])
        assert ids == []
        client.remember.assert_not_called()
        # #731: the skip line carries decision + category, never the text.
        skip = [r.getMessage() for r in caplog.records if "Session Crystal skipped" in r.getMessage()]
        assert len(skip) == 1
        assert "decision=" in skip[0] and "category=" in skip[0]
        assert "category=None" not in skip[0]
        assert "Paid with" not in skip[0] and CARD not in skip[0]

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


CARD_TEXT_2 = "my other card is 5500 0000 0000 0004"


class TestConfirmTokenBinding:
    """#718: a token is bound to the value (and the conversation, when known)."""

    async def test_same_value_accepted_whitespace_and_case_insensitive(self, store) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        first = await _remember({"text": CARD_TEXT}, state)
        store.note_turn_completed()
        res = await _remember(
            {"text": "  " + CARD_TEXT.upper().replace(" ", "  ") + " ", "allow_sensitive": True,
             "confirm_token": first["confirm_token"]},
            state,
        )
        assert res["stored"] is True

    async def test_different_value_same_category_rejected_like_invalid_token(self, store, caplog) -> None:
        client = _remember_client()
        state = _plugin_state(client)
        caplog.set_level(logging.DEBUG)
        first = await _remember({"text": CARD_TEXT}, state)
        store.note_turn_completed()
        res = await _remember(
            {"text": CARD_TEXT_2, "allow_sensitive": True, "confirm_token": first["confirm_token"]}, state
        )
        # Same response shape as an invalid token: a fresh block notice.
        assert res["blocked"] == "sensitive" and res["category"] == "payment_card"
        assert res["confirm_token"] != first["confirm_token"]
        assert "reason=value_mismatch" in caplog.text
        assert "5500" not in caplog.text
        client.remember.assert_not_called()
        # The original value still redeems with its own token.
        res2 = await _remember(
            {"text": CARD_TEXT, "allow_sensitive": True, "confirm_token": first["confirm_token"]}, state
        )
        assert res2["stored"] is True

    def test_store_holds_no_plaintext(self) -> None:
        s = pii.ConfirmationStore(clock=_Clock())
        s.issue("payment_card", CARD_TEXT, "conv-a")
        assert "4111" not in repr(s._tokens)

    def test_reuse_rejected(self) -> None:
        s = pii.ConfirmationStore(clock=_Clock())
        t = s.issue("payment_card", CARD_TEXT)
        s.note_turn_completed()
        assert s.redeem(t, "payment_card", CARD_TEXT) is None
        assert s.redeem(t, "payment_card", CARD_TEXT) == "unknown_token"

    def test_expiry_unchanged(self) -> None:
        clock = _Clock()
        s = pii.ConfirmationStore(clock=clock)
        t = s.issue("payment_card", CARD_TEXT)
        s.note_turn_completed()
        clock.t += pii.CONFIRM_TOKEN_TTL_SECONDS
        assert s.redeem(t, "payment_card", CARD_TEXT) == "expired"

    def test_conversation_mismatch_rejected(self) -> None:
        s = pii.ConfirmationStore(clock=_Clock())
        t = s.issue("payment_card", CARD_TEXT, "chat-a")
        s.note_turn_completed("chat-a")
        assert s.redeem(t, "payment_card", CARD_TEXT, "chat-b") == "conversation_mismatch"
        assert s.redeem(t, "payment_card", CARD_TEXT, None) == "conversation_mismatch"
        assert s.redeem(t, "payment_card", CARD_TEXT, "chat-a") is None

    def test_token_issued_without_id_redeemable_with_or_without_one(self) -> None:
        for redeem_conv in (None, "chat-a"):
            s = pii.ConfirmationStore(clock=_Clock())
            t = s.issue("payment_card", CARD_TEXT)
            s.note_turn_completed()
            assert s.redeem(t, "payment_card", CARD_TEXT, redeem_conv) is None

    def test_untracked_conversation_falls_back_to_global_counter(self, caplog) -> None:
        # (a) tool sees "A"; ingest_turn only ever sees "B" (or None).
        caplog.set_level(logging.DEBUG)
        for ingest_id in ("B", None):
            s = pii.ConfirmationStore(clock=_Clock())
            t = s.issue("payment_card", CARD_TEXT, "A")
            assert s.redeem(t, "payment_card", CARD_TEXT, "A") == "same_turn"
            s.note_turn_completed(ingest_id)
            assert s.redeem(t, "payment_card", CARD_TEXT, "A") is None
        assert "used the global counter" in caplog.text
        assert "4111" not in caplog.text

    def test_tracked_conversation_uses_its_own_counter(self, caplog) -> None:
        # (b) both sides see "A": per-conversation counter enforced.
        caplog.set_level(logging.DEBUG)
        s = pii.ConfirmationStore(clock=_Clock())
        s.note_turn_completed("A")  # A is now tracked
        t = s.issue("payment_card", CARD_TEXT, "A")
        s.note_turn_completed("B")
        assert s.redeem(t, "payment_card", CARD_TEXT, "A") == "same_turn"
        s.note_turn_completed("A")
        assert s.redeem(t, "payment_card", CARD_TEXT, "A") is None
        assert "used the conversation counter" in caplog.text

    def test_token_from_a_rejected_with_b_even_if_both_tracked(self) -> None:
        # (c)
        s = pii.ConfirmationStore(clock=_Clock())
        s.note_turn_completed("A")
        s.note_turn_completed("B")
        t = s.issue("payment_card", CARD_TEXT, "A")
        s.note_turn_completed("A")
        s.note_turn_completed("B")
        assert s.redeem(t, "payment_card", CARD_TEXT, "B") == "conversation_mismatch"
        assert s.redeem(t, "payment_card", CARD_TEXT, "A") is None

    def test_other_conversations_turn_does_not_release_token(self) -> None:
        s = pii.ConfirmationStore(clock=_Clock())
        s.note_turn_completed("chat-a")  # chat A is tracked
        t = s.issue("payment_card", CARD_TEXT, "chat-a")
        s.note_turn_completed("chat-b")  # chat B finishes a turn
        assert s.redeem(t, "payment_card", CARD_TEXT, "chat-a") == "same_turn"
        s.note_turn_completed("chat-a")
        assert s.redeem(t, "payment_card", CARD_TEXT, "chat-a") is None

    async def test_session_id_kwarg_binds_conversation(self, store) -> None:
        from totalreclaw.hermes.tools import remember

        client = _remember_client()
        state = _plugin_state(client)

        async def call(args, **kw):
            with patch("totalreclaw.embedding.get_embedding", return_value=None):
                return json.loads(await remember(args, state, **kw))

        store.note_turn_completed("chat-a")  # chat A is tracked
        first = await call({"text": CARD_TEXT}, session_id="chat-a")
        store.note_turn_completed("chat-b")
        args = {"text": CARD_TEXT, "allow_sensitive": True, "confirm_token": first["confirm_token"]}
        assert (await call(args, session_id="chat-a"))["blocked"] == "sensitive"
        client.remember.assert_not_called()
        second = await call({"text": CARD_TEXT}, session_id="chat-a")
        store.note_turn_completed("chat-a")
        args["confirm_token"] = second["confirm_token"]
        assert (await call(args, session_id="chat-b"))["blocked"] == "sensitive"
        client.remember.assert_not_called()
