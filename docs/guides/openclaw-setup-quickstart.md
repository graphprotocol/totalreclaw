# TotalReclaw — OpenClaw quickstart (agent-executable)

> Audience: the LLM agent fetching this URL during install. Human-readable rationale lives in `openclaw-setup.md`.
> Stable contract: ~3 KB, copy-paste-runnable, all rules in scope.

## Hard rules (read once, never break)

1. **Emit ONLY the four numbered user-visible lines below.** No narration, no "let me check…", no "Now let me X", no "Need to Y", no "Standing by", no version-lag commentary, no path-resolution prose. The transcript Pedro sees during a healthy install is exactly four messages.
2. **Never use the word "pair" / "pairing" / "paired" in user-facing text.** It's internal jargon. The user-facing word is **"set up an account"** / "your account". (Behind the scenes the CLI command is still `tr pair`; that's an implementation detail.)
3. **Don't claim memories are "stored on a relay server" / "TotalReclaw's server" / any centralized-custody phrasing.** Memories are encrypted with a key derived from the recovery phrase, submitted on-chain (Account Abstraction), stored across a decentralized network, indexed via The Graph subgraph. The relay only forwards encrypted bundles. See "How does this work?" section below for the canonical answer.
4. **Do NOT restart the gateway** (`/totalreclaw-restart`, `kill -USR1`, `docker restart`, `openclaw gateway restart`). The plugin self-patches and signals its own restart needs.
5. **Do NOT write to `~/.openclaw/openclaw.json`**. The plugin's `patchOpenClawConfig()` already wrote everything it needs.
6. **Do NOT save QR PNGs**. The `tr pair --json` payload returns `qr_ascii` — emit that inline in a code block. No `/tmp/totalreclaw-pair-qr.png`, no PNG decode, no `qr_png_b64` (legacy field, no longer exists).
7. **The `tr pair --json` command MUST be invoked via `setsid -f`** so the WS-holding subprocess is in its own POSIX session and survives any subsequent gateway reload. Foreground invocation is killed when your shell-tool exec returns → relay returns 502.

## User-visible lines (the entire transcript)

1. `Setting up TotalReclaw — this takes about a minute…`
2. `✓ Installed (totalreclaw <version>, hybrid mode)`
3. *Triple-backticked `qr_ascii` payload, then:* `Open <url> in your browser. Enter PIN <pin>. Generate or paste a 12-word recovery phrase. Reply \`done\` once it's sealed.`
4. `✓ You're set up on the TotalReclaw free tier — 500 memories/month, unlimited reads. https://totalreclaw.xyz/pricing. Ask me to remember anything.`

If `~/.totalreclaw/credentials.json` already exists and is non-empty, emit only `✓ TotalReclaw is already set up.` and stop.

### ⚡ Line 1 fires BEFORE the first shell tool call — not after

Install + setup runs ~1 minute of silent shell work. Without an immediate ack the user assumes the prompt didn't register. Emit line 1 the moment you decide to install — before resolving `TR_CLI`, before `openclaw plugins install`, before any tool call. Then start work silently.

❌ Forbidden order: `openclaw plugins install` → 60 s silent → emit line 1.
✅ Required order: emit line 1 → `openclaw plugins install` → … → emit line 2 when `tr status` returns.

## Steps (copy-paste verbatim)

### 1. Install (emit line 1 FIRST, then run install silently)

```bash
openclaw plugins install @totalreclaw/totalreclaw@rc
openclaw skills install totalreclaw
```

For stable use `@totalreclaw/totalreclaw` (no `@rc`). Plugin BEFORE skill — skill install triggers a config-driven reload that races a concurrent plugin install.

If install reports `already exists` or non-zero on a re-run: add `--force`:
```bash
openclaw plugins install @totalreclaw/totalreclaw@rc --force
openclaw skills install totalreclaw --force
```

If the shell call fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment): emit verbatim `Your environment doesn't expose the OpenClaw CLI to me. Please install \`totalreclaw\` from your service's plugins / skills UI, then reply \`done\`.` Wait for `done` and skip to step 3.

### 2. Verify CLI + emit line 2

```bash
TR_CLI="$(ls -d ~/.openclaw/npm/node_modules/@totalreclaw/totalreclaw/dist/tr-cli.js ~/.openclaw/extensions/totalreclaw/dist/tr-cli.js 2>/dev/null | head -1)"
node "$TR_CLI" status --json
```

Parse `{"version": "...", "hybrid_mode": true, ...}` → emit line 2 with `<version>` substituted.

If `tr status` exits non-zero or `$TR_CLI` is empty:
- 1st failure: sleep 5s, retry. The install may still be writing files.
- 2nd failure: re-run the install commands from step 1 with `--force`, then retry.
- 3rd failure: emit `Plugin install verification failed twice — the install staged on disk but didn't register. Try \`openclaw plugins install @totalreclaw/totalreclaw@rc --force\` from a terminal, then reply \`done\`.` Stop.

