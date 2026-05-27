# Production deployment — credentials at rest

This guide covers two hardening patterns for production container deployments of the TotalReclaw plugin:

1. **Encrypted volume at the host layer** — LUKS / dm-crypt unlocks once at host boot; the container sees a regular `~/.totalreclaw` directory and the plaintext `credentials.json` lives only on an encrypted block device. Defeats disk theft / host snapshot exfiltration. Operator unlock burden = once per host reboot.
2. **External credential provider** — the plugin loads its `CredentialsFile` from a secret manager (Railway secrets, Docker Compose secrets, Kubernetes secrets, HashiCorp Vault / AWS Secrets Manager / GCP Secret Manager via an ops wrapper) instead of from `~/.totalreclaw/credentials.json`. The secret manager is the canonical source of truth; the plugin never persists the mnemonic to the container filesystem.

The two patterns compose — typical production stack uses encrypted volume **plus** external provider for defense in depth. Both are opt-in. Default behavior is unchanged (`credentials.json` written to `~/.totalreclaw/` with mode `0600`, as documented in [SECURITY.md](../SECURITY.md)).

> **Scope:** this is the Phase 3 deliverable from the credentials-at-rest roadmap. Phase 1 (chmod 600 startup checks) and Phase 2 (desktop OS-keychain) are out of scope here; see SECURITY.md for the full roadmap.

---

## Pattern 1 — LUKS / dm-crypt host volume

### When to use

- Self-hosted VPS where you own the host kernel (Hetzner Cloud, OVH, bare-metal, any IaaS that lets you `cryptsetup`).
- You're already running a Docker / Podman container for the plugin host (OpenClaw, Hermes, NanoClaw).
- You're willing to unlock the volume manually (or via a keyfile on a separate KMS-protected volume) on each host reboot.

### One-time host setup

```bash
# 1. Provision a dedicated block device for credentials. Sizes ≥ 64 MiB
#    are plenty — credentials.json is < 1 KiB. Using a separate device
#    keeps the rest of the host filesystem out of the encryption envelope.
sudo cryptsetup luksFormat /dev/sdb1

# 2. Open the encrypted device once. cryptsetup will prompt for the
#    passphrase you set above.
sudo cryptsetup luksOpen /dev/sdb1 tr-credentials

# 3. Format + mount.
sudo mkfs.ext4 /dev/mapper/tr-credentials
sudo mkdir -p /var/lib/tr-credentials
sudo mount /dev/mapper/tr-credentials /var/lib/tr-credentials

# 4. chmod so the container UID can read.
#    Inside the standard OpenClaw / Hermes Docker image, the plugin
#    runs as UID 1000 (`node`). Match that UID on the host or chown
#    to the UID the operator picked.
sudo chown 1000:1000 /var/lib/tr-credentials
sudo chmod 700 /var/lib/tr-credentials
```

### Per-boot unlock

Add to `/etc/crypttab` for automatic unlock at boot via a keyfile, OR keep manual unlock and unlock interactively after each host reboot:

```bash
# Manual unlock (after each host reboot)
sudo cryptsetup luksOpen /dev/sdb1 tr-credentials
sudo mount /dev/mapper/tr-credentials /var/lib/tr-credentials

# Or fully automatic (less secure — keyfile becomes a single point of failure):
# /etc/crypttab line:
#   tr-credentials /dev/sdb1 /root/keyfiles/tr-credentials.key luks
# /etc/fstab line:
#   /dev/mapper/tr-credentials /var/lib/tr-credentials ext4 defaults,nofail 0 2
```

### Container mount

Mount the encrypted directory into the container at the path the plugin uses:

```bash
docker run \
  -v /var/lib/tr-credentials:/home/node/.totalreclaw \
  -e TOTALRECLAW_CREDENTIALS_PATH=/home/node/.totalreclaw/credentials.json \
  totalreclaw/openclaw:latest
```

