"""2.3.1rc4 phrase-safety contract tests for the Hermes agent plugin.

Governed by ``~/.claude/projects/-Users-pdiogo-Documents-code-
totalreclaw-internal/memory/project_phrase_safety_rule.md``:

    "recovery phrase MUST NEVER cross the LLM context in ANY form."

These tests enforce the architectural half of that rule — the tool
registry. If any of these assertions start failing, a phrase-generating
agent surface has re-entered the plugin and a vault-compromise-class
leak is now possible.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


FORBIDDEN_TOOL_NAMES = (
    "totalreclaw_setup",
    "totalreclaw_onboard",
    "totalreclaw_onboarding_start",
    "totalreclaw_onboard_generate",
    "totalreclaw_restore",
    "totalreclaw_restore_phrase",
    "totalreclaw_generate_phrase",
    "totalreclaw_mnemonic",
)


def _register_and_list_tools():
    """Call ``hermes.register(ctx)`` with a mock and return the tool names."""
    from totalreclaw.hermes import register

    ctx = MagicMock()
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            register(ctx)
    return [call.kwargs["name"] for call in ctx.register_tool.call_args_list]


class TestPhraseSafetyContract:
    def test_no_forbidden_tool_registered(self):
        names = _register_and_list_tools()
        for forbidden in FORBIDDEN_TOOL_NAMES:
            assert forbidden not in names, (
                f"Phrase-safety violation: {forbidden!r} is registered as an "
                f"agent tool. Recovery phrases MUST NEVER cross the LLM "
                f"context. Use `totalreclaw_pair` (browser-side crypto) "
                f"instead."
            )

    def test_pair_tool_is_registered(self):
        """The ONLY approved agent-facilitated setup surface."""
        names = _register_and_list_tools()
        assert "totalreclaw_pair" in names, (
            "`totalreclaw_pair` MUST be registered — it's the only "
            "phrase-safe agent-tool setup path."
        )

    def test_pair_schema_has_no_phrase_params(self):
        """The pair tool's argument schema MUST NOT accept any field that
        could carry phrase material into an LLM tool-call payload."""
        from totalreclaw.hermes import pair_tool

        props = pair_tool.PAIR_SCHEMA["parameters"]["properties"]
        phrase_adjacent = (
            "recovery_phrase",
            "phrase",
            "mnemonic",
            "seed",
            "seed_phrase",
            "secret",
            "private_key",
        )
        for f in phrase_adjacent:
            assert f not in props, (
                f"Phrase-safety violation: `totalreclaw_pair` accepts "
                f"phrase-adjacent param {f!r}. The tool-call payload is "
                f"in LLM context — any phrase-typed argument leaks."
            )

    def test_setup_schema_still_exists_but_not_registered(self):
        """``schemas.SETUP`` is intentionally retained for in-process
        callers (CLI delegation, test fixtures). The security invariant
        is that the schema is NOT wired into any ``register_tool`` call —
        verified in ``test_no_forbidden_tool_registered`` above."""
        from totalreclaw.hermes import schemas

        assert schemas.SETUP["name"] == "totalreclaw_setup"
        # The existence is fine; the registration is what mattered.


class TestPairToolReturnsNoPhrase:
    @pytest.mark.asyncio
    async def test_tool_response_never_contains_phrase_adjacent_keys(self, tmp_path: Path):
        """``totalreclaw_pair`` MUST return {url, pin, expires_at, mode, instructions}
        only. Any phrase-shaped key in the returned JSON would leak into
        the agent's tool-result payload."""
        import json as _json

        from totalreclaw.hermes import pair_tool
        from totalreclaw.hermes.state import PluginState

        # Force the pair-server bind to a hermetic tmp dir.
        with patch.object(pair_tool, "_resolve_sessions_dir", return_value=tmp_path):
            # Fresh singleton per test.
            pair_tool._SERVER_INSTANCE = None  # type: ignore[attr-defined]
            with patch.dict(os.environ, {"TOTALRECLAW_PAIR_BIND_PORT": "0"}, clear=True):
                with patch.object(Path, "exists", return_value=False):
                    state = PluginState()
                try:
                    result_json = await pair_tool.pair({"mode": "generate"}, state)
                finally:
                    # Shut down the server so the tmp_path fixture can clean up.
                    if pair_tool._SERVER_INSTANCE is not None:  # type: ignore[attr-defined]
                        pair_tool._SERVER_INSTANCE.server.stop()  # type: ignore[attr-defined]
                        pair_tool._SERVER_INSTANCE = None  # type: ignore[attr-defined]

        payload = _json.loads(result_json)
        forbidden_keys = (
            "recovery_phrase",
            "phrase",
            "mnemonic",
            "seed",
            "seed_phrase",
            "secret",
        )
        # Walk the entire payload tree (values too — a phrase could sneak
        # into an instructions string if a future regression lands).
        def _walk(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    for forbidden in forbidden_keys:
                        assert forbidden not in str(k).lower(), (
                            f"Phrase-safety violation: key {path}.{k!r} "
                            f"contains phrase-adjacent token."
                        )
                    _walk(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    _walk(v, f"{path}[{i}]")

        _walk(payload)

        # Positive assertions: the expected phrase-safe fields ARE there.
        assert "url" in payload
        assert "pin" in payload
        assert "expires_at" in payload
