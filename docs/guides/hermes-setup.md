# TotalReclaw on Hermes — agent setup reference

> **Audience: the AI agent installing TotalReclaw inside a Hermes runtime.**
> This is the operational / agent reference — the procedure the agent follows end-to-end. For a human-friendly quick start, see [client-setup-v1.md](./client-setup-v1.md) §Python client.

This document describes how the agent installs and sets up TotalReclaw on Hermes. The agent uses the tool surface listed here. The user only types one of the install prompts below in their chat client; everything else happens through tools.

> **Wrong runtime?** This guide is for **Hermes** only. If you are an **OpenClaw** agent, follow [`openclaw-setup.md`](./openclaw-setup.md); if you are **Claude Desktop / Cursor / Windsurf / any MCP host**, follow [`claude-code-setup.md`](./claude-code-setup.md); if you are unsure which runtime you are, follow the universal dispatcher [`install.md`](./install.md) instead.

---

## Versions

| Channel | Resolves to | Install command |
|---|---|---|
| stable (default) | newest final | `pip install totalreclaw` |
| latest RC | newest pre-release | `pip install --pre --upgrade totalreclaw` |

<!-- LATEST_RC -->_Most recent RC published: `2.5.3rc6`._<!-- /LATEST_RC --> <sub>(display only — auto-bumped by `publish-python-client.yml` on each RC cut; the install commands above are version-agnostic, so this number never affects an install.)</sub>

**Both rows are version-agnostic by design — neither pins a number, so this table never goes stale when an RC is cut.** `pip install totalreclaw` always lands the newest *final*; `pip install --pre --upgrade totalreclaw` always lands the newest *pre-release*. To install a SPECIFIC version, pin it: `pip install --pre totalreclaw==<version>`. `pip index versions totalreclaw` shows exactly what's live on PyPI.

---

## Install prompts

The user pastes one of these three prompts. The shape is fixed; the agent extracts intent from the shape, not from arbitrary phrasing.

### Stable (default)

```
Install TotalReclaw. See https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/hermes-setup.md
```

The agent installs the stable channel from the table above and proceeds to setup.

### RC (user specifies a version)

```
Install TotalReclaw RC <version>. See https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/hermes-setup.md
```

`<version>` is a PEP 440 RC string the user picks (e.g. `2.4.4rc9`). When the user omits `<version>` and writes only `Install TotalReclaw RC`, the agent installs the **newest pre-release** via `pip install --pre --upgrade totalreclaw` (no pin — always the latest cut, so this never needs the doc to be bumped).

### RC against the staging relay (QA only)

```
Install TotalReclaw RC <version> against the staging relay (TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz). See https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/hermes-setup.md
```

The agent exports `TOTALRECLAW_SERVER_URL=https://api-staging.totalreclaw.xyz` and `TOTALRECLAW_RELAY_URL=https://api-staging.totalreclaw.xyz` in the install / setup shell before any TotalReclaw command. Without these env vars, both stable AND RC installs default to the production relay `api.totalreclaw.xyz`.

---

## Update prompt

TotalReclaw has NO native Hermes update path (`hermes plugins update` is git-clone-only; `hermes update` updates Hermes itself, not a pip plugin). So updates run the same install machinery, re-pointed at the newest **stable**. The user pastes:

```
Update TotalReclaw. See https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/hermes-setup.md
```

The agent updates to the newest **stable** and re-binds — **silently**, exactly like install (same HARD RULE: no `pip` / `venv` / interpreter narration). Reuse the **Install procedure below**:

1. **Steps 1–2 with `PIN="totalreclaw"`** (stable channel — `pip install --upgrade` lands the newest final, without `--pre`). Run the same `(a)`/`(b)`/`(c)` install-and-verify block. Emit the one `✓ Installed totalreclaw <version>` line only after `(c)`'s RUNTIME verify confirms the Hermes interpreter now imports the new version (guards the same shadow-install trap as a fresh install).
2. **Step 4 restart request** — re-emit the surface-matched restart line verbatim (Telegram / docker / systemd / launchd / standalone `hermes chat`) so the freshly-upgraded plugin binds. There is no hot-reload for an updated plugin either.

**Budget: ≤3 user-visible lines** — the `✓ Installed` line, the restart line, and (optionally) a one-line "you're up to date on <version>" confirmation after the user's continuation reply. Account setup / pairing is **skipped** on an update (credentials already exist); do NOT re-run it.

### Automatic update notice

The client also nudges on its own. When the relay advertises a newer stable than the installed build, the next session start injects a single line into the agent's context:

