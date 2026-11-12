// ── Layer 1: Server Instructions ──────────────────────────────────────────────
// Returned in the MCP `initialize` response. Clients SHOULD inject this into the
// system prompt, giving the LLM persistent behavioral guidance for the session.

export const SERVER_INSTRUCTIONS = `
You have access to TotalReclaw, a persistent encrypted memory system. You SHOULD use these tools PROACTIVELY -- do not wait for the user to say "remember this" or "recall that."

## CRITICAL SAFETY: Recovery Phrase
TotalReclaw uses a 12-word BIP-39 recovery phrase as the user's identity. The recovery phrase MUST NEVER cross your context window. NEVER ask the user to paste their phrase into chat. NEVER echo, print, or summarize a phrase. NEVER invoke a phrase-touching CLI (e.g. "npx @totalreclaw/mcp-server setup", "npx @totalreclaw/mcp-server show-phrase") via a shell tool — its stdout enters your context. If the user pastes a phrase in chat anyway, tell them it is compromised and they need a fresh account via totalreclaw_pair.

## Setup Flow
If any TotalReclaw tool returns a "not_configured" error, the user has not set up an account yet. Call totalreclaw_pair: it returns a link and a 6-digit PIN. Give both to the user. They open the link in a browser and create a new recovery phrase there, or enter an existing one there; the phrase is encrypted in the browser and never passes through this chat. If the user pastes a phrase in chat anyway, tell them it is compromised. There is no in-chat setup tool: the totalreclaw_setup tool was removed in 3.2.1 because returning the phrase through tool output crossed your context, violating phrase-safety.

## After Pairing
When the user finishes the totalreclaw_pair page ("You're all set"), ask them to fully quit and reopen this app (or start a new session) before any memory tool is used: credentials are loaded at startup, so until the restart every tool still returns not_configured. Closing the window is not enough.

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
Both tiers store memories permanently on Gnosis mainnet (E2E encrypted). Each tier has a monthly write limit that resets on the 1st of the month. Reads are unlimited on both tiers. Do NOT guess or hardcode pricing or quota numbers — use totalreclaw_status to check exact usage, the current cap, and current pricing. For pricing details, refer users to https://totalreclaw.xyz/pricing

When a totalreclaw_remember call fails with "quota_exceeded":
1. Inform the user they've hit a usage limit
2. Offer to check their usage with totalreclaw_status
3. If they want to upgrade, use totalreclaw_upgrade to get a checkout link — upgrading is done right here through the agent
Do NOT repeatedly attempt to store memories after a quota error.

Note: Billing status is cached locally for up to 2 hours. After upgrading to Pro, the new tier may take up to 2 hours to take effect. If the user reports that Pro features are not active after upgrading, suggest restarting their agent to force a billing cache refresh.

### Importing memories from other tools
If the user mentions migrating from Mem0, MCP Memory Server, or any other AI memory tool, offer to use totalreclaw_import_from. Start with dry_run=true to preview, then confirm before importing.
API keys provided by the user are used only for the import and are never stored.

### Agent-specific tips
- If your agent supports background routines or scheduled tasks, set up a routine to call totalreclaw_remember after conversations to ensure nothing is missed.
- TotalReclaw memories are fully portable -- they work across OpenClaw, IronClaw, Claude Desktop, Cursor, NanoClaw, and any MCP-compatible agent.

### User-controlled memory edits (v1)
In addition to remember + forget, three tools let the user correct stored memories without
wiping the vault. Use these when the user says something like:
- "pin that" / "never forget this" → totalreclaw_pin (preserves a fact across auto-resolution)
- "that was actually a directive, not a preference" → totalreclaw_retype
- "that was work context" / "file that under personal" → totalreclaw_set_scope

Scopes (work, personal, health, family, creative, finance, misc, unspecified) are how memories are filed: whenever the user says "file that under X", "put that in my work memories", "categorize as X" or "that's a work thing", call totalreclaw_set_scope on the memory just recalled or stored.

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

export const RECALL_TOOL_DESCRIPTION = `Search encrypted vault. Top 16 reranked by provenance+semantic+recency.
INVOKE WHEN USER SAYS:
- "what's my [phone/address/doctor]"
- "remember when I..." / "what did I say about..."
- "do you know my..." / "my preferences for..."
- opens convo on a topic (auto-recall first msg)
WHEN NOT TO USE: world knowledge, small talk.
DOES: trapdoor search, decrypt, rerank BM25+cosine+source-provenance.
PARAMS: query (5-15 words), k (def 16, max 50).
Don't announce "I found these memories" — just use them.`;

