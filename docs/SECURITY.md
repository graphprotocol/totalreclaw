# TotalReclaw security model

> Last reviewed: 2026-05-09 (cred-1)
> If you find a vulnerability, please email security@totalreclaw.xyz with details. Do not file public issues for live exploits.

## What's protected (in-transit + on-chain)

- **Recovery phrase never crosses any LLM context.** It is generated and entered exclusively in the user's browser during the pair flow, encrypted with x25519 + XChaCha20-Poly1305 against the gateway's ephemeral public key, and forwarded through the relay as opaque ciphertext. The relay sees ciphertext only. The agent / LLM provider never sees plaintext at any point. This is the canonical contract the project commits to.
- **Memories are encrypted at the user's device** with a key derived from the recovery phrase before they are submitted on-chain. The blockchain stores ciphertext + blind indices for search; the data is not decryptable by anyone except the holder of the recovery phrase.
- **Storage is decentralized.** Memories live on Gnosis mainnet (all tiers) and are indexed by The Graph subgraph. The TotalReclaw relay only forwards encrypted bundles to the bundler and proxies subgraph reads — it never sees plaintext, can't read memories, and could be replaced by any compatible relay without losing data.
- **Search is privacy-preserving.** Searches are blind-trapdoor lookups against the subgraph; the relay sees the trapdoor (a SHA-256 digest of query tokens), not the query text.
- **Account-setup PIN is dual-channel.** The 6-digit PIN is delivered by the agent in chat. The QR code / URL is opened on a separate device or window. The relay validates both before forwarding the encrypted phrase. A third party who steals only the URL or only the PIN cannot complete pairing.

## What's NOT protected (at-rest)

**The recovery phrase is stored in plaintext at `~/.totalreclaw/credentials.json` after a successful pair.**

```json
{
  "version": 1,
  "userId": "…",
  "salt": "…",
  "mnemonic": "abandon abandon … about",
  "scope_address": "0x…"
}
```

Only filesystem protection: file mode `0600` (owner-only read).

This is a deliberate tradeoff. The plugin's daemon-mode auto-extraction needs the mnemonic on every poll cycle to derive the Smart Account address and sign UserOps for on-chain submission. A headless server (Docker container, VPS) has no GUI session to unlock an OS keychain or prompt for a passphrase, so we ship plaintext-at-rest with chmod 600 today.

### Threat model — what this does and doesn't defend against

| Threat | chmod 600 defends? | Real risk |
|---|---|---|
| Same-UID sibling process (e.g. agent shell-tool, malicious skill running as the same user) reads the file | No | **High** in container deployments — every shell tool the agent runs has the same UID as the plugin |
| Disk image steal (`docker save`, rsync of a Docker volume, drive theft, lost laptop without FDE) | No | **Medium** for VPS, **lower** for laptops with FileVault / BitLocker / LUKS |
| Cloud-synced backup leak (Time Machine, Dropbox, automatic OS backup) | No | **Medium** for desktop natives — the backup process bypasses file mode |
| Another login user on the same machine reads the file | Yes (mode 600) | **Low** for single-user setups |
| Full root compromise of the host | No | **High** but out of scope of any application-level defense |

### What you should do today

- **Run the plugin under its own UID** if you can. Don't share the UID with untrusted code. In a container, that's automatic — the container has its own user namespace.
- **Use full-disk encryption** on the host. macOS FileVault, Windows BitLocker, Linux LUKS / dm-crypt. This defeats disk steal.
- **Disable cloud-synced backups** of `~/.totalreclaw/` if your backup tool is opt-out. Add `~/.totalreclaw/credentials.json` to your backup exclude list. Cloud-sync of a recovery phrase = same as posting it on a public bucket.
- **For VPS / cloud deployments** — use the provider's encrypted-volume feature (Hetzner CX volumes, Railway encrypted volumes, AWS EBS with KMS, etc.). Mount the credentials directory off the encrypted volume. Container restart inside a running host = transparent. Host reboot = host operator unlocks once.
- **Don't pass the recovery phrase as an environment variable** (`TOTALRECLAW_RECOVERY_PHRASE=…`). Env vars leak into process listings (`/proc/<pid>/environ`), child-process inheritance, container inspect output (`docker inspect`), and crash dumps. The supported path is the credentials.json file with chmod 600. The env-var override exists for one-shot CLI testing, not production.

## Roadmap — better at-rest defense

The plaintext-at-rest tradeoff is documented and being addressed in phases. The full UX matrix and phasing is tracked in the private ops tracker (issue #229).

- **Phase 1 (cred-1 — shipped)** — Document the threat model (this file) and enforce chmod 600 at plugin startup. The plugin now **refuses to load** if `credentials.json` is found with permissions broader than `0600`. Fix: `chmod 600 ~/.totalreclaw/credentials.json` then restart the gateway. The plugin also warns if the file is detected on a tmpfs or shared-volume mount (`/tmp/`, `/dev/shm/`, `/run/`, `/var/run/`).
- **Phase 2 (cred-2 — Hermes wrap helper landed)** — Desktop OS-keychain wrap (macOS Keychain, Linux Secret Service / libsecret, Windows Credential Manager). The Hermes (Python) client ships the wrap helper as an **opt-in** extra: `pip install totalreclaw[keychain]`. On a desktop session with an unlocked keychain, the secret moves out of `credentials.json` into the OS keychain and the file keeps only non-secret discovery metadata (`userId`, `salt`, `scope_address`) plus a `keychain_wrapped: true` flag. **Headless / container deployments** (no D-Bus session bus / no logged-in Keychain), and installs without the `[keychain]` extra, **fall back to status quo** — plaintext at rest + chmod 600 — and record `keychain_wrapped: false`. No prompt, no crash; you simply stay on the Phase-1 protection level. Automatic migration of an existing plaintext `credentials.json` is wired in a follow-up (cred-5 / cred-10).
- **Phase 3 (3.4.x)** — Container deployment patterns. Documented LUKS / dm-crypt setup, plus optional `TOTALRECLAW_CREDENTIALS_PROVIDER=vault` config for HashiCorp Vault / Railway secrets / AWS Secrets Manager / GCP Secret Manager.
- **Phase 4 (optional, 3.5+)** — TPM / Secure Enclave hardware-bound wrap. Defeats `docker save` and disk theft completely, at the cost of platform-specific code paths.

The phasing reflects which user segments take priority. Today the active user base is container-deployers (pop-os docker, Hetzner VPS); Phase 3 helps them most. Desktop natives (future Hermes laptop installer, Cursor / Codex plugins) ship later, and Phase 2 is the right answer for them.

## Reporting a vulnerability

Email security@totalreclaw.xyz with:

- A clear description of the issue
- Steps to reproduce (commands, environment, version)
- The plugin version (`tr status --json` reports it)
- Any relevant logs, with the recovery phrase and PIN redacted

We aim to acknowledge within 48 hours. Do not file public issues for unpatched vulnerabilities.
