"""Unit tests for ``totalreclaw.pair.pair_page`` — the self-contained
browser page served during the local-mode pair flow.

Two things matter here and neither is exercised elsewhere:

  1. ``_escape_js_string`` is the XSS boundary. Every dynamic value the
     Python side injects into the inline ``<script>`` (session id, mode,
     api_base, default mode) flows through it. If it fails to neutralize a
     ``</script>`` sequence or a unicode line separator, an attacker who can
     influence any of those values gets script injection on the page that
     handles the user's recovery phrase.

  2. ``render_pair_page`` must never emit a pre-filled phrase (there is none
     server-side — the phrase is generated/entered in the browser), must
     escape its injected values, and must produce a structurally complete
     page for both ``generate`` and ``import`` modes.

Pure unit tests — no network, no crypto, no browser. Control characters that
cannot appear as literals in Python source (NUL, U+2028, U+2029) are built
with ``chr()`` so this file stays plain ASCII.
"""
from __future__ import annotations

import json
import re

import pytest

from totalreclaw.pair.pair_page import (
    _escape_js_string,
    render_pair_page,
)

LINE_SEP = chr(0x2028)
PARA_SEP = chr(0x2029)
NUL = chr(0)


# ---------------------------------------------------------------------------
# _escape_js_string — the XSS-escaping helper
# ---------------------------------------------------------------------------


class TestEscapeJsStringRoundTrip:
    """Whatever we escape must still decode back to the original string —
    escaping that corrupts the value is a correctness bug, not just a
    security one. The page reads these back as JS string literals, so a
    valid JSON string that round-trips is the contract."""

    @pytest.mark.parametrize(
        "value",
        [
            "abc123",
            "session-id-with-dashes",
            "/pair/deadbeef",
            "",
            "unicode: cafe ☕ 日本語",
            "quotes ' and \"",
            "back\\slash",
            "new\nline\tand\ttab",
        ],
    )
    def test_decodes_back_to_input(self, value):
        escaped = _escape_js_string(value)
        # The escaped form is a JS/JSON string literal. json.loads accepts
        # the \uXXXX escapes we substitute, so a correct escaping decodes
        # back to the original.
        assert json.loads(escaped) == value


class TestEscapeJsStringScriptBreakout:
    """The critical class: a value must not be able to terminate the
    surrounding ``<script>`` element that the HTML parser would honor before
    the JS parser sees it."""

    def test_closing_script_tag_is_neutralized(self):
        payload = "</script><script>alert(1)</script>"
        escaped = _escape_js_string(payload)
        # No raw '<' or '>' may survive — the HTML tokenizer scans for
        # '</script' case-insensitively regardless of JS string context.
        assert "<" not in escaped
        assert ">" not in escaped
        assert "</script" not in escaped.lower()
        # Value integrity preserved.
        assert json.loads(escaped) == payload

    def test_mixed_case_closing_script_tag(self):
        payload = "</ScRiPt >"
        escaped = _escape_js_string(payload)
        assert "<" not in escaped and ">" not in escaped

    def test_angle_brackets_always_escaped(self):
        escaped = _escape_js_string("a < b && b > c")
        assert "<" not in escaped
        assert ">" not in escaped
        assert "&" not in escaped
        assert json.loads(escaped) == "a < b && b > c"

    def test_ampersand_escaped(self):
        # '&' matters because the value can also land in HTML-ish contexts
        # and because it defends against entity-based smuggling.
        escaped = _escape_js_string("Tom & Jerry")
        assert "&" not in escaped
        assert json.loads(escaped) == "Tom & Jerry"


class TestEscapeJsStringUnicodeLineSeparators:
    """U+2028 / U+2029 are valid whitespace in JSON but ILLEGAL raw inside a
    JavaScript string literal — a raw one terminates the statement and breaks
    the script (a classic inline-script injection vector). They must be
    emitted as ``\\u2028`` / ``\\u2029``."""

    def test_line_separator_u2028_escaped(self):
        value = "before" + LINE_SEP + "after"
        escaped = _escape_js_string(value)
        assert LINE_SEP not in escaped
        assert "\\u2028" in escaped
        assert json.loads(escaped) == value

    def test_paragraph_separator_u2029_escaped(self):
        value = "before" + PARA_SEP + "after"
        escaped = _escape_js_string(value)
        assert PARA_SEP not in escaped
        assert "\\u2029" in escaped
        assert json.loads(escaped) == value


