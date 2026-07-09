"""Focused unit tests for the pure helpers in ``totalreclaw.hermes.tools``.

``hermes/tools.py`` (~1660 LOC) is otherwise verified only through the
async tool integration paths. These tests pin the small pure helpers that
gate real behavior:

  - ``_is_likely_agent_self_directive`` — the issue #337 / F5 write guard
    (blocks the agent from storing its own operational reasoning).
  - ``_is_allowlisted_export_host`` — the phishing / malicious-download
    guard on import URLs (security-sensitive).
  - ``_provider_name_from_base_url`` — the privacy-disclosure provider label
    derived from the LLM endpoint host.
  - ``_mint_disclosure_token`` / ``_redeem_disclosure_token`` /
    ``_disclosure_consent_ok`` — the one-time consent-token round-trip that
    forces the privacy disclosure to actually reach the user (#421).

Disk-touching helpers are redirected to a tmp dir via ``IMPORT_STATE_DIR``.
No network, no LLM.
"""
from __future__ import annotations

import json

import pytest

from totalreclaw.hermes import tools


# ---------------------------------------------------------------------------
# _is_likely_agent_self_directive
# ---------------------------------------------------------------------------


class TestIsLikelyAgentSelfDirective:
    @pytest.mark.parametrize(
        "text",
        [
            "I'll always use totalreclaw_remember over the built-in memory tool",
            "I will always call totalreclaw_recall before answering",
            "I'll prefer totalreclaw_remember for storing facts",
            "I will route everything through totalreclaw_status",
        ],
    )
    def test_agent_voice_plus_tool_name_blocked(self, text):
        assert tools._is_likely_agent_self_directive(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            # Agent voice but no internal tool name.
            "I'll always prefer dark mode",
            # Tool name but no agent voice (a legit user instruction).
            "please use totalreclaw_remember to store this",
            # Neither.
            "User lives in Lisbon and prefers Python",
            "",
        ],
    )
    def test_only_one_signal_allowed(self, text):
        assert tools._is_likely_agent_self_directive(text) is False

    def test_case_insensitive(self):
        assert tools._is_likely_agent_self_directive(
            "I WILL ALWAYS USE TOTALRECLAW_REMEMBER"
        ) is True


# ---------------------------------------------------------------------------
# _is_allowlisted_export_host
# ---------------------------------------------------------------------------


class TestIsAllowlistedExportHost:
    @pytest.mark.parametrize(
        "url",
        [
            "https://chatgpt.com/backend-api/export",
            "https://openai.com/whatever",
            "https://takeout.google.com/download",
            "https://claude.ai/export.zip",
            "https://anthropic.com/x",
            "https://sub.domain.chatgpt.com/x",  # subdomain allowed
        ],
    )
    def test_allowlisted_hosts_pass(self, url):
        assert tools._is_allowlisted_export_host(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://chatgpt.com/export",  # not https
            "https://evil.com/export",
            "https://storage.googleapis.com/bucket/export",  # deliberately excluded
            "https://chatgpt.com.evil.com/export",  # suffix-match trap
            "https://notchatgpt.com/export",
            "ftp://chatgpt.com/export",
            "not a url",
            "",
        ],
    )
    def test_non_allowlisted_rejected(self, url):
        assert tools._is_allowlisted_export_host(url) is False

    def test_hostname_case_insensitive(self):
        assert tools._is_allowlisted_export_host("https://ChatGPT.com/x") is True


# ---------------------------------------------------------------------------
# _provider_name_from_base_url
# ---------------------------------------------------------------------------


class TestProviderNameFromBaseUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://api.z.ai/v1", "z.ai (GLM)"),
            ("https://open.bigmodel.cn/api", "z.ai (GLM)"),
            ("https://api.openai.com/v1", "OpenAI"),
            ("https://api.anthropic.com/v1", "Anthropic"),
            ("https://api.groq.com/openai/v1", "Groq"),
        ],
    )
    def test_known_providers(self, url, expected):
        assert tools._provider_name_from_base_url(url) == expected

    def test_unknown_host_returns_hostname(self):
        assert tools._provider_name_from_base_url("https://llm.internal.corp/v1") == "llm.internal.corp"

    def test_empty_returns_generic(self):
        assert tools._provider_name_from_base_url("") == "your configured LLM provider"

    def test_unparseable_returns_generic(self):
        # No hostname component → generic fallback, never a crash.
        assert tools._provider_name_from_base_url("not-a-url") == "your configured LLM provider"


