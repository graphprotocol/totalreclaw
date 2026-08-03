# TotalReclaw security model

> Last reviewed: 2026-08-02 (Option E Phase 2 — derived-material provisioning)
> If you find a vulnerability, please email security@totalreclaw.xyz with details. Do not file public issues for live exploits.

## Threat-model ceiling — read this before anything else

**Phase 2 does not improve memory confidentiality at a compromised host, and it does
not by itself make a host compromise revocable.**

The derived `encryption_key` still materialises whole in the agent's RAM on every
recall, and every fact's ciphertext is publicly readable from the Gnosis subgraph by
anyone. An attacker who reads the bundle off a compromised server can decrypt the
user's entire vault, exactly as they could today with the phrase. Phase 2 removes the
**root** (the BIP-39 mnemonic and the 64-byte seed) from the host; it does not remove
the working keys, because the E2EE pipeline cannot function without them.

Additionally — and this differs from the framing in the Phase 2 brief — with
`signing.kind = "owner-eoa"` (the Phase 2 shipping configuration) the bundle
still carries **Smart Account owner authority**. An owner-key leak is not revocable on
a canonical `SimpleAccount`, so incident response for a compromised host is still
"rotate the vault identity", not "re-pair one server". The "scoped, revocable,
re-pair-one-server" property arrives only with `signing.kind = "session-key"`, which
requires a future signing-delegation phase.

**Third ceiling: any holder of derived key material can verify a guessed mnemonic
offline, and Phase 2 does not change that.** Every derived value is a deterministic
function of the BIP-39 seed — `auth_key = HKDF-SHA256(ikm = seed, salt = seed[0..32],
info = "totalreclaw-auth-key-v1")`, and likewise for the other three. So a candidate
phrase can be tested by computing PBKDF2 once and comparing the result: **`auth_key`,
`encryption_key`, `dedup_key` and `lsh_seed` are each a root-guessing oracle of
essentially identical cost**, since the 2048-iteration PBKDF2 dominates and the
trailing HKDF is free.

This is structural, not incidental. It follows from deriving both authentication and
encryption material from one phrase, and it cannot be removed without changing the
derivation scheme over an immutable corpus. It is a **pre-existing property since
v0.3**, present today wherever a phrase-configured client runs and wherever the relay
stores `auth_key_hash`; Phase 2 neither introduces nor worsens it, and neither fixes
it.

Practically it is infeasible against a CSPRNG-generated 12-word phrase (128 bits). The
guarantee does **not** hold for imported phrases of unknown provenance: the SPA accepts
anything passing `validateMnemonic`, which checks wordlist membership and the BIP-39
checksum and nothing about entropy source. For that population the oracle is a live
accelerant.

Since the derivation cannot change, **the phrase-entropy precondition is the only place a
control could live**, and that means the import path rather than any storage decision.
Be realistic about how much is available there, though: a 12-word phrase is 128 bits of
entropy plus a 4-bit checksum, and a value drawn from `/dev/urandom` is
indistinguishable from `SHA-256("password")` once it is a phrase. **Entropy is a
property of the generation process and is not recoverable from the string**, so
"validate entropy at import" is not implementable. What is available is narrower: refuse
or warn on membership of a known-weak set (published test vectors, precomputed
brain-wallet phrases), and an advisory at import that a phrase we did not generate
carries a guarantee we cannot bound. Neither is a fix. Anyone scoping work here should
start from that ceiling rather than from "add entropy validation". Tracked as a product
question outside this phase.

What Phase 2 **does** deliver: it kills the *portable* root on the host (the property
that makes a leak spread beyond this product — phrase reuse across services, wallet
import), and it lands the schema, transport, core API, and migration so that a future
scoped/revocable signing phase is a one-field change rather than a second forced
migration of every install. See
`docs/specs/totalreclaw/client-consistency.md#credential-material` for the technical
contract this ceiling applies to.

## What's protected (in-transit + on-chain)

