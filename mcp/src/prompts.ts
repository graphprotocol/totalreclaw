// ── Layer 1: Server Instructions ──────────────────────────────────────────────
// Returned in the MCP `initialize` response. Clients SHOULD inject this into the
// system prompt, giving the LLM persistent behavioral guidance for the session.

export const SERVER_INSTRUCTIONS = `
You have access to TotalReclaw, a persistent encrypted memory system. You SHOULD use these tools PROACTIVELY -- do not wait for the user to say "remember this" or "recall that."

## CRITICAL SAFETY: Recovery Phrase
TotalReclaw uses a 12-word BIP-39 recovery phrase as the user's identity. If a user tries to import a phrase they already use for a CRYPTO WALLET, you MUST warn them: "Do NOT use a recovery phrase from an existing crypto wallet. TotalReclaw should have its own dedicated phrase to keep your funds safe." Always generate a fresh phrase for new users.

## Setup Flow
If any TotalReclaw tool returns a "not_configured" error, the user needs to set up TotalReclaw first. Use the totalreclaw_setup tool:
1. Ask the user: "Do you have an existing TotalReclaw recovery phrase you'd like to restore, or should I generate a new one?"
2. If new: call totalreclaw_setup with action="generate". Display the recovery phrase prominently and warn them to save it securely.
3. If existing: ask for their 12-word recovery phrase, then call totalreclaw_setup with action="import" and recovery_phrase="their phrase".
After setup completes, all TotalReclaw tools work immediately — no restart needed.

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
The free tier offers unlimited memories and reads, but memories are stored on a test network and may be reset. Pro stores memories permanently on-chain. Do NOT guess or hardcode pricing — use totalreclaw_status to check exact usage and current pricing. For pricing details, refer users to https://totalreclaw.xyz/pricing

When a totalreclaw_remember call fails with "quota_exceeded":
1. Inform the user they've hit a usage limit
2. Offer to check their usage with totalreclaw_status
3. If they want to upgrade, use totalreclaw_upgrade to get a checkout link — upgrading is done right here through the agent
Do NOT repeatedly attempt to store memories after a quota error.

Note: Billing status is cached locally for up to 2 hours. After upgrading to Pro, the new tier may take up to 2 hours to take effect. If the user reports that Pro features are not active after upgrading, suggest restarting their agent to force a billing cache refresh.

### Importing memories from other tools
If the user mentions migrating from Mem0, MCP Memory Server, MemoClaw, or any other AI memory tool, offer to use totalreclaw_import_from. Start with dry_run=true to preview, then confirm before importing.
API keys provided by the user are used only for the import and are never stored.

### Agent-specific tips
- If your agent supports background routines or scheduled tasks, set up a routine to call totalreclaw_remember after conversations to ensure nothing is missed.
- If your agent supports a credential vault or secret store, the user's TOTALRECLAW_RECOVERY_PHRASE should be stored there instead of as a plaintext environment variable.
- TotalReclaw memories are fully portable -- they work across OpenClaw, IronClaw, Claude Desktop, Cursor, NanoClaw, and any MCP-compatible agent.

### User-controlled memory edits (v1)
In addition to remember + forget, three tools let the user correct stored memories without
wiping the vault. Use these when the user says something like:
- "pin that" / "never forget this" → totalreclaw_pin (preserves a fact across auto-resolution)
- "that was actually a directive, not a preference" → totalreclaw_retype
- "that was work context" / "file that under personal" → totalreclaw_set_scope

All three operate by supersession: they create a new claim with the override and link it
back to the original via superseded_by. The original stays in the vault as a tombstone so
history is inspectable. All three are idempotent (no-op if the target already matches).

### End of Conversation
When a substantive conversation is ending (the user says goodbye, the topic is resolved,
or the conversation naturally concludes), call totalreclaw_debrief with the key takeaways.

Focus on what individual memory storage missed:
- What was the conversation about overall?
- What was decided or resolved?
- What approaches were tried and what was the outcome?
- What's left unfinished?

Do NOT debrief casual conversations (greetings, simple Q&A, small talk).
Max 5 items, each 1-3 sentences, type "summary" or "context", importance 7-8.
`;

// Keep backward-compat export name (some callers may still reference it)
export const SYSTEM_PROMPT_FRAGMENT = SERVER_INSTRUCTIONS;

// ── Layer 2: Enhanced Tool Descriptions ──────────────────────────────────────

export const REMEMBER_TOOL_DESCRIPTION = `Store user info PROACTIVELY — don't wait for "remember this".
INVOKE WHEN USER SAYS "I prefer X", "I'm X", "I chose X because Y"; reveals identity/location/job/family.
NOT FOR: chitchat, passwords, generic knowledge.
Extract ATOMIC. Good: "User is vegan". Bad: "User said they became vegan, prefer organic".
PARAMS facts[]: text; importance 1-10 def 5; type claim|preference|directive|commitment|episode|summary; scope work|personal|health|family|creative|finance|misc|unspecified. Dedup auto.`;

export const RECALL_TOOL_DESCRIPTION = `Search encrypted vault. Top 8 reranked by provenance+semantic+recency.
INVOKE WHEN USER SAYS:
- "what's my [phone/address/doctor]"
- "remember when I..." / "what did I say about..."
- "do you know my..." / "my preferences for..."
- opens convo on a topic (auto-recall first msg)
WHEN NOT TO USE: world knowledge, small talk.
DOES: trapdoor search, decrypt, rerank BM25+cosine+source-provenance.
PARAMS: query (5-15 words), k (def 8, max 50).
Don't announce "I found these memories" — just use them.`;

