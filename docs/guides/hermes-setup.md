# Hermes Agent + TotalReclaw

TotalReclaw gives your Hermes agent encrypted, persistent memory. Install the plugin, run setup, chat.

## Prerequisites

- Hermes Agent v0.5.0+ (https://github.com/NousResearch/hermes-agent)
- An LLM provider configured in Hermes (zai / openai / anthropic / gemini)
- Python 3.11+

## 1. Install

```bash
pip install totalreclaw
```

Hermes auto-discovers the plugin via entry-point registration at next start. No manual copy, no config editing. Ubuntu/Debian/Docker: add `--break-system-packages` or use a venv if you hit `externally-managed-environment`.

**Installing a release candidate (RC / pre-release)?** Pre-releases on PyPI are hidden from `pip install` by default. Use `--pre` and pin the version explicitly:

```bash
pip install --pre totalreclaw==2.3.1rc2        # replace with the RC version you want
```

Find the latest RC via `pip index versions totalreclaw --pre` or on [PyPI](https://pypi.org/project/totalreclaw/#history). Never install an RC on production — only for QA against staging.

## 2. Set up your vault

**Recommended -- CLI wizard (runs outside the LLM path):**

```bash
hermes setup
```

Pick "generate" or "import". The phrase is written to `~/.totalreclaw/credentials.json` (mode `0600`). **The phrase is never printed to stdout, logged, or sent to any LLM.** Retrieve it later with `cat ~/.totalreclaw/credentials.json | jq -r .mnemonic`. Save it somewhere safe -- it is the ONLY way to recover your vault.

**Via chat:** ask "Set up TotalReclaw for me" -- the agent points you at the CLI. Phrases are never generated or shown in chat.

## 3. Restart Hermes

```bash
hermes gateway restart
```

Required for the plugin to register its tools and hooks.

## 4. Verify

Ask "What's my TotalReclaw status?", or run `hermes` then `/plugins` -- `totalreclaw` should be listed.

## Troubleshooting

| Issue | Fix |
|---|---|
| "No LLM available for auto-extraction" | Configure a provider in Hermes (`hermes login` or set `ZAI_API_KEY` / `OPENAI_API_KEY` in `~/.hermes/.env`). TotalReclaw reuses it automatically. |
| Plugin not in `/plugins` | `hermes gateway restart`. |
| Recovery phrase appeared in chat | File a bug. Use the CLI: `hermes setup`. |

## Returning user (new machine)

```bash
hermes setup   # choose "import", paste your phrase when prompted (masked).
```

## See also

- [Memory types guide](memory-types-guide.md) -- v1 taxonomy
- [Importing memories](importing-memories.md)
- [OpenClaw plugin setup](openclaw-setup.md) -- same vault, different runtime