What this gets you:
- `credentials.json` lives on the encrypted block device.
- Container restart = transparent (the device stays unlocked while the host is running).
- Host reboot = operator unlocks once.
- `docker save` of the running container does **not** include the credential file (it's a bind mount from outside the container's writable layer).
- Disk theft / host snapshot of `/dev/sdb1` = encrypted blob without the LUKS passphrase.

### What this does NOT protect against

- A live attacker who reaches the unlocked filesystem (root on the host, container escape, etc.). Mitigation: tight host hardening, run unprivileged containers, limit kernel capabilities.
- The recovery phrase being readable by anything that can read the file inside the running container (the plugin itself, the LLM agent if compromised). Mitigation: pair Pattern 1 with Pattern 2.

---

## Pattern 2 — External credential provider

### When to use

- Cloud-managed deployments (Railway, Fly.io, Render, K8s clusters) where the platform already exposes secrets to containers via env vars or mounted files.
- Multi-host / multi-replica deployments where copying a per-host LUKS volume is impractical.
- You're using a managed vault (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager, Hetzner Vault) and want the plugin to load credentials from there instead of from disk.

### How it works

Set `TOTALRECLAW_CREDENTIALS_PROVIDER=external` and provide the credential JSON via one of two transports (the env-var transport wins if both are set):

| Env var                                       | Transport                          | Typical platform                                                                |
|-----------------------------------------------|------------------------------------|---------------------------------------------------------------------------------|
| `TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON`       | Inline JSON in an env var          | Railway secrets, Heroku config vars, Docker `--env-file`, K8s `envFrom`         |
| `TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH`       | Path to a JSON file in the FS      | Docker Compose `secrets:`, K8s secret `volumeMount`, tmpfs from ops wrapper     |

The JSON payload uses the same schema as `credentials.json`:

```json
{
  "userId": "u_aBcDeF...",
  "salt": "0123abcd...",
  "mnemonic": "twelve word bip-39 recovery phrase here",
  "scope_address": "0xabcd...",
  "firstRunAnnouncementShown": true
}
```

**Read-only by design.** The external provider does not write back to the secret manager — your secret manager is the canonical source of truth, and a write-back would split it. If the plugin generates a new mnemonic (first-run on a fresh deploy), set `firstRunAnnouncementShown: true` in the secret manager payload to suppress the in-product announcement; the secret manager already authored the mnemonic outside the plugin.

### Example — Docker Compose secret file

```yaml
services:
  totalreclaw:
    image: totalreclaw/openclaw:latest
    environment:
      TOTALRECLAW_CREDENTIALS_PROVIDER: external
      TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH: /run/secrets/tr-credentials
    secrets:
      - tr-credentials

secrets:
  tr-credentials:
    file: ./secrets/tr-credentials.json   # gitignored; chmod 600
```

Docker mounts `secrets/tr-credentials.json` at `/run/secrets/tr-credentials` inside the container with mode `0444`, owned by root. The plugin reads it once at boot.

### Example — Railway / Heroku-style env-var secret

Set `TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON` in the platform's secrets UI to the JSON payload above. No filesystem changes:

```bash
TOTALRECLAW_CREDENTIALS_PROVIDER=external
TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON='{"userId":"u_...","salt":"...","mnemonic":"..."}'
```

### Example — Kubernetes secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: tr-credentials
type: Opaque
stringData:
  credentials.json: |
    {"userId":"u_...","salt":"...","mnemonic":"..."}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: totalreclaw
spec:
  template:
    spec:
      containers:
        - name: totalreclaw
          image: totalreclaw/openclaw:latest
          env:
            - name: TOTALRECLAW_CREDENTIALS_PROVIDER
              value: external
            - name: TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH
              value: /run/secrets/tr-credentials/credentials.json
          volumeMounts:
            - name: tr-credentials
              mountPath: /run/secrets/tr-credentials
              readOnly: true
      volumes:
        - name: tr-credentials
          secret:
            secretName: tr-credentials
            defaultMode: 0400
