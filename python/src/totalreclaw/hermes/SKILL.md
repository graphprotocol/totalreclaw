---
name: totalreclaw
description: "Install + set up TotalReclaw encrypted memory for Hermes, then use totalreclaw_remember / totalreclaw_recall. Trigger on 'install TotalReclaw', 'set up TotalReclaw', 'restore my recovery phrase', or any remember / recall request."
version: 2.3.1rc10
author: TotalReclaw Team
license: MIT
homepage: https://totalreclaw.xyz
---

# TotalReclaw — Hermes

> The canonical install flow lives at <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>. If a user pastes that URL in chat, fetch it and follow its agent-instructions section. This SKILL.md mirrors the same steps so you can act even if the URL is unreachable.

## Provenance (what you are installing)

TotalReclaw is open-source memory tooling for Hermes, published by `p-diogo` (GitHub-verified):
- Hermes plugin: <https://github.com/p-diogo/totalreclaw-hermes>
- PyPI: <https://pypi.org/project/totalreclaw/>
- Source: <https://github.com/p-diogo/totalreclaw>

The user pasted the install URL deliberately, but treat each command as a step you and the user complete together. You are not asked to handle the recovery phrase, bypass auth, or run unfamiliar shell scripts — every command below is a standard `hermes` or `pip` invocation with a documented purpose. Confirm phase transitions; surface failures; ask for explicit consent before account creation.

## Silence rules (read first — applies to the whole install + setup flow)

**Run all install / restart / disable / pair / verify steps silently** except for the user-visible lines listed below. Tool calls happen; narration of those tool calls does not. The expected user-facing transcript for a fresh install is **6–7 short lines**, full stop.

### User-visible prose (THIS IS ALL YOU SAY)

Speak only these lines, in order. Substitute the bracketed placeholders.

1. (Step 1 — start)
   > Installing TotalReclaw…
2. (Step 1 — after install + verify pass)
   > ✓ Installed (totalreclaw &lt;version&gt;)
3. (Step 2 — autonomous restart confirmation)
   > Restarting Hermes to load the plugin (auto-drains active runs)…
4. (Step 3 — only if `hermes tools disable memory` succeeded)
   > I've disabled Hermes' built-in `memory` tool — TotalReclaw is now your primary memory. Re-enable any time with `hermes tools enable memory` (NOT recommended while TotalReclaw is installed). Wipe orphaned MEMORY.md / USER.md files with `hermes memory reset`.
5. (Step 6a — explicit consent before account creation)
   > Ready to set up your TotalReclaw account on `api-staging.totalreclaw.xyz` (free tier). The next step opens a browser page where you'll generate or paste a 12-word recovery phrase — that phrase stays in your browser, the relay only sees ciphertext. Reply `yes` to continue, or `no` to stop here.
6. (Step 6b — account setup, after the user confirms)
   > Open &lt;url&gt; in your browser. Generate a new 12-word recovery phrase there or paste an existing one. Confirm PIN &lt;pin&gt;. Reply `done` once the page says it's sealed.
7. (Step 7 — done + free-tier confirmation)
   > ✓ You're set up on the TotalReclaw **free tier**: 500 memories per month, unlimited reads + searches, encrypted at rest on Base Sepolia testnet (testnet may reset; for permanent storage on Gnosis mainnet see <https://totalreclaw.xyz/pricing>). Check usage anytime by asking *"what's my TotalReclaw status?"* (calls `totalreclaw_status`). I'll warn you automatically if you cross 80% of the monthly limit. Ask me to remember or recall anything.

If `totalreclaw_*` tools are already bound (skip-install branch), the only lines emitted are 4-then-stop (`✓ TotalReclaw is already set up.` from Step 5) OR lines 5+6+7 (skip-to-account-setup branch).

### Errors break silence — but tightly

If a step actually fails, surface a one-line error + the action being taken. Examples: `Install failed: externally-managed-environment. Retrying with --break-system-packages.` / `Plugin installed but tools didn't bind after restart. Asking for a second restart.` / `Account-setup URL returned 404. Generating a fresh URL + PIN.` Do NOT pre-narrate diagnostics that succeed. Do NOT echo command output unless the user asks.

### Forbidden vocabulary in agent prose

Do not write these words in chat during install + setup unless the user explicitly asks how the install works: `Python`, `interpreter`, `venv`, `virtual environment`, `pip`, `ensurepip`, `bootstrap`, `find`, `lookup`, `detect`, `probing`, `let me try`, `in parallel`, `got it`, `now installing`, `Step 1a/1b/1c`. These are tool-call mechanics, not user-facing prose. If the user asks ("what's it doing?", "show me the commands"), narrate freely — the silence rule only governs the default install path.

