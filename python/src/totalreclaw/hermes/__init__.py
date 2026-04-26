"""TotalReclaw Hermes Agent Plugin.

Entry point for the Hermes plugin system. Registers tools and hooks
for E2E encrypted AI memory.

This is a thin adapter that wires the generic ``totalreclaw.agent``
layer into Hermes's lifecycle hooks. New agent integrations should
use ``totalreclaw.agent`` directly.
"""
from __future__ import annotations

import logging

from . import schemas, tools, hooks, pair_tool
from .state import PluginState

logger = logging.getLogger(__name__)


def register(ctx):
    """Called by Hermes plugin system at startup."""
    state = PluginState()

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
    # v2.1.0 Phase A parity additions — Stripe checkout + explicit debrief.
    ctx.register_tool(
        name="totalreclaw_upgrade",
        toolset="totalreclaw",
        schema=schemas.UPGRADE,
        handler=lambda args, **kw: tools.upgrade(args, state, **kw),
        is_async=True,
        description=schemas.UPGRADE["description"],
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

    # rc.26 (issue #167 path A): defensive WARN if the user re-enabled
    # Hermes' built-in `memory` tool after install. The SKILL.md install
    # flow auto-disables it (`hermes tools disable memory`), but this
    # belt-and-suspenders check runs on every plugin load to surface the
    # anti-pattern in logs if someone manually re-enabled the built-in.
    # Best-effort — never crashes plugin load. Shells out to the `hermes`
    # CLI; absence of the CLI is silently tolerated.
    try:
        _warn_if_built_in_memory_enabled()
    except Exception:  # pragma: no cover — defensive check must not crash
        logger.debug("built-in memory enablement check skipped", exc_info=True)

    logger.info("TotalReclaw plugin registered (14+ tools, 6 hooks)")


def _warn_if_built_in_memory_enabled() -> None:
    """Check whether Hermes' built-in `memory` tool is currently enabled.

    Emits a single WARN line if it is. No-op (DEBUG only) if the
    `hermes` CLI is not on PATH or if `hermes tools list` doesn't
    expose a parseable enabled-set — we don't want this defensive
    check to spam logs in environments where it can't be answered
    cleanly.

    rc.26 fix for the rc.24 NO-GO finding (issue #167): TotalReclaw
    and Hermes built-in `memory` solve the same problem and compete
    for "remember X" / "recall X" intents during natural conversation.
    The install flow disables the built-in via SKILL.md step 3, but
    this WARN catches users who re-enabled it manually.
    """
    import shutil
    import subprocess

    if shutil.which("hermes") is None:
        logger.debug("hermes CLI not found on PATH; skipping built-in memory check")
        return

    try:
        proc = subprocess.run(
            ["hermes", "tools", "list"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        logger.debug("hermes tools list failed; skipping built-in memory check", exc_info=True)
        return

    if proc.returncode != 0:
        logger.debug(
            "hermes tools list exited with code %d; skipping built-in memory check",
            proc.returncode,
        )
        return

    output = proc.stdout or ""
    # Parse heuristically — match `memory` token followed by an
    # enabled marker. Tolerant of whitespace + alternative formats
    # (`memory: enabled`, `memory  enabled`, table-style, JSON-style).
    lower = output.lower()
    if "memory" in lower and "enabled" in lower:
        # Tighten the match: only WARN if `memory` and `enabled` co-occur
        # on the same line.
        for line in output.splitlines():
            ll = line.lower()
            if "memory" in ll and "enabled" in ll and "disabled" not in ll:
                logger.warning(
                    "Hermes built-in 'memory' tool is enabled. TotalReclaw "
                    "recommends disabling it to avoid intent-stealing. "
                    "Run: hermes tools disable memory"
                )
                return
