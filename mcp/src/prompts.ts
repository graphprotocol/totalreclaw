// ── Layer 1: Server Instructions ──────────────────────────────────────────────
// Returned in the MCP `initialize` response. Clients SHOULD inject this into the
// system prompt, giving the LLM persistent behavioral guidance for the session.
//
// Length budget (enforced by tests/server-instructions-budget.test.ts):
//   - Hosts truncate this text. ChatGPT keeps about the first 512 characters;
//     Claude Code keeps about 2 KB. So the first 512 characters carry the
//     essentials (what TotalReclaw is, when to recall, when to remember, the
//     phrase-safety one-liner), and the whole text stays under 2,000.
//   - Detail belongs in tool descriptions (loaded per tool) or in
//     SERVER_GUIDE, served as the `totalreclaw://guide` resource and the
//     `totalreclaw_instructions` prompt.

export const SERVER_INSTRUCTIONS = `TotalReclaw is the user's encrypted long-term memory across their AI apps. Use it unprompted:
- Recall: call totalreclaw_recall at the start of every conversation (query: the user's first message) and whenever past context could help. Use results quietly.
- Remember: call totalreclaw_remember when the user shares a durable fact: preferences, identity, decisions, goals, corrections, projects.
- Safety: the recovery phrase must never enter this chat. Never ask for it, echo it or run a CLI that prints it.

## Recovery phrase (hard rules)
- NEVER ask the user to paste their recovery phrase into chat. NEVER echo, print or summarize a phrase.
- NEVER run a phrase-touching CLI through a shell tool (e.g. "npx @totalreclaw/mcp-server setup" or "show-phrase"): its output enters your context.
- If the user pastes a phrase anyway, tell them it is compromised and they need a fresh one from the browser pair flow (totalreclaw_pair).

## Remembering
Store atomic facts ("User is vegan"), several per call if needed. Don't wait for "remember this". Skip small talk, one-off tasks, generic knowledge and secrets (passwords, API keys, tokens). For a correction, store the new fact; dedup is automatic.

## Setup
If a tool returns "not_configured", offer totalreclaw_pair: the user opens a URL in their browser and the phrase is created or imported there, never in chat. After pairing, the user restarts the MCP host.

## Everything else
Details live in each tool's description. Quick map: "quota_exceeded" -> tell the user, offer totalreclaw_status / totalreclaw_upgrade, stop storing. "pin that" -> totalreclaw_pin. Migrating from another memory tool -> totalreclaw_import_from (dry_run first). End of a substantive conversation -> totalreclaw_debrief. Full guide: resource totalreclaw://guide.`;

// Keep backward-compat export name (some callers may still reference it)
export const SYSTEM_PROMPT_FRAGMENT = SERVER_INSTRUCTIONS;

/** URI of the static usage-guide resource (all modes, including unconfigured). */
export const SERVER_GUIDE_URI = 'totalreclaw://guide';

/**
 * Full usage guide: the detail that used to live in SERVER_INSTRUCTIONS
 * before the 512 / 2,000-character budget. Not sent on `initialize`.
 * Served as the `totalreclaw://guide` resource and appended to the
 * `totalreclaw_instructions` prompt.
 */
