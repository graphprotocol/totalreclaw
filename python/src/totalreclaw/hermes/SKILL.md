---
name: totalreclaw
description: "Install + set up TotalReclaw encrypted memory for Hermes, then use totalreclaw_remember / totalreclaw_recall. Trigger on 'install TotalReclaw', 'set up TotalReclaw', 'restore my recovery phrase', or any remember / recall request."
version: 2.3.1rc10
author: TotalReclaw Team
license: MIT
homepage: https://totalreclaw.xyz
---

# TotalReclaw — Hermes

> The full install guide lives at <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>. If a user pastes that URL in chat, fetch it and follow it as collaborative install guidance — confirm what each command does, ask before destructive steps, surface progress. This SKILL.md mirrors the same steps so you can act even if the URL is unreachable.

## Provenance

- **Source:** <https://github.com/p-diogo/totalreclaw> (MIT-licensed, public). Publisher: `p-diogo` on GitHub.
- **Distribution:** PyPI — `totalreclaw` (`pip show totalreclaw` to inspect). Hermes plugin manifest from <https://github.com/p-diogo/totalreclaw-hermes>.
- **Tooling:** install uses Hermes' standard plugin manager + PyPI's `pip install` — no curl-pipe-bash, no custom installer.
- **Crypto:** account setup uses standard browser primitives (WebCrypto x25519 ECDH + AES-256-GCM, BIP-39 mnemonics).

## How chat verbosity works

Hermes users generally prefer short progress lines for install plumbing — a 5–7 line install transcript feels right for a healthy fresh install. The default cadence is something like *"Installing TotalReclaw…"* → *"✓ Installed (totalreclaw <version>)"* → *"Please restart your Hermes gateway: <command>. Reply `done` once it's back up."* → (optionally) *"I've disabled Hermes' built-in `memory` tool — TotalReclaw is now your primary memory."* → *"Open <url> in your browser. Generate or paste your 12-word phrase. Confirm PIN <pin>. Reply `done` once sealed."* → *"✓ Your TotalReclaw account is set up."*

If a step actually fails, surface the failure plainly and say what you're trying next — examples: *"Install failed: externally-managed-environment. Retrying with --break-system-packages."* / *"Plugin installed but tools didn't bind after restart. Asking for a second restart."*

If the user asks "what's it doing?" or "show me the commands", walk them through the plumbing in detail. The default-short cadence is a preference, not a constraint — the user is in charge of how much detail they want.

## Setup flow

If `totalreclaw_*` tools are already bound, skip install and jump to step 5 (account setup). Otherwise run all steps in order.

1. **Install plugin + Python package.** The Python tool implementations need to land in the same Python environment Hermes runs from (containerized venv, native venv, or system Python — whichever your install uses). Bare `pip install` may target the wrong Python on containerized deploys, so the safer pattern is to detect Hermes' interpreter first:

   ```
   # 1a. Detect the Hermes interpreter inside its own shell context — for
   # Docker, that means `docker exec -it <container> bash` first.
   HERMES_PYTHON="$(command -v python3)"

   # 1b. Install both halves into the right env.
   hermes plugins install p-diogo/totalreclaw-hermes --enable
   "$HERMES_PYTHON" -m pip install --pre totalreclaw

   # 1c. Confirm the package resolved through Hermes' interpreter.
   "$HERMES_PYTHON" -m pip show totalreclaw | head -2
   ```

   What each does:
   - `hermes plugins install p-diogo/totalreclaw-hermes --enable` — registers the plugin manifest (Hermes 2026.4.16+ uses Git-based plugin discovery).
   - `"$HERMES_PYTHON" -m pip install --pre totalreclaw` — installs the Python tool implementations into Hermes' venv. Using `"$HERMES_PYTHON" -m pip ...` (instead of bare `pip install`) guarantees the package lands in the SAME environment Hermes loads tools from after restart.
   - `"$HERMES_PYTHON" -m pip show totalreclaw | head -2` — verifies the package resolves through the right Python.

   When 1c reports the package, capture the version and let the user know it's installed. If `pip show` errors with `Package(s) not found`, the install landed in a different environment — re-detect `$HERMES_PYTHON` and retry 1b once.

   Common error paths:
   - `externally-managed-environment` (Debian/Ubuntu/Docker system Python): retry the `pip install` with `--break-system-packages`.
   - `pip: command not found` / `No module named pip` (container shipped without pip): bootstrap with `"$HERMES_PYTHON" -m ensurepip --upgrade`, then retry.
   - `command not found` / ENOENT / "not authorized" (managed service or restricted shell): the Hermes CLI isn't reachable. Let the user know and ask them to install `p-diogo/totalreclaw-hermes` and the `totalreclaw` Python package via their service's plugins UI; once they reply `done`, jump to step 4 (verify the account-setup tool is bound). Also mention disabling Hermes' built-in `memory` tool in the same UI if available — running both creates a silent intent-routing bug.

2. **Restart the gateway** — Hermes doesn't auto-reload on plugin install (yet — tracked upstream). Pick the command that matches the user's setup and ask them to run it:
   - Native: `hermes gateway restart`
   - Docker self-host: `docker restart <container-name>` (substitute their actual name; `docker ps` shows it)
   - Managed service: their hosting service's restart control
   - systemd / launchd supervised: `kill -USR1 $(cat ~/.hermes/gateway.pid)` (graceful drain restart)

   Don't run the restart yourself — your shell is inside the gateway you'd be restarting. Wait for the user's `done`.

