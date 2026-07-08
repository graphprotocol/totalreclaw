"""Tests for GeminiAdapter format handling.

The Gemini adapter must parse THREE input shapes from a Google Takeout export:
  1. ``MyActivity.json``  -- Google "My Activity" JSON records (the recommended,
     stable export; far less brittle than scraping HTML).
  2. ``My Activity.html`` -- the legacy HTML export (characterization guard).
  3. "Saved info" paste   -- plain-text bullets a user copies from
     gemini.google.com/saved-info (that page is NOT exportable as a file).

These are pure-parser tests: no network, no keys, no relay. They assert the
adapter turns each format into ``ConversationChunk``s the import engine can feed
to LLM extraction.
"""
from __future__ import annotations

import json

from totalreclaw.imports.adapters.gemini_adapter import GeminiAdapter


# ---------------------------------------------------------------------------
# MyActivity.json  (Google Data Portability "My Activity" schema)
# ---------------------------------------------------------------------------

def _activity_record(title: str, time: str, response: str | None = None,
                     header: str = "Gemini Apps", description: str | None = None):
    rec: dict = {
        "header": header,
        "title": title,
        "titleUrl": "https://gemini.google.com/app/abc",
        "time": time,
        "products": ["Gemini Apps"],
        "activityControls": ["Gemini Apps Activity"],
    }
    if response is not None:
        rec["subtitles"] = [{"name": response}]
    if description is not None:
        rec["description"] = description
    return rec


class TestMyActivityJson:
    def test_parses_my_activity_json_into_chunks(self) -> None:
        data = [
            _activity_record(
                "Prompted Plan a 3-day trip to Lisbon",
                "2026-05-14T09:21:03.512Z",
                "Here's a 3-day Lisbon itinerary: day 1 Alfama...",
            ),
            _activity_record(
                "Prompted What's a good pastel de nata recipe?",
                "2026-05-14T09:25:10.000Z",
                "Use puff pastry and an egg custard...",
            ),
        ]
        result = GeminiAdapter().parse(content=json.dumps(data))

        assert result.errors == []
        assert len(result.chunks) >= 1
        assert result.source_metadata["format"] == "gemini-my-activity-json"

        # All messages flattened across chunks
        msgs = [m for c in result.chunks for m in c.messages]
        roles = [m["role"] for m in msgs]
        texts = [m["text"] for m in msgs]
        assert "user" in roles and "assistant" in roles
        # Prompt text present, "Prompted " prefix stripped
        assert any("Plan a 3-day trip to Lisbon" in t for t in texts)
        assert all(not t.startswith("Prompted ") for t in texts)
        # Response captured from subtitles
        assert any("Lisbon itinerary" in t for t in texts)

    def test_json_strips_prompted_prefix_from_user_text(self) -> None:
        data = [_activity_record("Prompted Remember I am vegetarian",
                                 "2026-05-14T10:00:00Z", "Noted.")]
        result = GeminiAdapter().parse(content=json.dumps(data))
        user_msgs = [m["text"] for c in result.chunks for m in c.messages
                     if m["role"] == "user"]
        assert user_msgs == ["Remember I am vegetarian"]

    def test_json_response_falls_back_to_description(self) -> None:
        # No subtitles; response text only in `description`.
        data = [_activity_record("Prompted Tell me a joke",
                                 "2026-05-14T11:00:00Z",
                                 response=None,
                                 description="Why did the chicken cross the road?")]
        result = GeminiAdapter().parse(content=json.dumps(data))
        asst = [m["text"] for c in result.chunks for m in c.messages
                if m["role"] == "assistant"]
        assert any("chicken cross the road" in t for t in asst)

    def test_json_skips_non_gemini_records(self) -> None:
        data = [
            _activity_record("Searched for cat pictures",
                             "2026-05-14T12:00:00Z", header="Search"),
            _activity_record("Prompted What is 2+2?",
                             "2026-05-14T12:01:00Z", "4"),
        ]
        result = GeminiAdapter().parse(content=json.dumps(data))
        texts = [m["text"] for c in result.chunks for m in c.messages]
        assert any("What is 2+2?" in t for t in texts)
        assert not any("cat pictures" in t for t in texts)

    def test_json_title_without_prompted_prefix(self) -> None:
        # Some records lack the "Prompted " prefix; whole title is the prompt.
        data = [_activity_record("How do I boil an egg",
                                 "2026-05-14T13:00:00Z", "Boil water...")]
        result = GeminiAdapter().parse(content=json.dumps(data))
        user_msgs = [m["text"] for c in result.chunks for m in c.messages
                     if m["role"] == "user"]
        assert user_msgs == ["How do I boil an egg"]

    def test_json_preserves_iso8601_timestamp(self) -> None:
        data = [_activity_record("Prompted hi", "2026-05-14T09:21:03.512Z", "hello")]
        result = GeminiAdapter().parse(content=json.dumps(data))
        assert result.chunks[0].timestamp is not None
        assert result.chunks[0].timestamp.startswith("2026-05-14T09:21:03")

    def test_empty_json_array_warns_not_errors(self) -> None:
        result = GeminiAdapter().parse(content="[]")
        assert result.errors == []
        assert result.chunks == []
        assert len(result.warnings) >= 1