# ---------------------------------------------------------------------------
# disclosure-token round trip
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect import-state persistence to a hermetic tmp dir.

    The import subsystem was consolidated into ``totalreclaw.imports.state``;
    ``totalreclaw.import_state`` is now a ``sys.modules`` alias of the same
    module object, so patching the canonical module updates both names —
    including the binding ``tools.py`` reads via ``from totalreclaw import
    import_state``.
    """
    from totalreclaw.imports import state as ist

    d = tmp_path / "import-state"
    monkeypatch.setattr(ist, "IMPORT_STATE_DIR", d)
    return d


class TestDisclosureTokenRoundTrip:
    def test_mint_then_redeem_succeeds_once(self, state_dir):
        token = tools._mint_disclosure_token("chatgpt")
        assert token and token.isalnum()
        # First redeem consumes it.
        assert tools._redeem_disclosure_token("chatgpt", token) is True
        # Second redeem fails (one-time use).
        assert tools._redeem_disclosure_token("chatgpt", token) is False

    def test_redeem_wrong_source_fails(self, state_dir):
        token = tools._mint_disclosure_token("chatgpt")
        assert tools._redeem_disclosure_token("claude", token) is False
        # Token still valid for the correct source (wrong-source redeem did
        # not consume it).
        assert tools._redeem_disclosure_token("chatgpt", token) is True

    @pytest.mark.parametrize("bad", [None, "", 123, "has-dash", "space bar", "x!y"])
    def test_redeem_rejects_malformed_token(self, state_dir, bad):
        assert tools._redeem_disclosure_token("chatgpt", bad) is False

    def test_redeem_unknown_token_fails(self, state_dir):
        assert tools._redeem_disclosure_token("chatgpt", "deadbeefdeadbeef") is False


class TestDisclosureConsentOk:
    def test_non_extraction_source_always_ok(self, state_dir):
        # Sources outside the extraction set (e.g. mem0) never need the
        # cleartext-to-LLM disclosure.
        assert tools._disclosure_consent_ok("mem0", {}) is True

    def test_extraction_source_needs_confirmed_and_valid_token(self, state_dir):
        # No consent yet.
        assert tools._disclosure_consent_ok("chatgpt", {}) is False
        # Confirmed flag alone is not enough — needs a valid one-time token.
        assert tools._disclosure_consent_ok("chatgpt", {"disclosure_confirmed": True}) is False
        # Confirmed + valid token → consent granted.
        token = tools._mint_disclosure_token("chatgpt")
        assert tools._disclosure_consent_ok(
            "chatgpt", {"disclosure_confirmed": True, "disclosure_token": token}
        ) is True

    def test_confirmed_with_bogus_token_rejected(self, state_dir):
        assert tools._disclosure_consent_ok(
            "chatgpt",
            {"disclosure_confirmed": True, "disclosure_token": "notarealtoken12"},
        ) is False

    def test_prior_persisted_consent_short_circuits(self, state_dir, monkeypatch):
        # A persisted ImportState with disclosure_confirmed makes the
        # import_from -> import_batch loop skip the re-prompt.
        # internal#418: the persisted consent must carry the SAME provider
        # label as the current one to be honored.
        from totalreclaw.imports import state as ist

        monkeypatch.setattr(tools, "_extraction_provider_label", lambda: "z.ai (GLM)")
        st = ist.ImportState(
            import_id="imp-1",
            source="chatgpt",
            status="in_progress",
            started_at="2026-07-07T00:00:00Z",
            last_updated="2026-07-07T00:00:00Z",
            disclosure_confirmed=True,
            disclosure_provider="z.ai (GLM)",
        )
        ist.write_import_state(st)
        assert tools._disclosure_consent_ok("chatgpt", {}, resume_id="imp-1") is True


# ---------------------------------------------------------------------------
# _disclosure_required_response — payload shape (provider named, token present)
# ---------------------------------------------------------------------------


class TestDisclosureRequiredResponse:
    def test_payload_names_provider_and_carries_token(self, state_dir, monkeypatch):
        monkeypatch.setattr(tools, "_extraction_provider_label", lambda: "OpenAI — model gpt-x")
        raw = tools._disclosure_required_response("chatgpt", "imp-42")
        payload = json.loads(raw)
        assert payload["disclosure_required"] is True
        assert payload["import_id"] == "imp-42"
        assert payload["llm_provider"] == "OpenAI — model gpt-x"
        # A fresh, redeemable token accompanies the disclosure.
        token = payload["disclosure_token"]
        assert token and token.isalnum()
        assert tools._redeem_disclosure_token("chatgpt", token) is True
        # The verbatim provider name is embedded in the user-facing message.
        assert "OpenAI — model gpt-x" in payload["message"]

    def test_estimate_fields_included_when_provided(self, state_dir, monkeypatch):
        monkeypatch.setattr(tools, "_extraction_provider_label", lambda: "z.ai (GLM)")
        raw = tools._disclosure_required_response(
            "gemini", "imp-9", estimate={"estimated_facts": 120, "estimated_minutes": 3}
        )
        payload = json.loads(raw)
        assert payload["estimated_facts"] == 120
        assert payload["estimated_minutes"] == 3
