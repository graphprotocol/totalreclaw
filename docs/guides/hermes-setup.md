# TotalReclaw on Hermes — agent setup reference

> **Audience: the AI agent installing TotalReclaw inside a Hermes runtime.**
> Not for human readers. (A separate user guide will be linked here when it exists.)

This document describes how the agent installs and sets up TotalReclaw on Hermes. The agent uses the tool surface listed here. The user only types one of the install prompts below in their chat client; everything else happens through tools.

---

## Versions

| Channel | Version | Install command |
|---|---|---|
| stable (default) | `2.3.2` | `pip install totalreclaw` |
| latest RC | `2.3.7rc1` | `pip install --pre --upgrade totalreclaw==2.3.7rc1` |

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

---

## Install procedure

After the procedure completes (the user replies `done` to the `/restart` request in step 4), **proceed directly to Account setup**. Do not ask the user "is it ready?" — the user's `done` IS the signal.

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
4. **Ask the user to issue `/restart`** so the gateway reloads with the new plugin tools active. The agent does NOT issue `/restart` itself — agents have repeatedly mis-tracked restart state and issued it twice, killing the pair sidecar mid-flight. Emit ONE user-visible line:

   > Send `/restart` in chat now. Reply `done` once you see `Gateway restarted successfully`.

   Wait for the user to reply `done`. Do NOT proceed to Account setup until the user confirms. Do NOT issue `/restart` yourself.

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

   Do not paraphrase the URL or PIN. Do not invent values when the tool fails — surface the failure verbatim and stop.
4. After the user replies `done`, re-check `~/.totalreclaw/credentials.json`. Present → continue to Post-setup. Absent → the PIN expired; call `totalreclaw_pair` again and resend step 3 once.

---

## Post-setup

After credentials.json is confirmed present (step 4 above), call `totalreclaw_status` once and emit a single user-visible block containing:

- A recovery-phrase backup reminder (verbatim — the user just generated it in their browser; this is the reminder to actually save it somewhere durable).
- Current account state from `totalreclaw_status` (tier, write count, free-write limit, network).
- A short confirmation that the agent will now use TotalReclaw for memory operations.

Emit verbatim:

> ✓ TotalReclaw set up.
>
> Back up your 12-word recovery phrase NOW — store it offline (password manager, paper, hardware key). It is the only way to recover your encrypted memory vault if you lose access to this device. We cannot recover it for you.
>
> Account status: `<tier>` tier, `<used>` / `<limit>` writes used, stored on `<network>`. Ask me to remember anything and I'll save it to your encrypted vault.

(`<tier>`, `<used>`, `<limit>`, `<network>` from `totalreclaw_status`. `<network>` is `Base Sepolia (testnet)` for free tier, `Gnosis (mainnet)` for Pro.)

This block ships on first successful pair completion. On subsequent install runs where credentials already exist (early-exit in Account setup step 1), instead emit just:

> ✓ TotalReclaw is already set up. Account status: `<tier>` tier, `<used>` / `<limit>` writes used, stored on `<network>`.

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
| `totalreclaw_status` | Report account tier + write counts. |
| `totalreclaw_export` | Export the full vault. |
| `totalreclaw_set_scope` | Switch active scope. |
| `totalreclaw_retype` | Change a memory's taxonomy type. |

---

## Diagnostics

- Plugin not loaded after `/restart`: `"$(dirname "$HERMES_PYTHON")/hermes" plugins list` should show `totalreclaw` as enabled. If absent, re-run install procedure step 3.
- `totalreclaw_pair` returns a URL that 404s on the browser: the relay is unreachable — check `TOTALRECLAW_SERVER_URL` env var matches the user's intended channel (prod default, or staging from the QA prompt).
- `totalreclaw_pair` returns a 5xx: ask the user to retry; report the error code verbatim.
- Tool not bound after install: `/restart` once. If still missing, the package install hit the wrong Python; re-resolve `HERMES_PYTHON` and re-install.
- `/restart` returns "not authorized" or "command not found" (managed Hermes deployments may gate it behind RBAC): issue `/new` to start a fresh session instead — the plugin manifest is reloaded on session start and the new tools bind without a full gateway restart.

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