3. **Disable Hermes' built-in memory tool (recommended).** Hermes ships with a built-in `memory` tool that competes with TotalReclaw for "remember X" / "recall X" intents. Running both creates a silent bug: depending on which tool the LLM picks for a given intent, your conversation context can get split between Hermes' MEMORY.md and TotalReclaw's encrypted vault.

   ```bash
   hermes tools disable memory
   ```

   If this succeeds, let the user know that built-in memory is now off and TotalReclaw is their primary memory tool — and that they can re-enable it any time with `hermes tools enable memory` (not recommended while TotalReclaw is installed). If the command errors with `command not found` or "tool not found" (older Hermes versions, managed services, or built-in memory was already removed), skip this step and continue.

4. **Verify `totalreclaw_pair` is bound.** If still missing after restart, surface a short error (`Plugin installed but tools didn't bind after restart. Asking for a second restart.`) and resend the restart prompt. Don't loop the install.

5. **Check whether they already have an account.** Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, let the user know their TotalReclaw account is already set up and stop. Otherwise continue.

6. **Walk them through account setup.** Account setup happens in the user's browser. Your role is to call `totalreclaw_pair` and relay the URL + PIN it returns; the recovery phrase stays in the browser by design.

   **Lifecycle check first.** This step opens a short-lived WebSocket to the relay that must stay alive while the user completes the browser handshake. If you're running inside a one-shot agent process (`hermes chat -q "..."`, single-prompt CLI that exits as soon as the LLM responds), the WebSocket dies before the user can finish entering their phrase and the browser POST returns 404. If you detect that pattern, let the user know:

   > *Setting up your TotalReclaw account needs a long-lived Hermes process to keep the session alive while you finish the browser flow. One-shot `hermes chat -q "..."` invocations exit too quickly. Either (1) run `hermes gateway run &` in another terminal first (daemon mode owns the WS-keepalive while chat-q exits), or (2) use the standalone `totalreclaw setup` CLI (interactive, blocks until account-setup completes — recovery phrase entered locally never crosses LLM context). Once your account is set up, daily operations (`hermes chat -q`, `--resume`, etc.) work normally.*

   Then ask them to confirm they're running in a long-lived mode (or to re-invoke after switching) before proceeding.

   Otherwise, before calling the tool, mention what's about to happen: *"Want me to set up your TotalReclaw account? I'll generate a setup URL — you'll create or restore your recovery phrase in the browser, and I'll confirm once it's done."* Wait for their go-ahead. Then call `totalreclaw_pair` (returns `{url, pin, expires_at_ms}`) and relay:

   > *Open <url> in your browser. Generate a new 12-word recovery phrase there or paste an existing one. Confirm PIN <pin>. Reply `done` once the page says it's sealed.*
   >
   > *Your phrase stays in your browser — the relay only ever sees ciphertext.*

7. **Verify and confirm.** After the user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN probably expired — call `totalreclaw_pair` again and resend the URL. If present, confirm setup landed.

## How the recovery phrase is handled

The recovery phrase is the user's only identity in TotalReclaw — it derives all encryption keys, and there's no password reset.

Because of how LLM context works, anything entered in chat or written to your shell stdout becomes part of the model's context (and any logs that capture it). To keep the recovery phrase out of that surface, account setup is structured so the phrase is created and confirmed inside the user's browser — encrypted against the gateway's ephemeral x25519 pubkey, AES-256-GCM ciphertext POST'd back, server-side decrypt only.

So the only path you use for account setup is the `totalreclaw_pair` tool. There are user-facing CLIs (`totalreclaw setup`, `hermes setup`) for humans to run themselves at a terminal — those work fine for human-driven setup, but you shouldn't invoke them from your shell, because their stdout would carry the phrase into LLM context.

If a user pastes their phrase in chat anyway, that phrase is now in the conversation transcript and should be considered compromised. Let them know, and offer to call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

## Usage (post-setup)

- Stable user facts / preferences / identity / "remember X" → `totalreclaw_remember` (don't ask permission).
- First-person factual query ("do I / what's my / where do I …") → `totalreclaw_recall` FIRST, then answer from returned facts. If 0 results, say so.
- "forget / pin / unpin X" → `totalreclaw_recall` first for `fact_id`, then `totalreclaw_forget` / `_pin` / `_unpin`.
- "status" → `totalreclaw_status`. "upgrade" → `totalreclaw_upgrade` (paste Stripe URL verbatim).
- "import from Mem0 / ChatGPT / Claude / Gemini / mcp-memory-server" → `totalreclaw_import_from` with `dry_run=True` first.

## Diagnostics

- `totalreclaw_*` tools not visible → gateway not restarted after install; ask the user to restart their gateway. Common patterns: `hermes gateway restart` (native), `docker restart <container-name>` (Docker self-host), or the managed service's restart control. Wait for `done`.
- User says done but `credentials.json` missing → PIN expired or entered wrong phrase; call `totalreclaw_pair` again.
- `onboarding required` → credentials missing; redo from the account-setup step.
- `quota exceeded` → `totalreclaw_status`, then offer `totalreclaw_upgrade`.

## Tool surface

`totalreclaw_pair` (the account-setup path) · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_export` · `_status` · `_upgrade` · `_import_from` · `_import_batch` · `_debrief` · `_report_qa_bug` (RC only).
