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
import math
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .llm_client import LLMConfig, detect_llm_config, chat_completion

logger = logging.getLogger(__name__)

#: The 8 canonical memory types — single source of truth for the Python
#: package. Keep in sync with ``skill/plugin/extractor.ts`` (VALID_MEMORY_TYPES),
#: ``mcp/src/memory-types.ts`` (VALID_MEMORY_TYPES), and the Rust
#: ``ClaimCategory`` enum in ``rust/totalreclaw-core/src/claims.rs``. A cross-
#: language parity test at ``tests/parity/test_kg_phase1_parity.py`` enforces
#: this stays in sync.
#:
#: Any Python consumer (tool schemas, type mappings, validation whitelists,
#: canonical-claim builders) MUST import from this constant — never re-declare
#: the list inline. See ``claims_helper.TYPE_TO_CATEGORY`` for the compact
#: short-form mapping.
VALID_MEMORY_TYPES: tuple[str, ...] = (
    "fact",
    "preference",
    "decision",
    "episodic",
    "goal",
    "context",
    "summary",
    "rule",
)

#: Backward-compat alias — prefer ``VALID_MEMORY_TYPES`` in new code.
VALID_TYPES: frozenset[str] = frozenset(VALID_MEMORY_TYPES)

VALID_ACTIONS = {"ADD", "UPDATE", "DELETE", "NOOP"}

#: Allowed entity types — must match ``skill/plugin/extractor.ts``.
VALID_ENTITY_TYPES = {"person", "project", "tool", "company", "concept", "place"}

#: Default confidence score when the LLM response omits ``confidence``.
#: Must match the plugin's ``DEFAULT_EXTRACTION_CONFIDENCE``.
DEFAULT_EXTRACTION_CONFIDENCE: float = 0.85


@dataclass
class ExtractedEntity:
    """A named entity referenced by an extracted fact.

    Mirrors ``skill/plugin/extractor.ts`` ``ExtractedEntity``. The ``type``
    field is one of :data:`VALID_ENTITY_TYPES`. ``role`` is optional free
    text describing the entity's role in the claim (``"chooser"``, etc.).
    """

    name: str
    type: str  # person | project | tool | company | concept | place
    role: Optional[str] = None


@dataclass
class ExtractedFact:
    text: str
    type: str  # fact, preference, decision, episodic, goal, context, summary
    importance: int  # 1-10
    action: str  # ADD, UPDATE, DELETE, NOOP
    existing_fact_id: Optional[str] = None
    entities: Optional[List[ExtractedEntity]] = None
    confidence: float = DEFAULT_EXTRACTION_CONFIDENCE


def normalize_confidence(raw: Any) -> float:
    """Clamp a raw confidence value to ``[0, 1]`` with default fallback.

    Must match ``normalizeConfidence`` in ``skill/plugin/extractor.ts``:

    - numeric in range → returned as-is
    - numeric > 1 → clamped to 1
    - numeric < 0 → clamped to 0
    - non-finite / non-number (strings, None, NaN) → :data:`DEFAULT_EXTRACTION_CONFIDENCE`
    """
    if isinstance(raw, bool):
        return DEFAULT_EXTRACTION_CONFIDENCE
    if not isinstance(raw, (int, float)):
        return DEFAULT_EXTRACTION_CONFIDENCE
    f = float(raw)
    if not math.isfinite(f):
        return DEFAULT_EXTRACTION_CONFIDENCE
    if f < 0:
        return 0.0
    if f > 1:
        return 1.0
    return f


def _parse_entity(raw: Any) -> Optional[ExtractedEntity]:
    """Parse a single entity dict from LLM output; return None if invalid.

    Invalid entities are silently dropped so one bad entry never kills the
    whole fact. Mirrors the plugin's ``parseEntity``.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None
    etype = str(raw.get("type", "")).lower()
    if etype not in VALID_ENTITY_TYPES:
        return None
    entity = ExtractedEntity(name=name[:128], type=etype)
    role = raw.get("role")
    if isinstance(role, str) and role.strip():
        entity.role = role.strip()[:128]
    return entity


# Same extraction prompt as OpenClaw plugin (skill/plugin/extractor.ts)
EXTRACTION_SYSTEM_PROMPT = """You are a memory extraction engine. Analyze the conversation and extract valuable long-term memories.

Rules:
1. Each memory must be a single, self-contained piece of information
2. Focus on user-specific information that would be useful in future conversations
3. Skip generic knowledge, greetings, small talk, and ephemeral task coordination
4. Score importance 1-10 using the rubric below (6+ = worth storing)
5. Only extract memories with importance >= 6