> TotalReclaw X is available (you're running Y). Say 'update TotalReclaw' to upgrade.

It fires at most once per 24h across sessions, and only ever when a newer **final** is out (an rc user is nudged when the matching final ships; a user already on a newer rc line is not). Set `TOTALRECLAW_DISABLE_UPDATE_NOTICE=1` to silence it (see [env-vars-reference.md](env-vars-reference.md)). When the user acts on it, follow the **Update prompt** above.

---

## Conversational style — HARD RULE for install + setup

**The user must NOT see install mechanics. Narrating shell / `pip` / `venv` / interpreter-detection / "let me verify…" during install or setup is a DEFECT** — it spams the chat with garbage (QA regression, 2026-06). This is not aspirational: run every tool/shell step **silently** and emit ONLY the short verbatim lines each procedure names. **Budget for a full install + setup: ≤6 short user-visible lines, total.** If you wrote more than that, you narrated something that should have been silent — delete it.

(Post-setup usage replies are normal conversation — this hard rule governs the install + account-setup path specifically.)

- Do not surface tool-call mechanics verbatim to the user. The user does not need to see `🐍 execute_code: "from hermes_tools import terminal..."` or `📚 skill_view: "..."` or `📋 todo: "planning N tasks"`. Run the tools silently and emit only the user-visible lines listed in the procedures below.
- Use the explicit verbatim emit lines where the procedure provides them. Otherwise keep replies short and to-the-point.
- Do not narrate "Now I'll do X, then Y, then Z." Do the work.
- **Budget: a fresh install + setup should be ~5–6 short user-visible lines total.** Anything beyond that is almost always narration that should have stayed silent.
- **Forbidden vocabulary during install / setup** (unless the user explicitly asks how it works): `Python`, `interpreter`, `venv`, `pip`, `ensurepip`, `bootstrap`, `detect`, `probing` — plus narration openers: "Let me check…", "Let me verify…", "Now installing…", "Plugin loaded, let me verify…", "Quick fix…", "One sec…", "Got it, doing X…", and any "I'm going to do X" / "About to do X" pre-narration of a tool call. These are tool-call mechanics, not user-facing prose. If the user asks ("what's it doing?", "show me the commands"), narrate freely — this only governs the default silent path.

---

## Install procedure

**The ENTIRE install procedure (steps 1–5) is SILENT except for exactly TWO user-visible lines:** (1) `✓ Installed totalreclaw <version>` after step 2 verifies, and (2) the restart request from step 4. Steps 1, 3, 5 and the shell/verify mechanics of step 2 produce **zero** chat output — run them with your shell tool and say nothing about them. No `pip`, no `venv`, no "let me verify", no "resolving the interpreter".

After the procedure completes (the user replies *"Done — continue setting up TotalReclaw"* or equivalent to the restart request in step 4), **proceed directly to Account setup**. Do not ask the user "is it ready?" — the user's reply IS the signal.

**Order matters on Hermes 0.22+:** install the package (step 2), enable the plugin (step 3), restart once (step 4), check the tools are bound (step 5), and only then run Account setup. Pairing needs the `totalreclaw_pair` tool. **Pairing makes TotalReclaw Hermes' memory provider** unless another provider (Honcho, ...) is already active; Account setup step 5 checks it, and one more restart loads it. Hermes' built-in memory (`USER.md` / `MEMORY.md`) stays on beside TotalReclaw. That is intended: see [Compatibility](#compatibility-with-hermes-built-in-memory).

1. **(SILENT — emit nothing.)** Resolve the Python environment Hermes imports packages from. **Hermes 0.22+ runs on its own bundled Python** (`~/.hermes/tools/python-3.14.*/bin/python3`) and loads its dependencies from a separate managed venv under `~/.hermes/installs/…/venv`; the `hermes` command is a `#!/bin/sh` launcher, so its shebang is NOT the interpreter. Ask Hermes for its import path; fall back to the shebang on older Hermes, where `hermes` is a Python script inside the venv:
   ```bash
   # Hermes 0.22+: the first site-packages on Hermes' own sys.path is its managed venv.
   HERMES_SITE="$(hermes --run-module site 2>/dev/null | sed -n "s|^ *'\(/[^']*/site-packages\)',*$|\1|p" | head -1)"
   HERMES_VENV="${HERMES_SITE%/lib/*}"
   if [ -n "$HERMES_SITE" ] && [ -f "$HERMES_VENV/pyvenv.cfg" ]; then
     HERMES_PYTHON="$HERMES_VENV/bin/python"
     # The managed venv has no pip of its own; its base interpreter (Hermes' bundled Python) does.
     PIP_PYTHON="$(sed -n 's/^home *= *//p' "$HERMES_VENV/pyvenv.cfg")/python3"
   else
     # Older Hermes: the `hermes` script's shebang is the venv interpreter.
     HERMES_PYTHON="$(command -v hermes >/dev/null 2>&1 && head -1 "$(command -v hermes)" | sed 's|^#!||')"
     test -x "$HERMES_PYTHON" || HERMES_PYTHON="$(find / -maxdepth 6 -name hermes -type f -executable 2>/dev/null | head -1 | xargs -I{} head -1 {} | sed 's|^#!||')"
     PIP_PYTHON="$HERMES_PYTHON"
   fi
   ```
2. **(SILENT except the single `✓ Installed totalreclaw <version>` line, emitted only AFTER (c) verifies.) Install the package into that environment — with its dependencies — and VERIFY the runtime version actually landed there.** Use pip's `--python` option so the install lands in `$HERMES_PYTHON`'s environment even when that venv has no pip. Do **not** use a bare `pip`/`pip3`, `pip --user`, or the system Python: the package then lands where Hermes never looks, or an older copy shadows it (rc6 QA). Do **not** pass `--no-deps`. Do **not** use `uv pip install` on Hermes 0.22+: Hermes' uv refuses packages published in the last few days.
   ```bash
   # Stable (default): PIN="totalreclaw" and no --pre.
   # Latest RC (user gave NO version): PIN="totalreclaw" plus PRE="--pre" — resolves the newest pre-release.
   # Explicit version (user typed "RC <version>"): PIN="totalreclaw==<version>", e.g. totalreclaw==2.4.4rc9 (no --pre needed).
   PIN="<pin>"; PRE=""
   # (a) Install with the pip that ships with the base interpreter, targeting Hermes' environment:
   #     --no-cache-dir: a cached package index can hide an RC published in the last few minutes.
   "$PIP_PYTHON" -m pip --python "$HERMES_PYTHON" install --disable-pip-version-check --no-cache-dir --upgrade $PRE "$PIN" \
     || { # (b) No pip there: bootstrap pip into the target environment, then install:
          "$HERMES_PYTHON" -m ensurepip --upgrade 2>/dev/null
          "$HERMES_PYTHON" -m pip install --disable-pip-version-check --upgrade $PRE "$PIN"; }
   # (c) VERIFY the version the Hermes environment actually IMPORTS (not just what pip installed).
   #     Run from a neutral cwd: from $HOME a stray checkout can mask a failure.
   ( cd /tmp && "$HERMES_PYTHON" -c "import totalreclaw as t, websockets as w; print('RUNTIME', t.__version__, t.__file__, 'websockets', w.__version__)" )
   ```
   The printed `RUNTIME` `__file__` MUST be under `$HERMES_PYTHON`'s own site-packages (on Hermes 0.22+, under `~/.hermes/installs/…/venv/`; NOT `~/.local/...` and NOT the bundled `~/.hermes/tools/python-*/` tree), and — for an explicit `==` pin — the version MUST equal it. (For the unpinned latest-RC path, just confirm the version is a pre-release, i.e. contains `rc`.) `websockets` must still be the version Hermes shipped (15.x on Hermes 0.22); TotalReclaw no longer caps it. If `websockets` is missing, older or newer than that (e.g. 13.x left by a TotalReclaw version before 2.5.3, or 17.x when Hermes' copy was already gone), run `hermes pm repair` to restore Hermes' own dependencies, then repeat this step: the repair also removes TotalReclaw from Hermes' environment. If `__file__` points elsewhere or the version is an OLDER build than expected, the package landed in the wrong site — re-run (a) with `--force-reinstall`, or remove the stale copy, until `RUNTIME` loads from the Hermes environment. **Do NOT proceed to setup on a shadow / version mismatch** — the whole flow silently runs old code (the rc6 NO-GO root cause).
3. **(SILENT — emit nothing.) Enable the plugin.** On Hermes 0.22+ plugins are opt-in: the package's `hermes_agent.plugins` entry point is discovered automatically, but its tools only load once the plugin is in `plugins.enabled`. `hermes plugins list` shows `totalreclaw … not enabled … entrypoint` until then.
   ```bash
   hermes plugins enable totalreclaw 2>/dev/null || true
   ```
   Older Hermes without this subcommand loads entry-point plugins on its own; the `|| true` keeps the step harmless there. Only if `hermes tools list` still shows no `totalreclaw` after the step-4 restart, fall back to the git-plugin wrapper (step 5 does this automatically):
   ```bash
   hermes plugins install graphprotocol/totalreclaw-hermes --enable
   ```

4. **Ask the user to restart the gateway** so the new plugin tools bind. The agent does NOT — and *cannot* — issue the restart itself. Two facts from the Hermes internals make this non-negotiable:
   - **Only the user can trigger a restart.** Hermes parses slash commands from **user input only**; an agent that emits `/restart` (or `/new`) in its reply just sends plain text that does nothing — on *every* surface (CLI/TUI and messaging platforms alike). (`gateway/platforms/base.py` parses commands off inbound user events; agent output is returned verbatim.)
   - **A full gateway restart is required.** There is no hot-reload — `hermes plugins install` only writes files and prints *"Restart the gateway for the plugin to take effect."* Plugins are discovered **once at gateway boot**, and starting a new session (`/new`) does NOT re-scan them. So `/new` is not a shortcut; only a full restart binds the freshly-installed plugin.

   A user-typed `/restart` triggers a graceful SIGUSR1 exit that respawns ONLY under a process supervisor (systemd / launchd / s6). In bare docker or an ephemeral `hermes chat` there is no supervisor, so the user restarts the process / container out-of-band. Pick the line below by surface:

   Probe the surface before emitting:

   ```bash
   # /proc/1/cgroup names docker only on cgroup v1; the marker files cover cgroup v2 hosts and podman.
   IN_DOCKER=$( { [ -f /.dockerenv ] || [ -f /run/.containerenv ] || grep -q -E 'docker|containerd' /proc/1/cgroup 2>/dev/null; } && echo yes || echo no)
   HAS_MSG_BOT="$(printenv | grep -E '^HERMES_(TELEGRAM|DISCORD|SLACK|MATRIX|FEISHU|WHATSAPP)_(BOT_TOKEN|HOMESERVER|APP_ID|PHONE_NUMBER_ID)=' | head -1)"
   # Fallback (#390): some Hermes gateway deployments expose only
   # HERMES_SESSION_PLATFORM=<telegram|discord|slack|matrix|feishu|whatsapp> —
   # the bot-token vars live in a separate secret store the agent shell can't see.
   # Without this fallback the probe falls into the "Detection fails / ambiguous"
   # row and asks the user a question that the env already answered.
   [ -z "$HAS_MSG_BOT" ] && printenv HERMES_SESSION_PLATFORM 2>/dev/null \
     | grep -q -E '^(telegram|discord|slack|matrix|feishu|whatsapp)$' \
     && HAS_MSG_BOT="HERMES_SESSION_PLATFORM=$(printenv HERMES_SESSION_PLATFORM)"
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

   Use the systemd / launchd rows only when that service manager was actually found. When `HAS_SYSTEMCTL=no` and `HAS_LAUNCHCTL=no` and there is no messaging bot, take the "ambiguous" row and ask: inside a container, `hermes gateway restart` runs the gateway in the foreground and never returns.

   Substitute the actual container name in the docker line — usually visible via `docker ps --format '{{.Names}}'`; if the agent runs that probe and finds exactly one container, splice the name in. Fall back to `<container-name>` placeholder otherwise.

   **Standalone `hermes chat` CLI (no separate gateway daemon) — rc6 QA F11/#379.** The `hermes gateway restart` rows above apply only when a long-running `hermes gateway` daemon is serving the chat. If the user is just running `hermes chat` directly (the common laptop case — each invocation spawns its own short-lived process and exits after the turn), there is no gateway to restart and `/restart` is inert. Tell them instead: **exit and re-run `hermes chat`** — the freshly-installed plugin is discovered at the new process's boot (plugin discovery runs once per process start, so a fresh invocation picks it up). No `/restart`, no `hermes gateway restart`.

   Wait for the user's reply containing "continue" / "set up" / similar resumption language. Do NOT proceed to Account setup until the user confirms. Do NOT issue any restart yourself.

5. **(SILENT — emit nothing on the GO path.) Verify the plugin tools bound after restart (#390).** With the plugin enabled (step 3), entry-point discovery is usually fine. On Hermes 0.22+ `hermes tools list` shows `✓ enabled  totalreclaw` once it is. A small fraction of Hermes deployments don't re-scan entry points on a single restart, leaving `totalreclaw_pair` and the rest unregistered — and the agent then can't proceed because *every* Account-setup step routes through plugin-registered tools. Probe + auto-fallback to the manual manifest install:
   ```bash
   if ! ( hermes tools list 2>/dev/null | grep -q '\btotalreclaw' \
          || "$(dirname "$HERMES_PYTHON")/hermes" tools list 2>/dev/null | grep -q '\btotalreclaw' ); then
     hermes plugins install graphprotocol/totalreclaw-hermes --enable 2>/dev/null \
       || "$(dirname "$HERMES_PYTHON")/hermes" plugins install graphprotocol/totalreclaw-hermes --enable
     # Manifest write requires ANOTHER restart for the tools to bind. Re-emit the
     # surface-matched restart line from step 4 ONCE and wait for the user.
     NEEDS_SECOND_RESTART=yes
   fi
   ```
   - If `NEEDS_SECOND_RESTART=yes`: re-emit the matching restart line from the step-4 matrix verbatim (same surface — Telegram / docker / systemd / launchd / standalone), wait for the user's continuation reply, then re-run the `tools list | grep` probe ONE MORE time.
   - If after the second restart `tools list` still shows no `totalreclaw`: surface the failure verbatim (`> Couldn't bind the totalreclaw plugin tools after install. Check 'hermes plugins list' / 'hermes tools list' and the gateway logs.`) and **stop** — do NOT proceed to Account setup with no tools bound (the `totalreclaw_pair` call below would fail and there is no CLI fallback per the phrase-safety rule).
   - If the probe passed on the first restart: proceed silently to Account setup.

The install procedure ends here. Pairing (Account setup) makes TotalReclaw Hermes' memory provider, and Account setup step 5 checks it.

> **Do NOT run `hermes tools disable memory`** (older versions of this guide had it as step 7). On Hermes 0.22+ it also gates off the memory provider's tools and its system-prompt block, so the agent stops seeing TotalReclaw and writes to local memory files instead. Hermes' built-in memory is meant to stay on beside TotalReclaw (see [Compatibility](#compatibility-with-hermes-built-in-memory)).

---

## Account setup

The agent never generates the recovery phrase. Only the user's browser does, via the `totalreclaw_pair` tool.

**This section starts AUTOMATICALLY after the user confirms `done` to the install-procedure step 4 `/restart` request.** Do not ask "want me to set up your account?". The user already consented by sending the install prompt.

> **Precondition (#390):** `totalreclaw_pair` is a *plugin-registered tool*, not a CLI subcommand — it only resolves once the plugin has bound after gateway restart. Install-procedure step 5 (`Verify the plugin tools bound after restart`) is the gate; do NOT skip it. If step 5 reported a fallback restart was needed, only enter this section after the SECOND restart and after the verify probe returned `totalreclaw` present in `hermes tools list`.

1. Check whether credentials already exist:
   ```bash
   test -s ~/.totalreclaw/credentials.json && echo "already set up"
   ```
   If present, run step 5 below (silently), then emit `✓ TotalReclaw is already set up.` and skip to the Post-setup section below.

   > **cred-2 (keychain wrap):** on a desktop with an OS keychain, the `mnemonic` /
   > `recovery_phrase` field in `credentials.json` holds a non-secret marker
   > (`__keychain__:v1:0x<eoa>`), NOT the phrase — the phrase lives in the OS
   > keychain. On headless/container hosts (or with `TOTALRECLAW_NO_KEYCHAIN=1`)
   > the field holds the plaintext phrase as before. Either way the existence
   > check above is unchanged, and the agent must never read, echo, or pipe that
   > field — see the phrase-safety rules below.
2. Call the `totalreclaw_pair` tool. **Inputs: none required.** The tool defaults to `mode=either` — the browser pair page will render BOTH "Generate new" and "Import existing" tabs so the user picks at pair time. Do NOT pass `mode=generate` or `mode=import` explicitly unless the user has specifically asked for one path (e.g. *"restore my account"* → `mode=import`). The tool returns a JSON object: `{url, pin, expires_at_ms}`.
3. Emit ONE user-visible line containing the URL and PIN verbatim:

   > Open `<url>` in your browser. Enter PIN `<pin>`. On the page, choose **Generate new** (creates a fresh 12-word recovery phrase) OR **Import existing** (paste a phrase you already have). This link expires in 10 minutes (at `<HH:MM UTC>` — from the tool's `expires_at_ms`). Reply `done` once the page says it's sealed.

   Do not paraphrase the URL or PIN. Do not invent values when the tool fails — surface the failure verbatim and stop. **This single line is the ENTIRE browser instruction.** Do not follow it with a numbered list restating the steps, a "what's done so far" recap, or a trailing "waiting for you to finish…" / "standing by…" line — the line already names every step and `Reply \`done\`` already encodes the wait (auto-QA #340 / #342).
4. After the user replies `done`, re-check `~/.totalreclaw/credentials.json`. Present → continue to step 5. Absent → the PIN expired; call `totalreclaw_pair` again and resend step 3 once.
5. **(SILENT — emit nothing except the one question in the table below.) Check that TotalReclaw is Hermes' memory provider.** Pairing activates it on its own when no other provider is configured, and leaves another provider in place for the user to decide; that activation is best-effort, so confirm it here. Without the provider, Hermes' native per-turn recall and the mirroring of its built-in memory writes don't route through TotalReclaw (the plugin's own hooks still save and recall, so nothing is lost). Hermes runs exactly **one** external memory provider at a time, always next to its own built-in memory. Probe what's active:

   ```bash
   # After a restart the shell is new: re-run Install step 1 first if HERMES_PYTHON is unset.
   TR_CLI="$(dirname "$HERMES_PYTHON")/totalreclaw"
   ACTIVE_PROVIDER=$("$TR_CLI" memory-status --hermes-home "${HERMES_HOME:-$HOME/.hermes}" 2>/dev/null | sed -n 's/.*"provider": *"\([^"]*\)".*/\1/p')
   # Reads memory.provider from config.yaml; `hermes config get memory.provider` returns the same value.
   ```

   Branch on the result:

   | `ACTIVE_PROVIDER` | Agent behaviour |
   |---|---|
   | `"none"` / `""` / `"totalreclaw"` | Silently run `"$TR_CLI" activate-memory-provider`. No prompt. It installs the provider file at `~/.hermes/plugins/totalreclaw/__init__.py` and sets `memory.provider: totalreclaw`, leaving Hermes' built-in memory on (if a TotalReclaw version before 2.5.3rc4 turned the built-in stores off, it turns them back on). It edits `config.yaml` in place, keeps your comments, checks the result parses before writing it, and repairs the duplicate `user_profile_enabled` key that versions before 2.5.3 could leave behind. Do NOT add `--exclusive` unless the user asked for TotalReclaw to be their only memory (see [Compatibility](#compatibility-with-hermes-built-in-memory)). |
   | Anything else (`honcho`, `byterover`, `openviking`, ...) | Emit ONE line: *"Hermes is currently using `<X>` as the memory provider. Switch to TotalReclaw (E2E-encrypted, on-chain, cross-device) or install TotalReclaw as tools-only? Reply `switch` or `tools-only`."*<br/>**Branch:**<br/>• `switch` → warn first (*"Switching deactivates `<X>` — your existing `<X>` memories stay on disk but won't be queried by the agent until you switch back."*), then run `"$TR_CLI" activate-memory-provider`.<br/>• `tools-only` → run `"$TR_CLI" install-memory-provider` (provider file only, no activation). TR tools remain available; the active provider stays `<X>`. Skip the restart line in Post-setup. |

   If the command refuses because `~/.hermes/plugins/totalreclaw/__init__.py` is not a TotalReclaw-managed file, that file is the git wrapper from the install step-5 fallback: re-run with `--force` (the provider file also registers the tools, so nothing is lost). The commands are idempotent; re-running on an installed provider file is safe.

   Check it took: `"$TR_CLI" memory-status --hermes-home "${HERMES_HOME:-$HOME/.hermes}"` → `{"provider": "totalreclaw"}`. The new provider loads at the next gateway start, which is why the Post-setup block ends with a restart line.

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
>
> Last step: restart Hermes once more, the same way as before (`<restart>`), so Hermes starts using TotalReclaw as its memory provider.

(`<tier>`, `<used>`, `<limit>` from `totalreclaw_status` — do not invent the numbers, and do not name the underlying chain/network to the user.)

`<restart>` is the same surface you picked in Install step 4:

| Surface | `<restart>` |
|---|---|
| Telegram / Discord / Slack / other chat app | `send /restart here` |
| `hermes chat` inside docker, no chat app | `run docker restart <container-name> from a host shell, then reopen hermes chat` |
| `hermes chat` with a gateway service (systemd / launchd) | `run hermes gateway restart in a second terminal` |
| Standalone `hermes chat`, no gateway daemon | `exit and re-run hermes chat` |

Never tell a chat-app user to run shell commands for this, and never tell a docker or CLI user to send `/restart`.

**Emit this block VERBATIM on the `done` turn**, filling in only the placeholders. Do not paraphrase, do not substitute the tier numbers (never render `<limit>` as "50,000 writes", "unlimited", or any figure not returned by `totalreclaw_status` — and if `totalreclaw_status` would show a higher staging limit, still quote what it returns, not a guess), do not add a preamble ("All green. Here's the summary:", "Everything looks good!", "✓ Setup complete. Tier info:"), and do not run any verification command (`hermes doctor`, `hermes plugins list`, a second diagnostic `totalreclaw_status`) on this turn — the credentials check in Account setup step 4 and the provider check in step 5 are the ONLY verification. Adding bullets, recaps, or "standing by…" lines inflates the transcript and is a setup-flow violation (auto-QA #340 / #342).

This block ships on first successful pair completion. On subsequent install runs where credentials already exist (early-exit in Account setup step 1), instead emit just:

> ✓ TotalReclaw is already set up. Account status: `<tier>` tier, `<used>` / `<limit>` memories used.

If step 5 changed anything on this run (the provider wasn't `totalreclaw` before, or `activate-memory-provider` printed `builtin restored: True`), add the same `Last step: restart Hermes once more…` line.

---

## Recall behaviour

**For any user query that asks about prior facts about themselves** — "what do you know about me", "what are my preferences for X", "did I tell you about Y", etc. — the agent MUST call `totalreclaw_recall` even if it appears to have the answer in its current context.

Rationale: Hermes's built-in memory layer auto-loads a local `~/.hermes/memories/USER.md` cache into the agent's system context at session start. That cache may contain answers from prior sessions, but it is NOT the canonical cross-session store. TotalReclaw on chain IS. Always querying through `totalreclaw_recall` ensures the integration is exercised + that the on-chain memory is the source the user is recovering against.

If `totalreclaw_recall` returns matching facts: use those as the answer.
If it returns nothing: say so honestly, do not fabricate from context.

If you see *"Memory lookups are paused"*, your memories are safe. TotalReclaw can't search them right now, either because the account used its monthly read allowance or because of a short rate limit. New memories keep saving. Lookups resume on their own (the notice says when), and the upgrade link lifts the monthly limit. Until then, the agent will say lookups are paused rather than claim you have no memories.

---

## Phrase safety

These are hard rules. None of them are negotiable.

1. The recovery phrase is generated only in the user's browser via `totalreclaw_pair`. The agent never sees it.
2. The agent does not run `totalreclaw setup` from its shell. The CLI refuses to run from an agent runtime (exit code 3 with a stderr redirect to `totalreclaw_pair`). The agent does not bypass the gate with `--allow-non-tty`.
3. The agent does not `cat ~/.totalreclaw/credentials.json` or read the `mnemonic` / `recovery_phrase` field from any file at any time.
4. The agent does not echo, paraphrase, or ask the user to paste a recovery phrase in chat. If the user pastes one, the agent tells the user the phrase is compromised and calls `totalreclaw_pair` with `mode=generate` to mint a fresh wallet.
5. The agent does not pass the recovery phrase to any shell command, log line, model prompt, or tool argument.

---

## Local phrase migration (Option E Phase 2)

The first time the plugin loads after upgrading, a host that holds the recovery
phrase in `~/.totalreclaw/credentials.json` (plaintext or OS-keychain-wrapped) is
**automatically migrated** to a `derived-bundle-v1` credential: the phrase is
replaced with derived vault keys and a signing key, and is no longer stored on that
host. This is transparent — no user action required, no re-pairing — and it prints
a one-time notice to stdout the moment it happens:

> Your recovery phrase has been replaced on this machine with derived vault
> credentials, and is no longer stored here. Your phrase is still your only
> recovery path — make sure you have your written copy. A backup of the
> previous credentials file is at ~/.totalreclaw/credentials.json.bak; delete
> it once you have confirmed your phrase backup.

**What changes:**

- `~/.totalreclaw/credentials.json` moves to `~/.totalreclaw/credentials.json.bak`
  (mode `0600`), and a fresh `version: 2` file takes its place.
- Remember, recall, forget, export, pin/unpin, retype, and set-scope all keep
  working identically — they use the same derived vault keys, byte-for-byte,
  whether the host holds the phrase or the bundle.
- **After migration, the phrase can no longer be recovered from this host.** The
  `jq -r .mnemonic credentials.json` trick (or reading it from the OS keychain)
  stops working — there is nothing phrase-shaped left to read. Your own written or
  otherwise-backed-up copy of the phrase is your only recovery path going forward.

**The `.bak` file:** kept for one release cycle as a safety net for exactly the
"oops, I didn't actually have my phrase backed up" case — restoring it (plus
downgrading the package to the pre-migration version) gets you back to the
phrase-on-host state. It is chmod `0600` but is still a plaintext copy of your root
key sitting on disk; **delete it once you've confirmed your own phrase backup**:

```bash
rm ~/.totalreclaw/credentials.json.bak
```

A future release removes `.bak` retention entirely (immediate destruction, no
recovery window) — that switch is a deliberate, evidence-gated decision, not
something this guide's current instructions assume.

**Opting out:** set `TOTALRECLAW_NO_AUTO_MIGRATE=1` to keep the phrase on this host
indefinitely (see [env-vars-reference.md](env-vars-reference.md)). You might do this
if you have tooling that depends on reading the phrase from `credentials.json` — but
note this means the phrase stays exposed to anything that can read this host's
filesystem.

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
| `totalreclaw_set_extraction_model` | Change the model that extracts memories, only when the user asks for a specific one (`default` resets). Applies next turn, no restart. |
| `totalreclaw_top_up` | Buy a **one-time top-up pack** of extra memories (`1000` / `5000` / `10000`) — returns a Stripe checkout URL. **Route here whenever the user asks to buy extra memories, a top-up pack, or one-time credits (e.g. before a big import, or when over/near quota).** This is DISTINCT from the Pro subscription upgrade — never substitute the upgrade flow when the user asked for a pack; offer the upgrade only as an additional option. |
| `totalreclaw_export` | Export the full vault. |
| `totalreclaw_set_scope` | Switch active scope. |
| `totalreclaw_retype` | Change a memory's taxonomy type. |

---

## Extraction model

TotalReclaw extracts memories with the LLM provider Hermes already uses (same key), and picks the model automatically: a tested default per provider (on z.ai: `glm-5.3-flash` on the Coding Plan, `glm-4.5-flash` on the Standard/PAYG endpoint). If that model isn't available on the user's plan, it moves to the next candidate and finally to the chat model. There is nothing to configure. Extraction calls go to the z.ai endpoint Hermes is configured for (`model.base_url` in `config.yaml`, or `GLM_BASE_URL` / `ZAI_BASE_URL` in `.env`); a configured endpoint is never switched, so Coding-plan calls stay on the Coding endpoint.

If the user asks for a specific model to extract or store memories, call `totalreclaw_set_extraction_model` with that model id (`default` goes back to automatic). It applies from the next turn: no `.env` edit, no restart. `totalreclaw_status` reports the model in use and why (`extraction_model.source`). An operator-set `TOTALRECLAW_EXTRACTION_MODEL` takes precedence over the user's choice.

---

## Diagnostics

- Plugin not loaded after the restart: `hermes tools list` should show `totalreclaw` as enabled. If absent, check `hermes plugins list`: `not enabled` means step 3 was skipped (run `hermes plugins enable totalreclaw` and restart). Otherwise re-check that step 2 actually installed into the Hermes environment (not user-site, not the bundled Python's own site-packages) and that step 4 restarted the gateway process; as a last resort, run the step 3 git-plugin fallback.
- **Auto-extraction in single-shot `hermes chat -q` (rc6 QA F9/#377):** a one-shot CLI invocation exits as soon as the turn completes, so the extraction hook can't drain synchronously at interpreter shutdown (the async runtime is already torn down). This is not a bug — the client persists the unprocessed messages and drains them at the **next** session start. For real-time extraction guarantees, run a long-lived `hermes gateway` (daemon) rather than per-turn `hermes chat -q`.
- **When the session summary (Session Crystal) is written:** when a conversation ends with at least 4 turns, or when the next message arrives after `TOTALRECLAW_SESSION_IDLE_MINUTES` of silence (default 60), which covers gateway chats such as Telegram that Hermes never ends. Shorter stretches are kept and added to until one qualifies. `Session Crystal stored` in `agent.log` confirms one.
- `totalreclaw_pair` returns a URL that 404s on the browser: the relay is unreachable — check `TOTALRECLAW_SERVER_URL` env var matches the user's intended channel (prod default, or staging from the QA prompt).
- `totalreclaw_pair` returns a 5xx: ask the user to retry; report the error code verbatim.
- Tool not bound after install: enable the plugin (step 3) and restart once per Install procedure step 4. If still missing, the package install hit the wrong Python; re-resolve `HERMES_PYTHON` (step 1) and re-install.
- **The agent tries to issue `/restart` itself / says "I'll restart now":** it can't. Hermes only executes slash commands typed by the **user** — agent output is never parsed for commands, on any surface. The agent's job is to emit the right per-surface restart instruction (Install-procedure step 4 matrix) and then wait for the user's continuation reply.
- **`hermes gateway restart` from inside a docker container returns "no running gateway" or fails silently:** the container has no systemd / launchd service supervisor to respawn the gateway after a self-exit. The correct restart for docker is `docker restart <container-name>` from the **host** shell, not from inside the container.
- **`/restart` returns "not authorized" / "command not found", or the surface has no supervisor to respawn the gateway:** the user restarts the gateway out-of-band — `hermes gateway restart` (native) or `docker restart <container-name>` (docker, from the host). **Do NOT suggest `/new` as a shortcut** — a new session reuses the gateway's boot-time tool registry and will NOT pick up the freshly-installed plugin (Hermes discovers plugins once, at gateway startup).
- **After restart, agent doesn't continue setup:** if the user replied only `done` without a continuation phrase, the agent may not infer it should resume. The rc.27+ proactive setup nudge fires on the first turn of every unconfigured session, so re-engaging the conversation with anything (even another bare message) should trigger the new-vs-restore question. If it doesn't, ask the user to type "Continue setting up TotalReclaw" verbatim.
- **All TotalReclaw tools/hooks vanished silently after a Hermes update or `hermes pm repair` (issue #638):** a Hermes update or `hermes pm repair` can replace the environment Hermes imports from (on Hermes 0.22+, a new generation of the managed venv under `~/.hermes/installs/…`; on older Hermes, a regenerated `hermes-agent/venv`), and the new one does **not** carry pip-installed packages outside Hermes' own dependency set. Verify from a **neutral cwd** (running it from `$HOME` or the checkout's parent masks the failure via a namespace-package pickup), with `HERMES_PYTHON` resolved by Install-procedure step 1:
  ```bash
  ( cd /tmp && "$HERMES_PYTHON" -c "import totalreclaw" )
  ```
  If that raises `ModuleNotFoundError`, re-run Install-procedure step 2 with `PIN="totalreclaw==<the version you had>"` and restart the gateway. The memory-provider file (`~/.hermes/plugins/totalreclaw/__init__.py`, written at pairing or by Account setup step 5) self-heals exactly this on the next gateway start: it reinstalls the version that wrote it, with its dependencies, into the environment Hermes imports from (a pre-release only if that version is one), then re-checks the import. If that fails too, the agent is told memory is OFFLINE on the first turn of each session and `totalreclaw_status` returns `"degraded": true` with the error and the exact fix command. Older installs carrying a previous sidecar can take the one-shot upgrade: `"$(dirname "$HERMES_PYTHON")/totalreclaw" install-memory-provider --force`, or simply re-pair. Credentials and vault data are never affected — this is an availability failure only.
- **The gateway log says `Memory provider 'totalreclaw' is configured but not installed`**, or `memory-status` says `totalreclaw` but Hermes reports no active provider: `config.yaml` names TotalReclaw but `~/.hermes/plugins/totalreclaw/__init__.py` isn't the provider file (usually the git wrapper from the install step-5 fallback is there). Run `"$(dirname "$HERMES_PYTHON")/totalreclaw" activate-memory-provider --force` and restart the gateway.
- **`memory-status` says `none` after pairing:** the activation pairing attempts is best-effort and was skipped or failed. The reason is in the gateway log (`memory provider activation`) or, for relay pairing, in `~/.totalreclaw/.pair_sidecar.log`. Run Account setup step 5.
- **The agent saved "remember X" with Hermes' built-in `memory` tool instead of `totalreclaw_remember`:** nothing is lost. TotalReclaw copies the built-in tool's new and edited entries into the vault (Hermes 0.22+, with TotalReclaw as the active provider). Its system-prompt block tells the agent to prefer `totalreclaw_remember`; if it keeps choosing the built-in tool, report it.
- **Checking that built-in memory reaches TotalReclaw:** the gateway log (`~/.hermes/logs/agent.log`) has one `memory mirror: <add|replace> target=<user|memory> stored=<0|1> forgotten=<n>` line per built-in add or edit (an edit that keeps the old memory adds `; old entry kept (not the same fact)`), one `memory mirror: remove target=<user|memory> removed from Hermes profile; kept in TotalReclaw` line per removal, and one `memory reconcile: first_run=… added=… changed=… removed=… forgotten=… skipped=… deferred=…` line (or `memory reconcile: no changes`) per session start and end. `deferred` above 0 means the rest is picked up next session.
- **The user wants TotalReclaw to be their only memory** (no local `USER.md` / `MEMORY.md`): run `"$(dirname "$HERMES_PYTHON")/totalreclaw" activate-memory-provider --exclusive` and restart the gateway. To undo it: `hermes config set memory.memory_enabled true`, `hermes config set memory.user_profile_enabled true`, restart.
- **Hermes says `config.yaml` has a formatting error / `found duplicate key "user_profile_enabled"` and runs on default settings:** a TotalReclaw version before 2.5.3 activated the memory provider by appending keys to the `memory:` block. Install 2.5.3 or later (step 2), then run `"$(dirname "$HERMES_PYTHON")/totalreclaw" activate-memory-provider` once — it rewrites those keys in place and removes the duplicate.
- **`hermes chat` fails with `Gateway connection failed; no local fallback (TypeError)` right after installing TotalReclaw:** a TotalReclaw version before 2.5.3 capped `websockets` below 14 and downgraded Hermes' copy. Install 2.5.3 or later (step 2), which allows Hermes' version, and restore it: `"$PIP_PYTHON" -m pip --python "$HERMES_PYTHON" install "websockets>=15"`.

---

## Memory provider modes

Two TotalReclaw paths exist into Hermes; they coexist but serve different surfaces.

| Mode | What's active | When to use | How to set |
|---|---|---|---|
| **Active provider** | Generic plugin tools + lifecycle hooks **+** `MemoryProvider` hooks (`on_pre_compress`, `on_memory_write`, `on_session_end`, `on_turn_start`). `on_memory_write` copies the built-in memory tool's new and edited entries into TotalReclaw (removals are kept). | The user has no other memory provider configured, or wants TR to be the canonical cross-session store. Default for fresh installs. | `totalreclaw activate-memory-provider` |
| **Tools-only** | Generic plugin tools + lifecycle hooks only. No `MemoryProvider` hooks — they fire on the user's other active provider (Honcho / Byterover / OpenViking / ...). | The user already has another provider configured and wants to keep using it alongside TR's chat tools. | `totalreclaw install-memory-provider` (no `--activate`) |

The two install commands are idempotent. Re-running on an already-installed sidecar overwrites with the current shim content (catches package upgrades) without disturbing the rest of the install. The sidecar refuses to clobber a hand-edited file unless `--force` is passed.

Switching providers manually (post-install):

```bash
# Switch TO TotalReclaw
totalreclaw activate-memory-provider

# Switch BACK to another provider (after a `switch` choice the user regrets)
hermes memory set-provider <name>   # upstream Hermes CLI
```

Switching deactivates the previous provider — its memories remain on disk but the agent stops querying them until you switch back. New memories go through whichever provider is active at write time.

Status check:

```bash
totalreclaw memory-status              # JSON: {"provider": "totalreclaw"|"honcho"|"none"|...}
```

This grep-reads `~/.hermes/config.yaml` (or `$HERMES_HOME/config.yaml`) and does not depend on an upstream `hermes memory status --json` command (which is not yet released as of 2026-05-15 — see Path B spec §"Open questions for upstream").

---

## Compatibility with Hermes built-in memory

**TotalReclaw runs alongside Hermes' built-in memory** (`~/.hermes/memories/USER.md` and `MEMORY.md`). Hermes always keeps its built-in memory next to the one external provider (`hermes memory --help` calls it "always active"), and since 2.5.3rc4 `activate-memory-provider` leaves it on. The two have different jobs:

- **Hermes' built-in memory is the short profile loaded into every session** (a few KB, capped by `memory_char_limit` / `user_char_limit`). It is a local, per-container context cache: plaintext, not shared with the user's other agents, and wiped on container reset or `hermes memory reset`. It keeps the agent oriented when TotalReclaw recall is slow or unavailable.
- **TotalReclaw is the canonical cross-session store**, derived from your recovery phrase: end-to-end encrypted, on chain, shared with the user's other agents, and recoverable on any device or container by re-importing the phrase via `totalreclaw_pair`. The agent is told to remember and recall with `totalreclaw_remember` / `totalreclaw_recall`.
- **New and edited built-in entries are copied into TotalReclaw; removals are not.** When the agent uses Hermes' `memory` tool, an add is stored and an edit (replace or patch) stores the new text. The old memory is forgotten only when the edit is clearly the same fact reworded (for example "prefers tea" → "prefers green tea") and it clearly matches one memory in the vault; when entries are merged into one, or the text changes a lot, the old memories are kept. **A removal never deletes anything from TotalReclaw:** the built-in stores are size-capped (about 2,200 and 1,375 characters), and Hermes removes or merges entries to make room, so a removal usually means "evicted from the short profile", not "forget this".
- **To forget something, the agent calls `totalreclaw_forget`** (after `totalreclaw_recall` to find it), and also removes it from the built-in memory if it's there. Its system-prompt block says so.
- **Changes Hermes doesn't report are caught at session start and end.** Hermes (0.22.5) doesn't pass writes from its background memory review, or hand edits, to memory providers. So TotalReclaw also compares `USER.md` / `MEMORY.md` with the entries it last saw, when a session starts and ends, and applies the same rules: new entries are stored, a same-fact edit replaces the old memory, a merge keeps the old memories, and a removed entry stays in TotalReclaw. The first time, it saves every existing entry once. It does at most 50 changes per run (the rest follow next time), skips entries Hermes flags as unsafe, and masks secrets the way Hermes does before anything leaves the machine. What it last saw is kept in `~/.totalreclaw/hermes-memory-snapshot-<id>.json` (readable only by you). `TOTALRECLAW_HERMES_MEMORY_RECONCILE=0` in the gateway's environment turns this off.
- **For any user-facing recall query, the agent calls `totalreclaw_recall`** even when USER.md may already contain a relevant fact in context (see Recall behaviour). This forces the answer through the canonical TotalReclaw read path.

**To make TotalReclaw the only memory**, run `activate-memory-provider --exclusive` and restart. It also sets `memory.memory_enabled: false` and `memory.user_profile_enabled: false`, and Hermes then stops loading and writing `USER.md` / `MEMORY.md` (`hermes memory status` shows both as disabled). That also drops the always-loaded profile and the background review's memory writes. A later plain `activate-memory-provider` keeps this choice. TotalReclaw versions before 2.5.3rc4 did this on every activation. The first gateway start on 2.5.3rc4 or later turns those stores back on, once per Hermes home (gateway log: `turned Hermes' built-in memory back on … Takes effect at the next gateway restart`); running `activate-memory-provider` does the same.

Do **not** also run `hermes tools disable memory`, in either mode: on Hermes 0.22+ it gates off the memory provider's tools and system-prompt block too, so the agent loses sight of TotalReclaw.

Feedback on how the two memories work together: <https://github.com/graphprotocol/totalreclaw/issues>. Changes to the built-in memory itself belong upstream at <https://github.com/NousResearch/hermes-agent/issues>.

---

## Out of scope for this guide

User-facing topics (how the user records their recovery phrase, what to do on a lost phrase, pricing tiers, billing) live in a separate user guide that will be linked here when it ships.