### 3. Check existing credentials (silent unless found)

```bash
test -s ~/.totalreclaw/credentials.json
```

Exit 0 = exists → emit `✓ TotalReclaw is already set up.` and stop. Exit 1 = continue.

### 4. Pair (DETACHED — survives gateway reload) + emit line 3

```bash
PAIR_OUT="/tmp/tr-pair-out-$(date +%s).json"
setsid -f node "$TR_CLI" pair --json > "$PAIR_OUT" 2>&1 < /dev/null
sleep 2
cat "$PAIR_OUT"
```

The `setsid -f` is non-negotiable (rule 5). If the cat output is empty after 2s, sleep 2 more and retry once (cold-network handshake can take up to 4s).

Parse the JSON `{"v":1,"sid":"...","url":"...","pin":"...","mode":"...","expires_at_ms":...,"qr_ascii":"..."}`. Emit a single chat message containing:

````
```
<qr_ascii contents verbatim — 43 lines of block chars>
```

Open <url> in your browser. Enter PIN <pin>. Generate or paste a 12-word recovery phrase. Reply `done` once it's sealed.
````

The triple-backtick block renders the Unicode QR in monospace on Telegram / Slack / web / terminal — every transport. Do NOT attach a PNG. Do NOT save the QR anywhere. Do NOT re-render through another library.

### 5. Confirm (silent until line 4)

After the user replies `done`, recheck:

```bash
test -s ~/.totalreclaw/credentials.json
```

Exit 0 → emit line 4. Exit 1 → re-run step 4 once and resend line 3 (PIN expired or phrase mismatch).

## Browser flow context (don't narrate; recover gracefully on user question)

After line 3 the user opens the URL and walks through a 3-step browser wizard. Know it so you can answer questions and recover from issues:

1. **Step 1 of 3 — Enter PIN.** Six PIN cells, 10-min countdown in corner. Continue button enables when all 6 digits match.
2. **Step 2 of 3 — Recovery phrase.** Default tab **Set up** generates a fresh 12-word BIP-39 phrase in-browser; **Log in** tab accepts a pasted existing phrase. User must check "I've written this down…" and click **Set up TotalReclaw**. Phrase encrypted in browser → relay only sees ciphertext → forwarded to plugin.
3. **Step 3 of 3 — You're all set.** Confirmation. Browser already POSTed; relay completed forward; plugin wrote `~/.totalreclaw/credentials.json`.

Common user issues — emit ONE tight line, don't pre-narrate:
- "Page won't load" → check URL host (`api-staging.totalreclaw.xyz` for RC, `api.totalreclaw.xyz` for stable). Wrong host → generate fresh URL.
- "PIN expired" / countdown ran out → re-run step 4, resend line 3.
- "Invalid phrase" on Log in → typo or wrong-source phrase. User re-checks paste OR switches to **Set up** for a fresh one (loses existing memories).
- "Clicked Set up but nothing happened" / 502 → wait 30s for any deferred gateway reload to complete; if persists, re-run step 4.
- "Where do I write down the phrase?" → paper, password manager, encrypted note. NOT this chat. NOT cloud-synced screenshot. Phrase IS the account.

## "How does this work?" — canonical answer for user questions

When the user asks "how does this work?" / "where are my memories stored?" / "is this safe?" — answer with this content. Do NOT say "stored on a relay server", "stored on TotalReclaw's server", or any phrasing that implies single-company custody.

> **End-to-end encrypted, decentralized memory.** When you tell me to remember something, I encrypt it locally with a key derived from your 12-word recovery phrase, then submit it on-chain via Account Abstraction (a UserOperation). It lives on a public blockchain (Base Sepolia for free tier, Gnosis mainnet for paid). The Graph subgraph indexes it for fast retrieval. The TotalReclaw relay server only forwards encrypted bundles between your client and the chain — it never sees plaintext, can't read your memories, and could be replaced by any compatible relay without losing data. Your recovery phrase is the only thing that can decrypt the memories.

Shorter slices:
- **"Where are memories stored?"** → "Encrypted, on-chain (Base or Gnosis depending on tier), indexed by The Graph. Not on any single company's server."
- **"Is the relay reading my data?"** → "No. The relay only forwards encrypted bundles. Plaintext never leaves your device. Only your recovery phrase can decrypt them."
- **"What if TotalReclaw shuts down?"** → "Your memories survive. They're on a public blockchain. Any compatible relay (or self-hosted relay) can read them with your recovery phrase."

## CLI reference (post-setup)

```bash
node "$TR_CLI" status --json
node "$TR_CLI" remember --json "<fact>"
node "$TR_CLI" recall --json "<query>" --limit 5
```

Foreground for these is fine — they're single-shot HTTP calls, not WS-holding. Only `tr pair --json` needs `setsid -f`.
