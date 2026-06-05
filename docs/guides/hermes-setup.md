# TotalReclaw on Hermes — agent setup reference

> **Audience: the AI agent installing TotalReclaw inside a Hermes runtime.**
> Not for human readers. (A separate user guide will be linked here when it exists.)

This document describes how the agent installs and sets up TotalReclaw on Hermes. The agent uses the tool surface listed here. The user only types one of the install prompts below in their chat client; everything else happens through tools.

---

## Versions

| Channel | Version | Install command |
|---|---|---|
| stable (default) | `2.4.3` | `pip install totalreclaw` |
| latest RC | `2.4.4rc6` | `pip install --pre --upgrade totalreclaw==2.4.4rc6` |

Source of truth for what's currently published: `pip index versions totalreclaw`. The table above is updated on every PyPI release.

---

## Install prompts

The user pastes one of these three prompts. The shape is fixed; the agent extracts intent from the shape, not from arbitrary phrasing.

### Stable (default)

```
Install TotalReclaw. See https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md
```

The agent installs the stable channel from the table above and proceeds to setup.

### RC (user specifies a version)

```
Install TotalReclaw RC <version>. See https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md
```

`<version>` is a PEP 440 RC string the user picks (e.g. `2.3.7rc1`). When the user omits `<version>` and writes only `Install TotalReclaw RC`, the agent installs `latest RC` from the table.

### RC against the staging relay (QA only)

```
Install TotalReclaw RC <version> against the staging relay (TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz). See https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md
```

The agent exports `TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz` and `TOTALRECLAW_RELAY_URL=https://api-staging.totalreclaw.xyz` in the install / setup shell before any TotalReclaw command. Without these env vars, both stable AND RC installs default to the production relay `api.totalreclaw.xyz`.

---

## Conversational style

This is not a rule the agent follows perfectly, but the more it can, the better the UX:

- Do not surface tool-call mechanics verbatim to the user. The user does not need to see `🐍 execute_code: "from hermes_tools import terminal..."` or `📚 skill_view: "..."` or `📋 todo: "planning N tasks"`. Run the tools silently and emit only the user-visible lines listed in the procedures below.
- Use the explicit verbatim emit lines where the procedure provides them. Otherwise keep replies short and to-the-point.
- Do not narrate "Now I'll do X, then Y, then Z." Do the work.
- **Budget: a fresh install + setup should be ~5–6 short user-visible lines total.** Anything beyond that is almost always narration that should have stayed silent.
- **Forbidden vocabulary during install / setup** (unless the user explicitly asks how it works): `Python`, `interpreter`, `venv`, `pip`, `ensurepip`, `bootstrap`, `detect`, `probing` — plus narration openers: "Let me check…", "Let me verify…", "Now installing…", "Plugin loaded, let me verify…", "Quick fix…", "One sec…", "Got it, doing X…", and any "I'm going to do X" / "About to do X" pre-narration of a tool call. These are tool-call mechanics, not user-facing prose. If the user asks ("what's it doing?", "show me the commands"), narrate freely — this only governs the default silent path.

---

## Install procedure

After the procedure completes (the user replies *"Done — continue setting up TotalReclaw"* or equivalent to the restart request in step 4), **proceed directly to Account setup**. Do not ask the user "is it ready?" — the user's reply IS the signal.

1. Resolve the Hermes Python interpreter:
   ```bash
   HERMES_PYTHON="$(command -v hermes >/dev/null 2>&1 && head -1 "$(command -v hermes)" | sed 's|^#!||')"
   test -x "$HERMES_PYTHON" || HERMES_PYTHON="$(find / -maxdepth 6 -name hermes -type f -executable 2>/dev/null | head -1 | xargs -I{} head -1 {} | sed 's|^#!||')"
   ```
