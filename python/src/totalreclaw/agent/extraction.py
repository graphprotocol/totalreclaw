"""
LLM-guided and heuristic fact extraction for TotalReclaw.

Uses the same extraction prompt as the OpenClaw plugin for parity.
Falls back to heuristic extraction if no LLM is available.

This module is framework-agnostic and can be used by any Python agent
integration (Hermes, LangChain, CrewAI, or custom agents).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .llm_client import detect_llm_config, chat_completion

logger = logging.getLogger(__name__)

VALID_TYPES = {"fact", "preference", "decision", "episodic", "goal", "context", "summary"}
VALID_ACTIONS = {"ADD", "UPDATE", "DELETE", "NOOP"}


@dataclass
class ExtractedFact:
    text: str
    type: str  # fact, preference, decision, episodic, goal, context, summary
    importance: int  # 1-10
    action: str  # ADD, UPDATE, DELETE, NOOP
    existing_fact_id: Optional[str] = None


# Same extraction prompt as OpenClaw plugin (skill/plugin/extractor.ts)
EXTRACTION_SYSTEM_PROMPT = """You are a memory extraction engine. Analyze the conversation and extract valuable long-term memories.

Rules:
1. Each memory must be a single, self-contained piece of information
2. Focus on user-specific information that would be useful in future conversations
3. Skip generic knowledge, greetings, small talk, and ephemeral task coordination
4. Score importance 1-10 (6+ = worth storing)
5. Only extract memories with importance >= 6

Types:
- fact: Objective information about the user (name, location, job, relationships)
- preference: Likes, dislikes, or preferences ("prefers dark mode", "allergic to peanuts")
- decision: Choices WITH reasoning ("chose PostgreSQL because data is relational and needs ACID")
- episodic: Notable events or experiences ("deployed v1.0 to production on March 15")
- goal: Objectives, targets, or plans ("wants to launch public beta by end of Q1")
- context: Active project/task context ("working on TotalReclaw v1.2, staging on Base Sepolia")
- summary: Key outcome or conclusion from a discussion ("agreed to use phased rollout for migration")

Extraction guidance:
- For decisions: ALWAYS include the reasoning. "Chose X" is weak. "Chose X because Y" is strong.
- For context: Capture what the user is actively working on, including versions, environments, and status.
- For summaries: Only extract when a conversation reaches a clear conclusion or agreement.
- For facts: Prefer specific over vague. "Lives in Lisbon" beats "lives in Europe".
- Decisions and context should be importance >= 7 (they are high-value for future conversations).

Actions (compare against existing memories if provided):
- ADD: New memory, no conflict with existing
- UPDATE: Refines or corrects an existing memory (provide existingFactId)
- DELETE: Contradicts an existing memory -- the old one is now wrong (provide existingFactId)
- NOOP: Already captured or not worth storing

Return a JSON array (no markdown, no code fences):
[{"text": "...", "type": "...", "importance": N, "action": "ADD|UPDATE|DELETE|NOOP", "existingFactId": "..."}, ...]

If nothing is worth extracting, return: []"""


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


def _parse_response(response: str) -> list[ExtractedFact]:
    """Parse LLM JSON response into ExtractedFact objects."""
    cleaned = response.strip()
    # Strip markdown code fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, list):
            return []
    except json.JSONDecodeError:
        return []

    facts = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if len(text) < 5:
            continue

        fact_type = str(item.get("type", "fact"))
        if fact_type not in VALID_TYPES:
            fact_type = "fact"

        importance = item.get("importance", 5)
        try:
            importance = max(1, min(10, int(importance)))
        except (ValueError, TypeError):
            importance = 5

        action = str(item.get("action", "ADD")).upper()
        if action not in VALID_ACTIONS:
            action = "ADD"

        # DELETE actions pass regardless of importance
        if importance < 6 and action != "DELETE":
            continue

        existing_id = item.get("existingFactId") or item.get("existing_fact_id")

        facts.append(ExtractedFact(
            text=text[:512],
            type=fact_type,
            importance=importance,
            action=action,
            existing_fact_id=str(existing_id) if existing_id else None,
        ))

    return facts


async def extract_facts_llm(
    messages: list[dict],
    mode: str = "turn",  # "turn" or "full"
    existing_memories: Optional[list[dict]] = None,
) -> list[ExtractedFact]:
    """Extract facts using LLM. Returns empty list if no LLM available."""
    config = detect_llm_config()
    if not config:
        return []

    # Use all provided messages — the caller (hooks.py) already scopes
    # to unprocessed messages via state.get_unprocessed_messages().
    # Turn vs full mode only affects the user prompt framing.
    relevant = messages
    conversation_text = _truncate_messages(relevant)
    if len(conversation_text) < 20:
        return []

    # Build existing memories context
    memories_ctx = ""
    if existing_memories:
        mem_lines = [f"[ID: {m['id']}] {m['text']}" for m in existing_memories[:50]]
        memories_ctx = (
            "\n\nExisting memories (classify as UPDATE/DELETE/NOOP if they conflict or overlap):\n"
            + "\n".join(mem_lines)
        )

    user_prompt = (
        f"Extract important facts from these recent conversation turns:\n\n{conversation_text}{memories_ctx}"
        if mode == "turn"
        else f"Extract ALL valuable long-term memories from this conversation before it is lost:\n\n{conversation_text}{memories_ctx}"
    )

    response = await chat_completion(config, EXTRACTION_SYSTEM_PROMPT, user_prompt)
    if not response:
        return []

    return _parse_response(response)


def extract_facts_heuristic(messages: list[dict], max_facts: int) -> list[ExtractedFact]:
    """Simple heuristic fact extraction (no LLM needed).

    Looks for patterns like:
    - "I prefer/like/want..."
    - "My name is..."
    - "Remember that..."
    - Decisions: "I chose/decided..."
    - Facts stated by the user
    """
    facts: list[ExtractedFact] = []
    patterns = [
        (r"(?:I|my)\s+(?:prefer|like|want|need|use|enjoy|love|hate)\s+(.+)", "preference", 7),
        (r"(?:my name is|I'm called|call me)\s+(.+)", "fact", 8),
        (r"(?:remember|don't forget|keep in mind)\s+(?:that\s+)?(.+)", "fact", 7),
        (r"(?:I chose|I decided|we decided|decision:)\s+(.+)", "decision", 7),
        (r"(?:I work|I'm a|my job|my role)\s+(.+)", "fact", 7),
    ]

    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        for pattern, category, importance in patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                text = match.strip().rstrip(".")
                if len(text) > 10 and len(facts) < max_facts:
                    facts.append(ExtractedFact(
                        text=text[:512],
                        type=category,
                        importance=importance,
                        action="ADD",
                    ))

    return facts
