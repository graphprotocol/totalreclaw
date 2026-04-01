"""Tests for TotalReclaw Hermes debrief module."""
import json

import pytest

from totalreclaw.hermes.debrief import (
    DEBRIEF_SYSTEM_PROMPT,
    DebriefItem,
    parse_debrief_response,
)


class TestParseDebriefResponse:
    def test_valid_json_array(self):
        data = [
            {"text": "Session was about refactoring the auth module", "type": "summary", "importance": 8},
            {"text": "Migration to new API is still pending", "type": "context", "importance": 7},
        ]
        result = parse_debrief_response(json.dumps(data))
        assert len(result) == 2
        assert result[0].type == "summary"
        assert result[0].importance == 8
        assert result[1].type == "context"

    def test_empty_array(self):
        assert parse_debrief_response("[]") == []

    def test_strips_markdown_fences(self):
        data = '```json\n[{"text": "Session summary here with enough text", "type": "summary", "importance": 8}]\n```'
        result = parse_debrief_response(data)
        assert len(result) == 1

    def test_strips_bare_fences(self):
        data = '```\n[{"text": "Session summary here with enough text", "type": "context", "importance": 7}]\n```'
        result = parse_debrief_response(data)
        assert len(result) == 1

    def test_caps_at_5_items(self):
        items = [
            {"text": f"Debrief item number {i+1} with enough text", "type": "summary", "importance": 7}
            for i in range(8)
        ]
        result = parse_debrief_response(json.dumps(items))
        assert len(result) == 5

    def test_filters_importance_below_6(self):
        data = [
            {"text": "Important finding from the session", "type": "summary", "importance": 8},
            {"text": "Trivial detail that should be filtered", "type": "context", "importance": 3},
        ]
        result = parse_debrief_response(json.dumps(data))
        assert len(result) == 1
        assert result[0].importance == 8

    def test_importance_exactly_6_passes(self):
        data = [{"text": "Borderline importance item at exactly six", "type": "summary", "importance": 6}]
        result = parse_debrief_response(json.dumps(data))
        assert len(result) == 1

    def test_importance_exactly_5_filtered(self):
        data = [{"text": "Below threshold importance item at five", "type": "summary", "importance": 5}]
        result = parse_debrief_response(json.dumps(data))
        assert len(result) == 0

    def test_validates_type_defaults_to_context(self):
        data = [
            {"text": "Valid summary item for the session", "type": "summary", "importance": 7},
            {"text": "This has an invalid type value set", "type": "fact", "importance": 7},
        ]
        result = parse_debrief_response(json.dumps(data))
        assert len(result) == 2
        assert result[0].type == "summary"
        assert result[1].type == "context"

    def test_handles_invalid_json(self):
        assert parse_debrief_response("not json at all") == []

    def test_handles_non_array_json(self):
        assert parse_debrief_response('{"text": "not an array"}') == []

    def test_handles_empty_string(self):
        assert parse_debrief_response("") == []

    def test_filters_short_text(self):
        data = [
            {"text": "ok", "type": "summary", "importance": 8},
            {"text": "This is a valid debrief item text", "type": "summary", "importance": 8},
        ]
        result = parse_debrief_response(json.dumps(data))
        assert len(result) == 1

    def test_defaults_importance_to_7(self):
        data = [{"text": "A debrief item without importance score", "type": "summary"}]
        result = parse_debrief_response(json.dumps(data))
        assert len(result) == 1
        assert result[0].importance == 7

    def test_clamps_importance_to_10(self):
        data = [{"text": "Huge importance value far above maximum", "type": "summary", "importance": 99}]
        result = parse_debrief_response(json.dumps(data))
        assert result[0].importance == 10

    def test_truncates_text_to_512(self):
        data = [{"text": "x" * 600, "type": "summary", "importance": 8}]
        result = parse_debrief_response(json.dumps(data))
        assert len(result[0].text) == 512

    def test_skips_non_dict_entries(self):
        data = ["just a string", {"text": "Valid debrief item with content", "type": "summary", "importance": 7}, 42]
        result = parse_debrief_response(json.dumps(data))
        assert len(result) == 1


class TestDebriefPrompt:
    def test_contains_key_sections(self):
        assert "Broader context" in DEBRIEF_SYSTEM_PROMPT
        assert "Outcomes & conclusions" in DEBRIEF_SYSTEM_PROMPT
        assert "What was attempted" in DEBRIEF_SYSTEM_PROMPT
        assert "Relationships" in DEBRIEF_SYSTEM_PROMPT
        assert "Open threads" in DEBRIEF_SYSTEM_PROMPT
        assert "Maximum 5 items" in DEBRIEF_SYSTEM_PROMPT
        assert "{already_stored_facts}" in DEBRIEF_SYSTEM_PROMPT
        assert "summary|context" in DEBRIEF_SYSTEM_PROMPT

    def test_starts_with_canonical_text(self):
        assert DEBRIEF_SYSTEM_PROMPT.startswith("You are reviewing a conversation that just ended.")

    def test_ends_with_empty_return(self):
        assert DEBRIEF_SYSTEM_PROMPT.strip().endswith("return: []")
