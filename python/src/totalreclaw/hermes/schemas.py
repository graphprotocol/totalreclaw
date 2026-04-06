"""Tool schemas for Hermes Agent (OpenAI function-calling format)."""

REMEMBER = {
    "name": "totalreclaw_remember",
    "description": (
        "Store a memory in TotalReclaw's encrypted vault. Use this to save important "
        "facts, preferences, decisions, or context about the user. Memories are E2E "
        "encrypted and portable across AI agents."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The memory text to store",
            },
            "importance": {
                "type": "number",
                "description": "Importance score 0.0-1.0 (default 0.5)",
            },
        },
        "required": ["text"],
    },
}

RECALL = {
    "name": "totalreclaw_recall",
    "description": (
        "Search TotalReclaw's encrypted memory vault. Returns the most relevant "
        "memories matching the query, ranked by BM25 + semantic similarity."
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
