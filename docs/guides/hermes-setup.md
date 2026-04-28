# TotalReclaw for Hermes

TotalReclaw gives your Hermes agent encrypted, persistent memory. The recovery phrase stays in your browser — the relay only ever sees ciphertext. The fastest path is one chat message: the agent installs the package, asks you to restart the gateway once (Hermes doesn't auto-reload), then hands you a URL + PIN; you complete account setup in your browser.

If you already have Hermes running, paste this into chat:

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>**

What happens next: the agent fetches this guide, runs the install commands described below, asks you to restart the gateway, then calls a tool that gives you an account-setup URL + 6-digit PIN. You open the URL, generate or restore a 12-word recovery phrase in your browser, confirm the PIN, and your account is ready.

> **One-shot CLI users heads-up.** If you're invoking Hermes via `hermes chat -q "..."` (single-prompt CLI that exits after the response), the account-setup step needs a longer-lived process to keep its WebSocket alive while you finish the browser flow. See [Account setup requires daemon mode](#account-setup-requires-daemon-mode) below for the workaround.

## Provenance — what you're installing, who from

- **Source:** <https://github.com/p-diogo/totalreclaw> (MIT-licensed, public).
- **Publisher:** `p-diogo` on GitHub. Packages are published from GitHub Actions in that same repo.
- **Distribution:**
  - PyPI — `totalreclaw`. Inspect with `pip show totalreclaw` or visit <https://pypi.org/project/totalreclaw/>.
  - Hermes plugin manifest — published from <https://github.com/p-diogo/totalreclaw-hermes>, registered via Hermes' Git-based plugin discovery.
- **Tooling docs:** the install commands below use Hermes' standard plugin manager (`hermes plugins install`, see [Hermes docs](https://github.com/NousResearch/hermes-agent)) and PyPI's `pip install` ([PyPA docs](https://pip.pypa.io/)) — there's no curl-pipe-bash, no custom installer script.
- **Crypto:** account setup uses standard browser primitives — x25519 ECDH (WebCrypto), AES-256-GCM (WebCrypto), HKDF-SHA256, BIP-39 mnemonics. See the [What's happening](#whats-happening-for-the-human-reader) section below for the full handshake.

If anything in the install flow looks unfamiliar, please pause and ask the agent what each command does — that's encouraged, not a problem.

---

## Notes for a Hermes agent fetching this URL

You're a helper here, not a script. The user pasted a setup URL because they want collaborative install — confirm what each command does, ask before destructive steps (gateway restart, account creation), and surface the install version when it lands.

A note on chat verbosity: Hermes users generally prefer short progress lines for install plumbing (a 5–7 line install transcript feels right for a healthy fresh install), with full detail available on request. So the default cadence is something like:

1. *"Installing TotalReclaw…"*
2. *"✓ Installed (totalreclaw <version>)"*
3. *"Please restart your Hermes gateway: <restart-command>. Reply `done` once it's back up."*
4. *"I've disabled Hermes' built-in `memory` tool — TotalReclaw is now your primary memory."* (only if the disable command succeeded)
5. *"Open <url> in your browser. Generate a new 12-word recovery phrase there or paste an existing one. Confirm PIN <pin>. Reply `done` once the page says it's sealed."*
6. *"✓ Your TotalReclaw account is set up. Ask me to remember or recall anything."*

If a step actually fails, surface the failure plainly with what you're trying next. Examples: *"Install failed: externally-managed-environment. Retrying with --break-system-packages."* / *"Plugin installed but tools didn't bind after restart. Asking for a second restart."*

If the user asks "what's it doing?" or "show me the commands," walk them through the plumbing in detail — Python interpreter detection, venv selection, pip retries, all of it. The default-short cadence is a preference, not a constraint.

The user's recovery phrase never enters chat or your shell stdout — it's handled in the browser by design (described below).

### Step 1 — Install the plugin + Python package

The Python tool implementations need to land in the same Python environment Hermes runs from — otherwise the gateway will reload, find no `totalreclaw_*` tools, and you'll be stuck. So the first thing is to identify the right interpreter.

#### Step 1a — Find the Hermes Python

Stash the Hermes interpreter path in `$HERMES_PYTHON`:

```bash
# Containerized Hermes (Docker / docker-compose / supervised container).
# Run this INSIDE the container's shell context (e.g. `docker exec -it
# <container> bash`, then this loop). The container's PATH already points
# at the venv, so `which python3` resolves to the right interpreter.
# Example output: /opt/hermes/venv/bin/python3
HERMES_PYTHON="$(command -v python3)"

# Native install with a known venv (Hermes was set up with `python3 -m
# venv ~/.hermes/venv` or similar). Override the autodetect:
# HERMES_PYTHON=~/.hermes/venv/bin/python3

# Bare-metal install with a system Python (no venv). The default
# `python3`/`pip` should resolve fine; the autodetect above still works.

# Sanity check — if this prints nothing, set HERMES_PYTHON manually:
echo "Hermes Python: $HERMES_PYTHON"
"$HERMES_PYTHON" -c "import sys; print(sys.prefix)"
```

If you can't determine the interpreter from the shell context (managed service, restricted shell), don't guess — fall back to the [managed-service flow](#managed-hermes-service-no-terminal-no-agent-shell).

#### Step 1b — Install both halves into the right environment

These two commands install the plugin manifest and the Python tool implementations.

```bash
hermes plugins install p-diogo/totalreclaw-hermes --enable
"$HERMES_PYTHON" -m pip install --pre totalreclaw
```

**What each does:**
- `hermes plugins install p-diogo/totalreclaw-hermes --enable` — registers the plugin manifest (Hermes 2026.4.16+ uses Git-based plugin discovery). Standard Hermes plugin install — see [Hermes plugin docs](https://github.com/NousResearch/hermes-agent#plugins).
- `"$HERMES_PYTHON" -m pip install --pre totalreclaw` — installs the Python tool implementations into Hermes' venv. Using `"$HERMES_PYTHON" -m pip ...` (instead of bare `pip install`) guarantees the package lands in the SAME environment Hermes loads tools from after restart.

Common error paths and what to try (no need to narrate the retry unless it also fails):

- `externally-managed-environment` (Debian/Ubuntu/Docker system Python): retry the `pip install` with `--break-system-packages`.
- `pip: command not found` / `No module named pip` (container shipped without pip): bootstrap with `"$HERMES_PYTHON" -m ensurepip --upgrade`, then retry.
- `command not found` / ENOENT / "not authorized" (managed service or restricted shell): the Hermes CLI isn't reachable. Let the user know, ask them to install `p-diogo/totalreclaw-hermes` and the `totalreclaw` Python package via their service's plugins UI, then continue.

For an RC build, `--pre` resolves to whatever release candidate is latest on PyPI right now. To pin a specific RC (QA against a known-good build, restoring after a regression in a newer RC), drop `--pre` and use an explicit version specifier — see [Targeting a specific RC](#targeting-a-specific-rc).

#### Step 1c — Verify the package landed in the right Python

```bash
"$HERMES_PYTHON" -m pip show totalreclaw | head -2
```

If `pip show` reports the package, capture the version string and confirm to the user (`✓ Installed (totalreclaw <version>)`). If it errors with `Package(s) not found`, the install landed in a different environment — re-detect `$HERMES_PYTHON` and re-run 1b once.

### Step 2 — Ask the user to restart the gateway

Hermes doesn't auto-reload on plugin install (yet — tracked upstream). Pick the right restart command for the user's deploy:

- Native: `hermes gateway restart`
- Docker self-host: `docker restart <container-name>` (substitute their actual name; `docker ps` shows it)
- Managed service: their hosting service's restart control
- systemd / launchd supervised: `kill -USR1 $(cat ~/.hermes/gateway.pid)` triggers a graceful-drain restart

Don't run the restart yourself — your shell is inside the gateway you'd be restarting. Wait for the user's `done`.

### Step 3 — Disable Hermes' built-in memory tool (recommended)

Hermes ships with its own built-in `memory` tool. Running both it and TotalReclaw causes a silent bug: depending on which tool the LLM picks for a given "remember X" / "recall X" intent, your conversation context can get split between Hermes' MEMORY.md and TotalReclaw's encrypted vault. Neither sees the full picture.

```bash
hermes tools disable memory
```

If this succeeds, let the user know that built-in memory is now off and TotalReclaw is their primary memory tool — and that they can re-enable it any time with `hermes tools enable memory` (not recommended while TotalReclaw is installed). If the command errors with `command not found` or "tool not found" (older Hermes versions, managed services that don't expose the CLI, or built-in memory was already removed), skip this step and continue — the rc.25 tool-description bias still steers the LLM toward `totalreclaw_remember`.

### Step 4 — Check whether they already have an account

```bash
test -s ~/.totalreclaw/credentials.json
```

If that file exists and is non-empty, their TotalReclaw account is already set up — let them know, and stop.

### Step 5 — Walk them through account setup (browser flow)

Account setup happens in the user's browser. The agent's role is to call the `totalreclaw_pair` tool and relay the URL + PIN it returns; the recovery phrase stays in the browser by design.

**Lifecycle check first.** This step opens a short-lived WebSocket to the relay that must stay alive while the user completes the browser handshake. If you're running inside a one-shot agent process (`hermes chat -q "..."`, single-prompt CLI, transient invocation that exits as soon as the LLM responds), the WebSocket dies before the user can finish entering their phrase and the browser POST returns 404. If you detect that pattern, let the user know:

> *Setting up your TotalReclaw account needs a long-lived Hermes process to keep the session alive while you finish the browser flow. One-shot `hermes chat -q "..."` invocations exit too quickly. Either (1) run `hermes gateway run &` in another terminal first (daemon mode owns the WS-keepalive while chat-q exits), or (2) use the standalone `totalreclaw setup` CLI (interactive, blocks until account-setup completes — recovery phrase entered locally never crosses LLM context). Once your account is set up, daily operations (`hermes chat -q`, `--resume`, etc.) work normally.*

Otherwise, before calling the tool, mention what's about to happen: *"Want me to set up your TotalReclaw account? I'll generate an account-setup URL — you'll create or restore your recovery phrase in the browser, and I'll confirm once it's done."* Wait for their go-ahead.

Then call `totalreclaw_pair`. It returns `{url, pin, expires_at_ms}`. Relay these clearly:

> *Open <url> in your browser. Generate a new 12-word recovery phrase there or paste an existing one. Confirm PIN <pin>. Reply `done` once the page says it's sealed.*
>
> *Your phrase stays in your browser — the relay only ever sees ciphertext.*

### Step 6 — Confirm

After the user says `done`, re-check:

```bash
test -s ~/.totalreclaw/credentials.json
```

If the file is now present, their account is set up — let them know they can start asking you to remember or recall things. If the file is still missing, the PIN probably expired; call `totalreclaw_pair` again and resend the URL.

### Why the recovery phrase stays in the browser

This is a design choice driven by how LLM context works: anything entered in chat or written to your shell stdout becomes part of the model's context (and any logs that capture it). To keep the recovery phrase out of that surface, account setup does the phrase entry in the user's browser, encrypts the phrase against the gateway's ephemeral key before sending, and only the relay (server-side) ever decrypts. The agent never needs to see the plaintext phrase to do its job.

In practice, this means: the `totalreclaw_pair` tool is the only path the agent uses for account setup. There's a separate `totalreclaw setup` and `hermes setup` CLI for users to run themselves at a terminal — those work fine when a human is at the keyboard, but the agent shouldn't invoke them, because their stdout would carry the phrase into LLM context.

If a user does paste their phrase in chat, that phrase is now in the conversation transcript and should be considered compromised. The friendly thing to do: let them know, and offer to call `totalreclaw_pair` with `mode=generate` to create a fresh wallet.

---

## What's happening (for the human reader)

1. `hermes plugins install p-diogo/totalreclaw-hermes --enable` — registers TotalReclaw's plugin manifest from its Git repo.
2. `pip install --pre totalreclaw` — installs the Python tool implementations into the Hermes venv. `--pre` resolves to the latest release candidate; drop it once a stable is promoted.
3. The agent asks you to restart the Hermes gateway. (Hermes does not yet auto-reload on plugin install — tracked upstream.) After restart, the new tools bind.
4. The agent calls `totalreclaw_pair`, which generates an ephemeral x25519 keypair on the gateway and a 6-digit PIN. You get a URL + PIN.
5. You open the URL. The account-setup page has two tabs: **Generate new** (the browser creates a fresh BIP-39 12-word phrase locally using `crypto.getRandomValues`) and **Import existing** (paste a phrase you already have). Pick one, confirm the 6-digit PIN, click seal.
6. The browser performs x25519 ECDH against the gateway's ephemeral pubkey, derives an AES-256-GCM key via HKDF-SHA256, encrypts the phrase locally, and POSTs ciphertext + nonce + its pubkey back. The gateway decrypts server-side and writes `~/.totalreclaw/credentials.json` (mode `0600`).
7. The recovery phrase never crosses the LLM context — not the chat transcript, not the agent's shell stdout, not any tool-call payload. Browser-side crypto keeps it isolated by construction.

---

## Prerequisites

- Hermes Agent v0.5.0+ (https://github.com/NousResearch/hermes-agent)
- An LLM provider configured in Hermes (zai / openai / anthropic / gemini)
- Python 3.11+
- An up-to-date browser with WebCrypto x25519 + AES-GCM (Safari 17.2+ or Chromium 133+)

---

## Account setup requires daemon mode

Setting up your TotalReclaw account uses a brief WebSocket session that needs to stay alive while you complete the browser-side phrase generation / import. This requires Hermes running in a long-lived process — typically `hermes gateway run` (daemon mode, recommended for any production memory use) OR a fully interactive `hermes chat` session that stays open while you finish the browser flow.

**Not supported** (for account setup specifically): `hermes chat -q "..."` one-shot CLI invocations. The Hermes process exits as soon as the LLM responds; the WebSocket dies with it; the browser POST that delivers your encrypted recovery phrase to the gateway then sees a closed session and returns 404.

Workarounds for one-shot CLI users:

1. **Use daemon mode**: `hermes gateway run &` then `hermes chat -q "Set up TotalReclaw for me, see <URL>"` — the daemon owns the WS-keepalive while chat-q exits.
2. **Use the standalone setup CLI**: `totalreclaw setup` (interactive, blocks until your account is set up — recovery phrase entered locally, never crosses LLM context).

Once your account is set up, all daily operations (`hermes chat -q`, `--resume`, etc.) work fine — the lifecycle constraint is account-setup-flow-specific.

This is tracked in [#170 — sidecar / IPC handoff to long-lived daemon for account setup](https://github.com/p-diogo/totalreclaw/issues/170) for a future improvement.

---

## Managed Hermes service (no terminal, no agent shell)

If you're on a managed / hosted Hermes service that doesn't expose host shell to the agent, install via the service's web UI instead:

1. In your service's control panel, find the **Plugins** panel and search for `totalreclaw` (or `p-diogo/totalreclaw-hermes`). Install and enable it. Most managed Hermes deployments handle the underlying Python-package install transparently as part of plugin enable.
2. If the service exposes a separate restart control, use it.
3. Return to chat and paste the same canonical message:

   > **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>**

   The agent will detect the plugin is already loaded, skip the install steps, and jump straight to account setup.

The browser-side crypto and account-setup flow are identical to self-hosted setups.

> Managed-Hermes coverage is still emerging — if your service doesn't expose `totalreclaw` in its plugins UI yet, ask their support to surface the `p-diogo/totalreclaw-hermes` Hermes plugin + `totalreclaw` Python package, or run a self-hosted Hermes instance for now.

---

## Fully manual (CLI only — last resort)

If you'd rather drive the install yourself (self-hosted only — managed services don't expose the host shell):

```bash
hermes plugins install p-diogo/totalreclaw-hermes --enable

# Use the SAME Python that Hermes runs from — never bare `pip install` unless
# you already know `which pip` and Hermes' interpreter agree.
#   Native venv install:
~/.hermes/venv/bin/python3 -m pip install --pre totalreclaw
#   Containerized Hermes (run from inside the container shell):
python3 -m pip install --pre totalreclaw
#   System-Python install (no venv, may need --break-system-packages):
python3 -m pip install --pre totalreclaw

# Restart the gateway. Pick the line that matches your setup:
hermes gateway restart                       # native install
docker restart <your-hermes-container>       # Docker self-host (substitute your actual container name)
# Managed service: use the service's restart control (web UI).
```

Then in chat: *"Set up TotalReclaw"* — the agent will call `totalreclaw_pair` and hand you the URL + PIN.

---

## Upgrading

If you were on plugin 3.3.1-rc.2 or Hermes 2.3.1rc2, after upgrading also run `pip install --force-reinstall hermes-agent` to restore the `hermes` CLI entrypoint that rc.2's console-script collision left stale. Fresh installs are unaffected.

---

## Targeting a specific RC

`pip install --pre totalreclaw` always resolves to the latest published release candidate on PyPI. For QA harnesses pinned to a known build, or for re-installing a specific RC after a regression in a newer one, pin the version explicitly:

```bash
# Fresh install of a specific RC (no --pre needed when the version is explicit):
"$HERMES_PYTHON" -m pip install 'totalreclaw==2.3.1rc24'

# Re-pin (downgrade) over a newer RC that's already installed:
"$HERMES_PYTHON" -m pip install --force-reinstall 'totalreclaw==2.3.1rc24'
```

`uv pip install 'totalreclaw==2.3.1rc24' --reinstall` is the equivalent under `uv`. Stick with the same version family across plugin and Python package — mismatched versions can leave the gateway loading old tool signatures.

---

## Troubleshooting

- **Agent says "I'm not familiar with TotalReclaw"**: paste the canonical message above with the URL — the agent fetches the guide and follows the install steps.
- **Agent declines to install (safety refusal)**: that's a sign the agent is doing its job. Ask it to walk through what each command does first; once it's comfortable that the commands are standard `hermes plugins install` + `pip install` from named publishers and the recovery phrase stays in your browser, it should proceed. If it still declines, a human can run the [Fully manual](#fully-manual-cli-only--last-resort) flow.
- **Agent can't see TotalReclaw tools after install**: confirm both install steps completed — on a self-host, `hermes plugins list` should show `p-diogo/totalreclaw-hermes` enabled and `pip show totalreclaw` in the Hermes venv should resolve; on a managed service, confirm `totalreclaw` is listed in your service's plugins UI. Then restart: `hermes gateway restart` (native), `docker restart <your-hermes-container>` (Docker self-host), or your managed service's restart control. If supervised by systemd / launchd, `kill -USR1 $(cat ~/.hermes/gateway.pid)` is faster than `systemctl restart`. (Hermes does not yet auto-reload on plugin install — tracked upstream.)
- **Account-setup URL returns 404**: check that `~/.totalreclaw/credentials.json` isn't locked by a previous process and that the gateway is running. If you invoked `hermes chat -q "..."` (one-shot) for account setup, the WebSocket the relay needs may have died before the browser POST landed — see [Account setup requires daemon mode](#account-setup-requires-daemon-mode).
- **Browser fails to POST the encrypted phrase**: check the account-setup page's Content-Security-Policy — older browsers without WebCrypto x25519 (pre-Safari 17.2 / Chromium 118) cannot run the AEAD crypto.
- **"No LLM available for auto-extraction"**: configure a provider in Hermes (`hermes login` or set `ZAI_API_KEY` / `OPENAI_API_KEY` in `~/.hermes/.env`). TotalReclaw reuses it automatically.
- **Recovery phrase appeared in chat**: file a bug. Rotate by generating a new wallet via `totalreclaw_pair` with `mode=generate`. The leaked phrase is unrecoverable once shipped through LLM context.
- **Agent narrating Python / venv / pip details aloud during install**: the install transcript is intentionally short by default to keep noise down. If you'd prefer detailed progress, just ask — *"show me what you're running"* — and the agent will narrate freely. If the default-short cadence isn't holding across sessions, the published RC's `SKILL.md` may be stale — file an issue.

---

## Returning user (new machine)

Paste the same canonical message. When the account-setup page loads, switch to the **Import existing** tab and enter your existing 12/24-word phrase. The browser encrypts it against the gateway's ephemeral key before uploading — the relay never sees plaintext.

## First-time user (no phrase yet)

Paste the same canonical message. The **Generate new** tab is selected by default. Click **Generate new 12-word phrase** — the browser creates a BIP-39 phrase using its own `crypto.getRandomValues` against the inlined 2048-word wordlist. Write the 12 words down somewhere safe (a password manager is fine), tick the acknowledgment, and click **Seal key and finish**. That is your one and only recovery key — there is no reset.

---

## Canonical prompt (matches the QA harness scenario contracts)

> **Install TotalReclaw, see <https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/hermes-setup.md>**

---

## Compatibility with Hermes built-in memory

TotalReclaw and Hermes' built-in `memory` tool BOTH respond to "remember X" / "recall X" intents. Running both simultaneously creates a silent bug: depending on the LLM's tool-selection, your conversation context may be split between Hermes' MEMORY.md and TotalReclaw's encrypted vault — neither sees the full picture.

The TotalReclaw account-setup flow auto-disables Hermes built-in memory on install. We don't recommend re-enabling it while TotalReclaw is installed.

If you want to use Hermes built-in memory instead of TotalReclaw:

```bash
hermes tools enable memory
totalreclaw forget --all     # optional: clear TotalReclaw's vault
pip uninstall totalreclaw    # optional: remove the package
```

If you want to use both (not currently supported, results may be unreliable): file an issue at <https://github.com/p-diogo/totalreclaw/issues> describing your use case so we can scope dual-mode support.

---

## See also

- [Memory types guide](memory-types-guide.md) — v1 taxonomy
- [Importing memories](importing-memories.md)
- [OpenClaw plugin setup](openclaw-setup.md) — same vault, different runtime
