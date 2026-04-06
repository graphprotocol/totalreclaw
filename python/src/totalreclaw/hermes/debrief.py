"""
Session debrief extraction for TotalReclaw Hermes plugin.

This module re-exports from the generic ``totalreclaw.agent.debrief``
for backward compatibility. New code should import from
``totalreclaw.agent.debrief`` directly.
"""
from totalreclaw.agent.debrief import (  # noqa: F401
    DebriefItem,
    VALID_DEBRIEF_TYPES,
    DEBRIEF_SYSTEM_PROMPT,
    parse_debrief_response,
    generate_debrief,
)
