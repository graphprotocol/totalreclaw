# QA Autopilot — Vault SPA

End-to-end regression harness for the vault SPA at `app/`. Drives a headless browser through the recovery-phrase → `/vault` flow against any deployed URL, captures console + network + visible errors, and opens an issue on the public repo when something regresses.

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