- **Recovery phrase never crosses any LLM context.** It is generated and entered exclusively in the user's browser during the pair flow, encrypted with x25519 + XChaCha20-Poly1305 against the gateway's ephemeral public key, and forwarded through the relay as opaque ciphertext. The relay sees ciphertext only. The agent / LLM provider never sees plaintext at any point. This is the canonical contract the project commits to.
- **Memories are encrypted at the user's device** with a key derived from the recovery phrase before they are submitted on-chain. The blockchain stores ciphertext + blind indices for search; the data is not decryptable by anyone except the holder of the recovery phrase.
- **Storage is decentralized.** Memories live on Gnosis mainnet (all tiers) and are indexed by The Graph subgraph. The TotalReclaw relay only forwards encrypted bundles to the bundler and proxies subgraph reads — it never sees plaintext, can't read memories, and could be replaced by any compatible relay without losing data.
- **Search is privacy-preserving.** Searches are blind-trapdoor lookups against the subgraph; the relay sees the trapdoor (a SHA-256 digest of query tokens), not the query text.
- **Account-setup PIN is dual-channel.** The 6-digit PIN is delivered by the agent in chat. The QR code / URL is opened on a separate device or window. The relay validates both before forwarding the encrypted phrase. A third party who steals only the URL or only the PIN cannot complete pairing.

## What's protected at-rest — desktop keychain wrap (cred-2)

