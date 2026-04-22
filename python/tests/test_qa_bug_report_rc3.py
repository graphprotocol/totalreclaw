"""Tests for the RC-gated QA bug-report tool (Hermes edition)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from totalreclaw.hermes.qa_bug_report import (
    REDACTED,
    SCHEMA,
    build_issue_body,
    is_rc_build,
    post_qa_bug_issue,
    redact_secrets,
    validate_args,
)


# ---------------------------------------------------------------------------
# is_rc_build
# ---------------------------------------------------------------------------


class TestIsRcBuild:
    def test_semver_rc(self):
        assert is_rc_build("3.3.1-rc.3")
        assert is_rc_build("3.3.1-rc.0")
        assert is_rc_build("1.0.0-rc.1")

    def test_pep440_rc(self):
        assert is_rc_build("2.3.1rc3")
        assert is_rc_build("2.3.1rc0")

    def test_stable(self):
        assert not is_rc_build("3.3.1")
        assert not is_rc_build("2.3.0")

    def test_beta_not_rc(self):
        assert not is_rc_build("3.3.1-beta.1")

    def test_empty(self):
        assert not is_rc_build("")
        assert not is_rc_build(None)


# ---------------------------------------------------------------------------
# redact_secrets
# ---------------------------------------------------------------------------


class TestRedactSecrets:
    def test_bip39_12_words(self):
        phrase = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )
        out = redact_secrets(f"my recovery is {phrase} please help")
        assert phrase not in out
        assert REDACTED in out

    def test_bip39_24_words(self):
        phrase = (
            "legal winner thank year wave sausage worth useful "
            "legal winner thank year wave sausage worth useful "
            "legal winner thank year wave sausage worth title"
        )
        out = redact_secrets(f"recovery: {phrase} end")
        assert "legal winner" not in out

    def test_openai_sk_key(self):
        out = redact_secrets("OPENAI_API_KEY=sk-abc123XYZ456DEF789012ABC")
        assert "sk-abc123" not in out
        assert REDACTED in out

    def test_google_key(self):
        out = redact_secrets("GOOGLE_API_KEY=AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz012345678")
        assert "AIzaSyAbCdEfGhIjKlMn" not in out

    def test_telegram_token(self):
        token = "1234567890:AAHdqTcvGhLxjkM12345_hjklzxcv67890abcd"
        out = redact_secrets(f"TELEGRAM_BOT_TOKEN={token}")
        assert "AAHdqTcvGhLx" not in out

    def test_bearer_token(self):
        out = redact_secrets("Authorization: Bearer a1b2c3d4e5f67890abcdef12345678901234567890abcdef")
        # Header name survives.
        assert "authorization" in out.lower()
        assert REDACTED in out

    def test_hex_blob(self):
        out = redact_secrets("authKey=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
        assert "0123456789abcdef0123456789abcdef" not in out

    def test_eth_private_key(self):
        out = redact_secrets("0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
        assert "0x0123456789" not in out

    def test_preserves_uuid(self):
        out = redact_secrets("fact_id=abc12345-def6-7890-abcd-ef0123456789")
        assert "abc12345-def6-7890" in out

    def test_preserves_commit_sha(self):
        out = redact_secrets("commit=1234567890abcdef1234567890abcdef12345678")
        assert "1234567890abcdef" in out

    def test_empty(self):
        assert redact_secrets("") == ""
        assert redact_secrets(None) == ""


# ---------------------------------------------------------------------------
# validate_args
# ---------------------------------------------------------------------------


VALID_ARGS: dict = {
    "integration": "hermes",
    "rc_version": "2.3.1rc3",
    "severity": "high",
    "title": "Auto-extract fails on zai",
    "symptom": "No facts extracted after 5 turns",
    "expected": "Facts should be extracted",
    "repro": "1. ...\n2. ...",
    "logs": "warning: no facts",
    "environment": "VPS, zai, Hermes 2.3.1rc3",
}


class TestValidateArgs:
    def test_valid(self):
        assert validate_args(VALID_ARGS) is None

    def test_unknown_integration(self):
        bad = {**VALID_ARGS, "integration": "unknown"}
        assert "integration" in validate_args(bad)

    def test_unknown_severity(self):
        bad = {**VALID_ARGS, "severity": "critical"}
        assert "severity" in validate_args(bad)

    def test_title_too_long(self):
        bad = {**VALID_ARGS, "title": "x" * 70}
        assert "60" in validate_args(bad)

    def test_missing_field(self):
        bad = {**VALID_ARGS}
        del bad["symptom"]
        assert "symptom" in validate_args(bad)

    def test_non_string_field(self):
        bad = {**VALID_ARGS, "symptom": 42}
        assert "symptom" in validate_args(bad)


# ---------------------------------------------------------------------------
# build_issue_body
# ---------------------------------------------------------------------------


class TestBuildIssueBody:
    def test_contains_section_headers(self):
        body = build_issue_body(VALID_ARGS)
        assert "### What happened" in body
        assert "### Environment" in body
        assert "Hermes Python" in body  # integration display name
        assert "2.3.1rc3" in body

    def test_redacts_each_field(self):
        args_with_secret = {
            **VALID_ARGS,
            "logs": "sk-abc123XYZ456DEF789012ABC",
            "environment": "phrase abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        }
        body = build_issue_body(args_with_secret)
        assert "sk-abc123" not in body
        assert "abandon abandon abandon" not in body


# ---------------------------------------------------------------------------
# post_qa_bug_issue
# ---------------------------------------------------------------------------


class TestPostQaBugIssue:
    @pytest.mark.asyncio
    async def test_success_path(self):
        captured: dict = {}

        async def fake_post(url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return MagicMock(
                status_code=201,
                text="",
                json=lambda: {
                    "number": 42,
                    "html_url": "https://github.com/p-diogo/totalreclaw-internal/issues/42",
                },
            )

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=fake_post)

        result = await post_qa_bug_issue(
            VALID_ARGS,
            github_token="gh-test-token",
            http_client=mock_client,
        )
        assert result["issue_number"] == 42
        assert "/issues/42" in result["issue_url"]
        assert captured["url"].endswith("/repos/p-diogo/totalreclaw-internal/issues")
        assert captured["headers"]["Authorization"] == "Bearer gh-test-token"
        assert captured["headers"]["Accept"] == "application/vnd.github+json"
        assert captured["json"]["title"].startswith("[qa-bug]")
        assert "qa-bug" in captured["json"]["labels"]
        assert "severity:high" in captured["json"]["labels"]
        assert "component:hermes" in captured["json"]["labels"]
        assert any(l.startswith("rc:") for l in captured["json"]["labels"])

    @pytest.mark.asyncio
    async def test_secrets_redacted_before_post(self):
        captured: dict = {}

        async def fake_post(url, headers=None, json=None):
            captured["json"] = json
            return MagicMock(
                status_code=201,
                text="",
                json=lambda: {"number": 1, "html_url": "https://example/1"},
            )

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=fake_post)

        bad_args = {
            **VALID_ARGS,
            "logs": "TELEGRAM_BOT_TOKEN=1234567890:AAHdqTcvGhLxjkM12345_hjklzxcv67890abcd leaked",
            "environment": "with mnemonic abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        }
        await post_qa_bug_issue(bad_args, github_token="gh-token", http_client=mock_client)
        body = captured["json"]["body"]
        assert "AAHdqTcvGhLx" not in body
        assert "abandon abandon abandon" not in body
        assert REDACTED in body

    @pytest.mark.asyncio
    async def test_github_500_raises(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=MagicMock(
            status_code=500,
            text="Internal Server Error",
            json=lambda: {},
        ))
        with pytest.raises(RuntimeError) as exc_info:
            await post_qa_bug_issue(
                VALID_ARGS, github_token="gh-token", http_client=mock_client,
            )
        assert "500" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_missing_token(self):
        with pytest.raises(RuntimeError) as exc_info:
            await post_qa_bug_issue(VALID_ARGS, github_token="")
        assert "github_token" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_invalid_args(self):
        bad = {**VALID_ARGS, "integration": "bogus"}
        with pytest.raises(RuntimeError) as exc_info:
            await post_qa_bug_issue(bad, github_token="gh-token")
        assert "integration" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Tool schema sanity
# ---------------------------------------------------------------------------


class TestSchema:
    def test_tool_name(self):
        assert SCHEMA["name"] == "totalreclaw_report_qa_bug"

    def test_required_fields(self):
        required = set(SCHEMA["parameters"]["required"])
        assert required == {
            "integration", "rc_version", "severity", "title",
            "symptom", "expected", "repro", "logs", "environment",
        }

    def test_integration_enum(self):
        enum = set(SCHEMA["parameters"]["properties"]["integration"]["enum"])
        assert "plugin" in enum
        assert "hermes" in enum
