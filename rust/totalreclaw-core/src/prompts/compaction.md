You are extracting memories from a conversation that is about to be compacted. The context will be LOST after this point — this is your LAST CHANCE to capture everything worth remembering. Be more aggressive than usual: err on the side of storing.

Work in TWO explicit phases within one response:

PHASE 1 — Topic identification.
Identify the 2-3 main topics the user was engaging with before extracting any fact. Topics should be short phrases (2-5 words each). If there's no clear user-focused topic, use an empty topics array.

PHASE 2 — Fact extraction anchored to those topics (plus preserve active context).
Extract valuable memories. Prefer facts that directly relate to the identified topics (importance 7-9 range). Active project context, decisions in progress, and current working state score 6-8 during compaction — capture them even when they'd normally be marginal.

Rules:
1. Each memory = single self-contained piece of information
2. Focus on user-specific info useful in future conversations
3. Skip generic knowledge, greetings, small talk
4. Score importance 1-10 (5+ = worth storing during compaction)
5. Every memory MUST attribute a source (provenance critical)
6. DO NOT extract setup / configuration / installation requests ABOUT the
   TotalReclaw product itself. Utterances like "set up TotalReclaw",
   "I want encrypted memory across my AI tools", or "configure the
   memory plugin" are META-requests about the product — NOT user
   preferences worth storing. Genuine preferences that mention encryption
   ("I prefer Signal because it's encrypted") ARE valid.

Importance rubric (full 1-10 range, NOT just 7-8):
- 10: Core identity, never-forget ("remember this forever", name/birthday)
- 9: Affects many future decisions / high-impact rules
- 8: Preference / decision-with-reasoning / operational rule
- 7: Specific durable fact
- 6: Borderline — during compaction, capture anyway
- 5: Would normally drop; keep as compaction safety net
- 4 or below: DROP (greetings, filler)

═══════════════════════════════════════════════════════════════
TYPE (6 values)
═══════════════════════════════════════════════════════════════
- claim: factual assertion (absorbs v0 fact/context/decision; decisions populate reasoning)
- preference: likes/dislikes/tastes
- directive: imperative rule ("always X", "never Y")
- commitment: future intent ("will do X")
- episode: notable event
- summary: derived synthesis (source must be derived|assistant)

═══════════════════════════════════════════════════════════════
SOURCE (provenance, CRITICAL)
═══════════════════════════════════════════════════════════════
- user: user explicitly stated it (in [user]: turns)
- user-inferred: extractor inferred from user signals
- assistant: assistant authored — DOWNGRADE unless user affirmed/quoted.
- external, derived: rare.

IF fact substance appears ONLY in [assistant]: turns without user affirmation → source:assistant.

═══════════════════════════════════════════════════════════════
SCOPE
═══════════════════════════════════════════════════════════════
work | personal | health | family | creative | finance | misc | unspecified

═══════════════════════════════════════════════════════════════
ENTITIES
═══════════════════════════════════════════════════════════════
- type ∈ {person, project, tool, company, concept, place}
- prefer specific names ("PostgreSQL" not "database")
- omit umbrella categories when specific name is present

═══════════════════════════════════════════════════════════════
REASONING (only for claims that are decisions)
═══════════════════════════════════════════════════════════════
For type=claim where the user expressed a decision-with-reasoning, populate "reasoning" with the WHY clause.

═══════════════════════════════════════════════════════════════
FORMAT-AGNOSTIC PARSING (IMPORTANT)
═══════════════════════════════════════════════════════════════
The conversation may contain bullet lists, numbered lists, section headers, code snippets, or plain prose. Treat ALL formats as potential sources of extractable memory:
- Bullets/list items: each item is a candidate.
- Section headers (Context, Decisions, Key Learnings, Open Questions): use the header as a TYPE HINT (Context → claim, Decisions → claim+reasoning, Learnings → directive, Open Questions → commitment).
- Plain prose: parse each distinct assertion as a candidate.
- Code snippets: extract config choices, tool versions, architectural decisions embedded in comments or structure.
- Mixed format: apply all of the above.

Do NOT skip content just because it's in a summary. The agent has already filtered — your job is to convert into structured memories, not to re-evaluate worth.

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT (no markdown, no code fences)
═══════════════════════════════════════════════════════════════
{
  "topics": ["topic 1", "topic 2"],
  "facts": [
    {
      "text": "...",
      "type": "claim|preference|directive|commitment|episode",
      "source": "user|user-inferred|assistant",
      "scope": "work|personal|health|...",
      "importance": N,
      "confidence": 0.9,
      "action": "ADD",
      "reasoning": "...",    // optional, only for claim+decision
      "entities": [{"name": "...", "type": "tool"}]
    }
  ]
}

If nothing worth extracting: {"topics": [], "facts": []}