On a **desktop** with a usable OS keychain (macOS Keychain, Linux Secret Service),
the recovery phrase is **not** stored in `credentials.json`. cred-2 (internal#262)
wraps it: the phrase lives in the OS keychain under the wallet's EOA address, and
`~/.totalreclaw/credentials.json` carries a non-secret **marker** in its place:

```json
{
  "version": 1,
  "userId": "…",
  "salt": "…",
  "mnemonic": "__keychain__:v1:0x<eoa-address>",
  "keychain_wrapped": true,
  "scope_address": "0x…"
}
```

The marker is a single token (the EOA address carries no whitespace) and fails
BIP-39 validation at **every** consumer — the 12-word count gate, the
`eth_account` checksum, and the Rust key-derivation core all reject it — so a
tool that doesn't understand the marker can neither mistake it for a phrase nor
silently derive a different wallet. The wrap is applied on pair/restore and
opportunistically on the first boot of a legacy plaintext file.

**macOS: `keyring` is a mandatory dependency, zero argv exposure (#558).**
`pip install totalreclaw` on macOS always installs `keyring>=24` (a
`sys_platform == 'darwin'` marker in `pyproject.toml`), which wraps through
the native Security framework — the secret never touches a subprocess
argument list. Earlier versions fell back to a `security
add-generic-password -w <secret>` subprocess when `keyring` wasn't
installed; that command briefly exposed the phrase in the local process
list during the one-time wrap (local-attacker-only — the login keychain is
per-user, and the same user can already read any of their own keychain
items via `security find-generic-password` — but cheap to eliminate).
That subprocess WRITE path has been removed entirely. The Linux backend
(`secretstorage` / `keyring`) is unaffected — the plaintext fallback there
was always structural (no universal Linux keychain), so `keyring` remains
an optional install via the `totalreclaw[keychain]` extra.

## What's NOT protected at-rest — containers / headless / kill-switch

On a **headless host with no OS keychain** (Docker container, VPS), or when the
operator arms the `TOTALRECLAW_NO_KEYCHAIN=1` kill-switch, the wrap silently
falls back to the pre-cred-2 shape and the recovery phrase **is** stored in
plaintext at `~/.totalreclaw/credentials.json`:

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

This is a deliberate tradeoff. The daemon-mode auto-extraction needs the mnemonic
on every poll cycle to derive the Smart Account address and sign UserOps. A
headless server has no GUI session to unlock an OS keychain or prompt for a
passphrase, so on those hosts we ship plaintext-at-rest with chmod 600 and
recommend an encrypted volume (Phase 3).

> **Actionable path for containers/headless:** the **external credential
> provider** (cred-3, wired into the Hermes daemon boot path) lets you inject the
> phrase from a secret manager (systemd `LoadCredential`, Docker `secrets:`,
> Kubernetes Secrets, HashiCorp Vault / AWS / GCP SM) so `credentials.json` is
> never written to disk at all. The full worked examples + threat model are in
> the [headless deployment guide](guides/headless-deployment.md). This is the
> recommended posture for any long-lived server.

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
- **Don't pass the recovery phrase as an environment variable** (`TOTALRECLAW_RECOVERY_PHRASE=…`). Env vars leak into process listings (`/proc/<pid>/environ`), child-process inheritance, container inspect output (`docker inspect`), and crash dumps. For a headless server prefer the **external credential provider** with a file-mount transport (see the [headless deployment guide](guides/headless-deployment.md)); the plaintext `credentials.json` file with chmod 600 is the fallback. The env-var override exists for one-shot CLI testing, not production.

## Roadmap — better at-rest defense

The plaintext-at-rest tradeoff is documented and being addressed in phases. The full UX matrix and phasing is tracked in the private ops tracker (issue #229).

- **Phase 1 (cred-1 — shipped)** — Document the threat model (this file) and enforce chmod 600 at plugin startup. The plugin now **refuses to load** if `credentials.json` is found with permissions broader than `0600`. Fix: `chmod 600 ~/.totalreclaw/credentials.json` then restart the gateway. The plugin also warns if the file is detected on a tmpfs or shared-volume mount (`/tmp/`, `/dev/shm/`, `/run/`, `/var/run/`).
- **Phase 2 (cred-2 — shipped 2026-07-20)** — Desktop OS-keychain wrap (macOS Keychain via `keyring`, Linux Secret Service via `secretstorage` / `keyring`). The mnemonic is stored in the OS keychain and `credentials.json` carries a non-secret `__keychain__:v1:<eoa>` marker. Container / headless deployments with no keychain, or hosts with `TOTALRECLAW_NO_KEYCHAIN=1`, fall back to the status-quo plaintext file (chmod 600). Marker fail-loud + opportunistic upgrade of legacy plaintext on first boot are covered by `tests/test_credentials_wrap.py`. **[#558, fast-follow]** `keyring>=24` is now a mandatory dependency on macOS (platform marker), retiring the `security add-generic-password -w` subprocess write path that briefly exposed the phrase in the local process list.
- **Phase 3 (cred-3 / shipped)** — Container / headless external-secret provider. The Hermes daemon (`agent/state.py` `_try_auto_configure`) routes credential discovery through `get_credential_provider()`, so `TOTALRECLAW_CREDENTIALS_PROVIDER=external` + a file-mount transport (`TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH`) injects the phrase from a secret manager without ever writing `credentials.json` to disk. The LUKS / dm-crypt host-volume pattern is documented in [production-deployment.md](guides/production-deployment.md); the full worked examples (systemd `LoadCredential`, Docker Compose, Kubernetes, file-mode fallback) + threat model are in the [headless deployment guide](guides/headless-deployment.md). End-to-end boot + unit coverage in `tests/test_external_credentials_boot.py`.
- **Phase 4 (optional, 3.5+)** — TPM / Secure Enclave hardware-bound wrap. Defeats `docker save` and disk theft completely, at the cost of platform-specific code paths.
- **Option E Phase 2 (shipped, Hermes only)** — a different axis, not a continuation of the phase numbering above: removes the **portable root** (the mnemonic and the 64-byte seed) from the host entirely, transparently, on every plugin load. The host holds a `derived-bundle-v1` bundle (four derived vault keys + an `owner-eoa` signing key) instead of the phrase. See the threat-model ceiling at the top of this file — this does **not** improve at-rest confidentiality of the working keys (they're still needed for the E2EE pipeline to function) and does **not** make a host compromise revocable yet. Migration detail: [hermes-setup.md](guides/hermes-setup.md#local-phrase-migration-option-e-phase-2).

The phasing reflects which user segments take priority. Today the active user base is container-deployers (pop-os docker, Hetzner VPS); Phase 3 helps them most. Desktop natives (future Hermes laptop installer, Cursor / Codex plugins) ship later, and Phase 2 is the right answer for them.

## Reporting a vulnerability

Email security@totalreclaw.xyz with:

- A clear description of the issue
- Steps to reproduce (commands, environment, version)
- The plugin version (`tr status --json` reports it)
- Any relevant logs, with the recovery phrase and PIN redacted

We aim to acknowledge within 48 hours. Do not file public issues for unpatched vulnerabilities.
