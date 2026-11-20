# TotalReclaw for Claude Desktop

TotalReclaw gives Claude Desktop encrypted memory that lasts across chats and works with your other TotalReclaw apps. Setup takes about 5 minutes, and **your recovery phrase never goes through the chat.**

> **Wrong app?** For **Claude Code, Cursor or Windsurf**, use [`claude-code-setup.md`](./claude-code-setup.md). For anything else, use [`install.md`](./install.md).

## Set up

### 1. Install the extension

Download the file for your computer:

| Your computer | Download |
|---|---|
| macOS (Apple silicon) | [totalreclaw-3.6.0-darwin-arm64.mcpb](https://github.com/graphprotocol/totalreclaw/releases/download/mcp-server-v3.6.0/totalreclaw-3.6.0-darwin-arm64.mcpb) |
| Windows | [totalreclaw-3.6.0-win32-x64.mcpb](https://github.com/graphprotocol/totalreclaw/releases/download/mcp-server-v3.6.0/totalreclaw-3.6.0-win32-x64.mcpb) |

Double-click the file, click **Install**, and leave **Server URL (advanced)** empty. If double-clicking doesn't open Claude Desktop, open it, go to **Settings → Extensions**, and drag the file there.

Claude Desktop warns that the extension can access everything on your computer. It shows this warning for any extension installed from a file, because extensions run as normal programs on your computer. TotalReclaw's code is [open source](https://github.com/graphprotocol/totalreclaw/tree/main/mcp).

TotalReclaw doesn't run on Intel Macs yet. To check, open Apple menu → **About This Mac**: a supported Mac shows "Apple M…" next to **Chip**.

### 2. Set up your account

In a new chat, type **Set up TotalReclaw**. Allow the permission prompt if one appears. Claude gives you a link and a 6-digit PIN. Open the link in any browser, on this computer or your phone, and enter the PIN.

- **New to TotalReclaw:** the page makes a 12-word recovery phrase. Write it down (paper or a password manager) and retype 3 words to confirm. It is the only way back into your memories.
- **Already have a phrase:** type it into the page, never into the chat.

The link lasts about 10 minutes. If it expires, ask Claude for a new one.

### 3. Restart Claude Desktop

When the page says **"You're all set"**, quit Claude Desktop and open it again: on macOS press Cmd-Q; on Windows right-click the Claude icon in the system tray and choose **Quit**. TotalReclaw loads your account when Claude Desktop starts, so closing the window is not enough.

### 4. Try it

In a new chat, type **Remember that I prefer window seats on flights.** Then, in another new chat, type **What do you remember about how I like to travel?** To see your account and usage, ask **What's my TotalReclaw status?**

## Good to know

- Claude Desktop may ask permission before using a TotalReclaw tool. **Always allow** is fine; you can keep the prompt for `totalreclaw_forget`, `totalreclaw_export` and `totalreclaw_pair`.
- To use TotalReclaw on another computer, set it up there and choose **I already have a phrase** on the page.
- Already using TotalReclaw on this computer? See [Already using TotalReclaw on this computer?](#already-using-totalreclaw-on-this-computer).
- Claude doesn't save memories on its own: it stores one when you ask, or when it judges something important. Say **Remember that …** for things that matter.

<a id="already-using-totalreclaw-on-this-computer"></a>
<details>
<summary><b>Already using TotalReclaw on this computer?</b></summary>

Other TotalReclaw apps on the same computer, such as Hermes or OpenClaw, keep your account in `~/.totalreclaw/credentials.json`. Claude Desktop uses the same file.

- **A file TotalReclaw for Claude Desktop can read is used as is.** After step 1, ask **What's my TotalReclaw status?**. If it shows your account, skip steps 2 and 3.
- **Hermes keeps your phrase in the system keychain.** The server can't read the keychain yet. It says so when you use a TotalReclaw tool, and it won't set up a new account over that file, so your Hermes account stays as it is. TotalReclaw doesn't work in Claude Desktop on that computer until keychain support arrives.
- **Setup doesn't replace an existing account file** unless you ask Claude to switch accounts and confirm on the setup page. The old file is kept as a backup. If `totalreclaw_pair` says TotalReclaw is already set up, it is. Never move, rename or delete the file to get around this: that would switch every TotalReclaw app on the computer to a different account.

</details>

<a id="other-way-add-totalreclaw-to-the-config-file"></a>
<details>
<summary><b>Other way: add TotalReclaw to the config file</b></summary>

Use this only if you can't install the extension, for example because your organisation blocks extensions. It needs a Mac with Apple silicon, or Windows, and [Node.js](https://nodejs.org) 20 or later (22 recommended). Claude Desktop starts TotalReclaw with `npx`, which comes with Node.js. Claude can't change its own settings from a chat, so you do this yourself.

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

There is no recovery phrase and no `env` block here. You set up your account in the browser.

3. Quit Claude Desktop and open it again, as in step 3 above. It only loads new servers when it starts. Then continue from step 2 (ask Claude to set up TotalReclaw).

The config always installs the newest stable `@totalreclaw/mcp-server` (3.5.1 or later is required).

</details>

<details>
<summary><b>How it works</b></summary>

- The extension runs a small TotalReclaw server on your computer, using Claude Desktop's built-in Node. Claude Desktop talks to it over MCP, the standard way chat apps connect to tools. (The config-file method starts it with `npx` instead.)
- `totalreclaw_pair` opens a short setup session. Your browser encrypts your recovery phrase to a one-time key held by that server and sends only the encrypted version through the TotalReclaw relay. The server decrypts it locally and saves your account to `~/.totalreclaw/credentials.json`, readable only by you (mode `0600`).
- Every memory is encrypted on your computer before it leaves. The relay and the chain only see encrypted data.
- TotalReclaw works even when Claude's own memory setting is off. It is separate from Claude's built-in memory.
- Claude decides when to recall or store a memory. Unlike OpenClaw and Hermes, a chat app has no automatic memory hooks.
- The first time you use memory, TotalReclaw downloads an embedding model of about 230 MB. This happens once and is cached on your computer.

</details>

<details>
<summary><b>Troubleshooting</b></summary>

- **No TotalReclaw tools after installing the extension.** Open **Settings → Extensions** and check that TotalReclaw is listed and enabled. Then fully quit Claude Desktop (not just the window) and open it again. If the tools are still missing, check the server log under `~/Library/Logs/Claude/` on macOS or `%APPDATA%\Claude\logs\` on Windows.
- **No TotalReclaw tools after restarting (config-file method).** Check that the config file is valid JSON (a missing comma or brace is the usual cause) and that you fully quit the app. Claude Desktop must also find `npx`. If you installed Node.js with a version manager such as nvm, it may not. Set `"command"` to the full path that `which npx` (macOS) or `where npx` (Windows) prints in a terminal. Each server's log is under `~/Library/Logs/Claude/` on macOS or `%APPDATA%\Claude\logs\` on Windows.
- **Tools say "not configured".** Setup hasn't finished, or Claude Desktop wasn't restarted after it. Ask Claude to **Set up TotalReclaw**, finish the page, then quit and reopen the app.
- **Tools say "not configured" and mention the keychain.** Another TotalReclaw app (Hermes) set up this computer with the system keychain. See [Already using TotalReclaw on this computer?](#already-using-totalreclaw-on-this-computer)
- **The setup page says "Gateway could not finish pairing", or the link expired.** Ask Claude for a new link. If it keeps happening, you may have an older version (versions before 3.5.1 stopped waiting after 60 seconds). With the extension, download the newest file from step 1 and install it again. With the config-file method, set `"args"` to `["-y", "@totalreclaw/mcp-server@latest"]`, then quit and reopen Claude Desktop.
- **"Not authenticated" / 401 after importing a phrase.** Check the phrase: exact words, exact order.
- **The first memory takes a long time.** The embedding model (about 230 MB) is downloading. It happens once.
- **Quota exceeded (403).** The free tier has a monthly memory limit. Ask Claude to **Upgrade my TotalReclaw subscription**, or see [pricing](https://totalreclaw.xyz/pricing).
- **Your recovery phrase appeared in a chat.** Treat it as exposed and file a bug. A phrase that has been through an AI chat can't be made private again.

</details>

<a id="advanced-recovery-phrase-in-the-config-not-recommended"></a>
<details>
<summary><b>Advanced: recovery phrase in the config (not recommended)</b></summary>

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

</details>

## Other chat apps

TotalReclaw runs as a standard local MCP server (`npx -y @totalreclaw/mcp-server`), and setup always goes through `totalreclaw_pair` in the browser. Other chat apps and personal agents that can start a local MCP server can use it the same way. For **Codex** (CLI, IDE extension and the ChatGPT desktop app), follow [`codex-setup.md`](./codex-setup.md). For other apps, see the app's own documentation for where its MCP settings live.

## Further reading

- [Available tools and what to say](./claude-code-setup.md#available-tools): the same tools work in every MCP app.
- [Memory types guide](./memory-types-guide.md): what gets stored, and how to change it by asking.
- [Importing memories](./importing-memories.md): bring memories from ChatGPT, Claude, Gemini, Mem0 and others.
- [Hermes setup](./hermes-setup.md) and [OpenClaw setup](./openclaw-setup.md): the same account in other apps.
- [totalreclaw.xyz](https://totalreclaw.xyz)

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
4. When the user says the page is done, tell them: *"Last step: quit Claude Desktop completely (Cmd-Q on macOS, or Quit from the Claude icon in the system tray on Windows) and open it again. TotalReclaw will work after that."* The memory tools return `not_configured` until they restart.
5. If the link expired or the page couldn't finish, call `totalreclaw_pair` again for a new link and PIN.

**Phrase safety (never break):** never ask the user to type, paste or read out a recovery phrase in the chat, and never put one in a config file, an environment variable or a tool argument. The phrase is created or entered only on the setup page in the browser. If the user pastes a phrase into the chat anyway, don't repeat it. Tell them it has been exposed and should be replaced with a new account, and call `totalreclaw_support` for how to get help.