export const SERVER_GUIDE = `# TotalReclaw usage guide

## Recovery phrase
TotalReclaw uses a 12-word BIP-39 recovery phrase as the user's identity. The phrase MUST NEVER cross your context window.
- NEVER ask the user to paste their phrase into chat. NEVER echo, print or summarize a phrase.
- NEVER invoke a phrase-touching CLI (e.g. "npx @totalreclaw/mcp-server setup", "npx @totalreclaw/mcp-server show-phrase") via a shell tool: its stdout enters your context.
- If the user pastes a phrase in chat anyway, tell them it is compromised and they need a fresh one from the browser pair flow (totalreclaw_pair).
- If your agent has a credential vault or secret store, the user's TOTALRECLAW_RECOVERY_PHRASE belongs there, not in a plaintext environment variable.

## Setup
If a tool returns "not_configured", offer totalreclaw_pair. It returns a URL and a PIN. The user opens the URL in a browser, the browser creates or imports the phrase, and only ciphertext reaches this server. After the browser says pairing is complete, the user restarts the MCP host. On an install that is already set up, totalreclaw_pair refuses; only the user can confirm replacing an account, on the pairing page.
Manual setup is documented at https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/claude-code-setup.md. The totalreclaw_setup tool was removed in 3.2.1 because it returned the phrase through tool output.

## Recall
Call totalreclaw_recall:
- at the start of every new conversation, with a brief summary of the user's first message as the query;
- when the user asks about something that might relate to past conversations;
- when you need context about the user's background, projects or preferences;
- when the user says "remember when...", "what did I say about...", "do you know my...".
Present recalled memories naturally as context. Do not announce "I found these memories" or list them unless the user asks.

## Remember
Call totalreclaw_remember without being asked whenever the user shares:
- personal preferences ("I prefer...", "I like...", "I hate...");
- personal information (name, location, occupation, family);
- decisions or goals ("I decided to...", "I chose X because Y");
- corrections ("Actually, I'm vegan now", "We moved to Berlin");
- important events or dates;
- technical preferences (languages, tools, frameworks, workflows);
- project context (what they're working on, deadlines, team);
- opinions and values that shape future interactions.
Do NOT store temporary context (today's weather, a one-time task), generic knowledge, or credentials (passwords, API keys, tokens).

Hygiene:
- Check recalled memories before storing to avoid duplicates; store-time dedup also runs.
- For a correction, store the new fact. The system handles supersession.
- Importance: 7-9 for core identity and preferences, 4-6 for moderate facts, 1-3 for minor details.
- Atomic facts: "User prefers dark mode", not "User said they prefer dark mode and also use VS Code".

## Billing and quota
Memories are stored permanently on Gnosis mainnet, end-to-end encrypted. Free and Pro tiers have monthly write caps; reads are unlimited. Do not guess or hardcode pricing or caps: use totalreclaw_status for exact usage, cap and pricing, and refer users to https://totalreclaw.xyz/pricing.
When totalreclaw_remember fails with "quota_exceeded":
1. Tell the user they have hit a usage limit.
2. Offer to check usage with totalreclaw_status.
3. If they want to upgrade, totalreclaw_upgrade returns a checkout link.
Do not keep retrying stores after a quota error.
Billing status is cached for up to 2 hours. If Pro features are not active after upgrading, suggest restarting the agent to refresh the cache.

## Imports
If the user mentions migrating from Mem0, MCP Memory Server or another AI memory tool, offer totalreclaw_import_from. Run with dry_run=true first, show the preview, then confirm before importing. API keys the user provides are used only for the import and are never stored.

## Editing memories
Besides remember and forget, three tools correct stored memories without wiping the vault:
- "pin that" / "never forget this" -> totalreclaw_pin (keeps a fact through auto-resolution);
- "that was actually a directive, not a preference" -> totalreclaw_retype;
- "that was work context" / "file that under personal" -> totalreclaw_set_scope.
All three work by supersession: they write a new claim with the override and link the original via superseded_by. The original stays as a tombstone, so history is inspectable. All three are idempotent.

## End of conversation
When a substantive conversation ends (goodbye, topic resolved, natural close), call totalreclaw_debrief with the key takeaways: what it was about, what was decided, what was tried and how it went, what is unfinished. Skip casual conversations. At most 5 items, each 1-3 sentences, type "summary" or "context", importance 7-8.

## Other tips
- If your agent supports background routines, a routine that calls totalreclaw_remember after conversations helps make sure nothing is missed.
- Memories are portable: they work across OpenClaw, Hermes, Claude Desktop, Cursor, NanoClaw and any MCP-compatible agent.
`;

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

export const UPGRADE_TOOL_DESCRIPTION = `Stripe checkout URL for Pro (1,500 memories/month on Gnosis mainnet, permanent on-chain, LLM-guided dedup).
INVOKE WHEN USER SAYS:
- "how do I upgrade?" / "I want to go pro"
- "how much does it cost?" (share pricing from checkout, don't hardcode)
- after \`quota_exceeded\` — offer inline
- "raise my limit" / "I keep hitting the cap"
DOES: calls relay checkout, returns one-time Stripe URL.
WHEN NOT TO USE:
- user already Pro (check totalreclaw_status)
- unrelated — don't volunteer upgrades`;

export const IMPORT_FROM_TOOL_DESCRIPTION = `Import from Mem0, MCP Memory, ChatGPT, Claude, Gemini, JSON/CSV.
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

export const PAIR_TOOL_DESCRIPTION = `Set up the user's TotalReclaw account (browser-side recovery-phrase generation or import).
INVOKE WHEN USER SAYS:
- "set up totalreclaw" / "set up my account"
- "I don't have a totalreclaw recovery phrase yet" / "create a totalreclaw account"
- ANY tool returns "not_configured" — propose this tool BEFORE asking the user to edit MCP config.
DOES: opens a brief relay session, returns {url, pin, expires_at_ms}. Hand the URL + PIN to the user — they open the URL in any browser, the BROWSER generates (or imports) a 12-word phrase, encrypts it, uploads ciphertext to this MCP server. Server decrypts in-memory, writes ~/.totalreclaw/credentials.json (mode 0600). Phrase NEVER touches LLM context, chat transcript, stdout, or logs.
AFTER PAIRING: tell the user to RESTART their MCP host (Claude Desktop / Cursor / Claude Code) so the server re-reads credentials.json and switches out of unconfigured mode.
WHEN NOT TO USE:
- user already has TOTALRECLAW_RECOVERY_PHRASE in env → tool will return URL but pairing is unnecessary
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
          content: { type: 'text', text: `${SERVER_INSTRUCTIONS}\n\n${SERVER_GUIDE}` },
        },
      ];

    default:
      throw new Error(`Unknown prompt: ${promptName}`);
  }
}
