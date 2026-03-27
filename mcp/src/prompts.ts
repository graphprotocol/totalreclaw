// ── Layer 1: Server Instructions ──────────────────────────────────────────────
// Returned in the MCP `initialize` response. Clients SHOULD inject this into the
// system prompt, giving the LLM persistent behavioral guidance for the session.

export const SERVER_INSTRUCTIONS = `
You have access to TotalReclaw, a persistent encrypted memory system. You SHOULD use these tools PROACTIVELY -- do not wait for the user to say "remember this" or "recall that."

## CRITICAL SAFETY: Recovery Phrase
TotalReclaw uses a 12-word BIP-39 recovery phrase as the user's identity. If a user tries to import a phrase they already use for a CRYPTO WALLET, you MUST warn them: "Do NOT use a recovery phrase from an existing crypto wallet. TotalReclaw should have its own dedicated phrase to keep your funds safe." Always generate a fresh phrase for new users.

## CRITICAL: Automatic Memory Behaviors

### At Conversation Start
ALWAYS call totalreclaw_recall at the beginning of EVERY new conversation with a brief summary of the user's first message as the query. This loads relevant context from past conversations. Present recalled memories naturally as context -- do not announce "I found these memories" or list them explicitly unless the user asks.

### Proactive Memory Storage (IMPORTANT)
You should call totalreclaw_remember WITHOUT being asked whenever the user shares:
- Personal preferences ("I prefer...", "I like...", "I hate...")
- Personal information (name, location, occupation, family details)
- Decisions or goals ("I decided to...", "I want to...", "I chose X because Y")
- Corrections to previous information ("Actually, I'm vegan now", "We moved to Berlin")
- Important events or dates ("My birthday is...", "I started a new job at...")
- Technical preferences (programming language, tools, frameworks, workflows)
- Project context (what they're working on, deadlines, team details)
- Opinions and values that shape future interactions

The user should NOT have to tell you to remember things. If information would be valuable in a future conversation, store it automatically. Think of yourself as a thoughtful assistant who naturally takes notes on important details.

Do NOT store:
- Temporary/transient context (weather today, a one-time task)
- Generic knowledge unrelated to the user
- Sensitive credentials (passwords, API keys, tokens)

### Memory Recall
Call totalreclaw_recall proactively when:
- Starting any new conversation (query based on the user's first message)
- The user asks about something that might relate to past conversations
- You need context about the user's background, projects, or preferences
- The user says "remember when...", "what did I say about...", "do you know my..."

### Memory Hygiene
- Before storing a new fact, check recalled memories to avoid duplicates
- When the user corrects information, store the correction (the system handles deduplication)
- Use importance 7-9 for core identity/preferences, 4-6 for moderate facts, 1-3 for minor details
- Extract atomic facts: "User prefers dark mode" not "User said they prefer dark mode and also use VS Code"

### Billing & Quota
When a totalreclaw_remember call fails with "quota_exceeded":
1. Inform the user they've reached their free tier limit
2. Offer to check their usage with totalreclaw_status
3. If they want to upgrade, use totalreclaw_upgrade to get a checkout link
Do NOT repeatedly attempt to store memories after a quota error.

Note: Billing status is cached locally for up to 2 hours. After upgrading to Pro, the new tier may take up to 2 hours to take effect. If the user reports that Pro features are not active after upgrading, suggest restarting their agent to force a billing cache refresh.

### Importing memories from other tools
If the user mentions migrating from Mem0, MCP Memory Server, MemoClaw, or any other AI memory tool, offer to use totalreclaw_import_from. Start with dry_run=true to preview, then confirm before importing.
API keys provided by the user are used only for the import and are never stored.

### Agent-specific tips
- If your agent supports background routines or scheduled tasks, set up a routine to call totalreclaw_remember after conversations to ensure nothing is missed.
- If your agent supports a credential vault or secret store, the user's TOTALRECLAW_RECOVERY_PHRASE should be stored there instead of as a plaintext environment variable.
- TotalReclaw memories are fully portable -- they work across OpenClaw, IronClaw, Claude Desktop, Cursor, NanoClaw, and any MCP-compatible agent.
`;

