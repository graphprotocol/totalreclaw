# QA Autopilot

Family of autonomous regression workflows that exercise TotalReclaw's user-facing surfaces against staging / preview / prod, and (on failure) open a labeled issue on the public repo for triage by `tr-triage-and-fix` or any other coding agent.

> **Canonical QA index lives privately at `totalreclaw-internal/docs/qa/README.md`.** It covers the full three-tier QA stack — autopilot (this doc), the `qa-totalreclaw` human-in-loop skill that drives real-user QA against the Hetzner VPS, and the `tr-triage-and-fix` autonomous fix loop. This file is the public, autopilot-only surface; everything VPS-side or skill-side is documented privately.

## Family framework

All autopilot workflows share these conventions:

- **Live on the internal repo** (`p-diogo/totalreclaw-internal`). Phrases, PATs, screenshots, and raw transcripts stay private. Only a sanitized issue body escapes to the public repo.
- **One label**, `qa-autopilot`, across every surface. One label = one triage queue. The issue **title prefix** differentiates: `qa-autopilot:vault-spa: ...`, `qa-autopilot:relay: ...`, `qa-autopilot:cross-client: ...`.
- **Workflow filename**: `qa-autopilot-<surface>.yml`. Surface names match the title prefix.
- **Three trigger shapes**:
  - `workflow_dispatch` (manual + cross-repo `gh workflow run` dispatch)
  - `schedule` (cron baseline against prod / staging)
  - (optional) `repository_dispatch` from the deploy workflow of the surface being tested
- **Common output**: stdout JSON with the fields `target`, `reachedX` (per surface), `*ErrorCount`, `visibleError`. Driver exits non-zero on any regression signal; workflow uploads `qa-output/*` as a 14-day artifact and opens the issue.
- **Common secrets** on the internal repo:
  - `QA_RECOVERY_PHRASE` — BIP-39 phrase for a **dummy** vault (zero real assets). Reused across surfaces.
  - `PUBLIC_ISSUE_PAT` — fine-grained PAT with `Issues: Read and write` on `p-diogo/totalreclaw` only.
- **On the public repo (`p-diogo/totalreclaw`)**:
  - `INTERNAL_DISPATCH_PAT` — fine-grained PAT with `Actions: Read and write` on `p-diogo/totalreclaw-internal` only. Used by deploy workflows to fire the matching autopilot.

## Current coverage

| Surface | Workflow | Driver | Status |
|---|---|---|---|
| **Vault SPA** | `qa-autopilot.yml` | `totalreclaw/tools/qa-vault.mjs` | Live ✅ |
| Relay smoke | `qa-autopilot-relay.yml` (planned) | `totalreclaw/tests/e2e-relay/smoke-test.ts` | Not wired |
| Managed-mode end-to-end | `qa-autopilot-managed.yml` (planned) | `totalreclaw-internal/e2e/` | Not wired |
| Cross-client parity | `qa-autopilot-cross-client.yml` (planned) | `totalreclaw/tests/parity/cross-impl-test.ts` + `totalreclaw-internal/e2e/cross-client/` | Not wired |
| Agent lifecycle (Docker) | `qa-autopilot-agent.yml` (planned) | `totalreclaw-internal/e2e/` (OpenClaw Docker) | Not wired |
| Pro tier upgrade | `qa-autopilot-pro-upgrade.yml` (planned) | None — needs Stripe-live-mode test path | Not wired |

Each planned surface ships as its own PR following the same template as the vault-SPA workflow. Add to this table as they land.

---

## Vault SPA autopilot (jobs[0])

End-to-end regression harness for the vault SPA at `app/`. Drives a headless browser to the vault against any deployed URL, captures console + network + visible errors, and opens an issue on the public repo when something regresses.

The driver is **flow-adaptive** — it detects the entry surface at runtime and drives whichever the app presents:

