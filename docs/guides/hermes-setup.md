# TotalReclaw on Hermes — agent setup reference

> **Audience: the AI agent installing TotalReclaw inside a Hermes runtime.**
> Not for human readers. (A separate user guide will be linked here when it exists.)

This document describes how the agent installs and sets up TotalReclaw on Hermes. The agent uses the tool surface listed here. The user only types one of the install prompts below in their chat client; everything else happens through tools.

---

## Versions

| Channel | Version | Install command |
|---|---|---|
| stable (default) | `2.3.2` | `pip install totalreclaw` |
| latest RC | `2.3.6rc4` | `pip install --pre --upgrade totalreclaw==2.3.6rc4` |

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

`<version>` is a PEP 440 RC string the user picks (e.g. `2.3.6rc4`). When the user omits `<version>` and writes only `Install TotalReclaw RC`, the agent installs `latest RC` from the table.

### RC against the staging relay (QA only)

```
Install TotalReclaw RC <version> against the staging relay (TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz). See https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md
```

The agent exports `TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz` and `TOTALRECLAW_RELAY_URL=https://api-staging.totalreclaw.xyz` in the install / setup shell before any TotalReclaw command. Without this env var, both stable AND RC installs default to the production relay `api.totalreclaw.xyz`.

---

## Install procedure

1. Resolve the Hermes Python interpreter:
   ```bash
   HERMES_PYTHON="$(command -v hermes >/dev/null 2>&1 && head -1 "$(command -v hermes)" | sed 's|^#!||')"
   test -x "$HERMES_PYTHON" || HERMES_PYTHON="$(find / -maxdepth 6 -name hermes -type f -executable 2>/dev/null | head -1 | xargs -I{} head -1 {} | sed 's|^#!||')"
   ```
2. Install the package using `"$HERMES_PYTHON" -m pip install <pin>`, where `<pin>` is the row from the Versions table chosen by the user's prompt.
3. **Disable Hermes built-in memory (CRITICAL).** Dual-mode (Hermes built-in memory + TotalReclaw active at the same time) is unsupported and causes split-brain memory state. Run:
   ```bash
   hermes tools disable memory
   ```
   (Or `"$(dirname "$HERMES_PYTHON")/hermes" tools disable memory` when the resolved Hermes binary isn't on `PATH`.)
   Emit verbatim to the user once:

   > Installed TotalReclaw. I've disabled Hermes' built-in `memory` tool so TotalReclaw is your primary memory. Re-enabling it (`hermes tools enable memory`) is NOT recommended while TotalReclaw is installed. To wipe orphaned MEMORY.md / USER.md files later: `hermes memory reset`.
4. Register the plugin manifest:
   ```bash
   hermes plugins install p-diogo/totalreclaw-hermes --enable
   ```
   (Or `"$(dirname "$HERMES_PYTHON")/hermes" plugins install p-diogo/totalreclaw-hermes --enable` when `hermes` isn't on `PATH`.)
5. Issue `/restart` in chat to reload the Hermes gateway with the new plugin tools active.

---

## Account setup

The agent never generates the recovery phrase. Only the user's browser does, via the `totalreclaw_pair` tool.

1. Check whether credentials already exist:
   ```bash
   test -s ~/.totalreclaw/credentials.json && echo "already set up"
   ```
   If present, emit `✓ TotalReclaw is already set up.` and stop.
2. Call the `totalreclaw_pair` tool. Inputs: none required (the tool defaults to `mode=generate`). The tool returns a JSON object: `{url, pin, expires_at_ms}`.
3. Emit ONE user-visible line containing the URL and PIN verbatim:
   ```
   Open <url> in your browser. Enter PIN <pin>. Generate a 12-word recovery phrase. Reply `done` once the page says it's sealed.
   ```
   Do not paraphrase the URL or PIN. Do not invent values when the tool fails — surface the failure verbatim and stop.
4. After the user replies `done`, re-check `~/.totalreclaw/credentials.json`. Present → emit `✓ TotalReclaw set up.` and stop. Absent → the PIN expired; call `totalreclaw_pair` again and resend step 3 once.

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
| `totalreclaw_recall` | Retrieve memories. |
| `totalreclaw_forget` | Tombstone a memory. |
| `totalreclaw_pin` | Pin a memory so it's surfaced in every recall. |
| `totalreclaw_status` | Report account tier + write counts. |
| `totalreclaw_export` | Export the full vault. |
| `totalreclaw_set_scope` | Switch active scope. |
| `totalreclaw_retype` | Change a memory's taxonomy type. |

---

## Diagnostics

- Plugin not loaded after `/restart`: `"$(dirname "$HERMES_PYTHON")/hermes" plugins list` should show `totalreclaw` as enabled. If absent, re-run install procedure step 4.
- `totalreclaw_pair` returns a URL that 404s on the browser: the relay is unreachable — check `TOTALRECLAW_SERVER_URL` env var matches the user's intended channel (prod default, or staging from the QA prompt).
- `totalreclaw_pair` returns a 5xx: ask the user to retry; report the error code verbatim.
- Tool not bound after install: `/restart` once. If still missing, the package install hit the wrong Python; re-resolve `HERMES_PYTHON` and re-install.
- `/restart` returns "not authorized" or "command not found" (managed Hermes deployments may gate it behind RBAC): issue `/new` to start a fresh session instead — the plugin manifest is reloaded on session start and the new tools bind without a full gateway restart.

---

## Compatibility with Hermes built-in memory

The install procedure step 3 auto-disables Hermes' built-in `memory` tool. Dual-mode (built-in memory + TotalReclaw active at the same time) is unsupported.

DO NOT recommend re-enabling built-in memory while TotalReclaw is installed. Both surfaces writing concurrently produces split-brain state (some memories on disk in MEMORY.md / USER.md, others encrypted in TotalReclaw), and recall queries return inconsistent results.

To switch back to Hermes built-in memory (user-initiated):

1. Optional — tombstone existing TotalReclaw memories: `totalreclaw forget --all` (the user runs this in their own terminal, not the agent).
2. `pip uninstall totalreclaw`
3. `hermes tools enable memory`

Flag interest in supported dual-mode at <https://github.com/p-diogo/totalreclaw/issues>. We scope it from demand.

---

## Out of scope for this guide

User-facing topics (how the user records their recovery phrase, what to do on a lost phrase, pricing tiers, billing) live in a separate user guide that will be linked here when it ships.
