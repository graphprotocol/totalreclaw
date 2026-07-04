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

schemas.py ↔ register() (issue #427)
------------------------------------

``TestSchemasRegisterParity`` (below) closes the vacuous-pass hole in the
manifest check: a schema absent from BOTH plugin.yaml AND register()
satisfied the subset check silently. That hole shipped the 2.4.5rc10
``totalreclaw_top_up`` bug (internal#412) — a schema + handler with no
registration anywhere. The new class anchors on ``schemas.py``: every
authored ``totalreclaw_*`` schema must be registered or explicitly
dormant (``DORMANT_SCHEMAS``, with a per-entry reason).
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


# ---------------------------------------------------------------------------
# schemas.py ↔ register() parity (issue #427)
# ---------------------------------------------------------------------------
#
# Why this class exists — the manifest parity contract above is
# necessary but NOT sufficient. It pins ``plugin.yaml::provides_tools``
# to the ``register()`` call list. But a tool schema that is absent from
# BOTH plugin.yaml and register() passes it vacuously: there's nothing to
# be a subset of. That's not hypothetical — this exact bug class shipped
# to users TWICE:
#
#   - 2.3.1 pin/unpin: schemas + manifest present, register() missing.
#     Caught by TestPluginYamlParity above (manifest ⊄ register).
#   - 2.4.5rc10 ``totalreclaw_top_up`` (internal#412, fixed in #423):
#     schema + handler present, but the tool was in NEITHER plugin.yaml
#     NOR register(). The manifest parity check saw nothing missing (the
#     manifest didn't advertise it either) and passed while a shipped,
#     handler-backed tool was silently unreachable by the agent.
#
# The fix is to anchor the triangle at ``schemas.py`` — the module where
# tool schemas are actually authored. Every ``totalreclaw_*`` schema dict
# declared there MUST be either (a) wired into ``register()`` or (b)
# explicitly declared dormant in ``DORMANT_SCHEMAS`` below, with a
# per-entry reason. A new schema that someone forgets to register now
# fails CI instead of shipping dark.


# Schemas that intentionally exist in ``schemas.py`` but are deliberately
# NOT registered as agent tools. Every entry needs a reason comment; a
# schema may only live here if it is *by design* unreachable by the LLM.
#
# Cross-check when editing: ``memory_provider.get_tool_schemas()`` is the
# dormant Path-B MemoryProvider surface (the schemas that provider mode
# exposes). A schema that is registered by ``register()`` OR listed in
# that provider surface is live and must NOT appear here.
DORMANT_SCHEMAS: dict[str, str] = {
    # Phrase-safety: rc.4 removed the ``totalreclaw_setup`` agent tool
    # because its handler could pipe a recovery phrase through the LLM
    # tool-call payload (and, phrase-less, GENERATE + RETURN a fresh
    # mnemonic in the tool response). Both cross the LLM context — a
    # vault-compromise-class violation of project_phrase_safety_rule.md.
    # The SETUP schema dict is kept in schemas.py for the CLI/pair-flow
    # ``state.configure`` code path, but it MUST NEVER be registered as an
    # agent tool. It is also (correctly) absent from
    # ``memory_provider.get_tool_schemas()``. Agents route to
    # ``totalreclaw_pair`` instead.
    "totalreclaw_setup": "phrase-safety — rc.4 removed the agent tool (see __init__.py); use totalreclaw_pair",
}


def _schema_tool_names() -> dict[str, str]:
    """Collect every module-level dict in ``schemas.py`` that carries a
    ``"name"`` key starting with ``totalreclaw_``.

    Returns a mapping of ``tool_name -> module_attribute_name`` so failure
    messages can point at the exact ``schemas.FOO`` dict that drifted.
    """
    from totalreclaw.hermes import schemas

    found: dict[str, str] = {}
    for attr_name, value in vars(schemas).items():
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        if isinstance(name, str) and name.startswith("totalreclaw_"):
            found[name] = attr_name
    return found


def _all_source_schema_names() -> set[str]:
    """Every ``totalreclaw_*`` tool name that has a source schema.

    The register() body may legitimately wire tools whose schemas live
    outside schemas.py — ``pair_tool.PAIR_SCHEMA`` and the RC-gated
    ``qa_bug_report.SCHEMA``. This is the full authored-schema universe
    used to close the inverse (register ⊆ source-schemas) direction.
    """
    from totalreclaw.hermes import pair_tool
    from totalreclaw.hermes.qa_bug_report import SCHEMA as QA_BUG_SCHEMA

    names = set(_schema_tool_names().keys())
    names.add(pair_tool.PAIR_SCHEMA["name"])
    names.add(QA_BUG_SCHEMA["name"])
    return names


class TestSchemasRegisterParity:
    """schemas.py ↔ register() parity contract (issue #427).

    Closes the vacuous-pass hole in ``TestPluginYamlParity``: a schema
    absent from both plugin.yaml and register() used to pass silently.
    Here schemas.py is the anchor — every authored ``totalreclaw_*``
    schema must be registered or explicitly dormant.
    """

    def test_every_schema_is_registered_or_dormant(self):
        """Every ``totalreclaw_*`` schema dict declared in ``schemas.py``
        must be EITHER registered by ``register()`` (stable or RC path)
        OR listed in ``DORMANT_SCHEMAS`` with a reason.

        This is the exact shield that would have caught the 2.4.5rc10
        ``totalreclaw_top_up`` regression (internal#412): the schema +
        handler shipped, but the tool was registered nowhere, so the
        agent could not invoke it. As of #423 TOPUP is registered, so it
        must NOT be in the dormant allow-list.
        """
        schema_names = _schema_tool_names()
        ctx = _register_with_mock_ctx()
        registered_names = {
            call.kwargs["name"] for call in ctx.register_tool.call_args_list
        }

        # A dormant schema that has since been wired is a stale allow-list
        # entry — force its removal so the list can't rot into a blanket
        # exemption.
        stale_dormant = sorted(set(DORMANT_SCHEMAS) & registered_names)
        assert not stale_dormant, (
            "These names are in DORMANT_SCHEMAS but ARE registered by "
            f"register(): {stale_dormant}. Remove them from "
            "DORMANT_SCHEMAS — the allow-list is only for tools that are "
            "intentionally unreachable by the agent."
        )

        unaccounted = sorted(
            name
            for name in schema_names
            if name not in registered_names and name not in DORMANT_SCHEMAS
        )
        assert not unaccounted, (
            "These totalreclaw_* schemas are declared in schemas.py but "
            "are NEITHER registered by register() NOR listed in "
            f"DORMANT_SCHEMAS: {unaccounted}. A shipped schema that is "
            "registered nowhere is invisible to the agent (this is the "
            "2.4.5rc10 totalreclaw_top_up / internal#412 bug class). "
            "Either wire it into totalreclaw/hermes/__init__.py::register "
            "or add it to DORMANT_SCHEMAS with a reason comment. "
            f"(schemas.py attribute names: "
            f"{ {n: schema_names[n] for n in unaccounted} })"
        )

    def test_dormant_setup_is_actually_dormant(self):
        """Guard the seed allow-list entry: ``totalreclaw_setup`` must
        genuinely be unregistered on BOTH the stable and RC paths.

        If a future change re-wires setup as an agent tool, the parity
        test above would pass (it's allow-listed) while a phrase-unsafe
        tool ships. This asserts the dormant claim directly so the
        allow-list can't paper over a real regression.
        """
        assert "totalreclaw_setup" in DORMANT_SCHEMAS
        ctx = _register_with_mock_ctx()
        registered_names = {
            call.kwargs["name"] for call in ctx.register_tool.call_args_list
        }
        assert "totalreclaw_setup" not in registered_names, (
            "totalreclaw_setup is in DORMANT_SCHEMAS (declared unreachable) "
            "but register() wired it as an agent tool. This is a "
            "phrase-safety regression — see project_phrase_safety_rule.md "
            "and the rc.4 removal note in __init__.py."
        )

    def test_every_registered_tool_has_a_source_schema(self):
        """Inverse direction — cheap completeness that keeps the triangle
        closed: every ``totalreclaw_*`` name passed to ``register_tool``
        must trace back to an authored schema (schemas.py,
        ``pair_tool.PAIR_SCHEMA``, or the RC ``qa_bug_report`` schema).

        This catches a register() call that invents a name with no schema
        behind it — e.g. a copy-paste typo in the ``name=`` kwarg — which
        would advertise a tool the model can't get a valid schema for.
        """
        ctx = _register_with_mock_ctx()
        registered_names = {
            call.kwargs["name"]
            for call in ctx.register_tool.call_args_list
            if call.kwargs["name"].startswith("totalreclaw_")
        }
        source_names = _all_source_schema_names()

        orphan = sorted(registered_names - source_names)
        assert not orphan, (
            "register() wires these totalreclaw_* tools but no source "
            f"schema defines them: {orphan}. Every registered tool needs "
            "a schema in schemas.py, pair_tool.PAIR_SCHEMA, or "
            "qa_bug_report.SCHEMA — check for a typo'd name= kwarg in "
            "totalreclaw/hermes/__init__.py::register."
        )
