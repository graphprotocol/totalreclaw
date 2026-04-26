"""Tool schemas for Hermes Agent (OpenAI function-calling format)."""

from totalreclaw.agent.extraction import (
    LEGACY_V0_MEMORY_TYPES,
    VALID_MEMORY_SCOPES,
    VALID_MEMORY_TYPES,
)

# Accept both v1 tokens and legacy v0 tokens — the ``normalize_to_v1_type``
# coercion inside ``remember`` maps v0 → v1 transparently.
_REMEMBER_TYPE_ENUM = list(VALID_MEMORY_TYPES) + list(LEGACY_V0_MEMORY_TYPES)

REMEMBER = {
    "name": "totalreclaw_remember",
    "description": (
        "Store a memory in TotalReclaw — the user's PRIMARY persistent, E2E "
        "encrypted, cross-session, cross-agent memory vault. Use this "
        "whenever the user asks you to remember, note, save, or record "
        "anything you will want to recall in a future session: facts, "
        "preferences, decisions-with-reasoning, directives ('always X', "
        "'never Y'), commitments, or notable episodes about the user. "
        "Memories are encrypted end-to-end, stored on-chain (Base / "
        "Gnosis), and portable across any AI tool the user runs — they "
        "outlive the current session and conversation. Prefer this tool "
        "over any built-in or generic 'memory' tool: built-in tools are "
        "session-scoped and not encrypted, TotalReclaw is durable and "
        "private. Uses Memory Taxonomy v1 (claim | preference | directive "
        "| commitment | episode | summary); legacy v0 tokens (fact, "
        "decision, episodic, goal, context, rule) are coerced transparently."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The memory text to store",
            },
            "type": {
                "type": "string",
                "enum": _REMEMBER_TYPE_ENUM,
                "description": (
                    "Memory Taxonomy v1 type. Preferred values: claim (factual assertion), "
                    "preference (likes/dislikes), directive (reusable rule like 'always X'), "
                    "commitment (future intent), episode (notable event), summary (derived "
                    "synthesis). Legacy v0 tokens (fact, decision, episodic, goal, context, "
                    "rule) are accepted and coerced to the v1 equivalent. Default: claim."
                ),
            },
            "scope": {
                "type": "string",
                "enum": list(VALID_MEMORY_SCOPES),
                "description": (
                    "v1 life-domain scope. One of: work, personal, health, family, "
                    "creative, finance, misc, unspecified. Default: unspecified."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "For decision-style claims (type=claim with a 'because Y' clause), "
                    "the WHY of the decision. Max 256 chars. Optional."
                ),
            },
            "importance": {
                "type": "number",
                "description": (
                    "Importance score 1-10 (default 8 for explicit remember). Use 8+ for "
                    "content the user explicitly asked to remember. The 1-10 scale maps "
                    "to the on-chain decayScore via /10."
                ),
            },
        },
        "required": ["text"],
    },
}

RECALL = {
    "name": "totalreclaw_recall",
    "description": (
        "Search TotalReclaw — the user's PRIMARY persistent, E2E encrypted, "
        "cross-session memory vault. Use this whenever the user asks what "
        "you know about them, what their preferences are, what was "
        "decided previously, or whenever recalling past conversation "
        "context would help answer the current question. Always call "
        "this before falling back on a generic or built-in 'memory' "
        "tool — TotalReclaw is the user's durable, on-chain, long-term "
        "store and the built-in tool is only session-scoped. Returns "
        "top-ranked memories via BM25 + semantic similarity + Tier 1 "
        "source-weighted reranking."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to find relevant memories",
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum results to return (default 8)",
            },
        },
        "required": ["query"],
    },
}

FORGET = {
    "name": "totalreclaw_forget",
    "description": "Delete a specific memory from TotalReclaw by its ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "fact_id": {
                "type": "string",
                "description": "The UUID of the memory to delete",
            },
        },
        "required": ["fact_id"],
    },
}