2. Install the package using `"$HERMES_PYTHON" -m pip install <pin>`, where `<pin>` is the row from the Versions table chosen by the user's prompt.
3. Register the plugin manifest:
   ```bash
   hermes plugins install p-diogo/totalreclaw-hermes --enable
   ```
   (Or `"$(dirname "$HERMES_PYTHON")/hermes" plugins install p-diogo/totalreclaw-hermes --enable` when `hermes` isn't on `PATH`.)
4. **Ask the user to restart the gateway** so the new plugin tools bind. The agent does NOT — and *cannot* — issue the restart itself. Two facts from the Hermes internals make this non-negotiable:
   - **Only the user can trigger a restart.** Hermes parses slash commands from **user input only**; an agent that emits `/restart` (or `/new`) in its reply just sends plain text that does nothing — on *every* surface (CLI/TUI and messaging platforms alike). (`gateway/platforms/base.py` parses commands off inbound user events; agent output is returned verbatim.)
   - **A full gateway restart is required.** There is no hot-reload — `hermes plugins install` only writes files and prints *"Restart the gateway for the plugin to take effect."* Plugins are discovered **once at gateway boot**, and starting a new session (`/new`) does NOT re-scan them. So `/new` is not a shortcut; only a full restart binds the freshly-installed plugin.

   A user-typed `/restart` triggers a graceful SIGUSR1 exit that respawns ONLY under a process supervisor (systemd / launchd / s6). In bare docker or an ephemeral `hermes chat` there is no supervisor, so the user restarts the process / container out-of-band. Pick the line below by surface:

   Probe the surface before emitting:

   ```bash
   IN_DOCKER=$(grep -q -E 'docker|containerd' /proc/1/cgroup 2>/dev/null && echo yes || echo no)
   HAS_MSG_BOT="$(printenv | grep -E '^HERMES_(TELEGRAM|DISCORD|SLACK|MATRIX|FEISHU|WHATSAPP)_(BOT_TOKEN|HOMESERVER|APP_ID|PHONE_NUMBER_ID)=' | head -1)"
   HAS_SYSTEMCTL=$(command -v systemctl >/dev/null 2>&1 && echo yes || echo no)
   HAS_LAUNCHCTL=$(command -v launchctl >/dev/null 2>&1 && echo yes || echo no)
   ```

   Emit ONE user-visible line per the matrix below. **In every case the user is asked to reply with an unambiguous continuation phrase** — bare `done` is insufficient because, after a restart, chat history is gone and the agent needs both a system-injected nudge (rc.27+ ships this automatically when credentials are absent) AND an explicit user message to reliably resume the setup flow.

   | Surface | Detected by | Emit (verbatim) |
   |---|---|---|
   | Messaging platform — user is chatting via Telegram / Discord / Slack / Matrix / Feishu / WhatsApp | `HAS_MSG_BOT != ""` | `> Send /restart in chat now. Once you see the gateway-restart confirmation, reply: "Done — continue setting up TotalReclaw".` |
   | `hermes chat` inside docker | `IN_DOCKER=yes` AND `HAS_MSG_BOT == ""` | `> Don't use /restart here — this container has no supervisor to bring the gateway back. Open a host shell (outside the container) and run: docker restart <container-name>. Wait for the container to come back up, reopen hermes chat, and reply: "Done — continue setting up TotalReclaw".` |
   | `hermes chat` native install with systemd | `IN_DOCKER=no` AND `HAS_SYSTEMCTL=yes` AND `HAS_MSG_BOT == ""` | `> The /restart slash command won't work from this CLI. Open a second terminal and run: hermes gateway restart. When it returns, reply here: "Done — continue setting up TotalReclaw".` |
   | `hermes chat` native install on macOS / launchd | `IN_DOCKER=no` AND `HAS_LAUNCHCTL=yes` AND `HAS_MSG_BOT == ""` | `> The /restart slash command won't work from this CLI. Open a second terminal and run: hermes gateway restart. When it returns, reply here: "Done — continue setting up TotalReclaw".` |
   | Detection fails / ambiguous | none of the above match | Ask the user how they're chatting with Hermes (Telegram / Discord / Slack / Matrix / Feishu / WhatsApp / `hermes chat` in docker / `hermes chat` native), then emit the matching line above. |

   Substitute the actual container name in the docker line — usually visible via `docker ps --format '{{.Names}}'`; if the agent runs that probe and finds exactly one container, splice the name in. Fall back to `<container-name>` placeholder otherwise.

   Wait for the user's reply containing "continue" / "set up" / similar resumption language. Do NOT proceed to Account setup until the user confirms. Do NOT issue any restart yourself.

5. **Install the MemoryProvider sidecar** (2.4.0+ / issue #275 — Path B).

   TotalReclaw also ships as a Hermes `MemoryProvider`, gaining `on_pre_compress` and `on_memory_write` hooks that capture writes the Background Review subsystem cannot reach via the generic plugin path (the BG-review whitelist is hardcoded to `["memory", "skills"]` toolsets, see [#7193](https://github.com/NousResearch/hermes-agent/issues/7193)). This unlocks captured writes during long-session compression + background extraction.

   Hermes activates exactly **one** memory provider at a time. Probe what's active before deciding:

   ```bash
   ACTIVE_PROVIDER=$(hermes memory-status --hermes-home "${HERMES_HOME:-$HOME/.hermes}" 2>/dev/null | jq -r '.provider // "none"')
   # ``hermes memory-status`` ships with the totalreclaw CLI (subcommand
   # registered alongside ``hermes setup``). It grep-reads ``config.yaml``
   # — does not depend on an upstream ``hermes memory status --json``
   # command (which is not yet released).
   ```

   Branch on the result:

   | `ACTIVE_PROVIDER` | Agent behaviour |
   |---|---|
   | `"none"` / `""` / `"totalreclaw"` | Silently run `hermes activate-memory-provider` (drops sidecar + sets `memory.provider=totalreclaw`). No prompt. |
   | Anything else (`honcho`, `byterover`, `openviking`, ...) | Emit ONE line: *"Hermes is currently using `<X>` as the memory provider. Switch to TotalReclaw (E2E-encrypted, on-chain, cross-device) or install TotalReclaw as tools-only? Reply `switch` or `tools-only`."*<br/>**Branch:**<br/>• `switch` → warn first (*"Switching deactivates `<X>` — your existing `<X>` memories stay on disk but won't be queried by the agent until you switch back."*), then run `hermes activate-memory-provider`.<br/>• `tools-only` → run `hermes install-memory-provider` (sidecar only, no activation). TR tools remain available; the active provider stays `<X>`. |

   The install commands are idempotent. Re-running on an already-installed sidecar is safe — the managed marker comment prevents clobbering hand-edited files (pass `--force` to override).

---

## Account setup

The agent never generates the recovery phrase. Only the user's browser does, via the `totalreclaw_pair` tool.

**This section starts AUTOMATICALLY after the user confirms `done` to the install-procedure step 4 `/restart` request.** Do not ask "want me to set up your account?". The user already consented by sending the install prompt.

1. Check whether credentials already exist:
   ```bash
   test -s ~/.totalreclaw/credentials.json && echo "already set up"
   ```
   If present, emit `✓ TotalReclaw is already set up.` and skip to the Post-setup section below.
2. Call the `totalreclaw_pair` tool. **Inputs: none required.** The tool defaults to `mode=either` — the browser pair page will render BOTH "Generate new" and "Import existing" tabs so the user picks at pair time. Do NOT pass `mode=generate` or `mode=import` explicitly unless the user has specifically asked for one path (e.g. *"restore my account"* → `mode=import`). The tool returns a JSON object: `{url, pin, expires_at_ms}`.
3. Emit ONE user-visible line containing the URL and PIN verbatim:

   > Open `<url>` in your browser. Enter PIN `<pin>`. On the page, choose **Generate new** (creates a fresh 12-word recovery phrase) OR **Import existing** (paste a phrase you already have). Reply `done` once the page says it's sealed.

   Do not paraphrase the URL or PIN. Do not invent values when the tool fails — surface the failure verbatim and stop. **This single line is the ENTIRE browser instruction.** Do not follow it with a numbered list restating the steps, a "what's done so far" recap, or a trailing "waiting for you to finish…" / "standing by…" line — the line already names every step and `Reply \`done\`` already encodes the wait (auto-QA #340 / #342).
4. After the user replies `done`, re-check `~/.totalreclaw/credentials.json`. Present → continue to Post-setup. Absent → the PIN expired; call `totalreclaw_pair` again and resend step 3 once.

---

## Post-setup

After credentials.json is confirmed present (step 4 above), call `totalreclaw_status` once and emit a single user-visible block containing:

- A recovery-phrase backup reminder (verbatim — the user just generated it in their browser; this is the reminder to actually save it somewhere durable).
- Current account state from `totalreclaw_status` (tier, memory count, free-tier memory limit). Do NOT surface the underlying chain/network to the user.
- A short confirmation that the agent will now use TotalReclaw for memory operations.

Emit verbatim:

> ✓ TotalReclaw set up.
>
> Back up your 12-word recovery phrase NOW — store it offline (password manager, paper, hardware key). It is the only way to recover your encrypted memory vault if you lose access to this device. We cannot recover it for you.
>
> Account status: `<tier>` tier, `<used>` / `<limit>` memories used. Ask me to remember anything and I'll save it to your encrypted vault.

(`<tier>`, `<used>`, `<limit>` from `totalreclaw_status` — do not invent the numbers, and do not name the underlying chain/network to the user.)

**Emit this block VERBATIM on the `done` turn.** Do not paraphrase, do not substitute the tier numbers (never render `<limit>` as "50,000 writes", "unlimited", or any figure not returned by `totalreclaw_status` — and if `totalreclaw_status` would show a higher staging limit, still quote what it returns, not a guess), do not add a preamble ("All green. Here's the summary:", "Everything looks good!", "✓ Setup complete. Tier info:"), and do not run any verification command (`hermes doctor`, `hermes plugins list`, a second diagnostic `totalreclaw_status`) on this turn — the credentials check in Account setup step 4 is the ONLY verification. Adding bullets, recaps, or "standing by…" lines inflates the transcript and is a setup-flow violation (auto-QA #340 / #342).

This block ships on first successful pair completion. On subsequent install runs where credentials already exist (early-exit in Account setup step 1), instead emit just:

> ✓ TotalReclaw is already set up. Account status: `<tier>` tier, `<used>` / `<limit>` memories used.

---

## Recall behaviour

**For any user query that asks about prior facts about themselves** — "what do you know about me", "what are my preferences for X", "did I tell you about Y", etc. — the agent MUST call `totalreclaw_recall` even if it appears to have the answer in its current context.

Rationale: Hermes's built-in memory layer auto-loads a local `~/.hermes/memories/USER.md` cache into the agent's system context at session start. That cache may contain answers from prior sessions, but it is NOT the canonical cross-session store. TotalReclaw on chain IS. Always querying through `totalreclaw_recall` ensures the integration is exercised + that the on-chain memory is the source the user is recovering against.

If `totalreclaw_recall` returns matching facts: use those as the answer.
If it returns nothing: say so honestly, do not fabricate from context.

---

## Phrase safety

These are hard rules. None of them are negotiable.

1. The recovery phrase is generated only in the user's browser via `totalreclaw_pair`. The agent never sees it.
2. The agent does not run `totalreclaw setup` from its shell. The CLI refuses to run from an agent runtime (exit code 3 with a stderr redirect to `totalreclaw_pair`). The agent does not bypass the gate with `--allow-non-tty`.
3. The agent does not `cat ~/.totalreclaw/credentials.json` or read the `mnemonic` / `recovery_phrase` field from any file at any time.
4. The agent does not echo, paraphrase, or ask the user to paste a recovery phrase in chat. If the user pastes one, the agent tells the user the phrase is compromised and calls `totalreclaw_pair` with `mode=generate` to mint a fresh wallet.
5. The agent does not pass the recovery phrase to any shell command, log line, model prompt, or tool argument.

---

## Tools

| Tool | Purpose |
|---|---|
| `totalreclaw_pair` | Open a browser-side pairing session. Returns `{url, pin, expires_at_ms}`. Sole agent-facilitated path for account setup. |
| `totalreclaw_remember` | Store a memory. |
| `totalreclaw_recall` | Retrieve memories. Always called for user-facing recall queries (see Recall behaviour above). |
| `totalreclaw_forget` | Tombstone a memory. |
| `totalreclaw_pin` | Pin a memory so it's surfaced in every recall. |
| `totalreclaw_status` | Report account tier + memory counts. |
| `totalreclaw_export` | Export the full vault. |
| `totalreclaw_set_scope` | Switch active scope. |
| `totalreclaw_retype` | Change a memory's taxonomy type. |

---

## Diagnostics

- Plugin not loaded after the restart: `"$(dirname "$HERMES_PYTHON")/hermes" plugins list` should show `totalreclaw` as enabled. If absent, re-run install procedure step 3.
- `totalreclaw_pair` returns a URL that 404s on the browser: the relay is unreachable — check `TOTALRECLAW_SERVER_URL` env var matches the user's intended channel (prod default, or staging from the QA prompt).
- `totalreclaw_pair` returns a 5xx: ask the user to retry; report the error code verbatim.
- Tool not bound after install: restart once per Install procedure step 4. If still missing, the package install hit the wrong Python; re-resolve `HERMES_PYTHON` and re-install.
- **The agent tries to issue `/restart` itself / says "I'll restart now":** it can't. Hermes only executes slash commands typed by the **user** — agent output is never parsed for commands, on any surface. The agent's job is to emit the right per-surface restart instruction (Install-procedure step 4 matrix) and then wait for the user's continuation reply.
- **`hermes gateway restart` from inside a docker container returns "no running gateway" or fails silently:** the container has no systemd / launchd service supervisor to respawn the gateway after a self-exit. The correct restart for docker is `docker restart <container-name>` from the **host** shell, not from inside the container.
- **`/restart` returns "not authorized" / "command not found", or the surface has no supervisor to respawn the gateway:** the user restarts the gateway out-of-band — `hermes gateway restart` (native) or `docker restart <container-name>` (docker, from the host). **Do NOT suggest `/new` as a shortcut** — a new session reuses the gateway's boot-time tool registry and will NOT pick up the freshly-installed plugin (Hermes discovers plugins once, at gateway startup).
- **After restart, agent doesn't continue setup:** if the user replied only `done` without a continuation phrase, the agent may not infer it should resume. The rc.27+ proactive setup nudge fires on the first turn of every unconfigured session, so re-engaging the conversation with anything (even another bare message) should trigger the new-vs-restore question. If it doesn't, ask the user to type "Continue setting up TotalReclaw" verbatim.

---

## Memory provider modes

Two TotalReclaw paths exist into Hermes; they coexist but serve different surfaces.

| Mode | What's active | When to use | How to set |
|---|---|---|---|
| **Active provider** | Generic plugin tools + lifecycle hooks **+** `MemoryProvider` hooks (`on_pre_compress`, `on_memory_write`, `on_session_end`, `on_turn_start`). Captures Background-Review writes the toolset whitelist would otherwise drop. | The user has no other memory provider configured, or wants TR to be the canonical cross-session store. Default for fresh installs. | `hermes activate-memory-provider` |
| **Tools-only** | Generic plugin tools + lifecycle hooks only. No `MemoryProvider` hooks — they fire on the user's other active provider (Honcho / Byterover / OpenViking / ...). | The user already has another provider configured and wants to keep using it alongside TR's chat tools. | `hermes install-memory-provider` (no `--activate`) |

The two install commands are idempotent. Re-running on an already-installed sidecar overwrites with the current shim content (catches package upgrades) without disturbing the rest of the install. The sidecar refuses to clobber a hand-edited file unless `--force` is passed.

Switching providers manually (post-install):

```bash
# Switch TO TotalReclaw
hermes activate-memory-provider

# Switch BACK to another provider (after a `switch` choice the user regrets)
hermes memory set-provider <name>   # upstream Hermes CLI
```

Switching deactivates the previous provider — its memories remain on disk but the agent stops querying them until you switch back. New memories go through whichever provider is active at write time.

Status check:

```bash
hermes memory-status              # JSON: {"provider": "totalreclaw"|"honcho"|"none"|...}
```

This grep-reads `~/.hermes/config.yaml` (or `$HERMES_HOME/config.yaml`) and does not depend on an upstream `hermes memory status --json` command (which is not yet released as of 2026-05-15 — see Path B spec §"Open questions for upstream").

---

## Compatibility with Hermes built-in memory

**Hermes built-in memory (`~/.hermes/memories/USER.md` and `MEMORY.md`) cannot be disabled.** Per Hermes upstream design, the built-in layer is always active — `hermes memory --help` explicitly states "Built-in memory (MEMORY.md/USER.md) is always active." The `hermes tools disable memory` command only blocks AGENTS from calling the `memory` tool; the gateway continues to write USER.md autonomously as part of its session-context management.

This is not a bug we can fix from the TotalReclaw side. We document it honestly:

- **TotalReclaw is the canonical cross-session store**, derived from your recovery phrase. Memories persist on chain, recoverable on any device or container by re-importing the phrase via `totalreclaw_pair`.
- **Hermes's USER.md is a local, per-container context cache.** It is NOT cross-session in the cross-device sense; it lives only inside this Hermes deployment and is wiped on container reset or `hermes memory reset`. Treat it as part of the LLM's working memory, not as a memory store.
- **For any user-facing recall query, the agent calls `totalreclaw_recall`** even when USER.md may already contain a relevant fact in context (see Recall behaviour). This forces the answer through the canonical TotalReclaw read path.
- **The agent may run `hermes tools disable memory`** to block the `memory` agent tool (this still has value: prevents the agent from being tempted to call the built-in `memory` tool when TotalReclaw's tools are the right ones). But this command does NOT stop the USER.md / MEMORY.md writes.

Flag interest in fully-disable-able built-in memory at <https://github.com/NousResearch/hermes-agent/issues> (Hermes upstream). We track interest at <https://github.com/p-diogo/totalreclaw/issues>.

---

## Out of scope for this guide

User-facing topics (how the user records their recovery phrase, what to do on a lost phrase, pricing tiers, billing) live in a separate user guide that will be linked here when it ships.
