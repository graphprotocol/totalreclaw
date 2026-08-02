# Phrase Escrow (Passkey Recovery Backup)

TotalReclaw's vault SPA (`app.totalreclaw.xyz`) unlocks with a passkey (Face ID / Touch ID / Windows Hello) instead of typing your 12-word recovery phrase every time. Until Option E Phase 1, that passkey lived only on the device that created it — lose the device *and* your written phrase, and the vault was gone for good.

Phrase escrow closes that gap: your recovery phrase can be **encrypted under your passkey** and either stored with TotalReclaw (so a synced passkey recovers it on any device) or downloaded as a file you keep yourself. TotalReclaw cannot decrypt either copy — only the passkey that sealed it can.

This is a **SPA-only** feature. Agent clients (Hermes, the MCP server, NanoClaw, the OpenClaw plugin, ZeroClaw) don't use it — they hold credentials via their own device-bound storage (macOS Keychain / Linux Secret Service, see `docs/guides/headless-deployment.md`) and your recovery phrase, and neither read nor write escrow records.

## Your three recovery options

Nothing here changes rung 3 — your written recovery phrase always works, on any client, forever. Escrow adds two more, in order of convenience:

| Option | What it needs | Reaches |
|---|---|---|
| **1. Local passkey unlock** | this browser, this device | this device only |
| **2. Backup to TotalReclaw** | your passkey (works across devices if it's synced via iCloud Keychain / Google Password Manager) | any device where that passkey is available |
| **2b. Encrypted backup file** | your passkey + the file, wherever you saved it | anywhere you kept the file |
| **3. Recovery phrase** | the 12 words | anywhere, any client, forever |

You can enable both 2 and 2b, either, or neither. Declining is never a dead end — you can turn on a backup later from Settings.

## Setting it up

When you create a vault, after writing down and confirming your phrase, you're asked:

> **Back up your recovery phrase?**

- **Back up my phrase** — stores an encrypted copy with TotalReclaw. On a new device, tap "Recover with passkey" and, if your passkey synced, you're back in with one biometric prompt — no typing.
- **Download an encrypted file instead** — no TotalReclaw storage at all. You get a `totalreclaw-backup-YYYY-MM-DD.json` file, opens only with the same passkey. **Where you save it matters** — see the caveat below.
- **Not now** — proceeds with no backup. Enable one later from **Settings → Passkeys & backup**.

You can also enable escrow for a passkey that already exists, or download a fresh file at any time, from Settings — you'll be asked to re-enter your phrase once (it isn't stored anywhere after setup), then it's sealed under one passkey assertion.

## The file caveat

If you choose the encrypted file, save it **somewhere other than the account that holds your passkey**. If your passkey is an iCloud Keychain passkey and you save the file to iCloud Drive, or a Google Password Manager passkey and you save it to Google Drive, then anyone who compromises that one account has both halves — the file protects you far less than it looks like it does. A different provider, or a USB drive, is safer.

Downloading a new file never disables an older one — every file you've ever downloaded keeps working for as long as you have it and the passkey that sealed it.

## Recovering a vault

On a new or wiped device, `app.totalreclaw.xyz` offers:

- **Recover with passkey** — one biometric prompt, no typing, if this passkey has a backup with TotalReclaw.
- **Open a backup file** — pick your downloaded file, one biometric prompt.
- **Use recovery phrase** — type your 12 words; this device enrols its own fresh passkey.
- **Create a new vault** — starts a separate, empty vault (confirmed first — your old vault isn't touched, just not what you're looking at).

If a passkey doesn't match any backup, or a file was sealed by a different passkey, you'll see a clear message and be offered phrase entry — never a dead end.

## What escrow cannot do

If you lose **both** your passkey and your written phrase, nothing recovers the vault — by design. A mechanism that could recover it without either would mean someone other than you could too, which is exactly what end-to-end encryption exists to prevent. Keep your written phrase somewhere safe regardless of whether you also use escrow.

## Managing backups

**Settings → Passkeys & backup** lists every relay-stored backup with its label and dates, and lets you remove one — useful when an authenticator is lost or you suspect it was stolen. Removing a backup only affects recovery *with that passkey*; your phrase and every other backup keep working. If a lost authenticator might be in someone else's hands, removing its backup record isn't enough on its own — the phrase itself may already have been read. Treat that the same as any other exposed recovery phrase: if your memories are sensitive, start a new vault.

## Technical detail

For the crypto envelope, wire contract, and full recovery-ladder writeup, see `docs/specs/web/phrase-escrow-relay.md`, `docs/specs/web/spa-passkey-unlock.md`, and `docs/specs/web/phrase-escrow-recovery.md` in the internal specs repo.