// Keep backward-compat export name (some callers may still reference it)
export const SYSTEM_PROMPT_FRAGMENT = SERVER_INSTRUCTIONS;

// ── Layer 2: Enhanced Tool Descriptions ──────────────────────────────────────

export const REMEMBER_TOOL_DESCRIPTION = `Store important information the user shares. Call this PROACTIVELY -- do not wait for the user to say "remember this."

Whenever the user mentions preferences, facts about themselves, decisions, project context, goals, or anything they would want remembered across conversations, call this tool immediately. You are the user's memory -- take notes automatically.

IMPORTANT: Extract atomic facts, not entire conversation snippets.
Good: "User is vegan"
Bad: "User said they recently became vegan and prefer organic food from local farms"

The facts parameter accepts an array, so you can store multiple facts in a single call.

Each fact needs:
- text: The atomic fact (required)
- importance: 1-10 scale (optional, default 5)
  - 9-10: Core identity (name, fundamental values)
  - 7-8: Important preferences (diet, work style)
  - 5-6: Moderate facts (schedule, minor preferences)
  - 3-4: Low priority (casual mentions)
  - 1-2: Ephemeral (likely to change)
- type: Category (optional) -- "fact", "preference", "decision", "episodic", "goal", "context", "summary"

The vault handles deduplication automatically. If a similar fact exists, it will be updated rather than duplicated.`;

export const RECALL_TOOL_DESCRIPTION = `Search your memory for relevant context. Returns the top 8 most relevant memories (not all — this is a targeted search, not a full dump).

Call this proactively when:
- The user's message suggests they've told you something before ("remember when I said...", "what's my...")
- The conversation topic likely relates to previously stored information
- The user asks about their preferences, history, or past decisions
- The user references something from a previous conversation
- You need context about the user's background, projects, or work style
- The user's question could be answered or enriched by stored memories

Parameters:
- query: Natural language search (required). Keep it concise -- 5-15 words work best.
- k: Number of results (default 8, max 50). Use 3-5 for quick lookups, 8-12 for broad context.

Present recalled memories naturally as context. Do not announce "I found these memories" unless the user asks.`;

export const FORGET_TOOL_DESCRIPTION = `Delete a specific memory from your vault.

WHEN TO USE:
- User explicitly asks to forget something
- User says information is outdated or incorrect
- User requests to remove sensitive information

WHEN NOT TO USE:
- To update information (use remember with updated fact instead)
- Without user's explicit request

EXAMPLES:
- User: "Forget that I work at Google, I left"
  -> Call: totalreclaw_forget({ fact_id: "the-fact-id" })

PARAMETERS:
- fact_id: The ID of the fact to forget (required)
  OR
- query: Forget all memories matching query (optional)`;

export const EXPORT_TOOL_DESCRIPTION = `Export all memories in plaintext for portability.

WHEN TO USE:
- User wants to backup their memories
- User wants to transfer memories to another system
- User wants to see all stored information

OUTPUT FORMATS:
- markdown: Human-readable format
- json: Machine-readable format for import

EXAMPLES:
- User: "Export all my memories"
  -> Call: totalreclaw_export({ format: "markdown" })

PARAMETERS:
- format: Output format "markdown" or "json" (default: markdown)`;

export const STATUS_TOOL_DESCRIPTION = `Check your TotalReclaw subscription status and usage.

Shows:
- Current tier (free/pro)
- Free writes used vs limit
- Subscription expiry date

Use this when:
- User asks about their memory quota or usage
- Before storing many memories, to check remaining capacity
- User asks about billing or subscription`;

export const UPGRADE_TOOL_DESCRIPTION = `Upgrade to TotalReclaw Pro for unlimited encrypted memories.

Returns a Stripe checkout URL for the user to complete payment via credit/debit card.

Use this when:
- User hits their free tier limit
- User asks about upgrading or pricing
- A remember call returns a quota_exceeded error`;