class TestEscapeJsStringEdgeChars:
    def test_null_byte_preserved_as_escape(self):
        value = "a" + NUL + "b"
        escaped = _escape_js_string(value)
        # json.dumps encodes NUL as a \u0000 escape; a raw NUL must not survive.
        assert NUL not in escaped
        assert json.loads(escaped) == value

    def test_backslash_not_left_dangling(self):
        # A lone trailing backslash must be doubled so it can't escape the
        # closing quote of the emitted literal.
        escaped = _escape_js_string("path\\")
        assert json.loads(escaped) == "path\\"

    def test_result_is_quoted_literal(self):
        escaped = _escape_js_string("x")
        assert escaped.startswith('"') and escaped.endswith('"')


# ---------------------------------------------------------------------------
# render_pair_page — page generation invariants
# ---------------------------------------------------------------------------


def _render(mode: str = "generate", sid: str = "sess-abc123") -> str:
    return render_pair_page(
        sid=sid,
        mode=mode,
        expires_at_ms=1_700_000_300_000,
        api_base="/pair/token-xyz",
        now_ms=1_700_000_000_000,
    )


class TestRenderPairPageInvariants:
    @pytest.mark.parametrize("mode", ["generate", "import"])
    def test_returns_complete_html_document(self, mode):
        html = _render(mode)
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert "</html>" in html
        # No unreplaced template placeholders.
        for placeholder in (
            "__STYLE__",
            "__SCRIPT__",
            "__PANELS__",
            "__SID_HTML__",
            "__SID_JS__",
            "__MODE_JS__",
            "__API_BASE_JS__",
            "__EXPIRES_AT_MS__",
            "__BIP39_JS__",
            "__DEFAULT_MODE_JS__",
        ):
            assert placeholder not in html, f"unreplaced placeholder {placeholder}"

    def test_generate_mode_renders_generate_panel(self):
        html = _render("generate")
        assert 'id="panel-generate"' in html
        assert 'id="panel-import"' not in html

    def test_import_mode_renders_import_panel(self):
        html = _render("import")
        assert 'id="panel-import"' in html
        assert 'id="panel-generate"' not in html

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            render_pair_page(
                sid="s",
                mode="not-a-mode",
                expires_at_ms=1,
                api_base="/pair/x",
                now_ms=0,
            )

    def test_expires_at_coerced_to_int_literal(self):
        # The expiry must be emitted as an integer literal (no decimal point)
        # into the JS ``EXPIRES_AT_MS`` constant.
        html = _render("generate")
        m = re.search(r"const EXPIRES_AT_MS = (\d+);", html)
        assert m, "EXPIRES_AT_MS constant not found / not an integer literal"
        assert m.group(1) == "1700000300000"


class TestRenderPairPageInjectionSafety:
    def test_sid_with_script_payload_is_escaped(self):
        payload = "</script><script>alert('xss')</script>"
        html = render_pair_page(
            sid=payload,
            mode="generate",
            expires_at_ms=1_700_000_300_000,
            api_base="/pair/x",
            now_ms=0,
        )
        # The raw breakout sequence must not appear anywhere — not in the JS
        # SID constant, not in the HTML session footer.
        assert "<script>alert" not in html
        assert "</script><script>" not in html

    def test_sid_html_context_is_html_escaped(self):
        html = render_pair_page(
            sid='a<b>&"c',
            mode="generate",
            expires_at_ms=1_700_000_300_000,
            api_base="/pair/x",
            now_ms=0,
        )
        # The footer prints the sid in an HTML text context; angle brackets
        # must be entity-escaped there.
        assert "a<b>" not in html
        assert "&lt;b&gt;" in html

    def test_api_base_injection_escaped(self):
        payload = '"; fetch("//evil");//'
        html = render_pair_page(
            sid="s",
            mode="import",
            expires_at_ms=1_700_000_300_000,
            api_base=payload,
            now_ms=0,
        )
        # The raw JS-breaking sequence must not appear verbatim; the value is
        # JSON-encoded into the API_BASE constant, and the escaped literal is
        # present.
        assert 'const API_BASE = "; fetch' not in html
        assert _escape_js_string(payload) in html

    def test_no_prefilled_phrase_server_side(self):
        # The server has no phrase to leak. This asserts the page never ships
        # a pre-populated word input — the word grid is built empty by JS.
        html = _render("import")
        assert 'class="word-input" value=' not in html
        # The generate grid is readonly + JS-filled, never server-filled.
        assert "phrase-grid-generate" in html
