"""TotalReclaw Hermes Agent Plugin.

Entry point for the Hermes plugin system. Registers tools and hooks
for E2E encrypted AI memory.

This is a thin adapter that wires the generic ``totalreclaw.agent``
layer into Hermes's lifecycle hooks. New agent integrations should
use ``totalreclaw.agent`` directly.
"""
from __future__ import annotations

import logging

from . import schemas, tools, hooks
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
    ctx.register_tool(
        name="totalreclaw_setup",
        toolset="totalreclaw",
        schema=schemas.SETUP,
        handler=lambda args, **kw: tools.setup(args, state, **kw),
        description=schemas.SETUP["description"],
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

    # Register hooks
    ctx.register_hook("on_session_start", lambda **kw: hooks.on_session_start(state, **kw))
    ctx.register_hook("pre_llm_call", lambda **kw: hooks.pre_llm_call(state, **kw))
    ctx.register_hook("post_llm_call", lambda **kw: hooks.post_llm_call(state, **kw))
    ctx.register_hook("on_session_end", lambda **kw: hooks.on_session_end(state, **kw))

    logger.info("TotalReclaw plugin registered (10 tools, 4 hooks)")