export const FORGET_TOOL_DESCRIPTION = `Permanently remove memory.
INVOKE WHEN USER SAYS:
- "forget what I said about X" / "delete that fact"
- "that's wrong/outdated — remove it"
- "I don't want you remembering X"
WHEN NOT TO USE:
- CORRECTING → totalreclaw_remember (dedup auto)
- never forget proactively
DOES: memory_id → tombstone. query → recall ≤50, tombstone all (self-hosted only; managed service needs an id).
PARAMS: memory_id = the fact_id from a recall result (fact_id also accepted), query, scope.`;

export const EXPORT_TOOL_DESCRIPTION = `Export decrypted vault — one-click backup. Self-hosted only (refused on the managed service).
INVOKE WHEN USER SAYS:
- "show everything you know about me" / "what do you have on me?"
- "export / back up / download my memories"
- "take my memory elsewhere" / "portable copy"
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

export const UPGRADE_TOOL_DESCRIPTION = `Stripe checkout URL for Pro (a higher monthly write limit, permanent on-chain, LLM-guided dedup).
INVOKE WHEN USER SAYS:
- "how do I upgrade?" / "I want to go pro"
- "how much does it cost?" (share pricing from checkout, don't hardcode)
- after \`quota_exceeded\` — offer inline
- "raise my limit" / "I keep hitting the cap"
DOES: calls relay checkout, returns one-time Stripe URL.
WHEN NOT TO USE:
- user already Pro (check totalreclaw_status)
- unrelated — don't volunteer upgrades`;

export const IMPORT_FROM_TOOL_DESCRIPTION = `Import from Mem0, MCP Memory, ChatGPT, Claude, Gemini, JSON/CSV. Self-hosted only (managed service refuses it, even dry_run).
INVOKE WHEN USER SAYS:
- "migrate memory from [Mem0/ChatGPT/Claude]"
- "import my ChatGPT memories" / "here's my Mem0 export"
- "I pasted my Claude memory"
WHEN NOT TO USE: TotalReclaw backup → totalreclaw_import. Single fact → totalreclaw_remember.
DOES: adapter parses+stores (unless dry_run). Fingerprint dedup.
ALWAYS dry_run=true first.`;

export const IMPORT_TOOL_DESCRIPTION = `Restore from totalreclaw_export backup (JSON/Markdown). Self-hosted only (managed service refuses it).
INVOKE WHEN USER SAYS:
- "restore memories from this backup" / "import this JSON"
- "I exported from another TotalReclaw account"
- "put the export back"
WHEN NOT TO USE:
- Mem0/ChatGPT/Claude → totalreclaw_import_from
- single fact → totalreclaw_remember
DOES: parse JSON/Markdown (auto), store + fingerprint dedup.
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

export const ACCOUNT_TOOL_DESCRIPTION = `Account overview: wallet, tier, usage, memories, features, credential kind.
INVOKE WHEN USER SAYS:
- "show my account" / "what's my profile?"
- "how many memories?" / "my wallet?"
- "summary of my setup"
DOES: billing+fact count parallel. Returns wallet, tier, writes_used/limit, total_facts, features, credential kind. Never any part of the recovery phrase.
WHEN NOT TO USE:
- only billing → totalreclaw_status (lighter)
- recovery phrase (any word) → NEVER available; user checks own backup.`;

export const PAIR_TOOL_DESCRIPTION = `Set up the user's TotalReclaw account (browser-side recovery-phrase generation or import).
INVOKE WHEN USER SAYS:
- "set up totalreclaw" / "set up my account"
- "I don't have a totalreclaw recovery phrase yet" / "create a totalreclaw account"
- ANY tool returns "not_configured" — propose this tool BEFORE asking the user to edit MCP config.
DOES: opens a relay pairing session, returns {url, pin, expires_at_ms}. Hand the URL + PIN to the user — they open the URL in any browser, the BROWSER generates (or imports) a 12-word phrase, encrypts it, uploads ciphertext to this MCP server. Server decrypts in-memory, writes ~/.totalreclaw/credentials.json (mode 0600). Phrase NEVER touches LLM context, chat transcript, stdout, or logs.
AFTER PAIRING: when the page says "You're all set", tell the user to fully quit and reopen their MCP host (Claude Desktop / Cursor / Claude Code); the server reads credentials.json only at startup. The link lasts about 10 minutes.
WHEN NOT TO USE:
- already set up (credentials.json exists or the server is configured) → the tool refuses with already_configured. Tell the user they are set up. Only if THEY ask to replace their account, call with replace=true; the pairing page asks the user to confirm. You cannot confirm it for them.
- user explicitly wants terminal-only setup → point them at \`npx @totalreclaw/mcp-server setup\` (CLI wizard, runs OUTSIDE your shell tool)
- NEVER ask the user to paste their phrase in chat. NEVER echo or print a phrase from any source.`;

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
