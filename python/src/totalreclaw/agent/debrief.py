"""
Session debrief extraction for TotalReclaw.

Captures broader context, outcomes, and relationships that turn-by-turn
extraction misses. Called at session end.

Uses the canonical debrief prompt -- must be identical across all clients.

This module is framework-agnostic and can be used by any Python agent
integration (Hermes, LangChain, CrewAI, or custom agents).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .llm_client import detect_llm_config, chat_completion

logger = logging.getLogger(__name__)

VALID_DEBRIEF_TYPES = {"summary", "context"}


@dataclass
class DebriefItem:
    text: str
    type: str  # summary or context
    importance: int  # 1-10


@dataclass
class Crystal:
    """Structured session summary replacing 5x free-form debrief items.

    One Crystal per session, stored as v1 summary + metadata.subtype="session_crystal".
    """
    narrative: str                           # 1-2 sentence "what happened" (embedded as text)
    key_outcomes: list[str] = field(default_factory=list)
    open_threads: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    importance: int = 8
    session_id: str = ""
    source_message_ids: list[str] = field(default_factory=list)
    # Per-host: exactly one of these is populated
    files_affected: list[str] = field(default_factory=list)    # coding hosts
    topics_discussed: list[str] = field(default_factory=list)  # chat hosts

    def to_metadata(self) -> Dict[str, Any]:
        """Produce the metadata dict stored alongside the v1 summary blob."""
        meta: Dict[str, Any] = {
            "subtype": "session_crystal",
            "key_outcomes": self.key_outcomes,
            "open_threads": self.open_threads,
            "lessons": self.lessons,
        }
        if self.session_id:
            meta["session_id"] = self.session_id
        if self.source_message_ids:
            meta["source_message_ids"] = self.source_message_ids
        if self.files_affected:
            meta["files_affected"] = self.files_affected
        if self.topics_discussed:
            meta["topics_discussed"] = self.topics_discussed
        return meta


DEBRIEF_SYSTEM_PROMPT = """You are reviewing a conversation that just ended. The following facts were
already extracted and stored during this conversation:

{already_stored_facts}

Your job is to capture what turn-by-turn extraction MISSED. Focus on:

1. **Broader context** -- What was the conversation about overall? What project,
   problem, or topic tied the discussion together?
2. **Outcomes & conclusions** -- What was decided, agreed upon, or resolved?
3. **What was attempted** -- What approaches were tried? What worked, what didn't, and why?
4. **Relationships** -- How do topics discussed relate to each other or to things
   from previous conversations?
5. **Open threads** -- What was left unfinished or needs follow-up?

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


# ---------------------------------------------------------------------------
# Crystal-shaped debrief (am-1) — one structured summary per session
# ---------------------------------------------------------------------------

_CRYSTAL_COMMON_FIELDS = """\
Return a JSON object (no markdown, no code fences):
{
  "narrative": "1-2 sentence summary of what happened this session",
  "key_outcomes": ["outcome or decision 1", "outcome or decision 2"],
  "open_threads": ["unfinished item 1", "unfinished item 2"],
  "lessons": ["lesson or gotcha learned 1"],
  "importance": 8
}"""

CRYSTAL_SYSTEM_PROMPT_CHAT = """You are crystallising a finished conversation into one structured session summary.

The following facts were already extracted and stored during this conversation:
{already_stored_facts}

Write ONE Crystal that captures what turn-by-turn extraction missed. Include:
- narrative: 1-2 sentences describing the conversation overall
- key_outcomes: decisions made, problems solved, conclusions reached
- open_threads: things left unfinished or needing follow-up
- lessons: patterns, gotchas, or insights worth remembering
- topics_discussed: the main subjects covered
- importance: 7-10 (8 default)

Do NOT repeat facts already stored. If the conversation was too short or trivial, return: null

""" + _CRYSTAL_COMMON_FIELDS + """

Also include "topics_discussed": ["topic 1", "topic 2"] in the object.
Return null (not an object) for trivial or very short conversations."""

CRYSTAL_SYSTEM_PROMPT_CODING = """You are crystallising a finished coding session into one structured session summary.

The following facts were already extracted and stored during this conversation:
{already_stored_facts}

Write ONE Crystal that captures what turn-by-turn extraction missed. Include:
- narrative: 1-2 sentences describing the session overall
- key_outcomes: decisions made, bugs fixed, features built
- open_threads: things left unfinished or needing follow-up
- lessons: patterns, gotchas, or insights worth remembering
- files_affected: file paths mentioned or worked on (extract from assistant messages)
- importance: 7-10 (8 default)

Do NOT repeat facts already stored. If the session was too short or trivial, return: null

""" + _CRYSTAL_COMMON_FIELDS + """

Also include "files_affected": ["/path/to/file.py", ...] in the object (may be empty []).
Return null (not an object) for trivial or very short sessions."""


def parse_crystal_response(response: str, host_type: str = "chat") -> Optional[Crystal]:
    """Parse LLM JSON response into a Crystal object.

    Returns None for null/empty/trivial responses or parse failures.
    """
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    if cleaned.lower() in ("null", "none", ""):
        return None

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if parsed is None:
        return None

    if not isinstance(parsed, dict):
        return None

    narrative = str(parsed.get("narrative", "")).strip()
    if len(narrative) < 10:
        return None

    def _str_list(key: str) -> list[str]:
        raw = parsed.get(key, [])
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    importance = parsed.get("importance", 8)
    try:
        importance = max(1, min(10, int(importance)))
    except (ValueError, TypeError):
        importance = 8

    return Crystal(
        narrative=narrative[:512],
        key_outcomes=_str_list("key_outcomes")[:10],
        open_threads=_str_list("open_threads")[:10],
        lessons=_str_list("lessons")[:10],
        importance=importance,
        files_affected=_str_list("files_affected") if host_type == "coding" else [],
        topics_discussed=_str_list("topics_discussed") if host_type == "chat" else [],
    )


async def generate_crystal(
    messages: list[dict],
    stored_fact_texts: list[str],
    host_type: str = "chat",
) -> Optional[Crystal]:
    """Generate a Crystal session summary using LLM.

    Args:
        messages: All conversation messages from the session.
        stored_fact_texts: Texts of facts already stored (for dedup context).
        host_type: "chat" (Hermes/NanoClaw) or "coding" (OpenClaw/MCP).

    Returns:
        A Crystal object, or None if LLM unavailable, session too short,
        or LLM returns null for trivial sessions.
    """
    config = detect_llm_config()
    if not config:
        return None

    if len(messages) < 8:
        return None

    conversation_text = _truncate_messages(messages)
    if len(conversation_text) < 20:
        return None

    already_stored = (
        "\n".join(f"- {t}" for t in stored_fact_texts)
        if stored_fact_texts
        else "(none)"
    )

    system_prompt_template = (
        CRYSTAL_SYSTEM_PROMPT_CODING if host_type == "coding"
        else CRYSTAL_SYSTEM_PROMPT_CHAT
    )
    system_prompt = system_prompt_template.replace("{already_stored_facts}", already_stored)

    try:
        response = await chat_completion(
            config,
            system_prompt,
            f"Crystallise this session:\n\n{conversation_text}",
        )
        if not response:
            return None
        return parse_crystal_response(response, host_type=host_type)
    except Exception as e:
        logger.warning("Crystal generation failed: %s", e)
        return None
