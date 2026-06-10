"""Plugin state management for TotalReclaw Hermes plugin.

This module provides ``PluginState`` as a backward-compatible alias for
``AgentState`` from the generic ``totalreclaw.agent`` layer. New code
should use ``AgentState`` directly.
"""
from typing import Optional

from totalreclaw.agent.state import (  # noqa: F401
    AgentState as PluginState,
    DEFAULT_EXTRACTION_INTERVAL,
    DEFAULT_MAX_FACTS,
    DEFAULT_MIN_IMPORTANCE,
    BILLING_CACHE_TTL,
    STORE_DEDUP_THRESHOLD,
)

# Process-wide shared state (provider conformance §5.3 — #351).
#
# The Hermes entry-point plugin (``register()``) and the MemoryProvider sidecar
# (``TotalReclawMemoryProvider``) are loaded by SEPARATE Hermes mechanisms, but
# they MUST observe the same state: same TotalReclaw client, message buffer,
# turn counter, billing cache, and the ``_provider_active`` single-driver flag.
# Without a shared instance the provider's ``sync_turn`` would record into a
# different buffer than the plugin's tools read, turn counts would diverge, and
# the gating flag set by the provider would be invisible to the hooks. Both call
# ``get_shared_state()`` so there is exactly one ``PluginState`` per process.
_SHARED_STATE: "Optional[PluginState]" = None


def get_shared_state() -> "PluginState":
    """Return the process-wide shared :class:`PluginState` (lazy singleton)."""
    global _SHARED_STATE
    if _SHARED_STATE is None:
        _SHARED_STATE = PluginState()
    return _SHARED_STATE
