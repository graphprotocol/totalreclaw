# TotalReclaw — OpenClaw quickstart (agent-executable)

> Audience: the LLM agent fetching this URL during install. Human-readable rationale lives in `openclaw-setup.md`.
> Stable contract: ~3 KB, copy-paste-runnable, all rules in scope.

## Hard rules (read once, never break)

1. Emit ONLY the four numbered user-visible lines below. No narration, no "let me check…", no version-lag commentary, no path-resolution prose.
2. **Do NOT restart the gateway** (`/totalreclaw-restart`, `kill -USR1`, `docker restart`, `openclaw gateway restart`). The plugin self-patches and signals its own restart needs.
3. **Do NOT write to `~/.openclaw/openclaw.json`**. The plugin's `patchOpenClawConfig()` already wrote `plugins.slots.memory`, `plugins.entries.totalreclaw.hooks.allowConversationAccess`, `channels.telegram.streaming.mode`. Any further write triggers a deferred SIGUSR1 reload that kills pair.
4. **Do NOT save QR PNGs**. The `tr pair --json` payload returns `qr_ascii` — emit that inline in a code block. No `/tmp/totalreclaw-pair-qr.png`, no PNG decode, no `qr_png_b64` (legacy field, no longer exists).
5. **`tr pair --json` MUST be invoked via `setsid -f`** so the WS-holding subprocess is in its own POSIX session and survives any subsequent gateway reload. Foreground `tr pair --json` is killed when your shell-tool exec returns → relay returns 502.

## User-visible lines (the entire transcript)

1. `Installing TotalReclaw…`
2. `✓ Installed (totalreclaw <version>, hybrid mode)`
3. *Triple-backticked `qr_ascii` payload, then:* `Open <url> in your browser. Enter PIN <pin>. Generate or paste a 12-word recovery phrase. Reply \`done\` once it's sealed.`
4. `✓ You're set up on the TotalReclaw free tier — 500 memories/month, unlimited reads. https://totalreclaw.xyz/pricing. Ask me to remember anything.`

If `~/.totalreclaw/credentials.json` already exists and is non-empty, emit only `✓ TotalReclaw is already set up.` and stop.

## Steps (copy-paste verbatim)

### 1. Install (silent — emit line 1 first)

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

## CLI reference (post-setup)

```bash
node "$TR_CLI" status --json
node "$TR_CLI" remember --json "<fact>"
node "$TR_CLI" recall --json "<query>" --limit 5
```

Foreground for these is fine — they're single-shot HTTP calls, not WS-holding. Only `tr pair --json` needs `setsid -f`.
