"""TotalReclaw Hermes Agent Plugin.

Entry point for the Hermes plugin system. Registers tools and hooks
for E2E encrypted AI memory.

This is a thin adapter that wires the generic ``totalreclaw.agent``
layer into Hermes's lifecycle hooks. New agent integrations should
use ``totalreclaw.agent`` directly.

Keep the Hermes memory-provider class names out of this file, comments
included. Hermes 0.22+ classifies an entry-point plugin by a plain text
scan of this module: if the provider base-class name or its register
call appears, the plugin is treated as a memory provider ("exclusive")
and its tools are not loaded until it is the active provider, which
means ``totalreclaw_pair`` is missing exactly when setup needs it.
tests/test_hermes_entrypoint_kind.py enforces this.
"""
from __future__ import annotations

import logging
import sys

from . import schemas, tools, hooks, pair_tool
from .state import PluginState, get_shared_state

logger = logging.getLogger(__name__)


def register(ctx):
    """Called by Hermes plugin system at startup."""
    # Provider conformance §5.3 (#351): the entry-point plugin and the
    # memory-provider sidecar must share ONE PluginState so the provider's
    # per-turn recall/extract and the plugin's tools/hooks observe the same
    # buffer + turn counter + the `_provider_active` single-driver flag.
    state = get_shared_state()

    # 2.3.1 first-run onboarding parity with plugin 3.3.0. Emit the
    # welcome + branch-question copy to stdout the first time the
    # plugin loads on a machine without credentials. ``maybe_emit_welcome``
    # handles all suppression logic (per-process flag, sentinel file,
    # already-onboarded short-circuit) — this call is a no-op for
    # returning users. Never writes the phrase itself; only the welcome
    # surface + ``hermes setup`` pointer.
    try:
        from totalreclaw.onboarding import maybe_emit_welcome
        maybe_emit_welcome()
    except Exception:  # pragma: no cover — welcome is best-effort
        logger.debug("first-run welcome emission skipped", exc_info=True)

    # Option E Phase 2 (#581) — transparent local phrase -> derived-bundle-v1
    # migration. Best-effort: maybe_migrate() itself never raises, but this
    # call site is defended too, since plugin load must never fail on
    # account of this. A no-op on every subsequent boot once migrated
    # (credentials.json version:2 short-circuits inside auto_migrate).
    try:
        from totalreclaw.hermes import auto_migrate
        auto_migrate.maybe_migrate()
    except Exception:  # pragma: no cover — migration is best-effort
        logger.debug("auto-migration skipped", exc_info=True)

    _refresh_provider_sidecar()

    # Register tools. Descriptions mirror the schemas — they are the
    # text the Hermes agent sees when selecting a tool, so they need to
    # clearly distinguish TotalReclaw from any built-in 'memory' tool.
    ctx.register_tool(
        name="totalreclaw_remember",
        toolset="totalreclaw",
        schema=schemas.REMEMBER,
        handler=lambda args, **kw: tools.remember(args, state, **kw),
        is_async=True,
        description=schemas.REMEMBER["description"],
    )
    ctx.register_tool(
        name="totalreclaw_recall",
        toolset="totalreclaw",
        schema=schemas.RECALL,
        handler=lambda args, **kw: tools.recall(args, state, **kw),
        is_async=True,
        description=schemas.RECALL["description"],
    )
    ctx.register_tool(
        name="totalreclaw_forget",
        toolset="totalreclaw",
        schema=schemas.FORGET,
        handler=lambda args, **kw: tools.forget(args, state, **kw),
        is_async=True,
        description=schemas.FORGET["description"],
    )
    ctx.register_tool(
        name="totalreclaw_export",
        toolset="totalreclaw",
        schema=schemas.EXPORT,
        handler=lambda args, **kw: tools.export_all(args, state, **kw),
        is_async=True,
        description=schemas.EXPORT["description"],
    )
    ctx.register_tool(
        name="totalreclaw_status",
        toolset="totalreclaw",
        schema=schemas.STATUS,
        handler=lambda args, **kw: tools.status(args, state, **kw),
        is_async=True,
        description=schemas.STATUS["description"],
    )
    # 2.5.3rc4 — the user picks an extraction model in chat ("use
    # glm-5.3-flash for memories") instead of editing ~/.hermes/.env and
    # restarting. Persists to ~/.totalreclaw/settings.json; read per turn.
    ctx.register_tool(
        name="totalreclaw_set_extraction_model",
        toolset="totalreclaw",
        schema=schemas.SET_EXTRACTION_MODEL,
        handler=lambda args, **kw: tools.set_extraction_model(args, state, **kw),
        is_async=True,
        description=schemas.SET_EXTRACTION_MODEL["description"],
    )
    # 2.3.1rc4 — `totalreclaw_setup` agent tool REMOVED for phrase-safety.
    #
    # rc.3 registered a `totalreclaw_setup` tool whose handler (a) accepted
    # a `recovery_phrase` tool argument, piping the phrase directly through
    # the LLM tool-call payload, and (b) on phrase-less invocations
    # GENERATED a fresh BIP-39 mnemonic and RETURNED it in the tool's JSON
    # response. Either path crosses the LLM context, which is a vault-
    # compromise-class violation of
    # `project_phrase_safety_rule.md` (memory file in the internal repo;
    # the absolute rule: "recovery phrase MUST NEVER cross the LLM context
    # in ANY form"). rc.4 drops the registration entirely. The underlying
    # `state.configure(phrase)` code path is still used — by the pair-
    # flow HTTP handler (browser-side crypto) and by the `totalreclaw
    # setup` CLI (user's own terminal) — but the agent has no direct
    # surface that could leak the phrase.
    #
    # Agents route to `totalreclaw_pair` instead; see registration below.
    ctx.register_tool(
        name="totalreclaw_pair",
        toolset="totalreclaw",
        schema=pair_tool.PAIR_SCHEMA,
        handler=lambda args, **kw: pair_tool.pair(args, state, **kw),
        is_async=True,
        description=pair_tool.PAIR_SCHEMA["description"],
    )
    # 2.3.1rc6 — wire `totalreclaw_pin` + `totalreclaw_unpin` into the
    # agent tool list. Both tools were shipped in Hermes 2.2.2 (tools.py
    # handlers + schemas) and advertised in ``plugin.yaml::provides_tools``
    # from that release onward, but the corresponding ``ctx.register_tool``
    # calls were never added to this ``register()`` body. Downstream
    # effect: the Hermes chat agent's toolset was a strict subset of the
    # manifest, so the agent could not pin/unpin even when the user asked
    # explicitly. Regression surfaced to a real user during rc.4 manual
    # QA on 2026-04-22 (the user's agent couldn't see any TotalReclaw
    # tools on a fresh ``pip install --pre totalreclaw==2.3.1rc4`` into a
    # Docker-hosted Hermes gateway — root cause was this class of drift
    # between the manifest and the register body). Auto-QA missed it
    # because its rc.3/rc.4 enumerations compared ``register()`` output
    # against itself, never against plugin.yaml.
    # See ``python/tests/test_hermes_plugin_manifest_parity.py`` for the
    # regression shield that pins the two lists to each other.
    ctx.register_tool(
        name="totalreclaw_pin",
        toolset="totalreclaw",
        schema=schemas.PIN,
        handler=lambda args, **kw: tools.pin(args, state, **kw),
        is_async=True,
        description=schemas.PIN["description"],
    )
    ctx.register_tool(
        name="totalreclaw_unpin",
        toolset="totalreclaw",
        schema=schemas.UNPIN,
        handler=lambda args, **kw: tools.unpin(args, state, **kw),
        is_async=True,
        description=schemas.UNPIN["description"],
    )
    # 2.3.1rc23 — Hermes Python parity for retype + set_scope (issue #150).
    # Mirrors ``skill/plugin/retype-setscope.ts`` (TS plugin 3.3.1-rc.2+).
    # Cross-client KG parity: a plugin write + Hermes retype on the same
    # fact id surface the new type/scope to either side after subgraph
    # confirmation. ``pin_status`` is preserved across the rewrite (issue
    # #117 / TS PR #114).
    ctx.register_tool(
        name="totalreclaw_retype",
        toolset="totalreclaw",
        schema=schemas.RETYPE,
        handler=lambda args, **kw: tools.retype(args, state, **kw),
        is_async=True,
        description=schemas.RETYPE["description"],
    )
    ctx.register_tool(
        name="totalreclaw_set_scope",
        toolset="totalreclaw",
        schema=schemas.SET_SCOPE,
        handler=lambda args, **kw: tools.set_scope(args, state, **kw),
        is_async=True,
        description=schemas.SET_SCOPE["description"],
    )
    ctx.register_tool(
        name="totalreclaw_import_from",
        toolset="totalreclaw",
        schema=schemas.IMPORT_FROM,
        handler=lambda args, **kw: tools.import_from(args, state, **kw),
        is_async=True,
        description="Import memories from other AI tools",
    )
    ctx.register_tool(
        name="totalreclaw_import_batch",
        toolset="totalreclaw",
        schema=schemas.IMPORT_BATCH,
        handler=lambda args, **kw: tools.import_batch(args, state, **kw),
        is_async=True,
        description="Process one batch of a large import",
    )
    ctx.register_tool(
        name="totalreclaw_import_status",
        toolset="totalreclaw",
        schema=schemas.IMPORT_STATUS,
        handler=lambda args, **kw: tools.import_status(args, state, **kw),
        is_async=True,
        description="Check background import progress",
    )
    ctx.register_tool(
        name="totalreclaw_import_abort",
        toolset="totalreclaw",
        schema=schemas.IMPORT_ABORT,
        handler=lambda args, **kw: tools.import_abort(args, state, **kw),
        is_async=True,
        description="Cancel a running background import",
    )
    # v2.1.0 Phase A parity additions — Stripe checkout + explicit debrief.
    ctx.register_tool(
        name="totalreclaw_upgrade",
        toolset="totalreclaw",
        schema=schemas.UPGRADE,
        handler=lambda args, **kw: tools.upgrade(args, state, **kw),
        is_async=True,
        description=schemas.UPGRADE["description"],
    )
    # #392 top-up packs — one-time over-quota purchases. Schema + handler
    # shipped in 2.4.5rc10 but were never registered here (internal#412:
    # rc10 S5 re-QA found the tool absent from the live manifest, so the
    # agent could not invoke it regardless of prompt steering).
    ctx.register_tool(
        name="totalreclaw_top_up",
        toolset="totalreclaw",
        schema=schemas.TOPUP,
        handler=lambda args, **kw: tools.top_up(args, state, **kw),
        is_async=True,
        description=schemas.TOPUP["description"],
    )
    ctx.register_tool(
        name="totalreclaw_debrief",
        toolset="totalreclaw",
        schema=schemas.DEBRIEF,
        handler=lambda args, **kw: tools.debrief(args, state, **kw),
        is_async=True,
        description=schemas.DEBRIEF["description"],
    )

    # 3.3.1-rc.3 — RC-gated QA bug-report tool. Only registered when the
    # installed package version is a pre-release RC (PEP-440 ``rcN`` or
    # SemVer ``-rc.``). Stable builds never expose the tool to end users.
    try:
        from totalreclaw import __version__ as _pkg_version
        from .qa_bug_report import is_rc_build, report_qa_bug, SCHEMA as QA_BUG_SCHEMA
        if is_rc_build(_pkg_version):
            ctx.register_tool(
                name="totalreclaw_report_qa_bug",
                toolset="totalreclaw",
                schema=QA_BUG_SCHEMA,
                handler=lambda args, **kw: report_qa_bug(args, state, **kw),
                is_async=True,
                description=QA_BUG_SCHEMA["description"],
            )
            logger.info(
                "totalreclaw_report_qa_bug registered (RC build %s — "
                "this tool is hidden in stable releases).",
                _pkg_version,
            )
    except Exception:  # pragma: no cover — registration must not crash plugin load
        logger.debug("QA bug-report tool registration skipped", exc_info=True)

    # Register hooks.
    #
    # ``on_session_end`` is registered as a no-op handler: hermes_cli
    # dispatches it at the end of every ``run_conversation()`` (per user
    # turn), not at true session end, so anything heavy belongs in
    # ``on_session_finalize``. See ``hooks.on_session_end`` docstring +
    # issue #101 for the failure mode this avoids.
    ctx.register_hook("on_session_start", lambda **kw: hooks.on_session_start(state, **kw))
    ctx.register_hook("pre_llm_call", lambda **kw: hooks.pre_llm_call(state, **kw))
    ctx.register_hook("post_llm_call", lambda **kw: hooks.post_llm_call(state, **kw))
    ctx.register_hook("on_session_end", lambda **kw: hooks.on_session_end(state, **kw))
    ctx.register_hook("on_session_finalize", lambda **kw: hooks.on_session_finalize(state, **kw))
    ctx.register_hook("on_session_reset", lambda **kw: hooks.on_session_reset(state, **kw))

    # Fix for internal#97: validate LLM-config resolution once at plugin
    # load and emit a single loud WARNING if it fails, so Hermes 0.10.0
    # schema drift surfaces immediately instead of accumulating one
    # silent DEBUG line per turn.
    try:
        from totalreclaw.agent.llm_client import validate_llm_config_at_load
        _config, reason = validate_llm_config_at_load(context="hermes-plugin-load")
        state._totalreclaw_llm_load_reason = reason
    except Exception:  # pragma: no cover — validation must not crash plugin load
        logger.debug("LLM-config load-time validation skipped", exc_info=True)

    # No check on Hermes' built-in `memory` tool here any more (2.5.3rc4).
    # rc.26 (issue #167) warned when it was enabled, by shelling out to
    # `hermes tools list` (with a recursion guard: that subprocess loads this
    # plugin too). TotalReclaw now runs alongside the built-in memory on
    # purpose and mirrors its writes (``memory_provider.on_memory_write``),
    # so the enabled tool is the expected state.

    # Issue #275 — Path B memory-provider integration.
    # The TotalReclaw provider class lives at
    # ``totalreclaw.hermes.memory_provider``; we deliberately do NOT
    # import / register it here. Activation happens via the sidecar shim
    # at ``$HERMES_HOME/plugins/memory/totalreclaw/__init__.py`` — see
    # ``totalreclaw hermes install-memory-provider``. This separation
    # keeps users with another active provider (Honcho, Byterover, ...)
    # unaffected by plugin import: TR tools stay available, but
    # memory-provider hooks only fire when TR is the active provider.

    logger.info("TotalReclaw plugin registered (14+ tools, 6 hooks)")


def _refresh_provider_sidecar() -> None:
    """Bring the memory-provider sidecar written at pairing up to date.

    The sidecar outlives package upgrades, and older ones carry the old
    self-heal (``--pre``, wrong interpreter on Hermes 0.22+). Rewriting it
    here means the next Hermes update that drops the package is healed by
    the current logic, pinned to this version. Only inside a real Hermes
    process, and only for our own managed file.
    """
    if "hermes_cli" not in sys.modules:
        return
    try:
        from .install_memory_provider import refresh_sidecar, sidecar_path

        refresh_sidecar(sidecar_path())
    except Exception:  # pragma: no cover — best-effort
        logger.debug("sidecar refresh skipped", exc_info=True)
