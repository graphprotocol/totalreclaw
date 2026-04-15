"""Tests for TotalReclaw LLM-guided fact extraction."""
import json
import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from totalreclaw.hermes.extractor import (
    ExtractedFact,
    _parse_response,
    _truncate_messages,
    extract_facts_heuristic,
    extract_facts_llm,
)
from totalreclaw.hermes.llm_client import (
    LLMConfig,
    detect_llm_config,
    chat_completion,
)


# ---------------------------------------------------------------------------
# _parse_response tests
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_valid_json_array(self):
        response = json.dumps([
            {"text": "User lives in Lisbon", "type": "fact", "importance": 8, "action": "ADD"},
            {"text": "Prefers dark mode", "type": "preference", "importance": 7, "action": "ADD"},
        ])
        facts = _parse_response(response)
        assert len(facts) == 2
        assert facts[0].text == "User lives in Lisbon"
        assert facts[0].type == "fact"
        assert facts[0].importance == 8
        assert facts[0].action == "ADD"
        assert facts[1].text == "Prefers dark mode"

    def test_empty_array(self):
        facts = _parse_response("[]")
        assert facts == []

    def test_malformed_json(self):
        facts = _parse_response("not json at all {{{")
        assert facts == []

    def test_markdown_wrapped_json(self):
        response = '```json\n[{"text": "User is a developer", "type": "fact", "importance": 7, "action": "ADD"}]\n```'
        facts = _parse_response(response)
        assert len(facts) == 1
        assert facts[0].text == "User is a developer"

    def test_markdown_no_language_tag(self):
        response = '```\n[{"text": "User is a developer", "type": "fact", "importance": 7, "action": "ADD"}]\n```'
        facts = _parse_response(response)
        assert len(facts) == 1

    def test_importance_below_threshold_excluded(self):
        response = json.dumps([
            {"text": "Low importance fact here", "type": "fact", "importance": 3, "action": "ADD"},
            {"text": "High importance fact here", "type": "fact", "importance": 8, "action": "ADD"},
        ])
        facts = _parse_response(response)
        assert len(facts) == 1
        assert facts[0].text == "High importance fact here"

    def test_delete_passes_regardless_of_importance(self):
        response = json.dumps([
            {"text": "Outdated info about user", "type": "fact", "importance": 2, "action": "DELETE", "existingFactId": "abc-123"},
        ])
        facts = _parse_response(response)
        assert len(facts) == 1
        assert facts[0].action == "DELETE"
        assert facts[0].existing_fact_id == "abc-123"

    def test_invalid_type_defaults_to_fact(self):
        response = json.dumps([
            {"text": "Some memory with invalid type", "type": "banana", "importance": 8, "action": "ADD"},
        ])
        facts = _parse_response(response)
        assert len(facts) == 1
        assert facts[0].type == "fact"

    def test_invalid_action_defaults_to_add(self):
        response = json.dumps([
            {"text": "Some memory with invalid action", "type": "fact", "importance": 8, "action": "REMOVE"},
        ])
        facts = _parse_response(response)
        assert len(facts) == 1
        assert facts[0].action == "ADD"

    def test_text_truncation_512(self):
        long_text = "x" * 1000
        response = json.dumps([
            {"text": long_text, "type": "fact", "importance": 9, "action": "ADD"},
        ])
        facts = _parse_response(response)
        assert len(facts) == 1
        assert len(facts[0].text) == 512

    def test_short_text_excluded(self):
        response = json.dumps([
            {"text": "hi", "type": "fact", "importance": 9, "action": "ADD"},
        ])
        facts = _parse_response(response)
        assert facts == []

    def test_non_dict_items_skipped(self):
        response = json.dumps(["just a string", 42, None, {"text": "Valid fact right here", "importance": 8}])
        facts = _parse_response(response)
        assert len(facts) == 1
        assert facts[0].text == "Valid fact right here"

    def test_non_array_response(self):
        response = json.dumps({"text": "not an array"})
        facts = _parse_response(response)
        assert facts == []

    def test_importance_clamped(self):
        response = json.dumps([
            {"text": "Over ten importance fact", "type": "fact", "importance": 99, "action": "ADD"},
            {"text": "Negative importance fact", "type": "fact", "importance": -5, "action": "DELETE", "existingFactId": "x"},
        ])
        facts = _parse_response(response)
        assert facts[0].importance == 10
        assert facts[1].importance == 1

    def test_importance_non_numeric_defaults_to_5(self):
        response = json.dumps([
            {"text": "Bad importance value here", "type": "fact", "importance": "high", "action": "DELETE", "existingFactId": "x"},
        ])
        facts = _parse_response(response)
        assert len(facts) == 1
        assert facts[0].importance == 5

    def test_existing_fact_id_snake_case(self):
        response = json.dumps([
            {"text": "Updated memory content", "type": "fact", "importance": 8, "action": "UPDATE", "existing_fact_id": "def-456"},
        ])
        facts = _parse_response(response)
        assert len(facts) == 1
        assert facts[0].existing_fact_id == "def-456"

    def test_all_valid_types(self):
        items = []
        # Phase 2.2: 8 memory types, including the new "rule" category.
        for t in ["fact", "preference", "decision", "episodic", "goal", "context", "summary", "rule"]:
            items.append({"text": f"Test {t} memory value", "type": t, "importance": 8, "action": "ADD"})
        response = json.dumps(items)
        facts = _parse_response(response)
        assert len(facts) == 8
        types = {f.type for f in facts}
        assert types == {"fact", "preference", "decision", "episodic", "goal", "context", "summary", "rule"}

    def test_rule_type_round_trip(self):
        """Phase 2.2: rule-typed facts pass parsing, retain entities, and stay above the importance floor."""
        response = json.dumps([
            {
                "text": "Stop the OpenClaw gateway before rm -rf ~/.totalreclaw/ — async flush can recreate stale files",
                "type": "rule",
                "importance": 8,
                "confidence": 1.0,
                "action": "ADD",
                "entities": [{"name": "OpenClaw gateway", "type": "tool"}],
            },
        ])
        facts = _parse_response(response)
        assert len(facts) == 1
        assert facts[0].type == "rule"
        assert facts[0].importance == 8
        assert facts[0].confidence == 1.0
        assert facts[0].entities is not None
        assert len(facts[0].entities) == 1
        assert facts[0].entities[0].name == "OpenClaw gateway"

    def test_all_valid_actions(self):
        items = [
            {"text": "ADD action memory test", "type": "fact", "importance": 8, "action": "ADD"},
            {"text": "UPDATE action memory test", "type": "fact", "importance": 8, "action": "UPDATE", "existingFactId": "a"},
            {"text": "DELETE action memory test", "type": "fact", "importance": 2, "action": "DELETE", "existingFactId": "b"},
            {"text": "NOOP action memory test", "type": "fact", "importance": 8, "action": "NOOP"},
        ]
        response = json.dumps(items)
        facts = _parse_response(response)
        assert len(facts) == 4
        actions = [f.action for f in facts]
        assert actions == ["ADD", "UPDATE", "DELETE", "NOOP"]


