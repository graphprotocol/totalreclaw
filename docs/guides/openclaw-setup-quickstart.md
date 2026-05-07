# TotalReclaw — OpenClaw quickstart

TotalReclaw is end-to-end encrypted, decentralized memory for AI agents. Memories are encrypted on your device with a key derived from your 12-word recovery phrase, then submitted on-chain via Account Abstraction. Storage lives across a public blockchain (Base Sepolia for the free tier, Gnosis mainnet for paid). Only your recovery phrase can decrypt them.

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

You are then on the free tier (500 memories per month, unlimited reads). Paid tiers and custom relays are at <https://totalreclaw.xyz/pricing>.

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

TotalReclaw is end-to-end encrypted. The TotalReclaw relay only forwards encrypted bundles between your device and the blockchain — it never sees plaintext, can't read your memories, and could be replaced by any compatible relay without losing data. Storage lives on a public blockchain (Base Sepolia for free, Gnosis mainnet for Pro), indexed by The Graph. No single company controls or can read your memories.

## Troubleshooting

If the install command reports `already exists` on a re-run, add `--force`:

```bash
openclaw plugins install @totalreclaw/totalreclaw --force
openclaw skills install totalreclaw --force
```

If your environment doesn't expose the OpenClaw CLI to the agent (managed-service or sandboxed shell), install `totalreclaw` from your service's plugins / skills UI and then ask your agent to set you up.

For more troubleshooting and the full setup rationale, see [`openclaw-setup.md`](./openclaw-setup.md).