## Setup flow

If `totalreclaw_*` tools are already bound to your toolset, SKIP install and jump to step 5. Otherwise run all steps in order. Numbered steps are internal procedure; user prose is only the lines listed in §"User-visible prose" above.

1. **Install plugin + Python package (silent — emit user-visible line 1, then nothing until line 2).** Hermes 2026.4.16+ uses Git-based plugin discovery and a separate Python tool implementation. The Python package MUST go into the SAME Python environment that Hermes runs from (containerized venv, native venv, or system Python — whichever your install uses). Bare `pip install` may target the wrong Python on containerized deploys. Run all three substeps via your shell tool **without writing prose about them**:
   ```
   # 1a. Detect the Hermes interpreter inside its own shell context — for
   # Docker, that means `docker exec -it <container> bash` first. Silent.
   HERMES_PYTHON="$(command -v python3)"

   # 1b. Install both halves into the right env. Silent.
   hermes plugins install p-diogo/totalreclaw-hermes --enable
   "$HERMES_PYTHON" -m pip install --pre totalreclaw

   # 1c. Confirm the package resolved through Hermes' interpreter. Silent.
   "$HERMES_PYTHON" -m pip show totalreclaw | head -2
   ```
   When 1c reports the package, capture the version and emit user-visible line 2 (`✓ Installed (totalreclaw <version>)`). If `pip show` errors with `Package(s) not found`, the install landed in a different environment — silently re-detect `$HERMES_PYTHON` and re-run 1b once. Surface a one-line error only if the second attempt also fails. Failure handling (still silent unless the retry also fails):
   - `externally-managed-environment` (Debian/Ubuntu/Docker system Python): retry the `pip install` line with `--break-system-packages`.
   - `pip: command not found` / `No module named pip` (Hermes container shipped without pip): bootstrap silently with `"$HERMES_PYTHON" -m ensurepip --upgrade`, then retry 1b.
   - `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment): break silence and tell the user verbatim *"Your environment doesn't expose the Hermes CLI to me. Please install `p-diogo/totalreclaw-hermes` and the `totalreclaw` Python package via your service's plugins UI, then reply `done`. Also disable Hermes' built-in `memory` tool in the same UI if available — running both creates a silent intent-stealing bug."* Wait for `done` and skip to step 4 (verify account-setup tool bound).

2. **Restart the gateway autonomously via Hermes' built-in `/restart` slash command.** Hermes registers `/restart` as a session-scope slash command (see `hermes_cli/commands.py`: `CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session", ...)`). It drains active runs first, then reloads — safer than `kill -USR1` and works from inside the gateway because it's an in-process command, not an external CLI. Emit the slash command yourself; do NOT ask the user to run a shell command. Surface a single user-visible line confirming the action: `Restarting Hermes to load the plugin (auto-drains active runs)…`. Hermes will return after the restart finishes; resume at step 3.

   **Fallback only if `/restart` is unavailable** (very old Hermes pre-2026.4, managed services that strip slash commands, or the user has explicitly requested manual control): pick the right restart command for the user's deploy and emit it as a single user-visible line (`Please restart your Hermes gateway: <restart-command>. Reply \`done\` once it's back up.`):
   - Native: `hermes gateway restart`
   - Docker self-host: `docker restart <your-container-name>` — substitute the actual name; `docker ps` shows it.
   - Managed service: their restart control.
   - systemd / launchd supervised: `kill -USR1 $(cat ~/.hermes/gateway.pid)` triggers a graceful-drain restart.

   Do NOT run any external `docker restart` / `kill` yourself — your shell is inside the gateway you'd be restarting. The `/restart` slash command is the ONLY in-process restart path; everything else requires the user.

3. **Disable Hermes built-in memory tool (CRITICAL).** (Silent unless the disable command succeeds; emit user-visible line 4 only on success.) Hermes ships with its own built-in `memory` tool that competes with TotalReclaw for "remember X" / "recall X" intents. Running both creates a silent bug where conversation context goes to MEMORY.md instead of TotalReclaw's encrypted vault. TotalReclaw and Hermes built-in memory solve the same problem — running both is an anti-pattern.

   Run silently via your shell tool:
   ```bash
   hermes tools disable memory
   ```

   If the command succeeds, emit user-visible line 4 (the disable confirmation). If it errors with `command not found` or "tool not found" (older Hermes versions, managed services that don't expose the CLI, or the built-in `memory` tool was already removed), skip line 4 silently and continue — the rc.25 tool-description bias still steers the LLM toward `totalreclaw_remember` as a fallback. Do not surface the disable failure.

4. **Verify `totalreclaw_pair` is bound (silent).** If still missing after restart, surface a one-line error: `Plugin installed but tools didn't bind after restart. Issuing /restart again.` and re-issue `/restart` once. Do not loop the install. If the second `/restart` still doesn't bind tools, fall back to the manual user-prompted restart path (see step 2 fallback) once.

