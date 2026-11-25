# TotalReclaw for Codex

TotalReclaw gives Codex encrypted memory that lasts across sessions and works with your other TotalReclaw apps. This guide covers the **Codex CLI**, the **Codex IDE extension** and the **ChatGPT desktop app**. They share one Codex configuration folder, `~/.codex` (per OpenAI's documentation), so you set up TotalReclaw once. Setup takes about five minutes. **Your recovery phrase never goes through the chat and is never written into a settings file.**

## The fast way

Paste this into Codex:

> **Install TotalReclaw. See https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/codex-setup.md**

Codex reads this page and runs the install commands. It asks you to approve them, because they write to `~/.codex`. Then it tells you to restart Codex and set up your account in the browser. The steps below are what it does.

> **Wrong app?** This guide is for **Codex**. For **Claude Desktop**, follow [`claude-desktop-setup.md`](./claude-desktop-setup.md). For **Claude Code, Cursor and Windsurf**, follow [`claude-code-setup.md`](./claude-code-setup.md). For **OpenClaw**, follow [`openclaw-setup.md`](./openclaw-setup.md). For **Hermes**, follow [`hermes-setup.md`](./hermes-setup.md). If you are unsure, follow the universal dispatcher [`install.md`](./install.md).

> **Regular ChatGPT chat (the website or the phone app) is not supported.** TotalReclaw runs as a small program on your own computer and keeps its keys there, and chat on the website or phone has no way to run a program on your computer.

**You need:**

- Codex: the CLI, the IDE extension, or the ChatGPT desktop app.
- [Node.js](https://nodejs.org) 20 or later. TotalReclaw starts with `npx`, which comes with Node.js. On a Mac, use an Apple silicon (arm64) build of Node.js; Intel Macs, and x64 Node under Rosetta, are not supported.
- A browser on this computer or your phone, for the one-time setup page. You don't need a recovery phrase beforehand. Setup creates one, or takes the one you already have.

---

## Set up

### 1. Check Node.js

```sh
node -v
```

It should print `v20` or higher. If not, install Node.js from [nodejs.org](https://nodejs.org). Codex has to be able to find `npx` when it starts, so after installing Node.js, quit and reopen Codex.

### 2. Add the TotalReclaw plugin

Run these two commands in a terminal:

```sh
codex plugin marketplace add graphprotocol/totalreclaw
codex plugin add totalreclaw@totalreclaw
```

The first prints `Added marketplace totalreclaw`. The second installs the TotalReclaw plugin. It brings a skill that tells Codex when to recall and store memories, and the TotalReclaw MCP server.

**ChatGPT desktop app, and no `codex` command in your terminal?** The app carries its own copy. On macOS, run the same two commands with this path:

```sh
CODEX=/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex
"$CODEX" plugin marketplace add graphprotocol/totalreclaw
"$CODEX" plugin add totalreclaw@totalreclaw
```

### 3. Turn off approval prompts for TotalReclaw (recommended)

By default Codex asks before each tool call. To let TotalReclaw's tools run without a prompt each time, add this to `~/.codex/config.toml` (create the file if it doesn't exist; if that section is already in the file, add only the missing line):

```toml
[plugins."totalreclaw@totalreclaw".mcp_servers.totalreclaw]
default_tools_approval_mode = "approve"
```

This applies to all TotalReclaw tools, including the ones that remove things, such as `totalreclaw_forget`. Skip this step if you prefer to approve each call.

Removing the plugin also removes this block. If you reinstall the plugin, add the block again.

### 4. Quit Codex and open it again

Codex loads new plugins when it starts. Quit it completely, then open it again, or start a new session or thread. Closing the window may not be enough.

### 5. Ask Codex to set up TotalReclaw

Start a new session and type:

> **Set up TotalReclaw**

Codex asks whether you're new to TotalReclaw or already have a recovery phrase. It then runs the `totalreclaw_pair` tool and gives you a link and a 6-digit PIN.

Open the link in any browser, on this computer or your phone, and enter the PIN. Then:

- **New to TotalReclaw:** the page creates your 12-word recovery phrase. Write it down on paper or save it in a password manager, then retype 3 of the words to confirm. It is the only way back into your memories if you lose this computer.
- **Already have a phrase:** type it into the page, never into the chat.

The link works for about 10 minutes. If it expires, ask Codex for a new one. When the page says you're all set, go back to Codex and say you're done.

### 6. Try it

In a new session:

> **Remember that I prefer window seats on flights.**

Then, in another new session:

> **What do you remember about how I like to travel?**

Or ask *"What's my TotalReclaw status?"* to see your account and usage.

TotalReclaw also saves things it judges worth keeping without being asked, but not every time. If something matters, say *"Remember that ..."*.

---

## Already using TotalReclaw on this computer?

Other TotalReclaw apps on the same computer, such as Hermes, OpenClaw or Claude Desktop, keep your account in `~/.totalreclaw/credentials.json`. Codex uses the same file.

- **The file holds your account in a form TotalReclaw for Codex can read.** It is used as is. After step 4, ask *"What's my TotalReclaw status?"*. If it shows your account, skip step 5.
- **Hermes keeps your phrase in the system keychain.** The server can't read the keychain yet. It says so when you use a TotalReclaw tool, and it won't set up a new account over that file, so your Hermes account stays as it is.
- **Setup never replaces an existing account file.** If `totalreclaw_pair` says TotalReclaw is already set up, it is. Don't move, rename or delete the file to get around this, because that would switch every TotalReclaw app on the computer to a different account.

To use the same memories on another computer, set up TotalReclaw there and choose *"I already have a phrase"*. You type the phrase into the setup page in your browser, never into a chat or a config file.

---

## Update, remove, or use without the plugin

**Update:**

```sh
codex plugin marketplace upgrade
```

**Remove:**

```sh
codex plugin remove totalreclaw@totalreclaw
```

This also removes the approval block from step 3. Your account file in `~/.totalreclaw` stays.

**MCP only, no plugin.** If your Codex host doesn't load plugins, add just the server:

```sh
codex mcp add totalreclaw -- npx -y @totalreclaw/mcp-server
```

This writes a `[mcp_servers.totalreclaw]` entry to `~/.codex/config.toml`. Without the plugin there is no skill, so Codex gets less guidance on when to recall and store.

To skip approval prompts in this setup, add `default_tools_approval_mode = "approve"` inside the `[mcp_servers.totalreclaw]` section the command above created. The section then looks like this (add only the last line if `command` and `args` are already there):

```toml
[mcp_servers.totalreclaw]
command = "npx"
args = ["-y", "@totalreclaw/mcp-server"]
default_tools_approval_mode = "approve"
```

Per OpenAI's documentation, the ChatGPT desktop app also has **Settings > MCP servers**, where you can add the same server (command `npx`, arguments `-y @totalreclaw/mcp-server`) without the terminal. Setup then continues from step 4.

**Codex memories.** Codex has its own "memories" feature, which is off by default. Leave it off when you use TotalReclaw. The two memory systems don't merge.

---

## Agent instructions (read this if you are Codex and were pointed at this page)

The user asked you to install TotalReclaw. You can run the install commands, but you can't restart Codex. The user does that. Follow these steps in order.

1. **If you have `totalreclaw_*` tools**, TotalReclaw is already installed. Skip to step 5.
2. **Check Node.js** with `node -v` and `npx -v`. If either is missing, tell the user to install Node.js 20 or later from nodejs.org, and stop.
3. **Install the plugin.** Use `codex` if it is on the PATH. If it isn't (common in the ChatGPT desktop app), use the bundled path on macOS: `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`. Run:
   - `codex plugin marketplace add graphprotocol/totalreclaw`
   - `codex plugin add totalreclaw@totalreclaw`

   These write to `~/.codex`, so the user will be asked to approve them. If either fails, show the user the error and stop. Don't pin a version and don't use `@rc`.
4. **Approval prompts (optional).** Offer to add the block from step 3 of this guide to `~/.codex/config.toml`. Add it only if the user agrees, and keep any existing content in the file. Then tell the user: *"TotalReclaw is installed. Quit Codex completely and open it again, or start a new session. Then say "Set up TotalReclaw"."* Stop here.
5. **After the restart**, if the user asks to set up, call `totalreclaw_status`.
   - It shows an account: TotalReclaw is already set up. Tell the user, and stop here.
   - It returns `not_configured` with a `reason`: tell the user the `message` in plain words. Don't call `totalreclaw_pair`.
   - It returns `not_configured` without a `reason`: go to step 6.
6. Ask one question: *"Are you new to TotalReclaw, or do you already have a TotalReclaw recovery phrase?"* Then call `totalreclaw_pair` with `mode: "generate"` for a new user, or `mode: "import"` for an existing phrase.
   - If the result says `already_configured`, tell the user TotalReclaw is already set up. Never move, rename or delete the credentials file.
   - Otherwise, give the user the `url` and `pin` with this message, filling in the two values: *"Open this link in any browser, on this computer or your phone: <url>. Enter this PIN when asked: <pin>. The link works for about 10 minutes; if it stops working, tell me and I'll make a new one. Tell me when the page says you're all set."* Add *"The page will create your recovery phrase. Write it down and keep it safe: it is the only way back into your memories."* for a new user, or *"Type your recovery phrase into that page, not into this chat."* for an existing one.
7. When the user says the page is done, call `totalreclaw_status`. It shows the account, and TotalReclaw works in this session with no restart. Only if it still returns `not_configured`, tell the user to quit Codex completely and open it again.
8. If the link expired or the page couldn't finish, call `totalreclaw_pair` again for a new link and PIN.

**Phrase safety (never break):** never ask the user to type, paste or read out a recovery phrase in the chat, and never put one in a config file, an environment variable or a tool argument. The phrase is created or entered only on the setup page in the browser. If the user pastes a phrase into the chat anyway, don't repeat it. Tell them it has been exposed and should be replaced with a new account, and call `totalreclaw_support` for how to get help.

---

## How it works

1. The plugin gives Codex a skill and starts the TotalReclaw MCP server with `npx`. The server is a small program on your computer. Codex talks to it over MCP, the standard way AI apps connect to tools. The plugin pins the server version it was tested with, and `codex plugin marketplace upgrade` brings a newer plugin when there is one.
2. `totalreclaw_pair` opens a short setup session. Your browser creates or takes your recovery phrase, encrypts it to a one-time key held by that program, and sends only the encrypted version through the TotalReclaw relay. The program decrypts it on your computer and saves your account to `~/.totalreclaw/credentials.json`, readable only by you (mode `0600`).
3. Every memory is encrypted on your computer before it leaves. The relay and the chain only ever see encrypted data.
4. The skill tells Codex to recall at the start of a conversation and when the topic changes, and to store things worth keeping. There are no automatic memory hooks in Codex yet, so Codex decides when to do this. If something matters, ask directly: *"Remember that ..."*.
5. The first time you use memory, TotalReclaw downloads an embedding model of about 600 MB. This happens once and is cached on your computer.

---

## Troubleshooting

- **"MCP startup failed: No such file or directory (os error 2)".** Codex couldn't find `npx`. Install Node.js 20 or later, make sure `npx -v` works in a terminal, then quit and reopen Codex. A Codex started from the Dock, the Start menu or an IDE may not see the PATH of your terminal, especially if you installed Node.js with a version manager such as nvm. Start Codex from a terminal where `npx -v` works, or install Node.js in a place every app can see.
- **No TotalReclaw tools after installing.** Quit Codex completely and open it again, or start a new session. Then check that `codex plugin list` shows `totalreclaw` as installed and enabled.
- **Codex still asks for approval on every TotalReclaw call.** Check the section name in `~/.codex/config.toml`. For the plugin it is exactly `[plugins."totalreclaw@totalreclaw".mcp_servers.totalreclaw]`, and the key is `default_tools_approval_mode = "approve"`. For the MCP-only setup it is `[mcp_servers.totalreclaw]`. Setting this key with a `-c` option on the command line does not work. If you removed and reinstalled the plugin, add the block again. Then restart Codex.
- **Tools say "not configured".** Setup hasn't finished. Ask Codex to *"Set up TotalReclaw"* and finish the page. If it still says so afterwards, quit and reopen Codex.
- **Tools say "not configured" and mention the keychain.** Another TotalReclaw app (Hermes) set up this computer with the system keychain. See [Already using TotalReclaw on this computer?](#already-using-totalreclaw-on-this-computer)
- **The setup page says the link expired, or setup can't finish.** Ask Codex for a new link.
- **"Not authenticated" / 401 after importing a phrase.** Check the phrase: exact words, exact order.
- **The first memory takes a long time.** The embedding model (about 600 MB) is downloading. It happens once.
- **Quota exceeded (403).** The free tier has a monthly memory limit. Ask Codex to *"Upgrade my TotalReclaw subscription"*, or see [pricing](https://totalreclaw.xyz/pricing).
- **Your recovery phrase appeared in a chat.** Treat it as exposed and file a bug. A phrase that has been through an AI chat can't be made private again.

---

## Further reading

- [Available tools and what to say](./claude-code-setup.md#available-tools): the same tools work in every MCP app.
- [Memory types guide](./memory-types-guide.md): what gets stored, and how to change it by asking.
- [Importing memories](./importing-memories.md): bring memories from ChatGPT, Claude, Gemini, Mem0 and others.
- [Claude Desktop setup](./claude-desktop-setup.md), [Hermes setup](./hermes-setup.md) and [OpenClaw setup](./openclaw-setup.md): the same account in other apps.
- [totalreclaw.xyz](https://totalreclaw.xyz)