export const FORGET_TOOL_DESCRIPTION = `Permanently remove memory.
INVOKE WHEN USER SAYS:
- "forget what I said about X" / "delete that fact"
- "that's wrong/outdated — remove it"
- "I don't want you remembering X"
- "erase that password I pasted"
WHEN NOT TO USE:
- CORRECTING → totalreclaw_remember (dedup auto)
- changing mind ("I prefer tea now") → remember new pref
- no explicit request — never forget proactively
DOES: fact_id → tombstone. query → recall ≤50, tombstone all.
PARAMS: fact_id (pref), query (max 50), scope (hint).`;

export const EXPORT_TOOL_DESCRIPTION = `Export decrypted vault — one-click backup.
INVOKE WHEN USER SAYS:
- "show everything you know about me" / "what do you have on me?"
- "export / back up / download my memories"
- "take my memory elsewhere" / "portable copy"
- "audit what's stored" / "what's in my vault?"
WHEN NOT TO USE:
- one thing → totalreclaw_recall
- cleanup → totalreclaw_consolidate/forget
DOES: decrypt all → Markdown or JSON. Surfaces v1 type/source/scope/reasoning.
PARAMS: format, include_metadata.`;

export const STATUS_TOOL_DESCRIPTION = `Check tier + usage + quota.
INVOKE WHEN USER SAYS:
- "what tier am I on?" / "am I on free or pro?"
- "how many memories have I stored?" / "what's my usage?"
- "is my subscription active?" / "when does it expire?"
- "am I close to the limit?"
DOES: query relay billing endpoint. Returns tier, writes_used/limit, expiry. Refreshes local billing cache. Call before bulk ops.
WHEN NOT TO USE:
- billing history/invoices → dashboard/support
- wants to upgrade → totalreclaw_upgrade`;

export const UPGRADE_TOOL_DESCRIPTION = `Stripe checkout URL for Pro (unlimited permanent on-chain, Gnosis).
INVOKE WHEN USER SAYS:
- "how do I upgrade?" / "I want to go pro"
- "how much does it cost?" (share pricing from checkout, don't hardcode)
- after \`quota_exceeded\` — offer inline
- "unlimited memory?" / "make my memories permanent"
DOES: calls relay checkout, returns one-time Stripe URL. On success suggest totalreclaw_migrate.
WHEN NOT TO USE:
- user already Pro (check totalreclaw_status)
- unrelated — don't volunteer upgrades`;

export const MIGRATE_TOOL_DESCRIPTION = `Copy memories Base Sepolia → Gnosis after Pro upgrade. Chain-agnostic encrypted data.
INVOKE WHEN user just upgraded via totalreclaw_upgrade, asks about testnet→mainnet migration, wants permanent on-chain storage.
SAFETY: dry-run default, idempotent, testnet never deleted.
WHEN NOT TO USE: user on Free tier (nothing to migrate).
PARAMS: confirm (true=execute, omit=preview).
WORKFLOW: 1) w/o confirm → share preview. 2) on approval → confirm=true. 3) report progress.`;

export const IMPORT_FROM_TOOL_DESCRIPTION = `Import from Mem0, MCP Memory, ChatGPT, Claude, Gemini, MemoClaw, JSON/CSV.
INVOKE WHEN USER SAYS:
- "migrate memory from [Mem0/ChatGPT/Claude]"
- "import my ChatGPT memories" / "here's my Mem0 export"
- "I pasted my Claude memory — store it"
WHEN NOT TO USE: TotalReclaw backup → totalreclaw_import. Single fact → totalreclaw_remember.
DOES: adapter parses+stores (unless dry_run). Fingerprint dedup. Convo sources return chunks for host LLM.
ALWAYS dry_run=true first → preview → confirm.`;

export const IMPORT_TOOL_DESCRIPTION = `Restore from totalreclaw_export backup (JSON/Markdown).
INVOKE WHEN USER SAYS:
- "restore memories from this backup" / "import this JSON"
- "I exported from another TotalReclaw account"
- "here's the Markdown export — put it back"
WHEN NOT TO USE:
- Mem0/ChatGPT/Claude → totalreclaw_import_from
- single fact → totalreclaw_remember
DOES: parse JSON/Markdown (auto), store via totalreclaw_remember + fingerprint dedup.
PARAMS: content (req), format, merge_strategy, validate_only.`;

export const SUPPORT_TOOL_DESCRIPTION = `Support contact, docs, troubleshooting.
INVOKE WHEN USER SAYS:
- "how do I get help?" / "who do I contact?"
- "memories aren't loading" / "getting errors"
- "lost my recovery phrase" / "where's the docs?"
- "how do I report a bug?"
- asks: slow recall, quota errors, failed imports, missing post-upgrade memories
DOES: static bundle (pre-filled email, docs URL, issues URL, troubleshooting table). Works unconfigured.
WHEN NOT TO USE: billing info → totalreclaw_status. Skip unrelated questions.`;

export const ACCOUNT_TOOL_DESCRIPTION = `Account overview: wallet, tier, usage, memories, features, safe recovery hint.
INVOKE WHEN USER SAYS:
- "show my account" / "what's my profile?"
- "how many memories?" / "my wallet?"
- "right recovery phrase? hint"
- "summary of my setup"
DOES: billing+fact count parallel. Returns wallet, tier, writes_used/limit, total_facts, features, FIRST+LAST recovery words (never full).
WHEN NOT TO USE:
- only billing → totalreclaw_status (lighter)
- FULL recovery phrase → NEVER; password manager.`;

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