# ---------------------------------------------------------------------------
# _truncate_messages tests
# ---------------------------------------------------------------------------

class TestTruncateMessages:
    def test_basic_truncation(self):
        messages = [
            {"role": "user", "content": "Hello there"},
            {"role": "assistant", "content": "Hi, how can I help?"},
        ]
        result = _truncate_messages(messages)
        assert "[user]: Hello there" in result
        assert "[assistant]: Hi, how can I help?" in result

    def test_respects_max_chars(self):
        messages = [
            {"role": "user", "content": "x" * 100},
            {"role": "assistant", "content": "y" * 100},
        ]
        result = _truncate_messages(messages, max_chars=50)
        # Only the first message should fit (its formatted length is ~108 chars)
        # Actually "[user]: " + 100 x's = 108 chars > 50, so nothing fits
        assert result == ""

    def test_partial_fit(self):
        messages = [
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "x" * 10000},
        ]
        result = _truncate_messages(messages, max_chars=100)
        assert "[user]: short" in result
        assert "x" * 100 not in result

    def test_missing_fields(self):
        messages = [{"role": "user"}, {"content": "no role"}]
        result = _truncate_messages(messages)
        assert "[user]:" in result
        assert "[unknown]: no role" in result


# ---------------------------------------------------------------------------
# detect_llm_config tests
# ---------------------------------------------------------------------------

