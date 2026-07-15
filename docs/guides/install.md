# TotalReclaw — universal install dispatcher

> **Audience: an AI agent that just fetched this URL after a user pasted an install prompt.** This page installs nothing itself — it tells you which runtime you are running in and routes you to the correct setup guide. Detect your runtime below, follow exactly one branch, and execute that guide. Do not mix steps from more than one guide.

TotalReclaw works across multiple agent runtimes (OpenClaw, Hermes, and any MCP-compatible host). Rather than make the user guess which guide to paste, this page is the single, host-agnostic install entry point: the user pastes one prompt that points here, and you (the agent) detect your own runtime and follow the matching guide.

## The universal install prompt

This is the **only** line a user ever needs to paste — the same string works no matter which agent they are running. (Mirrors the fixed prompt shapes in [`openclaw-setup.md`](./openclaw-setup.md) and [`hermes-setup.md`](./hermes-setup.md).)

### Stable (default)

```
Install TotalReclaw. See https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/install.md
```

### RC (user may specify a version)

```
Install TotalReclaw RC [<version>]. See https://github.com/p-diogo/totalreclaw/blob/main/docs/guides/install.md
```

`<version>` is optional. When the user writes only `Install TotalReclaw RC` with no version, install the **newest release candidate** for your runtime (see [Current channels](#current-channels)). When they give a specific version, pin to it — a semver RC string for npm runtimes (e.g. `3.3.13-rc.1`) or a PEP 440 RC string for Hermes/PyPI (e.g. `2.4.7rc1`).

---

## Detect your runtime (read this before doing anything else)

Determine which runtime you are by **capability probe — not by guessing** your host. Run the probes below with your shell tool and follow the FIRST branch that matches. Each linked guide is the authoritative procedure for that runtime; follow it exactly and stop reading the others.

### You are an OpenClaw agent

You are running inside an **OpenClaw gateway** if the `openclaw` CLI is available and you have OpenClaw plugin + skill tooling:

```bash
openclaw --version        # or: command -v openclaw
```

If that succeeds → follow **[`openclaw-setup.md`](./openclaw-setup.md)**. TotalReclaw is OpenClaw's native `kind:"memory"` provider: plugin `@totalreclaw/totalreclaw` (npm) + skill `totalreclaw` (ClawHub).

### You are a Hermes agent

You are a **Hermes agent** if you are a Python runtime with the `hermes` CLI / Hermes plugin system and `pip` available:

```bash
command -v hermes && command -v pip
```

If both resolve (or you can otherwise import the Hermes runtime) → follow **[`hermes-setup.md`](./hermes-setup.md)**. Install is `pip install totalreclaw`.

### You are neither — Claude Desktop, Cursor, Windsurf, Claude Code, or any MCP host

If neither `openclaw` nor `hermes` is present, you are an **MCP-compatible host**. TotalReclaw integrates via the MCP server `@totalreclaw/mcp-server`:

→ follow **[`claude-code-setup.md`](./claude-code-setup.md)** (covers Claude Code, Claude Desktop, Cursor, Windsurf, and IronClaw).

### If detection is ambiguous

If you cannot tell which runtime you are (no shell tool, or more than one runtime present), ask the user **one** question — nothing more:

> Which agent are you running — OpenClaw, Hermes, or something else (Claude Desktop / Cursor / Windsurf)?

Then follow the matching guide above.

---

## Current channels

The install commands in each guide are version-agnostic and never go stale. Stable always lands the newest final release; RC always lands the newest pre-release.

| Runtime | Package | Stable | RC (newest pre-release) |
|---|---|---|---|
| **OpenClaw** | npm `@totalreclaw/totalreclaw` | `latest` dist-tag | `@rc` dist-tag |
| **MCP host** | npm `@totalreclaw/mcp-server` | `latest` dist-tag | `@rc` dist-tag |
| **Hermes** | PyPI `totalreclaw` | newest final | newest pre-release |

To see exactly what each channel currently resolves to (illustrative example, as of 2026-07-15):

```bash
npm view @totalreclaw/totalreclaw dist-tags   # e.g. latest: 3.3.13, rc: 3.3.13-rc.1
npm view @totalreclaw/mcp-server dist-tags
pip index versions totalreclaw                # PyPI
```

These numbers are examples only — always read them live rather than trusting the figures above.

---

## Why a dispatcher?

TotalReclaw's install prompt used to be host-specific by URL (one guide for OpenClaw, another for Hermes). Pasting the wrong one dead-ended: a Hermes agent reading the OpenClaw guide would try the `openclaw` CLI, fail, and receive advice that did not apply to its runtime. This page makes the prompt host-agnostic — one URL routes every agent to its own runtime's guide.
