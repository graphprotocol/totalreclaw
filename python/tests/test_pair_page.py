"""Unit tests for ``totalreclaw.pair.pair_page`` — the self-contained
browser page served during the local-mode pair flow.

  1. The page script is a CONSTANT: per-request values (session id, mode,
     api_base, expiry) travel only as HTML-escaped ``data-*`` attributes on
     ``<body>``. An escaping bug therefore yields HTML injection, which the
     strict CSP (script-src pinned by SHA-256 hash) contains.

  2. ``render_pair_page`` must never emit a pre-filled phrase (there is none
     server-side — the phrase is generated/entered in the browser), must
     escape its injected values, and must produce a structurally complete
     page for both ``generate`` and ``import`` modes.

Pure unit tests — no network, no crypto, no browser.
"""
from __future__ import annotations

import base64
import hashlib
import re

import pytest

from totalreclaw.pair.pair_page import (
    pair_page_csp,
    pair_page_script,
    pair_page_style,
    render_pair_page,
)

SID = "sessAbc123_-sessAbc"  # 20 chars, matches the page's sid rule


def _sha(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def _render(mode: str = "generate", sid: str = SID) -> str:
    return render_pair_page(
        sid=sid,
        mode=mode,
        expires_at_ms=1_700_000_300_000,
        api_base=f"/pair/{sid}",
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
            "__BIP39_JS__",
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
        m = re.search(r'data-expires-at-ms="(\d+)"', html)
        assert m, "expiry attribute not found / not an integer literal"
        assert m.group(1) == "1700000300000"


class TestRenderPairPageInjectionSafety:
    def _scripts(self, html):
        return re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)

    def test_sid_with_script_payload_is_escaped(self):
        payload = "\"></script><script>alert('xss')</script>"
        html = render_pair_page(
            sid=payload,
            mode="generate",
            expires_at_ms=1_700_000_300_000,
            api_base="/pair/x",
            now_ms=0,
        )
        assert "<script>alert" not in html
        assert payload not in html
        scripts = self._scripts(html)
        assert scripts == [pair_page_script()]
        assert 'data-sid="&quot;&gt;&lt;/script&gt;&lt;script&gt;' in html

    def test_sid_html_context_is_html_escaped(self):
        html = render_pair_page(
            sid='a<b>&"c',
            mode="generate",
            expires_at_ms=1_700_000_300_000,
            api_base="/pair/x",
            now_ms=0,
        )
        assert "a<b>" not in html
        assert "&lt;b&gt;" in html

    def test_api_base_injection_escaped(self):
        payload = '"; fetch("//evil");//'
        html = render_pair_page(
            sid=SID,
            mode="import",
            expires_at_ms=1_700_000_300_000,
            api_base=payload,
            now_ms=0,
        )
        assert payload not in html
        assert 'data-api-base="&quot;; fetch(&quot;//evil&quot;);//"' in html
        assert pair_page_script() in html

    def test_placeholder_in_sid_is_not_rescanned(self):
        # A sid that looks like a template placeholder must come out verbatim
        # (single-pass substitution), not be substituted again.
        sid = "__SID_HTML____BODY_ATTRS__"
        html = render_pair_page(
            sid=sid, mode="import", expires_at_ms=1, api_base="/pair/x", now_ms=0
        )
        assert html.count(f'data-sid="{sid}"') == 1
        assert f"Session: {sid}<" in html

    def test_no_prefilled_phrase_server_side(self):
        # The server has no phrase to leak. This asserts the page never ships
        # a pre-populated word input — the word grid is built empty by JS.
        html = _render("import")
        assert 'class="word-input" value=' not in html
        # The generate grid is readonly + JS-filled, never server-filled.
        assert "phrase-grid-generate" in html


class TestStrictCsp:
    def test_header_has_no_unsafe_inline_or_nonce(self):
        csp = pair_page_csp()
        assert "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp
        assert "nonce-" not in csp

    @pytest.mark.parametrize("mode", ["generate", "import"])
    def test_hash_matches_served_script_and_style(self, mode):
        html = _render(mode)
        scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)
        styles = re.findall(r"<style[^>]*>([\s\S]*?)</style>", html)
        assert len(scripts) == 1 and len(styles) == 1
        assert scripts[0] == pair_page_script()
        assert styles[0] == pair_page_style()
        directives = {d.split(" ", 1)[0]: d for d in pair_page_csp().split("; ")}
        assert directives["script-src"] == f"script-src {_sha(scripts[0])}"
        assert directives["style-src"] == f"style-src {_sha(styles[0])}"

    def test_other_directives_present(self):
        parts = pair_page_csp().split("; ")
        for d in (
            "default-src 'none'",
            "img-src data:",
            "connect-src 'self'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
            "require-trusted-types-for 'script'",
            "trusted-types 'none'",
        ):
            assert d in parts

    def test_script_is_independent_of_request_values(self):
        a = _render("generate", sid="AAAAAAAAAAAAAAAAAAAA")
        b = _render("generate", sid="BBBBBBBBBBBBBBBBBBBB")
        sa = re.findall(r"<script[^>]*>([\s\S]*?)</script>", a)
        sb = re.findall(r"<script[^>]*>([\s\S]*?)</script>", b)
        assert sa == sb
        assert "AAAAAAAAAAAAAAAAAAAA" not in sa[0]

    def test_markup_has_no_style_attributes_or_inline_handlers(self):
        for mode in ("generate", "import"):
            html = _render(mode)
            markup = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", html)
            tags = re.findall(r"<[a-zA-Z][^>]*>", markup)
            assert len(tags) > 40
            for t in tags:
                assert not re.search(r"\sstyle\s*=", t, re.I), t
                assert not re.search(r"\son[a-z]+\s*=", t, re.I), t
                assert "javascript:" not in t.lower()

    def test_data_attributes_on_body(self):
        html = _render("import")
        body = re.search(r"<body ([^>]*)>", html).group(1)
        assert f'data-sid="{SID}"' in body
        assert f'data-api-base="/pair/{SID}"' in body
        assert 'data-mode="import"' in body
