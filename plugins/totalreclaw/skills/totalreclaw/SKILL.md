---
name: totalreclaw
description: End-to-end encrypted memory across conversations. Use at the start of every conversation and when the topic changes to recall what the user told you before, and whenever the user shares a durable fact, preference, decision or correction, to store it, without waiting to be asked to "remember". Also use to set up a TotalReclaw account and when a memory write fails with quota_exceeded.
---

# TotalReclaw memory

TotalReclaw is the user's private, end-to-end encrypted memory. Memories are encrypted on the user's computer with a key derived from their recovery phrase, and stored across a decentralized network. The relay only forwards ciphertext and never sees plaintext. Use the `totalreclaw_*` tools; do not keep user memories in local files such as `MEMORY.md`.

## Recall

- Call `totalreclaw_recall` at the start of every new conversation, using a short summary of the user's first message as the query.
- Call it again when the topic changes, or when the user asks about themselves, their preferences, their projects, or past conversations ("what did I say about...", "do you know my...").
- Use what comes back naturally. Do not announce "I found these memories" unless asked. If nothing comes back, say so plainly and do not guess.

## Store

Call `totalreclaw_remember` on your own, without waiting for "remember this", when the user shares:

- preferences ("I prefer...", "I hate...") and technical preferences (languages, tools, workflows)
- personal facts (name, location, job, family) and project context (what they work on, deadlines, team)
- decisions and goals ("I decided...", "we agreed...") and commitments
- corrections to earlier information ("actually I'm vegan now"); store the correction

Store atomic facts ("User prefers dark mode", not a paraphrase of the whole message). Importance 7 to 9 for core identity and preferences, 4 to 6 for moderate facts, 1 to 3 for minor details. Check recalled memories first to avoid duplicates.

Never store secrets: passwords, API keys, tokens, private keys, or a recovery phrase. Also skip transient context (today's weather, a one-off task) and generic world knowledge.

When the user says "forget X", use `totalreclaw_forget`. For pinning, retyping or re-scoping a memory, use `totalreclaw_pin`, `totalreclaw_retype` and `totalreclaw_set_scope`.

## Set up an account

If a tool says the account is not set up, or `totalreclaw_status` shows it is not onboarded:

1. Call `totalreclaw_pair` with `{"mode": "generate"}` for a new account, or `{"mode": "import"}` to restore an existing one.
2. It returns a link and a PIN. Tell the user: open the link in their browser, enter the PIN, and generate or paste their recovery phrase there. Ask them to reply "done" when it is sealed.
3. Wait for "done", then call `totalreclaw_status` to confirm.

## Recovery phrase: hard rules

- **Never ask the user for their recovery phrase in chat, accept it, or repeat it.** It is entered only in the browser page from `totalreclaw_pair`, and it must never enter your context.
- Never run any shell command that prints or generates a recovery phrase.
- If the user pastes a phrase into chat anyway, tell them it is now compromised, do not repeat it, and offer to set up a fresh account with `totalreclaw_pair` and `{"mode": "generate"}`.

## Quota

If `totalreclaw_remember` fails with `quota_exceeded`, tell the user they have hit their monthly memory limit, stop retrying, and offer to show their usage with `totalreclaw_status`. If they want to upgrade, call `totalreclaw_upgrade` and give them the checkout link. Do not quote prices or limits from memory; read them from `totalreclaw_status`.

## Speaking to the user

Say "your memory" and "your recovery phrase". Don't talk about relays, pairing, subgraphs or MCP. Memories are stored encrypted on a decentralized network, not "locally" and not on a company server.