Importance rubric (use the FULL 1-10 range, not just 7-8):
- 10: Critical, core identity, never-forget content. The user explicitly says "remember this forever", "critical", "never forget", or it's a fundamental fact like name/birthday/relationships that defines who they are.
- 9: Affects many future decisions or interactions. A high-impact rule, a major life decision with reasoning, a deeply held preference that shapes daily work.
- 8: High-value preference, decision-with-reasoning, or operational rule. The user clearly cares about it AND it will be relevant in many future conversations.
- 7: Specific durable fact about the user's setup, project, or context. Useful to remember but not life-changing.
- 6: Borderline — barely passes the "worth storing" threshold. Generic facts, low-signal preferences. If you're hesitating between 5 and 6, prefer 5 (it gets dropped).
- 5 or below: NOT WORTH STORING. Drop these. Casual mentions, ephemeral state, low-signal chatter.

DO NOT cluster every fact at 7-8. Use 9-10 for high-signal content and 5-6 for borderline content. The system depends on the full range working — over-clustering at 7-8 produces tied scores in the contradiction resolver and makes ranking/decay impossible.

Types:
- fact: Objective information about the user (name, location, job, relationships)
- preference: Likes, dislikes, or preferences ("prefers dark mode", "allergic to peanuts")
- decision: Choices WITH reasoning ("chose PostgreSQL because data is relational and needs ACID")
- episodic: Notable events or experiences ("deployed v1.0 to production on March 15")
- goal: Objectives, targets, or plans ("wants to launch public beta by end of Q1")
- context: Active project/task context ("working on TotalReclaw v1.2, staging on Base Sepolia")
- summary: Key outcome or conclusion from a discussion ("agreed to use phased rollout for migration")
- rule: A reusable operational rule, non-obvious gotcha, debugging shortcut, or convention the user wants to remember for next time. Distinct from decisions (which have reasoning for a specific choice) and preferences (which are personal tastes). Rules are impersonal, actionable, and transferable — they would help anyone in the same situation. Examples: "Always check the systemd unit file for environment pins before wiping state", "The subgraph schema uses sequenceId not seqId", "Don't open large JSON files in Neovim — use jq instead".

Extraction guidance:
- For decisions: ALWAYS include the reasoning. "Chose X" is weak. "Chose X because Y" is strong.
- For context: Capture what the user is actively working on, including versions, environments, and status.
- For summaries: Only extract when a conversation reaches a clear conclusion or agreement.
- For facts: Prefer specific over vague. "Lives in Lisbon" beats "lives in Europe".
- For rules: ALWAYS extract when the user explicitly signals "remember this", "gotcha", "rule of thumb", "always", "never", or describes a non-obvious learning. Importance >= 7 when the rule prevented a real bug or wasted time. Include the specific context (which tool, which error, which version) so the rule is actionable later. The boundary test: would this apply to anyone in the same situation? Rules generalize; decisions and preferences don't.
- Decisions and context should be importance >= 7 (they are high-value for future conversations).

Few-shot examples (rule type — when to use it and when NOT to use it):

Example 1 — rule embedded in a debugging narrative:
  User: "Spent two hours debugging the subgraph because my Python wrapper silently swallowed a GraphQL error and I read it as 'no data'. Turns out the schema field is sequenceId, not seqId. Note to self: always check d.get('errors') before trusting an empty result."
  Extract:
  [{"text": "Subgraph Fact schema uses sequenceId, not seqId — check d.get('errors') before trusting an empty facts array", "type": "rule", "importance": 8, "confidence": 1.0, "entities": [{"name": "subgraph", "type": "tool"}, {"name": "GraphQL", "type": "tool"}]}]

Example 2 — user stating a convention as a rule:
  User: "Convention for the team: before any rm -rf on the VPS state dir, stop the gateway first. Otherwise an async flush can recreate stale files mid-cleanup."
  Extract:
  [{"text": "Stop the OpenClaw gateway before rm -rf ~/.totalreclaw/ — async flush can recreate stale files mid-cleanup", "type": "rule", "importance": 7, "confidence": 1.0, "entities": [{"name": "OpenClaw gateway", "type": "tool"}]}]

Example 3 — rule vs decision (distinguishing them):
  User: "We chose DuckDB over ClickHouse for analytics because DuckDB fits in a single-file deployment and our scale is small."
  Extract:
  [{"text": "Chose DuckDB over ClickHouse for analytics because single-file deployment fits small-scale use", "type": "decision", "importance": 8, "confidence": 1.0, "entities": [{"name": "DuckDB", "type": "tool", "role": "chosen"}, {"name": "ClickHouse", "type": "tool", "role": "rejected"}]}]
  This is a DECISION, not a rule — it's a specific choice with reasoning, not a transferable pattern. The boundary test: it applies to THIS user's THIS analytics deployment, not to anyone in the same situation.