# ---------------------------------------------------------------------------
# Saved info paste (plain text, one fact per line)
# ---------------------------------------------------------------------------

class TestSavedInfoPaste:
    def test_parses_saved_info_bullets(self) -> None:
        text = (
            "Saved info\n"
            "- I work as a software engineer\n"
            "- I prefer concise answers\n"
            "* My dog is named Biscuit\n"
        )
        result = GeminiAdapter().parse(content=text)
        assert result.errors == []
        assert result.source_metadata["format"] == "gemini-saved-info-text"
        texts = [m["text"] for c in result.chunks for m in c.messages]
        # Header line dropped, bullet markers stripped
        assert "I work as a software engineer" in texts
        assert "I prefer concise answers" in texts
        assert "My dog is named Biscuit" in texts
        assert all(not t.startswith(("- ", "* ")) for t in texts)
        assert "Saved info" not in texts


# ---------------------------------------------------------------------------
# HTML export (characterization guard for the legacy path during refactor)
# ---------------------------------------------------------------------------

# NOTE: the real Takeout HTML separates "Prompted" from the prompt with a
# non-breaking space (U+00A0); the parser keys on that exact byte.
_HTML_SAMPLE = (
    '<div class="outer-cell foo"><div class="content-cell">'
    'Prompted What is the capital of Portugal?<br>'
    '1 Apr 2026, 18:39:35 WEST<br>'
    'The capital of Portugal is Lisbon.'
    '</div><div class="content-cell">details</div></div>'
)


class TestHtmlExportStillWorks:
    def test_html_export_parses_to_chunks(self) -> None:
        result = GeminiAdapter().parse(content=_HTML_SAMPLE)
        assert result.errors == []
        assert result.source_metadata["format"] == "gemini-takeout-html"
        texts = [m["text"] for c in result.chunks for m in c.messages]
        assert any("capital of Portugal" in t for t in texts)
        assert any("Lisbon" in t for t in texts)

    def test_html_handles_sept_4letter_month(self) -> None:
        # Real Google Takeout (en-GB locale) writes September as "Sept" (4 chars).
        # The 3-char month regex silently dropped these (~45% of one real export).
        html = (
            '<div class="outer-cell x"><div class="content-cell">'
            'Prompted What is Scientology?<br>'
            '15 Sept 2024, 00:49:15 WEST<br>'
            'Scientology is a set of beliefs and practices.'
            '</div><div class="content-cell">d</div></div>'
        )
        result = GeminiAdapter().parse(content=html)
        assert result.errors == []
        texts = [m["text"] for c in result.chunks for m in c.messages]
        assert any("Scientology" in t for t in texts), (
            f'"Sept" month must parse, not drop the entry. Got chunks: {result.chunks}'
        )
        # Timestamp resolves to September (month 09).
        ts = result.chunks[0].timestamp
        assert ts is not None and "-09-" in ts, f"expected September, got {ts}"