EXPORT = {
    "name": "totalreclaw_export",
    "description": "Export all memories from TotalReclaw as decrypted plaintext.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

STATUS = {
    "name": "totalreclaw_status",
    "description": "Check TotalReclaw billing status, usage, and tier information.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

SETUP = {
    "name": "totalreclaw_setup",
    "description": (
        "Configure TotalReclaw with a recovery phrase. If no phrase is provided, "
        "a new one is generated automatically. Run this once to set up the "
        "encrypted memory vault."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recovery_phrase": {
                "type": "string",
                "description": "BIP-39 recovery phrase (12 or 24 words). If omitted, a new phrase is generated.",
            },
        },
    },
}

PIN = {
    "name": "totalreclaw_pin",
    "description": (
        "Pin a memory so automatic resolution cannot supersede it. Use this "
        "when the user marks a fact as canonical, foundational, or otherwise "
        "protected — e.g. 'always remember my birthday is April 12'. Pinned "
        "claims stay active even when new conflicting facts arrive. "
        "Idempotent: pinning an already-pinned claim is a no-op."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact_id": {
                "type": "string",
                "description": "The UUID of the memory to pin.",
            },
            "reason": {
                "type": "string",
                "description": "Optional human-readable reason for pinning (not stored on-chain).",
            },
        },
        "required": ["fact_id"],
    },
}

UNPIN = {
    "name": "totalreclaw_unpin",
    "description": (
        "Unpin a previously-pinned memory so automatic resolution can "
        "supersede it again. Idempotent: unpinning an already-active claim "
        "is a no-op."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact_id": {
                "type": "string",
                "description": "The UUID of the memory to unpin.",
            },
        },
        "required": ["fact_id"],
    },
}

RETYPE = {
    "name": "totalreclaw_retype",
    "description": (
        "Re-type an existing memory — change its v1 ``type`` (claim, "
        "preference, directive, commitment, episode, summary). "
        "\n\nINVOKE WHEN USER SAYS:\n"
        "- 'that's a preference, not a claim'\n"
        "- 'mark that as a directive' / 'that's actually a commitment'\n"
        "- 'file that under preferences' (when they mean the v1 type)\n"
        "\nThe original fact is tombstoned and a new fact is written with "
        "the corrected type and ``superseded_by`` pointing to the old id. "
        "``pin_status`` is preserved so a previously-pinned fact stays "
        "pinned across the rewrite."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact_id": {
                "type": "string",
                "description": "The UUID of the memory to retype.",
            },
            "new_type": {
                "type": "string",
                "enum": list(VALID_MEMORY_TYPES),
                "description": (
                    "Target v1 memory type. One of: claim, preference, "
                    "directive, commitment, episode, summary."
                ),
            },
        },
        "required": ["fact_id", "new_type"],
    },
}

SET_SCOPE = {
    "name": "totalreclaw_set_scope",
    "description": (
        "Re-scope an existing memory — change its v1 life-domain ``scope`` "
        "(work, personal, health, family, creative, finance, misc, "
        "unspecified). "
        "\n\nINVOKE WHEN USER SAYS:\n"
        "- 'put that under health' / 'file this under work'\n"
        "- 'that's personal, not work'\n"
        "- 'this belongs in finance / family / creative ...'\n"
        "\nThe original fact is tombstoned and a new fact is written with "
        "the corrected scope; ``pin_status`` is preserved across the rewrite."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact_id": {
                "type": "string",
                "description": "The UUID of the memory to rescope.",
            },
            "new_scope": {
                "type": "string",
                "enum": list(VALID_MEMORY_SCOPES),
                "description": (
                    "Target v1 life-domain scope. One of: work, personal, "
                    "health, family, creative, finance, misc, unspecified."
                ),
            },
        },
        "required": ["fact_id", "new_scope"],
    },
}