```

### Example — AWS Secrets Manager / Hetzner Vault via ops wrapper

Managed vaults that don't natively inject env vars into containers (AWS Secrets Manager, GCP Secret Manager, Hetzner Vault) need a short ops wrapper script that runs **before** the plugin host. The wrapper retrieves the secret and writes it to a tmpfs path the plugin reads:

```bash
#!/usr/bin/env bash
# /usr/local/bin/tr-fetch-credentials.sh
# Run as ExecStartPre on the systemd unit (or initContainer on K8s) that
# launches the plugin host.
set -euo pipefail

MOUNT=/run/tr-credentials                            # tmpfs is fine
mkdir -p "$MOUNT" && chmod 700 "$MOUNT"
aws secretsmanager get-secret-value \
  --secret-id totalreclaw/prod \
  --query SecretString --output text > "$MOUNT/credentials.json"
chmod 400 "$MOUNT/credentials.json"
```

Then launch the plugin host with:

```bash
TOTALRECLAW_CREDENTIALS_PROVIDER=external
TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH=/run/tr-credentials/credentials.json
```

On systemd, tmpfs is auto-wiped at host reboot; on K8s, the initContainer pattern gets you the same property per pod.

### What this does NOT cover (yet)

Stage 1 of cred-3 (this guide + plugin TS provider) ships the abstraction; subsequent stages wire the same abstraction through the Hermes Python client and the MCP server. Until then, deployments that run Hermes alongside the plugin will still need to manage Hermes credentials by file. Track in [cred-3](https://github.com/p-diogo/totalreclaw-internal/issues/263).

---

## Combining both patterns

Production-grade containerized deployments typically use Pattern 2 (external provider) as primary and Pattern 1 (LUKS) as defense in depth for any other state the plugin writes (cache files, billing state, pair sessions — none of which contain the mnemonic but all of which are user-data-adjacent).

Recommended posture for a Hetzner-style VPS running the plugin in a container:

1. LUKS-encrypt the volume that hosts `/var/lib/docker` (or whichever directory holds container writable layers + bind mounts).
2. Configure `TOTALRECLAW_CREDENTIALS_PROVIDER=external` with the credential JSON in Hetzner Vault (or whichever secret manager you use).
3. Use an ops wrapper to materialize the JSON onto a tmpfs path at container start; mount that path into the container.

This way the plugin **never writes the mnemonic to a persistent disk inside its own container**, and even a snapshot of the underlying block device is useless without both the LUKS passphrase and the vault credential.

---

## Migration checklist (existing `credentials.json` → external)

1. Read your existing `~/.totalreclaw/credentials.json`. Copy the JSON.
2. Store it in your secret manager under whatever name you prefer (Railway: `TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON`; AWS SM: secret id `totalreclaw/prod`; etc.).
3. Update the deployment config to set `TOTALRECLAW_CREDENTIALS_PROVIDER=external` + the appropriate transport env var.
4. Redeploy. The plugin will boot, find `credentialsProvider=external`, and load from the secret manager. Confirm via `tr status --json` (the userId + scope_address should match what was in your old `credentials.json`).
5. Once you've confirmed external mode works, securely delete the on-disk file: `shred -u ~/.totalreclaw/credentials.json` (or `rm -P` on macOS).

Reversing the migration is symmetric — set `TOTALRECLAW_CREDENTIALS_PROVIDER=file` (or just unset the env var; `file` is the default) and write the JSON back to `~/.totalreclaw/credentials.json` with mode `0600`.

---

## See also

- [SECURITY.md](../SECURITY.md) — threat model, chmod 600 enforcement, the full four-phase credentials-at-rest roadmap
- [env-vars-reference.md](env-vars-reference.md) — full list of `TOTALRECLAW_*` env vars
- [openclaw-setup.md](openclaw-setup.md) — basic single-host install
- [`totalreclaw-internal#263`](https://github.com/p-diogo/totalreclaw-internal/issues/263) — cred-3 tracking issue (covers staging into Hermes + MCP after this stage)