Entity extraction (new):
- Each memory MAY include an "entities" array of named entities it references
- Entity type must be one of: person | project | tool | company | concept | place
- Use the user's name when a fact is about them (e.g. "Pedro")
- role is optional free text ("chooser", "employer")
- confidence (0.0-1.0) is your self-assessed certainty; default 0.85 if you're unsure

Actions (compare against existing memories if provided):
- ADD: New memory, no conflict with existing
- UPDATE: Refines or corrects an existing memory (provide existingFactId)
- DELETE: Contradicts an existing memory -- the old one is now wrong (provide existingFactId)
- NOOP: Already captured or not worth storing

Return a JSON array (no markdown, no code fences):
[{"text": "...", "type": "...", "importance": N, "confidence": 0.9, "action": "ADD|UPDATE|DELETE|NOOP", "entities": [{"name": "PostgreSQL", "type": "tool"}], "existingFactId": "..."}, ...]

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
    """Parse the LLM extraction response into ExtractedFact objects.

    Phase 2.2.6 hardened this function against the three failure modes the
    Phase 2.2.5 investigation uncovered on the TypeScript side (same issues
    existed silently in Python):

    1. **Thinking-model prefix stripping.** Models like glm-5/glm-5.1, Claude
       reasoning, and gpt-o1 prefix their output with a ``<think>...</think>``
       or ``<thinking>...</thinking>`` reasoning trace. The old parser handed
       that straight to ``json.loads`` which silently returned ``[]``.
       We now strip these tags (case-insensitive) before any parse attempt.

    2. **Prose-wrapped JSON.** Models sometimes respond with conversational
       framing like "Here are the extracted facts: [...]". The old parser
       would fail the JSON parse. We now fall back to a regex scan for the
       first ``[...]`` block if the direct parse fails.

    3. **Silent parse failures.** The old parser had a blanket
       ``except json.JSONDecodeError: return []`` with zero logging. Genuine
       parse failures now log at WARNING level with a preview of the LLM
       response so operators can see what the model actually produced.
    """
    original_preview = response.strip()[:200]
    cleaned = response.strip()

    # Phase 2.2.6: strip <think>...</think> and <thinking>...</thinking> tags
    # (case-insensitive) BEFORE any other cleanup. Multi-tag, nested-line, and
    # mixed-with-markdown-fence variants all handled by the same regex.
    cleaned = re.sub(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", cleaned, flags=re.IGNORECASE).strip()

    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    parsed: object = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: scan for a JSON array anywhere in the cleaned output
        # (handles prose-wrapped "Here are the facts: [...]" cases).
        m = re.search(r"\[[\s\S]*\]", cleaned)
        if m:
            try:
                parsed = json.loads(m.group(0))
                logger.info(
                    "parseFactsResponse: recovered JSON array via bracket-scan fallback"
                )
            except json.JSONDecodeError:
                parsed = None

    if parsed is None:
        logger.warning(
            "parseFactsResponse: could not parse LLM output as JSON. Preview: %r",
            original_preview,
        )
        return []

    if not isinstance(parsed, list):
        logger.warning(
            "parseFactsResponse: parsed value is not an array (type=%s)",
            type(parsed).__name__,
        )
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

        # Parse entities (new, optional)
        raw_entities = item.get("entities")
        entities: Optional[List[ExtractedEntity]] = None
        if isinstance(raw_entities, list):
            parsed_entities = [e for e in (_parse_entity(r) for r in raw_entities) if e is not None]
            if parsed_entities:
                entities = parsed_entities

        # Parse confidence (new, optional; defaults to 0.85)
        confidence = normalize_confidence(item.get("confidence"))

        facts.append(ExtractedFact(
            text=text[:512],
            type=fact_type,
            importance=importance,
            action=action,
            existing_fact_id=str(existing_id) if existing_id else None,
            entities=entities,
            confidence=confidence,
        ))

    return facts


def compute_lexical_importance_bump(fact_text: str, conversation_text: str) -> int:
    """Phase 2.2.6: post-process bump for under-weighted facts.

    Mirrors ``computeLexicalImportanceBump`` in ``skill/plugin/extractor.ts``.
    See that function's docstring for the full design rationale and signal list.

    Returns an integer in [0, 2] representing how much to add to the LLM's
    importance score. The bump is additive and never overrides the importance
    >= 6 filter on its own (a fact still needs to score >= 5 from the LLM to
    benefit, since +2 from 5 = 7).
    """
    bump = 0
    lower_conv = conversation_text.lower()

    # Signal 1: strong intent phrases anywhere in the conversation
    strong_intent = re.compile(
        r"\b(remember this|never forget|rule of thumb|don't (?:ever )?forget|critical|important|gotcha|note to self)\b",
        re.IGNORECASE,
    )
    if strong_intent.search(lower_conv):
        bump += 1

    # Signal 2: emphasis markers — double exclamation OR 3+ consecutive all-caps words (3+ chars each)
    double_excl = "!!"
    all_caps_phrase = re.compile(r"\b[A-Z]{3,}(?:\s+[A-Z]{3,}){2,}\b")
    if double_excl in conversation_text or all_caps_phrase.search(conversation_text):
        bump += 1

    # Signal 3: repetition — extract content words (length >= 5, not stop words)
    # from the fact and check if any single one appears 2+ times in the
    # conversation. This is more robust to LLM paraphrasing than a leading-chars
    # fingerprint.
    lower_fact = fact_text.lower()
    stop_words = {
        "about", "after", "again", "against", "because", "before", "being",
        "between", "could", "doing", "during", "every", "further", "having",
        "their", "these", "those", "through", "under", "until", "where",
        "which", "while", "would", "should", "thing", "things", "something",
        "someone", "always", "never", "often", "still", "really", "maybe",
        "using", "works", "user", "users", "with", "from", "into", "like",
        "just", "than", "them", "they", "will", "when", "what", "were",
        "this", "that", "have",
    }
    fact_words = [
        w for w in re.split(r"[^a-z0-9_]+", lower_fact)
        if len(w) >= 5 and w not in stop_words
    ]
    triggered = False
    for word in fact_words:
        # \b word boundary; re.escape handles any regex meta chars
        occurrences = len(re.findall(rf"\b{re.escape(word)}\b", lower_conv))
        if occurrences >= 2:
            triggered = True
            break
    if triggered:
        bump += 1

    return min(bump, 2)


async def extract_facts_llm(
    messages: list[dict],
    mode: str = "turn",  # "turn" or "full"
    existing_memories: Optional[list[dict]] = None,
    llm_config: Optional["LLMConfig"] = None,
) -> list[ExtractedFact]:
    """Extract facts using LLM. Returns empty list if no LLM available.

    Phase 2.2.6 added observability: every early-return branch now logs at
    INFO or WARNING level so the auto-extraction path can be diagnosed from
    the gateway log without having to reproduce the exact failure. Prior to
    this, silent ``return []`` paths made debugging near-impossible (see
    QA-PHASE-2-2-5-20260415 for the multi-hour ghost-bug hunt this prevents).

    Parameters
    ----------
    llm_config : LLMConfig, optional
        Pre-resolved LLM configuration. If not provided, falls back to
        ``detect_llm_config()`` which auto-detects from environment variables.
    """
    config = llm_config or detect_llm_config()
    if not config:
        logger.info("extract_facts_llm: no LLM config resolved (skipping extraction)")
        return []

    # Use all provided messages — the caller (hooks.py) already scopes
    # to unprocessed messages via state.get_unprocessed_messages().
    # Turn vs full mode only affects the user prompt framing.
    relevant = messages
    conversation_text = _truncate_messages(relevant)
    if len(conversation_text) < 20:
        logger.info(
            "extract_facts_llm: conversation too short (%d chars < 20, messages=%d, mode=%s)",
            len(conversation_text),
            len(messages),
            mode,
        )
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

    try:
        response = await chat_completion(config, EXTRACTION_SYSTEM_PROMPT, user_prompt)
    except Exception as e:
        logger.warning("extract_facts_llm: chat_completion threw: %s", e)
        return []

    if not response:
        logger.info("extract_facts_llm: chat_completion returned None/empty")
        return []

    logger.info(
        "extract_facts_llm: LLM returned %d chars; handing to _parse_response",
        len(response),
    )
    facts = _parse_response(response)

    # Phase 2.2.6: lexical importance bumps. Mirrors the TS extractor — see
    # `compute_lexical_importance_bump` docstring for the full signal list.
    for f in facts:
        bump = compute_lexical_importance_bump(f.text, conversation_text)
        if bump > 0:
            old_importance = f.importance
            effective_bump = min(bump, 1) if f.importance >= 8 else bump
            f.importance = min(10, f.importance + effective_bump)
            logger.info(
                "extract_facts_llm: lexical bump +%d for %r (%d -> %d)",
                bump,
                f.text[:60],
                old_importance,
                f.importance,
            )

    return facts


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