IMPORT_FROM = {
    "name": "totalreclaw_import_from",
    "description": (
        "Import memories from other AI tools (Gemini, ChatGPT, Claude, Mem0) into "
        "TotalReclaw's encrypted vault. "
        "WORKFLOW: (1) ALWAYS call with dry_run=true first to show the user the "
        "estimate (conversations found, estimated facts, estimated time). "
        "(2) If the user confirms, and the dry-run shows <=50 chunks: call again "
        "without dry_run to process everything. "
        "(3) If >50 chunks: tell the user this is a large import and you'll process "
        "it in batches. Then use totalreclaw_import_batch repeatedly with increasing "
        "offset, reporting progress after each batch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": ["gemini", "chatgpt", "claude", "mem0", "mcp-memory", "generic-json"],
                "description": "The source system to import from",
            },
            "file_path": {
                "type": "string",
                "description": "Path to the export file on disk (e.g. Gemini Takeout HTML, ChatGPT conversations.json)",
            },
            "content": {
                "type": "string",
                "description": "For file-based sources: the file content (pasted JSON, HTML, CSV)",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Parse and estimate without importing. Shows chunk count and estimated facts. Default: false.",
            },
        },
        "required": ["source"],
    },
}

UPGRADE = {
    "name": "totalreclaw_upgrade",
    "description": (
        "Start the TotalReclaw Pro upgrade flow — creates a Stripe "
        "Checkout session and returns the URL the user opens to pay. "
        "\n\nINVOKE WHEN USER SAYS:\n"
        "- 'upgrade to Pro' / 'I want Pro' / 'how do I upgrade'\n"
        "- 'I hit the free limit' / 'pay for more memories' / 'unlimited'\n"
        "- 'subscribe' / 'how much does Pro cost'\n"
        "\nAfter calling this tool, read the returned ``message`` aloud — "
        "it contains the checkout URL the user must open in their browser. "
        "Do NOT call this tool speculatively; only when the user has "
        "explicitly asked to upgrade, pay, or hit the quota limit. Pro "
        "tier routes writes to Gnosis mainnet (permanent) instead of "
        "Base Sepolia (testnet) and removes the free-write cap."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

DEBRIEF = {
    "name": "totalreclaw_debrief",
    "description": (
        "Capture broader context, outcomes, and open threads that "
        "turn-by-turn auto-extraction missed. Writes a small number of "
        "``summary`` facts (v1 type=summary, provenance=derived) that a "
        "future session can use to reconstruct what happened overall. "
        "\n\nINVOKE WHEN USER SAYS:\n"
        "- 'goodbye' / 'bye' / 'thanks' / 'that's all'\n"
        "- 'I'm done' / 'wrapping up' / 'let's end here'\n"
        "- after a long debug, plan, or decision session\n"
        "- before a detected context compaction / reset\n"
        "\nWHEN NOT TO USE:\n"
        "- casual chat or pure Q&A — adds noise, no broader context\n"
        "- when no memories were stored this session — nothing to synthesize\n"
        "- if unsure → skip; debriefs are meant to be rare + high-signal.\n"
        "\nThe tool reuses the same extraction pipeline as the automatic "
        "``on_session_end`` hook, so the resulting facts are identical in "
        "shape (type=summary, provenance=derived, scope=unspecified). "
        "Returns ``stored`` count + ``fact_ids`` so the agent can confirm."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

IMPORT_BATCH = {
    "name": "totalreclaw_import_batch",
    "description": (
        "Process one batch of a large conversation import. Call repeatedly with "
        "increasing offset until the response contains is_complete=true. "
        "After each batch, report progress to the user: "
        "'Batch 3/14 complete — 45 facts stored so far, ~8 minutes remaining.' "
        "The response includes chunks_processed, total_chunks, facts_stored, "
        "and remaining_chunks for progress calculation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": ["gemini", "chatgpt", "claude", "mem0", "mcp-memory", "generic-json"],
                "description": "The source system to import from (must match the initial import_from call)",
            },
            "file_path": {
                "type": "string",
                "description": "Path to the export file on disk",
            },
            "content": {
                "type": "string",
                "description": "For file-based sources: the file content",
            },
            "offset": {
                "type": "integer",
                "description": "Chunk offset to start processing from (default: 0)",
            },
            "batch_size": {
                "type": "integer",
                "description": "Number of chunks to process in this batch (default: 25)",
            },
        },
        "required": ["source"],
    },
}