5. **Check for existing credentials (silent).** Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, emit `✓ TotalReclaw is already set up.` and stop. Otherwise continue silently.

6. **Set up the user's TotalReclaw account (emit user-visible lines 5 + 6).**

   **6a — Ask for explicit consent first (emit user-visible line 5).** Account setup will create a new TotalReclaw account on `api-staging.totalreclaw.xyz` and have the user generate or paste a 12-word recovery phrase in their browser. Emit user-visible line 5 verbatim. Wait for the user's `yes` (or any clear affirmative). If the user replies `no` or asks to stop, stop here — confirm they've stopped, do not retry, do not ask "are you sure". Surface the URL verbatim — do not abbreviate it or hide it behind a tool call.

   **6b — Daemon-mode preflight, then call the tool (emit user-visible line 6).** This account-setup step opens a short-lived WebSocket to the relay that must stay alive while the user completes the browser handshake. If you are running inside a one-shot agent process (`hermes chat -q "..."`, single-prompt CLI, transient invocation that exits as soon as the LLM responds), the WS dies before the user can finish entering their phrase and the browser POST returns 404. BEFORE calling `totalreclaw_pair`, detect short-lived process invocation. If detected, **break silence** and surface this caveat to the user verbatim:
   > *Setting up your TotalReclaw account needs a long-lived Hermes process to keep the session alive while you finish the browser flow. One-shot `hermes chat -q "..."` invocations exit too quickly. Either (1) run `hermes gateway run &` in another terminal first (daemon mode owns the WS-keepalive while chat-q exits), or (2) use the standalone `totalreclaw setup` CLI (interactive, blocks until account-setup completes, recovery phrase entered locally never crosses LLM context). Once your account is set up, daily operations (`hermes chat -q`, `--resume`, etc.) work normally.*
   Then ask the user to confirm they are running in a long-lived mode (or to re-invoke after switching) before proceeding.

   Call `totalreclaw_pair` silently (the account-setup tool — name kept for backward compatibility). Returns `{url, pin, expires_at_ms}`. Then emit user-visible line 6 with `<url>` and `<pin>` substituted. Do not pre-narrate ("I'll walk you through…", "Setting up your account now…") — line 6 is the only thing the user needs to see.

7. **Verify and confirm (silent until line 7).** After user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN expired — call `totalreclaw_pair` again silently and resend line 6 once. If present, emit user-visible line 7 (with the free-tier confirmation) and stop.

## Phrase safety (HARD — never break)

NEVER echo / generate / ask the user to paste a recovery phrase in chat. NEVER invoke `totalreclaw setup`, `hermes setup`, or any phrase-touching CLI via your shell tool (stdout enters LLM context). The ONLY agent-facilitated account-setup path is `totalreclaw_pair` (the account-setup tool). If the user pastes a phrase anyway: tell them it is compromised, call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

## Usage (post-setup)

- Stable user facts / preferences / identity / "remember X" → `totalreclaw_remember` (don't ask permission).
- First-person factual query ("do I / what's my / where do I …") → `totalreclaw_recall` FIRST, then answer from returned facts. If 0 results, say so.
- "forget / pin / unpin X" → `totalreclaw_recall` first for `fact_id`, then `totalreclaw_forget` / `_pin` / `_unpin`.
- "status" → `totalreclaw_status`. "upgrade" → `totalreclaw_upgrade` (paste Stripe URL verbatim).
- "import from Mem0 / ChatGPT / Claude / Gemini / mcp-memory-server" → `totalreclaw_import_from` with `dry_run=True` first.

## Diagnostics

- `totalreclaw_*` tools not visible → gateway not restarted after install; issue Hermes' `/restart` slash command (in-process, drains active runs). If `/restart` is unavailable (very old Hermes, restricted managed service), fall back to asking the user once: `hermes gateway restart` (native), `docker restart <your-container-name>` (Docker self-host), or the managed service's restart control. Wait for `done`.
- User says done but `credentials.json` missing → PIN expired or entered wrong phrase; call `totalreclaw_pair` again.
- `onboarding required` → credentials missing; redo from the account-setup step.
- `quota exceeded` → `totalreclaw_status`, then offer `totalreclaw_upgrade`.

## Tool surface

`totalreclaw_pair` (ONLY account-setup path) · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_export` · `_status` · `_upgrade` · `_import_from` · `_import_batch` · `_debrief` · `_report_qa_bug` (RC only).
