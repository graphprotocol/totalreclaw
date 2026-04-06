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

    # Register tools
    ctx.register_tool(
        name="totalreclaw_remember",
        toolset="totalreclaw",
        schema=schemas.REMEMBER,
        handler=lambda args, **kw: tools.remember(args, state, **kw),
        is_async=True,
        description="Store a memory in TotalReclaw",
    )
    ctx.register_tool(
        name="totalreclaw_recall",
        toolset="totalreclaw",
        schema=schemas.RECALL,
        handler=lambda args, **kw: tools.recall(args, state, **kw),
        is_async=True,
        description="Search memories in TotalReclaw",
    )
    ctx.register_tool(
        name="totalreclaw_forget",
        toolset="totalreclaw",
        schema=schemas.FORGET,
        handler=lambda args, **kw: tools.forget(args, state, **kw),
        is_async=True,
        description="Delete a memory from TotalReclaw",
    )
    ctx.register_tool(
        name="totalreclaw_export",
        toolset="totalreclaw",
        schema=schemas.EXPORT,
        handler=lambda args, **kw: tools.export_all(args, state, **kw),
        is_async=True,
        description="Export all memories from TotalReclaw",
    )
    ctx.register_tool(
        name="totalreclaw_status",
        toolset="totalreclaw",
        schema=schemas.STATUS,
        handler=lambda args, **kw: tools.status(args, state, **kw),
        is_async=True,
        description="Check TotalReclaw billing status",
    )
    ctx.register_tool(
        name="totalreclaw_setup",
        toolset="totalreclaw",
        schema=schemas.SETUP,
        handler=lambda args, **kw: tools.setup(args, state, **kw),
        description="Configure TotalReclaw credentials",
    )

    # Register hooks
    ctx.register_hook("on_session_start", lambda **kw: hooks.on_session_start(state, **kw))
    ctx.register_hook("pre_llm_call", lambda **kw: hooks.pre_llm_call(state, **kw))
    ctx.register_hook("post_llm_call", lambda **kw: hooks.post_llm_call(state, **kw))
    ctx.register_hook("on_session_end", lambda **kw: hooks.on_session_end(state, **kw))

    logger.info("TotalReclaw plugin registered (6 tools, 4 hooks)")