class TestDetectLLMConfig:
    def test_no_api_keys(self):
        with patch.dict(os.environ, {}, clear=True):
            config = detect_llm_config()
            assert config is None

    def test_anthropic_key_with_configured_model(self):
        """Uses configured_model param — no hardcoded default."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.api_key == "sk-ant-test"
            assert config.api_format == "anthropic"
            assert config.model == "test-model"
            assert "anthropic.com" in config.base_url

    def test_openai_key_with_configured_model(self):
        """Uses configured_model param — no hardcoded default."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.api_key == "sk-test"
            assert config.api_format == "openai"
            assert config.model == "test-model"

    def test_groq_key_with_configured_model(self):
        """Uses configured_model param — no hardcoded default."""
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test"}, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.model == "test-model"
            assert "groq.com" in config.base_url

    def test_no_model_returns_none(self):
        """API key present but no model configured anywhere -> None."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            config = detect_llm_config()
            assert config is None

    def test_openai_model_env_var(self):
        """OPENAI_MODEL env var provides the model name."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4.1-mini"}, clear=True):
            config = detect_llm_config()
            assert config is not None
            assert config.model == "gpt-4.1-mini"

    def test_anthropic_model_env_var(self):
        """ANTHROPIC_MODEL env var provides the model name."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_MODEL": "claude-haiku-4-5-20251001"}, clear=True):
            config = detect_llm_config()
            assert config is not None
            assert config.model == "claude-haiku-4-5-20251001"

    def test_llm_model_env_var(self):
        """LLM_MODEL env var provides the model name as fallback."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "LLM_MODEL": "my-custom-model"}, clear=True):
            config = detect_llm_config()
            assert config is not None
            assert config.model == "my-custom-model"

    def test_configured_model_overrides_env_var(self):
        """configured_model param takes priority over OPENAI_MODEL env var."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "env-model"}, clear=True):
            config = detect_llm_config(configured_model="param-model")
            assert config is not None
            assert config.model == "param-model"

    def test_priority_order(self):
        # Anthropic is first in the list, so it should be picked
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "ant", "OPENAI_API_KEY": "oai"}, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.api_key == "ant"
            assert config.api_format == "anthropic"

    def test_gemini_key_with_configured_model(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "gem-test"}, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.model == "test-model"
            assert config.api_format == "openai"

    def test_google_api_key_fallback(self):
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "goog-test"}, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.api_key == "goog-test"
            assert config.model == "test-model"

    def test_openai_base_url_override(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_BASE_URL": "https://my-proxy.example.com/v1",
        }, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.base_url == "https://my-proxy.example.com/v1"
            assert config.api_format == "openai"

    def test_openai_base_url_strips_trailing_slash(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_BASE_URL": "https://my-proxy.example.com/v1/",
        }, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.base_url == "https://my-proxy.example.com/v1"

    def test_openai_base_url_does_not_affect_anthropic(self):
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OPENAI_BASE_URL": "https://my-proxy.example.com/v1",
        }, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.api_format == "anthropic"
            assert "anthropic.com" in config.base_url

    def test_openai_base_url_does_not_affect_groq(self):
        with patch.dict(os.environ, {
            "GROQ_API_KEY": "gsk-test",
            "OPENAI_BASE_URL": "https://my-proxy.example.com/v1",
        }, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert "groq.com" in config.base_url

    def test_extraction_model_override(self):
        """TOTALRECLAW_EXTRACTION_MODEL overrides everything."""
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "TOTALRECLAW_EXTRACTION_MODEL": "gpt-4.1-nano",
        }, clear=True):
            config = detect_llm_config(configured_model="test-model")
            assert config is not None
            assert config.model == "gpt-4.1-nano"

    def test_extraction_model_overrides_configured_model(self):
        """TOTALRECLAW_EXTRACTION_MODEL beats configured_model param."""
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "TOTALRECLAW_EXTRACTION_MODEL": "gpt-4.1-nano",
        }, clear=True):
            config = detect_llm_config(configured_model="agent-default-model")
            assert config is not None
            assert config.model == "gpt-4.1-nano"

    def test_extraction_model_overrides_env_model(self):
        """TOTALRECLAW_EXTRACTION_MODEL beats OPENAI_MODEL env var."""
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "TOTALRECLAW_EXTRACTION_MODEL": "gpt-4.1-nano",
            "OPENAI_MODEL": "gpt-4o",
        }, clear=True):
            config = detect_llm_config()
            assert config is not None
            assert config.model == "gpt-4.1-nano"


# ---------------------------------------------------------------------------
# chat_completion tests
# ---------------------------------------------------------------------------

class TestChatCompletion:
    @pytest.mark.asyncio
    async def test_openai_format(self):
        config = LLMConfig(
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            api_format="openai",
        )
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={
            "choices": [{"message": {"content": "test response"}}]
        })

        with patch("totalreclaw.agent.llm_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await chat_completion(config, "system", "user prompt")
            assert result == "test response"

    @pytest.mark.asyncio
    async def test_anthropic_format(self):
        config = LLMConfig(
            api_key="sk-ant-test",
            base_url="https://api.anthropic.com/v1",
            model="claude-haiku-4-5-20251001",
            api_format="anthropic",
        )
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={
            "content": [{"type": "text", "text": "anthropic response"}]
        })

        with patch("totalreclaw.agent.llm_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await chat_completion(config, "system", "user prompt")
            assert result == "anthropic response"

    @pytest.mark.asyncio
    async def test_failure_returns_none(self):
        config = LLMConfig(
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            model="gpt-4.1-mini",
            api_format="openai",
        )

        with patch("totalreclaw.agent.llm_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Connection error"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await chat_completion(config, "system", "user prompt")
            assert result is None


# ---------------------------------------------------------------------------
# extract_facts_llm tests
# ---------------------------------------------------------------------------

class TestExtractFactsLLM:
    @pytest.mark.asyncio
    async def test_no_llm_returns_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            messages = [{"role": "user", "content": "I live in Lisbon"}]
            facts = await extract_facts_llm(messages)
            assert facts == []

    @pytest.mark.asyncio
    async def test_with_mocked_llm(self):
        llm_response = json.dumps([
            {"text": "User lives in Lisbon", "type": "fact", "importance": 8, "action": "ADD"},
            {"text": "User prefers dark mode", "type": "preference", "importance": 7, "action": "ADD"},
        ])

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4.1-mini"}, clear=True):
            with patch("totalreclaw.agent.extraction.chat_completion", new_callable=AsyncMock) as mock_chat:
                mock_chat.return_value = llm_response
                messages = [
                    {"role": "user", "content": "I live in Lisbon and I prefer dark mode for everything"},
                    {"role": "assistant", "content": "Got it! I'll remember that."},
                ]
                facts = await extract_facts_llm(messages, mode="turn")
                assert len(facts) == 2
                assert facts[0].text == "User lives in Lisbon"
                assert facts[0].type == "fact"
                assert facts[1].text == "User prefers dark mode"

    @pytest.mark.asyncio
    async def test_with_existing_memories(self):
        llm_response = json.dumps([
            {"text": "User now lives in Berlin", "type": "fact", "importance": 8, "action": "UPDATE", "existingFactId": "old-123"},
        ])

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4.1-mini"}, clear=True):
            with patch("totalreclaw.agent.extraction.chat_completion", new_callable=AsyncMock) as mock_chat:
                mock_chat.return_value = llm_response
                messages = [
                    {"role": "user", "content": "I just moved to Berlin from Lisbon"},
                    {"role": "assistant", "content": "Exciting move!"},
                ]
                existing = [{"id": "old-123", "text": "User lives in Lisbon"}]
                facts = await extract_facts_llm(messages, existing_memories=existing)
                assert len(facts) == 1
                assert facts[0].action == "UPDATE"
                assert facts[0].existing_fact_id == "old-123"

                # Verify existing memories were passed to the prompt
                call_args = mock_chat.call_args
                user_prompt = call_args[0][2]  # third positional arg
                assert "old-123" in user_prompt
                assert "User lives in Lisbon" in user_prompt

    @pytest.mark.asyncio
    async def test_llm_returns_none(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4.1-mini"}, clear=True):
            with patch("totalreclaw.agent.extraction.chat_completion", new_callable=AsyncMock) as mock_chat:
                mock_chat.return_value = None
                messages = [{"role": "user", "content": "Some conversation content here"}]
                facts = await extract_facts_llm(messages)
                assert facts == []

    @pytest.mark.asyncio
    async def test_short_conversation_skipped(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4.1-mini"}, clear=True):
            with patch("totalreclaw.agent.extraction.chat_completion", new_callable=AsyncMock) as mock_chat:
                messages = [{"role": "user", "content": "hi"}]
                facts = await extract_facts_llm(messages)
                assert facts == []
                mock_chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_mode_uses_all_unprocessed(self):
        """Turn mode uses all provided messages — caller scopes to unprocessed."""
        llm_response = json.dumps([])
        messages = [{"role": "user", "content": f"Message number {i} with content"} for i in range(20)]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4.1-mini"}, clear=True):
            with patch("totalreclaw.agent.extraction.chat_completion", new_callable=AsyncMock) as mock_chat:
                mock_chat.return_value = llm_response
                await extract_facts_llm(messages, mode="turn")
                call_args = mock_chat.call_args
                user_prompt = call_args[0][2]
                # All messages should be included (caller handles scoping)
                assert "Message number 0" in user_prompt
                assert "Message number 19" in user_prompt

    @pytest.mark.asyncio
    async def test_full_mode_uses_all(self):
        llm_response = json.dumps([])
        messages = [{"role": "user", "content": f"Message number {i} with content"} for i in range(10)]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-4.1-mini"}, clear=True):
            with patch("totalreclaw.agent.extraction.chat_completion", new_callable=AsyncMock) as mock_chat:
                mock_chat.return_value = llm_response
                await extract_facts_llm(messages, mode="full")
                call_args = mock_chat.call_args
                user_prompt = call_args[0][2]
                assert "Message number 0" in user_prompt
                assert "Message number 9" in user_prompt


# ---------------------------------------------------------------------------
# extract_facts_heuristic tests
# ---------------------------------------------------------------------------

class TestExtractFactsHeuristic:
    def test_preference_extraction(self):
        messages = [
            {"role": "user", "content": "I prefer dark mode for all my editors"},
            {"role": "assistant", "content": "Noted!"},
        ]
        facts = extract_facts_heuristic(messages, 15)
        assert len(facts) >= 1
        assert any("dark mode" in f.text.lower() for f in facts)
        assert all(isinstance(f, ExtractedFact) for f in facts)
        assert all(f.action == "ADD" for f in facts)

    def test_name_extraction(self):
        messages = [
            {"role": "user", "content": "My name is Pedro and I work at TotalReclaw"},
        ]
        facts = extract_facts_heuristic(messages, 15)
        assert len(facts) >= 1
        assert any("pedro" in f.text.lower() for f in facts)

    def test_decision_extraction(self):
        messages = [
            {"role": "user", "content": "I decided to use PostgreSQL for the database"},
        ]
        facts = extract_facts_heuristic(messages, 15)
        assert len(facts) >= 1
        assert any("postgresql" in f.text.lower() for f in facts)

    def test_assistant_messages_ignored(self):
        messages = [
            {"role": "assistant", "content": "I prefer dark mode for all my editors"},
        ]
        facts = extract_facts_heuristic(messages, 15)
        assert facts == []

    def test_max_facts_respected(self):
        messages = [
            {"role": "user", "content": "I prefer dark mode. I like Python. I want a dog. I need coffee. I use vim."},
        ]
        facts = extract_facts_heuristic(messages, 2)
        assert len(facts) <= 2

    def test_short_matches_excluded(self):
        messages = [
            {"role": "user", "content": "I prefer X"},
        ]
        facts = extract_facts_heuristic(messages, 15)
        # "X" is too short (< 10 chars after extraction)
        assert facts == []

    def test_returns_extracted_fact_objects(self):
        messages = [
            {"role": "user", "content": "Remember that the deployment uses Railway hosting"},
        ]
        facts = extract_facts_heuristic(messages, 15)
        assert len(facts) >= 1
        f = facts[0]
        assert isinstance(f, ExtractedFact)
        assert f.type == "fact"
        assert f.importance == 7
        assert f.action == "ADD"
        assert f.existing_fact_id is None
