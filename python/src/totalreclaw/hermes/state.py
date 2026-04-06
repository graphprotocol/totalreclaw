"""Plugin state management for TotalReclaw Hermes plugin.

This module provides ``PluginState`` as a backward-compatible alias for
``AgentState`` from the generic ``totalreclaw.agent`` layer. New code
should use ``AgentState`` directly.
"""
from totalreclaw.agent.state import (  # noqa: F401
    AgentState as PluginState,
    DEFAULT_EXTRACTION_INTERVAL,
    DEFAULT_MAX_FACTS,
    DEFAULT_MIN_IMPORTANCE,
    BILLING_CACHE_TTL,
    STORE_DEDUP_THRESHOLD,
)
