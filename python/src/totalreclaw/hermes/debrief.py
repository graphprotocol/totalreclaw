"""
Session debrief extraction for TotalReclaw Hermes plugin.

Captures broader context, outcomes, and relationships that turn-by-turn
extraction misses. Called at session end (on_session_end hook).

Uses the canonical debrief prompt — must be identical across all clients.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .llm_client import detect_llm_config, chat_completion

logger = logging.getLogger(__name__)

VALID_DEBRIEF_TYPES = {"summary", "context"}


@dataclass
class DebriefItem:
    text: str
    type: str  # summary or context
    importance: int  # 1-10


DEBRIEF_SYSTEM_PROMPT = """You are reviewing a conversation that just ended. The following facts were
already extracted and stored during this conversation:

{already_stored_facts}

Your job is to capture what turn-by-turn extraction MISSED. Focus on:

1. **Broader context** — What was the conversation about overall? What project,
   problem, or topic tied the discussion together?
2. **Outcomes & conclusions** — What was decided, agreed upon, or resolved?
3. **What was attempted** — What approaches were tried? What worked, what didn't, and why?
4. **Relationships** — How do topics discussed relate to each other or to things
   from previous conversations?
5. **Open threads** — What was left unfinished or needs follow-up?

Do NOT repeat facts already stored. Only add genuinely new information that provides
broader context a future conversation would benefit from.

Return a JSON array (no markdown, no code fences):
[{"text": "...", "type": "summary|context", "importance": N}]

- Use type "summary" for conclusions, outcomes, and decisions-of-the-session
- Use type "context" for broader project context, open threads, and what-was-tried
- Importance 7-8 for most debrief items (they are high-value by definition)
- Maximum 5 items (debriefs should be concise, not exhaustive)
- Each item should be 1-3 sentences, self-contained

If the conversation was too short or trivial to warrant a debrief, return: []"""


def _truncate_messages(messages: list[dict], max_chars: int = 12000) -> str:
    """Format and truncate messages to fit token budget."""
    lines = []
    total = 0
    for msg in messages:
        line = f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines)


def parse_debrief_response(response: str) -> list[DebriefItem]:
    """Parse LLM JSON response into DebriefItem objects.

    - Strips markdown code fences
    - Validates type is summary|context (defaults to context)
    - Filters importance < 6
    - Caps at 5 items
    - Defaults importance to 7 if missing/invalid
    """
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            return []
    except json.JSONDecodeError:
        return []

    items: list[DebriefItem] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text", "")).strip()
        if len(text) < 5:
            continue

        item_type = str(entry.get("type", "context"))
        if item_type not in VALID_DEBRIEF_TYPES:
            item_type = "context"

        importance = entry.get("importance", 7)
        try:
            importance = max(1, min(10, int(importance)))
        except (ValueError, TypeError):
            importance = 7

        if importance < 6:
            continue

        items.append(DebriefItem(
            text=text[:512],
            type=item_type,
            importance=importance,
        ))

    return items[:5]  # Cap at 5


async def generate_debrief(
    messages: list[dict],
    stored_fact_texts: list[str],
) -> list[DebriefItem]:
    """Generate a session debrief using LLM.

    Args:
        messages: All conversation messages from the session.
        stored_fact_texts: Texts of facts already stored (for dedup context).

    Returns:
        List of DebriefItem objects, or empty list if LLM unavailable or
        conversation too short.
    """
    config = detect_llm_config()
    if not config:
        return []

    # Minimum 4 turns (8 messages) to warrant a debrief
    if len(messages) < 8:
        return []

    conversation_text = _truncate_messages(messages)
    if len(conversation_text) < 20:
        return []

    already_stored = (
        "\n".join(f"- {t}" for t in stored_fact_texts)
        if stored_fact_texts
        else "(none)"
    )

    system_prompt = DEBRIEF_SYSTEM_PROMPT.replace(
        "{already_stored_facts}", already_stored
    )

    try:
        response = await chat_completion(
            config,
            system_prompt,
            f"Review this conversation and provide a debrief:\n\n{conversation_text}",
        )
        if not response:
            return []
        return parse_debrief_response(response)
    except Exception as e:
        logger.warning("Debrief generation failed: %s", e)
        return []
