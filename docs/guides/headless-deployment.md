# Headless / server deployment guide

> Covers: how to run the **TotalReclaw Hermes client** (the Python package on
> [PyPI](https://pypi.org/project/totalreclaw/)) on a headless host — VPS,
> Docker container, Kubernetes pod, or a `systemd` system service — with the
> recovery phrase supplied by a secret manager instead of a plaintext file.
> Internal#512, Phase 4. Last reviewed: 2026-08-01.

This is the authoritative guide for **server/headless** deployments. For the
desktop OS-keychain wrap see [SECURITY.md](../SECURITY.md); for the
TS-plugin-specific container patterns and the LUKS encrypted-volume deep dive
see [production-deployment.md](production-deployment.md).

---

## Why this guide exists

TotalReclaw is end-to-end encrypted (XChaCha20-Poly1305): the relay and the
chain never see plaintext. The one secret the host *does* need is the **recovery
phrase** — the BIP-39 seed every encryption key is derived from. On a laptop
that phrase is wrapped in the OS keychain ([cred-2](../SECURITY.md)); on a
**headless host with no keychain backend** the wrap is a no-op and the phrase
falls back to plaintext `~/.totalreclaw/credentials.json` (mode `0600`).

That plaintext-at-rest posture is acceptable for a throwaway test box but not
for a long-lived server. The Hermes client therefore also supports an
**external credential provider**: the phrase is injected by your secret manager
(Docker `secrets:`, Kubernetes Secret, systemd `LoadCredential`, HashiCorp Vault
agent, AWS/GCP Secrets Manager) and the on-disk `credentials.json` is never
written at all. This guide shows the four concrete ways to wire that up and is
honest about what each one does and does not protect against.

> **Read this once before copy-pasting any snippet:** the
> [Structural ceiling](#structural-ceiling--the-key-is-in-ram-at-every-recall)
> section. At-rest/injection hardening shrinks the exposure surface; it cannot
> make the decryption key absent at recall.

---

## Prerequisites

- The Hermes client installed: `pip install totalreclaw` (PyPI 2.4.6+). The
  `keyring` extra (`pip install totalreclaw[keychain]`) is **not** needed on a
  headless host — there is no keychain backend for it to talk to.
- Your **recovery phrase** from the pair flow (12 or 24 BIP-39 words). If you
  are restoring an existing vault, use the exact phrase you paired with.
- The relay URL: `https://api.totalreclaw.xyz` (production) or
  `https://api-staging.totalreclaw.xyz` (staging). Set via
  `TOTALRECLAW_SERVER_URL`. **Never point a production deploy at staging.**

---

## How the daemon resolves credentials at boot

At startup `AgentState._try_auto_configure` resolves the phrase in this order
(first hit wins):

1. **`TOTALRECLAW_RECOVERY_PHRASE` env var** — if set, used directly. This is
   the one-shot CLI path and is **not recommended for production**: the phrase
   lands in `/proc/<pid>/environ`, `docker inspect`, child-process inheritance,
   and crash dumps. It exists for quick CLI testing, not servers.
2. **The credential provider**, selected by `TOTALRECLAW_CREDENTIALS_PROVIDER`:
   - **`external`** (this guide) — the secret manager supplies the payload via a
     file mount or inline JSON. Read-only by design; **never silently falls back
     to file mode** (so a misconfigured deploy fails loudly instead of reading a
     stale on-disk file).
   - **`file`** (the default) — reads `~/.totalreclaw/credentials.json`, or the
     path in `TOTALRECLAW_CREDENTIALS_PATH`. On a desktop this is keychain-
     wrapped; on headless Linux it is plaintext `chmod 600`.

In `external` mode two transports are available; **inline JSON wins if both are
set**:

| Env var | Transport | Phrase in process env? | Typical platform |
|---|---|---|---|
| `TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH` | JSON file the secret manager mounts | **No** | systemd `LoadCredential`, Docker `secrets:`, K8s Secret `volumeMount`, tmpfs from a vault agent |
| `TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON` | Inline JSON in an env var | **Yes** | Railway/Heroku secrets, Docker `--env-file`, K8s `envFrom` |

**Prefer the file-mount transport** wherever the platform supports it — it keeps
the phrase out of the process environment listing entirely.

### Credential payload shape

The payload is the same JSON shape as `credentials.json`. The minimum that
works is just the phrase:

```json
{"mnemonic": "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"}
```

- `recovery_phrase` is accepted as an alias for `mnemonic` (legacy Python
  spelling).
- Optionally include `userId` and `salt` to restore a **specific existing
  account identity** verbatim (copy them from an existing `credentials.json`).
  Omitting them is fine for the common case — the phrase alone deterministically
  derives the canonical EOA + Smart Account (CREATE2).

---

## Option A — systemd `LoadCredential` (recommended)

This is the recommended posture for a self-managed VPS running Hermes as a
`systemd` system service. `systemd` reads a **root-owned** source file at unit
start and exposes it on a per-service tmpfs at
`/run/credentials/<unit-name>/`, owned by the service's `User` with mode
`0400`. The daemon user never reads `/etc/totalreclaw/creds.json` directly, the
phrase is never in the process environment, and `credentials.json` is never
written to disk.

```ini
# /etc/systemd/system/hermes-memory.service
[Unit]
Description=TotalReclaw Hermes memory agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hermes
Group=hermes

# --- Credential injection (file-mount transport, recommended) ---
# systemd reads the root-owned source file at start and exposes it on a
# per-service tmpfs at /run/credentials/hermes-memory.service/. The hermes
# user never touches /etc/totalreclaw/creds.json itself.
LoadCredential=totalreclaw.json:/etc/totalreclaw/creds.json

# Route TotalReclaw through the external provider and point it at the mount.
# %d expands to the per-service credentials directory.
Environment=TOTALRECLAW_CREDENTIALS_PROVIDER=external
Environment=TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH=%d/totalreclaw.json
Environment=TOTALRECLAW_SERVER_URL=https://api.totalreclaw.xyz

# Launch your Hermes host process. The credential wiring above is host-agnostic
# — see hermes-setup.md for the host-specific launch (Telegram bot / gateway /
# agent loop). Placeholder shown; replace with your real command.
ExecStart=/usr/bin/python -m hermes_agent
Restart=on-failure

# Hardening (see the hardening checklist below).
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
RestrictSUIDSGID=true
ReadWritePaths=/var/lib/hermes

[Install]
WantedBy=multi-user.target
```

`%d` is the systemd specifier for the credential directory; the literal
equivalent is
`/run/credentials/hermes-memory.service/totalreclaw.json`. Set up the
root-owned source file once:

```bash
sudo install -d -m 755 -o root -g root /etc/totalreclaw
sudo install -m 600 -o root -g root /dev/null /etc/totalreclaw/creds.json
# Edit /etc/totalreclaw/creds.json to contain: {"mnemonic":"… your phrase …"}
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-memory.service
```

For at-rest encryption of the source file itself, use
`LoadCredentialEncrypted=` (systemd 250+) instead of `LoadCredential=` — systemd
stores the secret encrypted and decrypts it with the host's TPM/keyring at unit
start.

---

## Option B — Docker Compose `secrets:`

For a containerized single-host deploy. Compose mounts the secret at
`/run/secrets/<name>` (mode `0444`, root-owned) — readable by the container
process via the mount, not present in the image or the process environment.

```yaml
# docker-compose.yml
services:
  hermes:
    image: <your-hermes-host-image>
    user: "1000:1000"
    environment:
      TOTALRECLAW_CREDENTIALS_PROVIDER: external
      TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH: /run/secrets/totalreclaw.json
      TOTALRECLAW_SERVER_URL: https://api.totalreclaw.xyz
    secrets:
      - totalreclaw.json
    restart: unless-stopped

secrets:
  totalreclaw.json:
    file: ./secrets/creds.json   # gitignored; chmod 600, root-owned, on the host
```

---

## Option C — Kubernetes Secret + volumeMount

Secret volumes are **tmpfs-backed by default** (node memory), so the phrase is
never written to the node's persistent disk. For managed vaults (HashiCorp
Vault Agent injector, External Secrets Operator, AWS/GCP Secrets Manager),
populate the same file via an initContainer or a projected volume — the env
vars below stay identical.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: totalreclaw-creds
type: Opaque
stringData:
  totalreclaw.json: |
    {"mnemonic":"abandon abandon … about"}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hermes-memory
spec:
  template:
    spec:
      containers:
        - name: hermes
          image: <your-hermes-host-image>
          env:
            - name: TOTALRECLAW_CREDENTIALS_PROVIDER
              value: "external"
            - name: TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH
              value: "/var/secrets/totalreclaw.json"
            - name: TOTALRECLAW_SERVER_URL
              value: "https://api.totalreclaw.xyz"
          volumeMounts:
            - name: totalreclaw-creds
              mountPath: /var/secrets
              readOnly: true
          securityContext:
            runAsNonRoot: true
            readOnlyRootFilesystem: true
      volumes:
        - name: totalreclaw-creds
          secret:
            secretName: totalreclaw-creds
            defaultMode: 0400
```

---

## Option D — file-mode fallback (plaintext, chmod 600)

The simplest deploy, and the default when no provider is configured. On headless
Linux the OS-keychain wrap ([cred-2](../SECURITY.md)) is a **no-op** (no
keychain backend detected), so this plaintext path is taken automatically. Set
`TOTALRECLAW_NO_KEYCHAIN=1` to force plaintext explicitly if a partial keychain
backend is present but unwanted.

```bash
mkdir -p ~/.totalreclaw
umask 077
printf '%s' '{"mnemonic":"abandon abandon … about"}' > ~/.totalreclaw/credentials.json
chmod 600 ~/.totalreclaw/credentials.json
# Optionally relocate the file:
#   export TOTALRECLAW_CREDENTIALS_PATH=/srv/tr/credentials.json
```

This is the least-protected option — pair it with full-disk encryption on the
host (LUKS/dm-crypt; see [production-deployment.md](production-deployment.md)).

**cred-1 guards** (enforced at boot):

- The client **refuses to load** if `credentials.json` has permissions broader
  than `0600`. Fix with `chmod 600`, then restart.
- The client **warns** if the file sits on a tmpfs or shared-volume mount
  (`/tmp/`, `/dev/shm/`, `/run/`, `/var/run/`).

---

## Inline-JSON transport (the other `external` transport)

If your platform only exposes secrets as environment variables (Railway secrets,
Heroku config vars, plain Docker `--env-file`, K8s `envFrom`), use the inline
transport instead of a file mount:

```bash
TOTALRECLAW_CREDENTIALS_PROVIDER=external
TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON='{"mnemonic":"abandon abandon … about"}'
```

This works, but the phrase lives in the process environment
(`/proc/<pid>/environ`, `docker inspect`, crash dumps) — the **same exposure
class as `TOTALRECLAW_RECOVERY_PHRASE`**. Use it only when the platform gives
you no file-mount option.

---

## Threat model — what each option protects against

No single option defends against a fully-compromised host; the differences are
about *which* exposure surface each one closes. The
[structural ceiling](#structural-ceiling--the-key-is-in-ram-at-every-recall)
applies to all of them.

| Option | Env-listing exposure (`/proc/environ`, `docker inspect`, crash dumps) | On-disk plaintext (`docker save`, disk theft, backup leak) | Host compromise → operational-key exfiltration |
|---|:---:|:---:|:---:|
| **A. systemd `LoadCredential`** | None — phrase not in env | None — tmpfs, not `credentials.json` | Still vulnerable |
| **B. Docker `secrets:`** | None | None — mounted, not in image | Still vulnerable |
| **C. K8s Secret volume** | None | None — tmpfs-backed | Still vulnerable |
| Inline JSON env (`_EXTERNAL_CREDENTIALS_JSON`) | **Phrase is in env** | None | Still vulnerable |
| `TOTALRECLAW_RECOVERY_PHRASE` env | **Phrase is in env** | None | Still vulnerable |
| **D. file mode (plaintext)** | None | **Yes — `credentials.json`** | Still vulnerable |

---

## Hardening checklist

- **Run as a dedicated unprivileged user.** Create a `hermes` user with no shell
  (`useradd --system --shell /usr/sbin/nologin hermes`); run the unit/container
  as that user. The credential source file should be owned `root:root` mode
  `0600` — the daemon user only ever sees the per-service exposed copy, never
  the source.
- **Restrict the credential file to that user.** systemd, Docker, and K8s all
  restrict the exposed mount to the service user by default; double-check the
  source file is not world-readable.
- **Encrypt the host disk.** LUKS/dm-crypt on a VPS, FileVault/BitLocker on a
  laptop. This defeats disk theft and snapshot exfiltration. See
  [production-deployment.md](production-deployment.md) for the LUKS setup.
- **Disable cloud-synced backups** of `~/.totalreclaw/` (Time Machine, Dropbox,
  etc.). Add the credential path to your backup exclude list.
- **Never pass the phrase via `TOTALRECLAW_RECOVERY_PHRASE` in production.** Use
  the file-mount transport (A/B/C) or, if impossible, the inline-JSON transport.
- **Lock the service down with the platform's sandbox.** systemd: `ProtectSystem`,
  `PrivateTmp`, `NoNewPrivileges`, `PrivateDevices`. Kubernetes: `runAsNonRoot`,
  `readOnlyRootFilesystem`, a minimal `securityContext`.
- **Restrict egress** to the relay domain only (`api.totalreclaw.xyz` or
  `api-staging.totalreclaw.xyz`) plus the bundler/subgraph hosts the relay
  proxies.

---

## Structural ceiling — the key is in RAM at every recall

This is the honest limit and it applies to **every** option above, including the
file-mount transports.

The symmetric decryption key is derived from the recovery phrase and must
materialize **whole in the agent process's RAM at every recall** (~140 ms per
search, unattended, on every poll cycle). At-rest hardening (encrypting the
file, wrapping in a keychain, injecting via a secret manager) shrinks the
surface where the *root phrase* and the *derived key* live — but it **cannot**
make the key absent at recall time, because recall needs it. A fully-compromised
host (root on the box, a malicious process sharing the daemon's UID, a container
escape) can therefore still exfiltrate operational keys by reading the daemon's
memory while it is running.

This is why **MPC / threshold custody was rejected for this asset** (see
internal#512): it would not have removed this in-RAM exposure and would have
added availability and operational complexity. The file-mount options in this
guide are the right defense for the *at-rest / injection* surface; the in-RAM
surface is defended by host hardening, not by credential plumbing.

---

## What's next — Phase 2 (derived material)

The future improvement that removes the **root** recovery phrase from the host
entirely is **pair-time derived-material provisioning** (Phase 2 of the
credentials-at-rest roadmap, tracked in the ops tracker). Instead of injecting
the root phrase so the daemon can derive keys from it, the pair flow provisions
per-session operational material and the host only ever holds that. The root
phrase stays on the user's device, never touching the server.

Phase 2 is **not** part of this guide (internal#512 Phase 4 ships the
external-secret plumbing documented here); it is the forward path for the "root
phrase never on the host" goal. Until it lands, the file-mount transports above
are the strongest available posture for a headless host.

---

## See also

- [SECURITY.md](../SECURITY.md) — full threat model, chmod-600 enforcement, and
  the four-phase credentials-at-rest roadmap.
- [production-deployment.md](production-deployment.md) — the LUKS/dm-crypt
  encrypted-volume deep dive and TS-plugin-specific container patterns.
- [hermes-setup.md](hermes-setup.md) — first-run setup and the host-specific
  launch command referenced by the `ExecStart` examples above.
- [deployment.md](deployment.md) — the canonical relay runbook
  (staging↔production service map).
- [self-hosted-deployment.md](self-hosted-deployment.md) — running your own
  PostgreSQL backend (note: the Hermes client speaks the managed-relay protocol
  only and cannot target a self-hosted server directly).
- [env-vars-reference.md](env-vars-reference.md) — the complete `TOTALRECLAW_*`
  environment-variable reference.
