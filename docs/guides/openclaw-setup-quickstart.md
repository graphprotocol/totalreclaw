# TotalReclaw — OpenClaw quickstart

TotalReclaw is end-to-end encrypted, decentralized memory for AI agents. Memories are encrypted on your device with a key derived from your 12-word recovery phrase, then submitted on-chain via Account Abstraction. Storage lives on Gnosis mainnet (permanent, public blockchain). Only your recovery phrase can decrypt them.

This page is a short summary for humans. The full setup guide with rationale and troubleshooting is at [`openclaw-setup.md`](./openclaw-setup.md).

## Install

If you already have OpenClaw running, install the plugin and the skill from the OpenClaw CLI:

```bash
openclaw plugins install @totalreclaw/totalreclaw
openclaw skills install totalreclaw
```

For release-candidate builds (newer features, may be unstable), append `@rc` to the plugin name:

```bash
openclaw plugins install @totalreclaw/totalreclaw@rc
openclaw skills install totalreclaw
```

The plugin is published by `p-diogo` on npm under the `@totalreclaw` scope. Source code: <https://github.com/p-diogo/totalreclaw>.

## Set up your account

After install, ask your agent something like *"set up TotalReclaw"* or just *"remember that I prefer Italian food"* — the agent will hand you an account-setup URL and a 6-digit PIN. Open the URL in your browser. You will:

1. **Enter the PIN** the agent showed you.
2. **Generate or paste a 12-word recovery phrase** — the browser handles this end-to-end encrypted, so the phrase never enters the chat or the relay. Pick *Set up* for a fresh phrase, or *Log in* if you already have one from another device.
3. **Confirm.** The browser will tell you when you are all set.

You are then on the free tier (250 memories per month on Gnosis mainnet, E2E encrypted, no credit card required). Pro tier raises the cap to 1,500 memories per month — pay via the `totalreclaw_upgrade` tool or visit <https://totalreclaw.xyz/pricing>. Self-hosting and custom relays are also documented at that link.

## Write down the recovery phrase

Your 12-word recovery phrase is the only key to your memories. Treat it like a password to a bank account:

- Save it somewhere durable — paper, a password manager, or an encrypted note.
- Do **not** paste it into the chat with your agent.
- Do **not** screenshot it to a cloud-synced photo library.
- If you lose it, you lose access to your memories forever.

If TotalReclaw the company shuts down, your memories are still on a public blockchain. Any compatible relay (or your own self-hosted one) can read them with your recovery phrase.

## Use it

Once set up, just talk to your agent normally. It will pull relevant memories into context when you ask things like *"what did I tell you about my dog?"* or *"what's my work email?"*. You can also explicitly say *"remember X"* or *"forget the last thing I said about Y"*.

## Privacy and decentralization

TotalReclaw is end-to-end encrypted. The TotalReclaw relay only forwards encrypted bundles between your device and the blockchain — it never sees plaintext, can't read your memories, and could be replaced by any compatible relay without losing data. Storage lives on Gnosis mainnet, indexed by The Graph. No single company controls or can read your memories.

## Troubleshooting

**After installing, always run `openclaw plugins enable totalreclaw`.** It binds the memory slot. On a clean install it's a harmless no-op; on a reinstall over a previous version it's what stops the plugin from landing disabled (memory slot stuck at `memory-core` → memory tools and pairing don't work).

**If the install reports `already exists` / `delete it first` on a re-run, do NOT use `--force`** (it produces a broken, disabled install). Instead clean the leftover package dir and reinstall:

```bash
openclaw plugins uninstall totalreclaw
rm -rf ~/.openclaw/npm/projects/*totalreclaw-totalreclaw*
openclaw plugins install @totalreclaw/totalreclaw
openclaw plugins enable totalreclaw
```

The `rm -rf` only removes the plugin's npm package folder — it does not touch your recovery-phrase credentials (those live under `~/.totalreclaw/`).

If your environment doesn't expose the OpenClaw CLI to the agent (managed-service or sandboxed shell), install `totalreclaw` from your service's plugins / skills UI and then ask your agent to set you up.

For more troubleshooting and the full setup rationale, see [`openclaw-setup.md`](./openclaw-setup.md).
