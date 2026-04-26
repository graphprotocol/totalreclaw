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

## Setup flow

If `totalreclaw_*` tools are already bound to your toolset, SKIP install and jump to step 4. Otherwise run all steps in order.

1. **Install plugin + Python package.** Hermes 2026.4.16+ uses Git-based plugin discovery and a separate Python tool implementation. The Python package MUST go into the SAME Python environment that Hermes runs from (containerized venv, native venv, or system Python — whichever your install uses). Bare `pip install` may target the wrong Python on containerized deploys. Step-by-step via your shell tool:
   ```
   # 1a. Detect the Hermes Python (run inside the gateway's shell context — for
   # Docker, that means `docker exec -it <container> bash` first).
   HERMES_PYTHON="$(command -v python3)"
   echo "Hermes Python: $HERMES_PYTHON"
   "$HERMES_PYTHON" -c "import sys; print(sys.prefix)"

   # 1b. Install both halves into the right env.
   hermes plugins install p-diogo/totalreclaw-hermes --enable
   "$HERMES_PYTHON" -m pip install --pre totalreclaw

   # 1c. Verify the package resolved through Hermes' Python.
   "$HERMES_PYTHON" -m pip show totalreclaw | head -2
   ```
   If `pip show` doesn't list the package, the install landed in a different Python — fix `$HERMES_PYTHON` and re-run 1b. On Debian/Ubuntu/Docker you may hit `externally-managed-environment` if Hermes runs against system Python — add `--break-system-packages` to the `pip install` line, or ask the user to recreate Hermes inside a venv. If either shell call fails with `command not found` / ENOENT / "not authorized" (managed-service or shell-restricted environment): tell the user verbatim *"Your environment doesn't expose the Hermes CLI / pip to me. Please install `p-diogo/totalreclaw-hermes` and the `totalreclaw` Python package via your service's plugins UI, then reply `done`."* Wait for `done` and skip to step 3.

2. **Restart the gateway — Hermes does NOT auto-reload on plugin install.** Tell the user verbatim: *"Please restart your Hermes gateway: `hermes gateway restart` (native), `docker restart <your-container-name>` (Docker self-host — substitute the actual name; `docker ps` shows it), or your managed service's restart control. If supervised by systemd / launchd, `kill -USR1 $(cat ~/.hermes/gateway.pid)` triggers a graceful-drain restart. Reply `done` once it's back up."* Do NOT run the restart yourself — your shell is inside the gateway you'd be restarting. Wait for the user's `done`.

3. **Verify `totalreclaw_pair` is bound.** If still missing after restart, tell the user the plugin didn't load and to retry. Do not loop the install.

4. **Check for existing credentials.** Run `test -s ~/.totalreclaw/credentials.json`. If it exists and is non-empty, confirm *"TotalReclaw is already set up."* and stop.

5. **Pair.** Call `totalreclaw_pair`. Returns `{url, pin, expires_at_ms}`. Relay verbatim:
   > *Open <url> in your browser. Generate a new 12-word recovery phrase there or paste an existing one — the phrase stays in your browser, the relay only sees ciphertext. Confirm PIN <pin>. Reply `done` once the page says it's sealed.*

6. **Verify and confirm.** After user says `done`, re-run `test -s ~/.totalreclaw/credentials.json`. If missing, the PIN expired — call `totalreclaw_pair` again and resend. If present, confirm *"TotalReclaw is set up. Ask me to remember or recall anything."*

## Phrase safety (HARD — never break)

NEVER echo / generate / ask the user to paste a recovery phrase in chat. NEVER invoke `totalreclaw setup`, `hermes setup`, or any phrase-touching CLI via your shell tool (stdout enters LLM context). The ONLY agent-facilitated setup path is `totalreclaw_pair`. If the user pastes a phrase anyway: tell them it is compromised, call `totalreclaw_pair` with `mode=generate` for a fresh wallet.

## Usage (post-setup)

- Stable user facts / preferences / identity / "remember X" → `totalreclaw_remember` (don't ask permission).
- First-person factual query ("do I / what's my / where do I …") → `totalreclaw_recall` FIRST, then answer from returned facts. If 0 results, say so.
- "forget / pin / unpin X" → `totalreclaw_recall` first for `fact_id`, then `totalreclaw_forget` / `_pin` / `_unpin`.
- "status" → `totalreclaw_status`. "upgrade" → `totalreclaw_upgrade` (paste Stripe URL verbatim).
- "import from Mem0 / ChatGPT / Claude / Gemini / mcp-memory-server" → `totalreclaw_import_from` with `dry_run=True` first.

## Diagnostics

- `totalreclaw_*` tools not visible → gateway not restarted after install; ask the user once to restart their gateway. Common patterns: `hermes gateway restart` (native), `docker restart <your-container-name>` (Docker self-host — replace with the user's actual container name), or the managed service's restart control. Wait for `done`.
- User says done but `credentials.json` missing → PIN expired or entered wrong phrase; call `totalreclaw_pair` again.
- `onboarding required` → credentials missing; redo from the pair step.
- `quota exceeded` → `totalreclaw_status`, then offer `totalreclaw_upgrade`.

## Tool surface

`totalreclaw_pair` (ONLY setup path) · `_remember` · `_recall` · `_forget` · `_pin` · `_unpin` · `_export` · `_status` · `_upgrade` · `_import_from` · `_import_batch` · `_debrief` · `_report_qa_bug` (RC only).