- **Legacy phrase-entry** (current production): paste-and-derive form (`input[placeholder="word 1"]`) → submit → `/vault`.
- **Passkey "Keeper"** (PR #329, `feat/spa-functional`): the app routes to `/bootstrap`. The driver attaches a CDP WebAuthn virtual authenticator (`ctap2` / `internal` / PRF-capable — mirroring the SPA's own E2E at `app/e2e/bootstrap-unlock.mjs`), then drives the **"I have a recovery phrase" → restore** flow. Restore derives keys from the *same* known QA phrase (so it lands on the real vault with real memories, equivalent to the legacy flow) and enrols a passkey via the virtual authenticator; the SPA then lands on `/memory`.

Both surfaces converge on the vault view (`/vault` legacy, `/memory` passkey), and the report's `reachedVault` is true for either — so the internal workflow's jq gate is surface-agnostic and needs **no change** when #329 merges. The stdout summary adds a `surface` field (`legacy` | `passkey` | `unknown`) for triage; all pre-existing keys (`reachedVault`, `pageErrorCount`, `consoleErrorCount`, `networkFailureCount`, `visibleError`) are preserved. Phrase-safety is unchanged: phrase only from keychain/env, redacted from all output paths.

## Pieces

| Piece | Where | What |
|---|---|---|
| `tools/qa-vault.mjs` | `p-diogo/totalreclaw` (public) | Playwright driver. Reads phrase from keychain locally or `QA_RECOVERY_PHRASE` env in CI. Redacts the phrase + any contiguous 3-word slice from all output before it touches stdout, JSON, or screenshots. Non-zero exit on any regression signal. |
| `.github/workflows/deploy-app.yml` | `p-diogo/totalreclaw` (public) | After a successful Cloudflare Pages preview deploy, fires `gh workflow run qa-autopilot.yml` on the internal repo with the preview URL + PR number. Gated behind `INTERNAL_DISPATCH_PAT`; deploys still flow if the secret is missing. |
| `.github/workflows/qa-autopilot.yml` | `p-diogo/totalreclaw-internal` (private) | Runs the driver, uploads the report + screenshot as a 14-day artifact, opens a public-repo issue on failure. Triggers: `workflow_dispatch`, daily cron 07:30 UTC against prod, and the cross-repo dispatch above. |

## Required secrets

Both are GitHub Actions repository secrets (not environment secrets — the workflow declares no `environment:` directive).

| Secret | Repo | Type | Purpose |
|---|---|---|---|
| `QA_RECOVERY_PHRASE` | `totalreclaw-internal` | BIP-39 phrase | The driver types this into the SPA. **Must be a dummy seed** (no real assets) — the matching Smart Account will receive automated traffic, and the screenshot/report artifacts are accessible to anyone with write access to the internal repo. |
| `PUBLIC_ISSUE_PAT` | `totalreclaw-internal` | Fine-grained PAT | Used by the internal workflow to create issues on `p-diogo/totalreclaw`. Scope to that one repo only; permissions: **Issues: Read and write**, everything else "No access". |
| `INTERNAL_DISPATCH_PAT` | `totalreclaw` (public) | Fine-grained PAT | Used by `deploy-app.yml` to invoke `gh workflow run qa-autopilot.yml`. Scope to `p-diogo/totalreclaw-internal` only; permissions: **Actions: Read and write**. |

If a PAT is missing the workflow doesn't crash — the deploy-app step short-circuits with a `::notice::` and the QA chain stays dark.

## Local invocation (no CI)

```bash
# one-time
security add-generic-password -a totalreclaw -s totalreclaw-qa-phrase -U -w
# (prompts for the dummy phrase; not echoed to terminal or shell history)

# any time after
cd tools
npm install
npx playwright install chromium

# headless against a preview
node qa-vault.mjs https://pr-237.totalreclaw-app.pages.dev

# or headed against local dev
node qa-vault.mjs http://localhost:5173 --headed
```

Reports + screenshots land in `tools/qa-output/` (gitignored).

## What "regression" means

The driver exits non-zero (and the workflow opens an issue) if **any** of these are true after the paste-and-derive flow:

- Page navigation threw
- The recovery-phrase form never rendered
- The `/vault` route was never reached
- A red `text-red-600` / `role="alert"` element became visible
- One or more `pageerror` events fired
- One or more console `error`-level messages logged
- One or more network requests failed (`requestfailed` Playwright event)

`200 OK` HTTP responses that carry GraphQL `errors[]` in their body are caught too — the visible-error gate fires when the SPA surfaces them.

## What gets redacted before leaving the runner

`redactPhrase()` in the driver replaces:

- the full phrase string (literal `replaceAll`)
- every contiguous 3-word slice of the phrase

with `[REDACTED_PHRASE]` / `[REDACTED_PHRASE_SLICE]` placeholders. This runs over every console line, every `pageerror.message`/`.stack`, every captured response body (capped at 2 KB per response), and the `visibleError` string before any of them are written to the JSON report or printed. Screenshots are taken after the form is submitted, so the rendered input is no longer on screen — but the screenshot itself is *not* OCR-scrubbed, which is one more reason the seed must be a dummy.

## Triggering manually

```bash
# Run against the prod alias
gh workflow run qa-autopilot.yml \
  -R p-diogo/totalreclaw-internal

# Run against a specific preview
gh workflow run qa-autopilot.yml \
  -R p-diogo/totalreclaw-internal \
  -f target_url=https://pr-237.totalreclaw-app.pages.dev \
  -f pr_number=237

# Watch the run
gh run list --workflow=qa-autopilot.yml -R p-diogo/totalreclaw-internal --limit 3
```

## What happens on failure

1. Internal workflow uploads `tools/qa-output/*` as `qa-report-<run_id>` artifact (14 days).
2. Internal workflow opens an issue on `p-diogo/totalreclaw` titled `qa-autopilot: regression on <target>`, labeled `qa-autopilot`, body containing:
   - run summary (`reachedVault`, error counts, visible error text)
   - link to the run on the internal repo (the artifact is reachable from there)
   - the redacted stdout JSON inline
   - the PR number, if dispatched from a preview deploy

From there the existing `tr-triage-and-fix` skill (or any other triage agent) can be pointed at the issue URL to run the fix loop.

## When to disable

- A relay outage or staging-down event will produce a real cascade of `qa-autopilot` issues. Either temporarily set the label to auto-close, or `gh workflow disable qa-autopilot.yml -R p-diogo/totalreclaw-internal` until the upstream is back.
- Schema migrations on the subgraph that change `Fact` fields will trip the driver until the SPA query is updated; expect issues during those windows.
