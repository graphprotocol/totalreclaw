"""Hermes plugin: the tools advertised in ``plugin.yaml`` MUST all be
registered by ``register(ctx)``.

Context — user-visible rc.4 regression (2026-04-22)
----------------------------------------------------

A user running ``pip install --pre totalreclaw==2.3.1rc4`` on a
Docker-hosted Hermes gateway reported that the Hermes chat agent could
not see any ``totalreclaw_*`` tool in its toolset — the plugin loaded
cleanly (SKILL.md surfaced, module imported) but the agent's tool list
was empty for our toolset. The user could not invoke ``totalreclaw_pair``
to complete setup; pairing dead-ended on the agent side.

Auto-QA for rc.4 passed on the same bundle because it verified the
registered-tool list via an in-process ``register(ctx)`` call — but
**only checked the set of tools that rc.4's ``__init__.py`` explicitly
registered**. It never cross-checked that list against the manifest
(``plugin.yaml::provides_tools``) or against the set of schemas declared
in ``schemas.py``. As a result the auto-QA tool list happily matched
the register-call list and both drifted together away from the manifest.

What this test enforces
-----------------------

For every agent-facing tool advertised in ``plugin.yaml`` under
``provides_tools``:

    - A matching ``ctx.register_tool(name=..., ...)`` call MUST fire
      during ``register(ctx)`` for a stable install.
    - The tool-names list in ``plugin.yaml`` MUST therefore be a subset
      of the names observed on the mock ``PluginContext``.

This is the regression shield we add as part of 3.3.1-rc.6 / 2.3.1rc6.
The test is designed to FAIL on rc.4 / rc.5 main (where pin + unpin are
declared in the manifest + schemas but never wired into ``register``)
and PASS after the rc.6 fix registers them.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Canonical plugin.yaml location
# ---------------------------------------------------------------------------


_PLUGIN_YAML = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "totalreclaw"
    / "hermes"
    / "plugin.yaml"
)


def _manifest_tool_names() -> list[str]:
    """Read ``plugin.yaml`` and return the list under ``provides_tools``."""
    data = yaml.safe_load(_PLUGIN_YAML.read_text())
    tools = data.get("provides_tools") or []
    if not isinstance(tools, list):
        raise TypeError(
            f"plugin.yaml::provides_tools must be a list, got {type(tools)!r}"
        )
    return [str(t) for t in tools if t]


def _register_with_mock_ctx() -> MagicMock:
    """Call ``totalreclaw.hermes.register(ctx)`` with a fresh mock context.

    Env/fs are patched to a fresh-install state (no credentials file,
    no env vars) so the plugin takes the same code path the failing
    user hit on their Docker-hosted gateway.
    """
    # Late import so any module-level import failure in totalreclaw.hermes
    # surfaces as a pytest error with a stack, not a collection-time crash.
    from totalreclaw.hermes import register

    ctx = MagicMock()
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            register(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPluginYamlParity:
    """plugin.yaml ↔ register() parity contract.

    One assertion per side so a failure tells you which half of the
    contract broke.
    """

    def test_manifest_exists_and_is_parseable(self):
        """plugin.yaml must be present in the package tree and parseable.

        This is a pre-condition for the parity checks below. If this
        fails the rest of the file's failures are meaningless.
        """
        assert _PLUGIN_YAML.exists(), (
            f"plugin.yaml missing at {_PLUGIN_YAML}. The Hermes package is "
            "incomplete; hermes-agent's plugin discovery treats the "
            "manifest as non-optional metadata."
        )
        data = yaml.safe_load(_PLUGIN_YAML.read_text())
        assert isinstance(data, dict), "plugin.yaml must parse as a mapping"
        assert data.get("name") == "totalreclaw"
        assert isinstance(data.get("provides_tools"), list)
        assert len(data["provides_tools"]) > 0

    def test_every_manifest_tool_is_registered(self):
        """Every tool advertised in ``plugin.yaml::provides_tools`` must
        be registered by ``register(ctx)``.

        This is the exact test that would have caught the rc.4 / rc.5
        regression that shipped to a real user on 2026-04-22:
        ``totalreclaw_pin`` and ``totalreclaw_unpin`` are listed in
        plugin.yaml (so the manifest advertises them as available
        agent-facing tools) but no ``ctx.register_tool`` call fires for
        them during ``register()``. Hermes reads plugin.yaml for tool
        listings in the UI but only the ``register()`` call actually
        wires the agent-callable tool — so the agent's toolset is a
        STRICT SUBSET of the manifest, not a match.

        The user's Hermes chat agent saw the SKILL.md (which loads via a
        separate mechanism) but none of the tools it expected.
        """
        manifest_names = set(_manifest_tool_names())
        ctx = _register_with_mock_ctx()

        registered_names = {
            call.kwargs["name"] for call in ctx.register_tool.call_args_list
        }

        missing = sorted(manifest_names - registered_names)

        assert not missing, (
            "Hermes plugin.yaml advertises these tools but register() "
            f"never calls ctx.register_tool() for them: {missing}. "
            "Either wire them into totalreclaw/hermes/__init__.py or "
            "remove them from plugin.yaml — the manifest is the public "
            "contract Hermes shows agents on plugin load."
        )

    def test_register_does_not_register_phrase_unsafe_tools(self):
        """Phrase-safety invariant — rc.4 dropped the phrase-generating
        tool; this test stays here so any future regression surfaces at
        the same choke-point as the manifest parity check.
        """
        ctx = _register_with_mock_ctx()
        registered_names = {
            call.kwargs["name"] for call in ctx.register_tool.call_args_list
        }
        forbidden = {
            "totalreclaw_setup",
            "totalreclaw_onboard",
            "totalreclaw_onboarding_start",
            "totalreclaw_onboard_generate",
            "totalreclaw_restore",
            "totalreclaw_restore_phrase",
        }
        leak = sorted(forbidden & registered_names)
        assert not leak, (
            f"Phrase-safety violation: these agent tools are registered: "
            f"{leak}. Per project_phrase_safety_rule.md, recovery phrases "
            "MUST NEVER cross the LLM context. Route agents to "
            "totalreclaw_pair instead."
        )

    def test_pair_tool_registered_and_advertised(self):
        """``totalreclaw_pair`` is the canonical agent-facilitated setup
        surface. It MUST appear in both the manifest and the register
        call so the Hermes agent can invoke it to start a browser-side
        pairing handshake.
        """
        manifest_names = set(_manifest_tool_names())
        assert "totalreclaw_pair" in manifest_names, (
            "plugin.yaml is missing totalreclaw_pair — the canonical "
            "phrase-safe setup tool. Agents must have a way to start "
            "the pair flow without ever touching the phrase."
        )

        ctx = _register_with_mock_ctx()
        registered_names = {
            call.kwargs["name"] for call in ctx.register_tool.call_args_list
        }
        assert "totalreclaw_pair" in registered_names, (
            "register() never wired totalreclaw_pair — the user's "
            "Hermes chat agent will see no pair tool and pairing flow "
            "dead-ends. This is exactly the rc.4 user-reported bug."
        )
