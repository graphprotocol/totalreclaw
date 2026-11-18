# TotalReclaw for Claude Desktop

TotalReclaw gives Claude Desktop encrypted memory that lasts across chats and works with your other TotalReclaw apps. Setup takes about five minutes: you install the TotalReclaw extension, restart the app, and ask Claude to set it up. Claude gives you a link to finish in your browser. **Your recovery phrase never goes through the chat and is never written into a settings file.**

> **Wrong app?** This guide is for **Claude Desktop**, the chat app. For coding tools (**Claude Code, Cursor, Windsurf**), follow [`claude-code-setup.md`](./claude-code-setup.md). If you are an **OpenClaw** agent, follow [`openclaw-setup.md`](./openclaw-setup.md). If you are a **Hermes** agent, follow [`hermes-setup.md`](./hermes-setup.md). If you are unsure, follow the universal dispatcher [`install.md`](./install.md).

**You need:**

- Claude Desktop on macOS or Windows.
- Nothing else to install. The extension runs on the Node.js built into Claude Desktop. (You need [Node.js](https://nodejs.org) 20 or later only for the config-file method in [Other way](#other-way-add-totalreclaw-to-the-config-file).)
- A browser on this computer or your phone, for the one-time setup page. You don't need a recovery phrase beforehand. Setup creates one, or takes the one you already have.

---

## Set up

### 1. Download the TotalReclaw extension

Download the file for your computer:

| Your computer | Download |
|---|---|
| macOS (Apple silicon) | [totalreclaw-3.6.0-darwin-arm64.mcpb](https://github.com/graphprotocol/totalreclaw/releases/download/mcp-server-v3.6.0/totalreclaw-3.6.0-darwin-arm64.mcpb) |
| macOS (Intel) | [totalreclaw-3.6.0-darwin-x64.mcpb](https://github.com/graphprotocol/totalreclaw/releases/download/mcp-server-v3.6.0/totalreclaw-3.6.0-darwin-x64.mcpb) |
| Windows | [totalreclaw-3.6.0-win32-x64.mcpb](https://github.com/graphprotocol/totalreclaw/releases/download/mcp-server-v3.6.0/totalreclaw-3.6.0-win32-x64.mcpb) |

Not sure which Mac you have? Click the Apple menu, choose **About This Mac**, and look at **Chip**. If it says Apple M1, M2, M3 or similar, choose Apple silicon. If it says Intel, choose Intel.

### 2. Install it

Double-click the downloaded file. Claude Desktop opens and shows the TotalReclaw extension. Click **Install**. Leave **Server URL (advanced)** empty.

If double-clicking doesn't open Claude Desktop, open Claude Desktop, go to **Settings → Extensions**, and drag the downloaded file onto that page.

### 3. Ask Claude to set up TotalReclaw

Start a new chat and type:

> **Set up TotalReclaw**

Claude asks whether you're new to TotalReclaw or already have a recovery phrase. It then runs the `totalreclaw_pair` tool. The first time it uses a TotalReclaw tool, Claude Desktop may ask for your permission; allow it. Claude gives you a link and a 6-digit PIN.

Open the link in any browser, on this computer or your phone, and enter the PIN. Then:

- **New to TotalReclaw:** the page creates your 12-word recovery phrase. Write it down on paper or save it in a password manager, then retype 3 of the words to confirm. It is the only way back into your memories if you lose this computer.
- **Already have a phrase:** type it into the page, never into the chat.

The link works for about 10 minutes, so there is time to write the phrase down carefully. If it expires, ask Claude for a new one.

### 4. Quit Claude Desktop and open it again

When the page says **"You're all set"**, quit Claude Desktop completely and open it again:

- **macOS:** press Cmd-Q, or choose **Claude → Quit Claude**.
- **Windows:** right-click the Claude icon in the system tray and choose **Quit**.

Closing the window is not enough, because the app keeps running in the background. TotalReclaw reads your account only when the app starts, so the memory tools work after this restart. The page suggests going back to your chat to try a memory; restart first.

### 5. Try it

In a new chat:

> **Remember that I prefer window seats on flights.**

Then, in another new chat:

> **What do you remember about how I like to travel?**

Or ask *"What's my TotalReclaw status?"* to see your account and usage.

**Tool permissions.** Claude Desktop may ask for your permission the first time Claude uses a TotalReclaw tool. Claude Desktop decides which tools it asks about, and this can change between versions. If it offers **"Always allow"**, you can choose it so memory works without a prompt every time. For tools that change or remove things, such as `totalreclaw_forget`, `totalreclaw_export` and `totalreclaw_pair`, you can keep the prompt.

---

## Already using TotalReclaw on this computer?

Other TotalReclaw apps on the same computer, such as Hermes or OpenClaw, keep your account in `~/.totalreclaw/credentials.json`. Claude Desktop uses the same file.

- **The file holds your account in a form TotalReclaw for Claude Desktop can read.** It is used as is. After step 2, ask *"What's my TotalReclaw status?"*. If it shows your account, skip steps 3 and 4.
- **Hermes keeps your phrase in the system keychain.** The server can't read the keychain yet. It says so when you use a TotalReclaw tool, and it won't set up a new account over that file, so your Hermes account stays as it is. TotalReclaw doesn't work in Claude Desktop on that computer until keychain support arrives.
- **Setup never replaces an existing account file.** If `totalreclaw_pair` says TotalReclaw is already set up, it is. Don't move, rename or delete the file to get around this, because that would switch every TotalReclaw app on the computer to a different account.

To use the same memories on another computer, set up TotalReclaw there and choose *"I already have a phrase"*. You type the phrase into the setup page in your browser, never into a chat or a config file.

---

## Other way: add TotalReclaw to the config file

Use this only if the extension can't be installed, for example because your organisation blocks extensions. It needs [Node.js](https://nodejs.org) 20 or later (22 recommended). Claude Desktop starts TotalReclaw with `npx`, which comes with Node.js.

Claude can't change its own settings from a chat, so you do this yourself.

1. In Claude Desktop, open **Settings → Developer → Edit Config**. This opens `claude_desktop_config.json`:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Add `totalreclaw` under `mcpServers`. If the file is empty, paste this whole block. If you already have other servers, add the `"totalreclaw": { … }` entry next to them.

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"]
    }
  }
}
```

There is no recovery phrase to add here and no `env` block. You set up your account in the browser.

3. Quit Claude Desktop and open it again, as in step 4 above. Claude Desktop only loads new servers when it starts. Then continue from step 3 (ask Claude to set up TotalReclaw).

The config always installs the newest stable `@totalreclaw/mcp-server` (3.5.1 or later is required).

---

## Agent instructions (read this if you are Claude, or another chat assistant, and were pointed at this page)

You can't install extensions, edit Claude Desktop's config file or restart the app. The user does these. Follow these steps in order.

1. **If you don't have any `totalreclaw_*` tools**, TotalReclaw isn't installed yet. Tell the user to install the TotalReclaw extension from this guide (download the file for their computer in step 1, then double-click it and click **Install**), and to come back to the chat afterwards. Then ask them to start a new chat and say *"Set up TotalReclaw"*. Never edit config files or run commands for the user.
2. **If you have the tools**, call `totalreclaw_status`.
   - It shows an account: TotalReclaw is already set up. Tell the user, and stop here.
   - It returns `not_configured` with a `reason`: tell the user the `message` in plain words. Don't call `totalreclaw_pair`.
   - It returns `not_configured` without a `reason`: go to step 3.
3. Ask one question: *"Are you new to TotalReclaw, or do you already have a TotalReclaw recovery phrase?"* Then call `totalreclaw_pair` with `mode: "generate"` for a new user, or `mode: "import"` for an existing phrase.
   - If the result says `already_configured`, tell the user TotalReclaw is already set up. Never move, rename or delete the credentials file.
   - Otherwise, give the user the `url` and `pin` with this message, filling in the two values: *"Open this link in any browser, on this computer or your phone: <url>. Enter this PIN when asked: <pin>. The link works for about 10 minutes; if it stops working, tell me and I'll make a new one. Tell me when the page says "You're all set"."* Add *"The page will create your recovery phrase. Write it down and keep it safe: it is the only way back into your memories."* for a new user, or *"Type your recovery phrase into that page, not into this chat."* for an existing one.
4. When the user says the page is done, tell them: *"Last step: quit Claude Desktop completely (Cmd-Q on macOS, or Quit from the tray icon on Windows) and open it again. TotalReclaw will work after that."* The memory tools return `not_configured` until they restart.
5. If the link expired or the page couldn't finish, call `totalreclaw_pair` again for a new link and PIN.

**Phrase safety (never break):** never ask the user to type, paste or read out a recovery phrase in the chat, and never put one in a config file, an environment variable or a tool argument. The phrase is created or entered only on the setup page in the browser. If the user pastes a phrase into the chat anyway, don't repeat it. Tell them it has been exposed and should be replaced with a new account, and call `totalreclaw_support` for how to get help.

---

## How it works

1. The extension runs the TotalReclaw server, a small program on your computer, with Claude Desktop's built-in Node. Claude Desktop talks to it over MCP, the standard way chat apps connect to tools. (With the config-file method, Claude Desktop starts it with `npx` instead.)
2. `totalreclaw_pair` opens a short setup session. Your browser creates or takes your recovery phrase, encrypts it to a one-time key held by that program, and sends only the encrypted version through the TotalReclaw relay. The program decrypts it on your computer and saves your account to `~/.totalreclaw/credentials.json`, readable only by you (mode `0600`).
3. Every memory is encrypted on your computer before it leaves. The relay and the chain only ever see encrypted data.
4. Claude decides when to recall or store a memory, based on the conversation. Unlike OpenClaw and Hermes, there are no automatic memory hooks in a chat app. If something matters, ask Claude directly: *"Remember that …"*.
5. The first time you use memory, TotalReclaw downloads an embedding model of about 600 MB. This happens once and is cached on your computer.

---

## Troubleshooting

- **No TotalReclaw tools after installing the extension.** Open **Settings → Extensions** and check that TotalReclaw is listed and enabled. Then fully quit Claude Desktop (not just the window) and open it again. If the tools are still missing, check Claude Desktop's logs: each server's log is under `~/Library/Logs/Claude/` on macOS or `%APPDATA%\Claude\logs\` on Windows.
- **No TotalReclaw tools after restarting (config-file method).** Check that the config file is valid JSON (a missing comma or brace is the usual cause) and that you fully quit the app, not just closed the window. Claude Desktop also has to be able to find `npx`. If you installed Node.js with a version manager such as nvm, Claude Desktop may not see it. In that case, set `"command"` to the full path that `which npx` (macOS) or `where npx` (Windows) prints in a terminal. Claude Desktop writes each server's log under `~/Library/Logs/Claude/` on macOS or `%APPDATA%\Claude\logs\` on Windows.
- **Tools say "not configured".** Setup hasn't finished, or Claude Desktop wasn't restarted after it. Ask Claude to *"Set up TotalReclaw"*, finish the page, then quit and reopen the app.
- **Tools say "not configured" and mention the keychain.** Another TotalReclaw app (Hermes) set up this computer with the system keychain. See [Already using TotalReclaw on this computer?](#already-using-totalreclaw-on-this-computer)
- **The setup page says "Gateway could not finish pairing" or the link expired.** Ask Claude for a new link. If it keeps happening, you may be running an older version. With the extension, download the newest file from step 1 and install it again. With the config-file method, you may have an older cached version: set `"args"` to `["-y", "@totalreclaw/mcp-server@latest"]`, then quit and reopen Claude Desktop. Versions before 3.5.1 stopped waiting after 60 seconds.
- **"Not authenticated" / 401 after importing a phrase.** Check the phrase: exact words, exact order.
- **The first memory takes a long time.** The embedding model (about 600 MB) is downloading. It happens once.
- **Quota exceeded (403).** The free tier has a monthly memory limit. Ask Claude to *"Upgrade my TotalReclaw subscription"*, or see [pricing](https://totalreclaw.xyz/pricing).
- **Your recovery phrase appeared in a chat.** Treat it as exposed and file a bug. A phrase that has been through an AI chat can't be made private again.

---

## Advanced: recovery phrase in the config (not recommended)

Use this only if you can't open the setup link in any browser, including on your phone. It stores your recovery phrase in plaintext in `claude_desktop_config.json`:

- Anyone or anything that can read that file (backups, sync tools, other apps on this computer) gets full access to your memories.
- TotalReclaw does not convert it to safer derived keys. The phrase stays in that file until you remove it.
- Never ask Claude, or any assistant, to write the phrase into the file for you. Edit the file yourself.

```json
{
  "mcpServers": {
    "totalreclaw": {
      "command": "npx",
      "args": ["-y", "@totalreclaw/mcp-server"],
      "env": {
        "TOTALRECLAW_RECOVERY_PHRASE": "your twelve word recovery phrase here"
      }
    }
  }
}
```

When the variable is set, it takes precedence over `~/.totalreclaw/credentials.json`. To switch back to the account from the setup page, delete the `env` block and restart Claude Desktop.

> **Pin a version** with `"args": ["-y", "@totalreclaw/mcp-server@latest"]` (newest stable, recommended) or a specific release candidate (`@totalreclaw/mcp-server@<version>-rc.<n>`) for testing. The `@rc` tag tracks the newest release-candidate line and is not necessarily newer than stable. See what each tag points to with `npm view @totalreclaw/mcp-server dist-tags`.

---

## Other chat apps

TotalReclaw runs as a standard local MCP server (`npx -y @totalreclaw/mcp-server`), and setup always goes through `totalreclaw_pair` in the browser. Other chat apps and personal agents that can start a local MCP server can use it in the same way. For **Codex** (CLI, IDE extension and the ChatGPT desktop app), follow [`codex-setup.md`](./codex-setup.md). For other apps, each app's own documentation explains where its MCP settings live.

---

## Further reading

- [Available tools and what to say](./claude-code-setup.md#available-tools): the same tools work in every MCP app.
- [Memory types guide](./memory-types-guide.md): what gets stored, and how to change it by asking.
- [Importing memories](./importing-memories.md): bring memories from ChatGPT, Claude, Gemini, Mem0 and others.
- [Hermes setup](./hermes-setup.md) and [OpenClaw setup](./openclaw-setup.md): the same account in other apps.
- [totalreclaw.xyz](https://totalreclaw.xyz)