export const MIGRATE_TOOL_DESCRIPTION = `Migrate memories from testnet (Base Sepolia) to mainnet (Gnosis) after upgrading to Pro.

When a user upgrades from Free to Pro, their memories are on the Base Sepolia testnet. This tool copies them to Gnosis mainnet for permanent storage. No re-encryption needed -- the encrypted data is chain-agnostic.

SAFETY:
- Dry-run by default: call without confirm=true to see a preview of what will be migrated
- Idempotent: running it again skips facts that already exist on mainnet
- Testnet facts are never deleted (they remain as a backup)

WHEN TO USE:
- After a user successfully upgrades to Pro via totalreclaw_upgrade
- User asks about migrating their testnet memories to mainnet
- User wants to ensure their memories are on permanent storage

PARAMETERS:
- confirm: Set to true to execute the migration. Without it, returns a dry-run preview showing how many memories will be migrated.

WORKFLOW:
1. First call with confirm=false (or omit) to see the preview
2. Share the preview with the user
3. If they confirm, call again with confirm=true
4. Report progress as batches complete`;

export const IMPORT_FROM_TOOL_DESCRIPTION = `Import memories from other AI memory tools into TotalReclaw.

Supported sources:
- **mem0**: Import from Mem0 (mem0.ai). Provide api_key + source_user_id, or paste the export JSON.
- **mcp-memory**: Import from MCP Memory Server (@modelcontextprotocol/server-memory). Provide the memory.jsonl content or file_path. Default path: ~/.mcp-memory/memory.jsonl.
- **memoclaw**: Import from MemoClaw. Provide api_key + source_user_id, or paste the export JSON.
- **generic-json**: Import from a generic JSON file. Expects an array of objects with "text" field.
- **generic-csv**: Import from a CSV file. Expects a header row with "text" column.

Security: API keys are used in-memory only for this import and are never stored.
Idempotent: Running the same import twice will not create duplicates (content fingerprint dedup).

Use dry_run=true to preview what would be imported without storing anything.`;

export const IMPORT_TOOL_DESCRIPTION = `Import memories from an exported backup.

WHEN TO USE:
- User wants to restore memories from backup
- User wants to transfer memories from another TotalReclaw instance
- User has a JSON or Markdown export they want to import

MERGE STRATEGIES:
- skip_existing: Skip facts that already exist (default)
- overwrite: Replace existing facts with imported ones
- merge: Use LLM-assisted conflict resolution

EXAMPLES:
- User: "Import memories from this backup file"
  -> Call: totalreclaw_import({ content: "...", format: "json" })

PARAMETERS:
- content: The exported content (JSON or Markdown string)
- format: Format of content "markdown" or "json" (auto-detected if not specified)
- merge_strategy: How to handle conflicts (default: skip_existing)
- validate_only: Parse and validate without importing (dry-run)`;

// ── Layer 5: Prompt Fallback Templates ───────────────────────────────────────

export const PROMPT_DEFINITIONS = [
  {
    name: 'totalreclaw_start',
    title: 'Start with Memory',
    description:
      'Load your memory context for this conversation. Use this if memories were not loaded automatically.',
    arguments: [
      {
        name: 'topic',
        description: 'Optional topic to focus memory recall on',
        required: false,
      },
    ],
  },
  {
    name: 'totalreclaw_save',
    title: 'Save to Memory',
    description:
      'Extract and save important facts from this conversation to your memory vault.',
    arguments: [] as Array<{ name: string; description: string; required: boolean }>,
  },
];

export function getPromptMessages(
  promptName: string,
  args?: Record<string, string>
): Array<{ role: string; content: { type: string; text: string } }> {
  switch (promptName) {
    case 'totalreclaw_start': {
      const topic = args?.topic || 'recent context';
      return [
        {
          role: 'user',
          content: {
            type: 'text',
            text: `Please recall my relevant memories about ${topic} and use them to personalize our conversation.`,
          },
        },
      ];
    }

    case 'totalreclaw_save':
      return [
        {
          role: 'user',
          content: {
            type: 'text',
            text: `Review our conversation and extract any important facts, preferences, decisions, or personal information I shared. Store each one using totalreclaw_remember with appropriate importance scores. Extract atomic facts, not summaries.`,
          },
        },
      ];

    case 'totalreclaw_instructions':
      return [
        {
          role: 'assistant',
          content: { type: 'text', text: SERVER_INSTRUCTIONS },
        },
      ];

    default:
      throw new Error(`Unknown prompt: ${promptName}`);
  }
}
