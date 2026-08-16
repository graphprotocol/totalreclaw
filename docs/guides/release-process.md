# Release process

TotalReclaw ships via a **release-candidate (RC) then promote** flow. Every
stable version on npm / PyPI / crates.io was first validated as an RC against
real-user QA on staging.

> **Retired channel — ClawHub (2026-07-30).** The OpenClaw plugin used to ship
> to ClawHub in parallel with npm. A registry-side defect (duplicate `skills`
> rows for one slug make every slug-scoped `.unique()` lookup throw) left
> publishes permanently invisible: `clawhub publish` returns OK with a release
> id, the listing never advances, and `scan` / `delete` / `hide` all fail. Ours
> froze at `3.3.13`; `3.4.0` and `3.4.1` both published into the void. Upstream:
> [openclaw/clawhub#3212](https://github.com/openclaw/clawhub/issues/3212).
> npm is now the only channel for the plugin.

> **Retired workflow — `promote-rc.yml` (2026-08-16, issue #625).** The
> "publish RC, then promote the same artifact to stable" two-step no longer
> exists as a working path. `promote-rc.yml` failed on npm, PyPI, AND
> crates.io the same day it was last used (core 2.6.0 → all three
> registries) — every failure was a gate bug, not a content problem, and the
> gate bugs turned out to be structural (npm Trusted Publisher binds
> `@totalreclaw/core` / `@totalreclaw/mcp-server` to `npm-publish.yml` only,
> exactly like the plugin before it; no publish workflow ever created the RC
> git tag its tag-backfill step depended on; PyPI/crates.io "promote" were
> always no-op placeholders). The file now fails fast with a pointer instead
> of silently misleading. **The single procedure for every package, RC or
> stable, is dispatching the relevant `publish-*.yml` with `-f
> release-type=stable`** — see "Standard flow" below.

This guide is for maintainers. Users install stable artifacts via the
integration-specific setup guides (`openclaw-setup.md`, `hermes-setup.md`,
etc.) and don't need to know about RCs.

## Environment binding rule (HARD INVARIANT)

**RC artifacts default to STAGING. Stable artifacts default to PRODUCTION.**

| `release-type` | Default `TOTALRECLAW_SERVER_URL` | Audience |
|---|---|---|
| `rc` | `https://api-staging.totalreclaw.xyz` | QA + maintainers preparing a stable rollout. **NEVER point real users here.** |
| `stable` | `https://api.totalreclaw.xyz` | Real users. Production data, durable, real SLA. |

Why this matters:
- Staging has **no SLA**, may be wiped or reset between RC cycles, and cannot
  serve real-user vaults reliably. Pointing a real user at staging means they
  can lose their account between QA runs.
- Production has **billed Stripe tiers, real chain anchoring, real backups**.
  Pointing a QA run at it generates noise, costs money, and pollutes
  production analytics with throwaway test accounts.

**Build-time binding** (shipped in plugin 3.3.3+ / hermes 2.3.3+):
- Publish workflows bake `defaultServerUrl` into the artifact based on
  `release-type`. RC artifacts contain the staging URL literal; stable
  artifacts contain the production URL literal. Pre-publish CI guard fails
  if a stable artifact contains `api-staging` or an RC artifact contains
  `api.totalreclaw.xyz`.
- Runtime sanity check warns loudly if an RC build is somehow pointed at
  production OR a stable build at staging.
- `RC mode` agents emit a prominent banner at install confirming the user
  is on staging-only and should NOT use the install for production data.
- User env (`TOTALRECLAW_SERVER_URL=...`) overrides always win — only the
  default changes between RC and stable.

**Pre-3.3.3 caveat:** every artifact had `api-staging.totalreclaw.xyz` baked
in regardless of `release-type`. Stable users had to set
`TOTALRECLAW_SERVER_URL` manually to hit production. From 3.3.3 forward the
default is correct out-of-the-box.

## Flow

```
PR merged to main
    │
    ▼
Trigger publish-*.yml with release-type=rc
    │
    ▼
RC artifacts land on public registries (pre-release channel)
    │
    ▼
Auto-QA (Phase 1: manual dispatch / Phase 2: webhook-triggered)
    │
    ├─ NO-GO ─────► Yank RC, fix, publish rc.N+1
    │
    └─ GO
        │
        ▼
Dispatch the SAME publish-*.yml with release-type=stable
    │
    ▼
Stable artifacts land on public registries (latest / default tag)
    │
    ▼
Announce (GitHub release, Telegram, website)
```

## Client tool-shipping checklist (Hermes)

A Hermes agent tool only works when it exists in **four** places. Shipping fewer than
four passes unit tests and still leaves the tool invisible or unroutable in real chat:

1. `python/src/totalreclaw/hermes/schemas.py` — the schema (`NAME = {...}`)
2. `python/src/totalreclaw/hermes/__init__.py` `register()` — the `ctx.register_tool(...)`
   call (**this is what puts it in the model's function list**)
3. `python/src/totalreclaw/hermes/plugin.yaml` `provides_tools` — the manifest
4. `docs/guides/hermes-setup.md` Tools table — routing steering for the agent

This bug class has now shipped **twice**: pin/unpin in 2.3.1 (schemas+manifest, no
register — caught rc6) and `totalreclaw_top_up` in 2.4.5rc10 (schema+handler, in
NEITHER manifest nor register — caught only by the S5 re-QA, internal#412). The
`test_hermes_plugin_manifest_parity.py` shield originally pinned only
manifest↔register, so a tool missing from BOTH slipped through. That gap is now
closed: `TestSchemasRegisterParity` in
`python/tests/test_hermes_plugin_manifest_parity.py` (issue #427) anchors on
`schemas.py` and asserts every `totalreclaw_*` schema dict is EITHER registered by
`register()` (stable or RC path) OR listed in the in-test `DORMANT_SCHEMAS`
allow-list with a per-entry reason — so a schema+handler that is registered nowhere
(the `totalreclaw_top_up` bug class) now fails CI. It also asserts the inverse
(every registered `totalreclaw_*` name traces back to an authored schema). Reviewers
should still eyeball all four places on any tool-adding PR, but a forgotten
registration is now caught automatically.

## Cross-registry version scheme

The RC suffix differs between registries because their pre-release formats
differ. Ship with the table below in front of you.

| Registry   | RC format             | Example            | User install (RC)                                      |
|------------|-----------------------|--------------------|--------------------------------------------------------|
| npm        | `<base>-rc.<N>`       | `2.1.0-rc.1`       | `npm install @totalreclaw/core@rc`                     |
| crates.io  | `<base>-rc.<N>`       | `2.1.0-rc.1`       | `cargo add totalreclaw-core@=2.1.0-rc.1`               |
| PyPI       | `<base>rc<N>` (PEP440)| `2.1.0rc1`         | `pip install totalreclaw==2.1.0rc1`                    |

- **npm** uses a `rc` dist-tag (not `latest`). Users who run
  `npm install @totalreclaw/core` without `@rc` get the current stable.
- **crates.io** has no dist-tag system. Cargo's pre-release semver rule
  means `"2.0"` / `"2.1"` / `"^2"` all refuse pre-release versions
  implicitly; users must explicitly pin `=2.1.0-rc.1`.
- **PyPI** `pip install foo` ignores pre-release resolutions by default; users
  pin with `==2.1.0rc1` or pass `--pre`.

## Standard flow (Wave 1 / Phase 1)

1. **Land the PR.** Merge to `main`. The version bump in `package.json` /
   `pyproject.toml` / `Cargo.toml` SHOULD already be the intended stable
   version — don't pre-suffix with `-rc.N`. The workflow adds the suffix at
   run time.

2. **Dispatch the relevant publish workflow** via the GitHub Actions UI or
   `gh workflow run`:

   ```bash
   # npm packages (core, client, mcp-server, nanoclaw, plugin, all)
   gh workflow run npm-publish.yml \
     -f package=all \
     -f release-type=rc \
     -f rc-number=1

   # crates.io (totalreclaw-core, totalreclaw-memory, all)
   gh workflow run publish-crates.yml \
     -f crate=all \
     -f release-type=rc \
     -f rc-number=1

   # PyPI — PyO3 core
   gh workflow run publish-pypi.yml \
     -f release-type=rc \
     -f rc-number=1

   # PyPI — Python client
   gh workflow run publish-python-client.yml \
     -f release-type=rc \
     -f rc-number=1

   ```

3. **Wait for green builds.** The workflows do not touch the stable
   `latest` channel in `rc` mode, so even if a build is flaky, the stable
   surface is untouched.

4. **Run auto-QA** against the published RCs. Use the `qa-totalreclaw`
   skill in `rc-mode`; point it at the RC versions. Reports land in
   `totalreclaw-internal/docs/notes/QA-<integration>-<YYYYMMDD>.md`.

5. **On GO verdict: dispatch the SAME `publish-*.yml` workflow again, with
   `release-type=stable`.** There is no separate "promote" workflow
   (`promote-rc.yml` was retired 2026-08-16, issue #625 — see the note at
   the top of this guide). This is a fresh publish of the same source tree
   at the clean stable version string, not a repack of the RC artifact —
   true for npm, PyPI, AND crates.io alike, since none of the three
   registries has a retag mechanism.

   First, bump the checked-in version (`package.json` / `pyproject.toml` /
   `Cargo.toml`) to the clean stable string via a normal PR to `main` (drop
   any `-rc.N` suffix — the RC suffix only ever existed in-workflow, never
   committed). Then dispatch:

   ```bash
   # npm (core, client, mcp-server, nanoclaw, plugin, all)
   gh workflow run npm-publish.yml \
     -f package=core \
     -f release-type=stable

   # crates.io (totalreclaw-core, totalreclaw-memory, all)
   gh workflow run publish-crates.yml \
     -f crate=all \
     -f release-type=stable

   # PyPI — PyO3 core
   gh workflow run publish-pypi.yml \
     -f release-type=stable

   # PyPI — Python client
   gh workflow run publish-python-client.yml \
     -f release-type=stable
   ```

   Each workflow's own "Guard — refuse to republish an existing version"
   step (npm) / "already uploaded" tolerance (crates) makes this idempotent
   to re-run — the same property `promote-rc.yml` used to advertise, now
   provided directly by the publish workflows instead of a second layer on
   top of them.

   This has been the OpenClaw plugin's ONLY npm path since 2026-07-13 (npm
   allows exactly one Trusted Publisher per package, and the plugin's is
   `npm-publish.yml`) — the same constraint that, as of 2026-08-16, also
   applies to `@totalreclaw/core` and `@totalreclaw/mcp-server`. It is now
   simply the procedure for every package on every registry, not a
   plugin-specific exception.

6. **Advertise the new stable to the fleet — set `LATEST_STABLE_PYTHON` on BOTH
   relay services.** This is the single env flip that makes the automatic update
   notice fire fleet-wide: the relay serves it in `/v1/billing/status` →
   `features.latest_stable_python`, and every Hermes client compares it against
   its installed `__version__` and nudges the user to say "update TotalReclaw"
   (once per 24h). Set it **only after** the PyPI stable publish above has
   succeeded — advertising a version that isn't installable yet would nudge
   users toward a `pip install` that resolves to an older build. Use the STABLE
   version string (no `rc`), e.g. `2.4.5`:

   ```bash
   # Staging first (verify), then production. Match to the version you just published.
   railway variables --set "LATEST_STABLE_PYTHON=2.4.5" -s totalreclaw
   railway variables --set "LATEST_STABLE_PYTHON=2.4.5" -s totalreclaw-production
   # `railway variables --set` triggers an automatic redeploy; it may time out on
   # the response yet still apply — re-read to confirm before retrying:
   railway variables -s totalreclaw --json | grep -i latest_stable_python
   railway variables -s totalreclaw-production --json | grep -i latest_stable_python
   ```

   Leaving it unset (or stale at a prior version) is safe — the feature is dark
   until configured, so the only cost of forgetting this step is that the
   announcement never fires. Verify with
   `curl -s https://api.totalreclaw.xyz/v1/billing/status | grep latest_stable_python`.

7. **Generate the changelog.** Dispatch `changelog.yml` for the stable
   version (see "Changelog automation" below). It opens a reviewable PR
   with the auto-generated section; review and merge it.

8. **Announce.** GitHub release, Telegram notification, website update.

## Changelog automation (git-cliff)

`python/CHANGELOG.md` entries are generated from
[Conventional Commits](https://www.conventionalcommits.org/) by
`.github/workflows/changelog.yml` (driven by `cliff.toml`). This replaces
hand-assembling the "what changed" bullet list each release.

**Design — decoupled on purpose.** The workflow is standalone and never runs
inside the publish workflows. A changelog failure can't block a release, and
every generated section lands as a normal, reviewable PR. git-cliff renders one
bullet per commit subject; enrich the prose before merging if you like — the
Keep-a-Changelog preamble and all prior entries are preserved (the new section
is spliced in above the first `## [` heading, not blindly prepended).

**Dispatch it after a stable publish:**

```bash
# Normal run (a python-v<prev> tag already exists -> range auto-detected):
gh workflow run changelog.yml -f version=2.4.5

# First run / bootstrap (no python-v* tag exists yet) -> give the lower bound
# explicitly: the tag or SHA where the PREVIOUS stable shipped.
gh workflow run changelog.yml -f version=2.4.5 -f from_ref=<prev-stable-sha>

# Preview only — render to the run's job summary, no file edit, no PR:
gh workflow run changelog.yml -f version=2.4.5 -f from_ref=<sha> -f dry_run=true
```

**Monorepo scoping.** This repo ships several independently-versioned packages
from one history with a shared tag namespace. Two guards keep the python range
honest: a per-package tag namespace (`python-v[0-9]*`, so the plugin's
`v3.3.x` tags can't bound the range) and a path filter (`--include-path
"python/**"`). Other packages reuse the same `cliff.toml` and override both on
the CLI (`-f tag_namespace=...`, `-f package_glob=...`).

**Bootstrap note.** Until a stable publish creates the first `python-v<version>`
tag, `from_ref` is REQUIRED (the workflow fails loud rather than dumping the
whole history). A planned fast-follow tags `python-v<version>` automatically on
stable publish, after which `from_ref` can be omitted.

## Troubleshooting

### RC publish fails mid-matrix

`publish-pypi.yml` has a matrix of 5 wheel builds. If one fails (e.g.
ARM64 linker error), rerun the single failed matrix leg via the Actions
UI. The `publish` job only fires after all `build-wheels` jobs succeed,
so no partial artifacts are shipped.

### QA flags a regression

Don't promote. Patch the bug, land the PR, publish `rc.N+1` (increment
the `rc-number` input). The previous `rc.N` stays on the registry with
the `rc` dist-tag overwritten — users pinning a specific RC still get the
old one, but the `rc` moniker points at the newest RC.

To actively yank a broken RC:

- **npm**: `npm deprecate @totalreclaw/core@2.1.0-rc.1 "superseded by rc.2"`
- **PyPI**: yank via the PyPI web UI (Projects → Manage → Releases →
  "Yank release"). Yanked versions remain installable via explicit pin
  but disappear from normal resolution.
- **crates.io**: `cargo yank --version 2.1.0-rc.1 totalreclaw-core`

### Stable publish fails (formerly "promote fails")

There is no separate promote step to debug — the stable dispatch runs the
exact same `publish-*.yml` job graph as the RC dispatch did, just with
`release-type=stable`. So a stable-publish failure is a normal
`publish-*.yml` failure. Check:

1. Did the version-bump PR (dropping the `-rc.N` suffix) actually merge to
   `main` before you dispatched? The workflow builds from the checked-in
   version, not from the RC's in-workflow-mutated one.
2. Each package's own "Guard — refuse to republish an existing version"
   step (npm) fails loud if that exact version is already on the registry
   — bump the version rather than re-dispatch with the same one.
3. npm specifically: confirm which auth path the package actually uses.
   `@totalreclaw/core`, `@totalreclaw/mcp-server`, and
   `@totalreclaw/totalreclaw` (the plugin) publish via OIDC Trusted
   Publisher bound to `npm-publish.yml` — no `NODE_AUTH_TOKEN` involved, so
   an expired `NPM_TOKEN` secret is never the cause for those three.
   `@totalreclaw/client` and `@totalreclaw/skill-nanoclaw` still use the
   classic `NODE_AUTH_TOKEN` secret.

### Stable rollback

If a stable release ships and is later discovered to be broken:

- **npm**: `npm deprecate @totalreclaw/core@2.1.0 "broken; use 2.0.5"` —
  users get a warning on install. You cannot unpublish after 72 hours.
- **PyPI**: yank via the web UI. Users on `pip install totalreclaw` (no
  pin) will resolve to the previous version.
- **crates.io**: `cargo yank --version 2.1.0 totalreclaw-core`.

## Manual escape hatches

- **Skip RC for urgent hotfixes.** `release-type=stable` is still the
  default. For a true hotfix (e.g. security CVE), you can dispatch
  `release-type=stable` directly with a tested patch. Mark it as a
  hotfix in the announcement so the next feature wave doesn't skip the
  QA gate.
- **Re-running the stable dispatch is idempotent.** Each `publish-*.yml`
  job's own guard treats an already-published version as a tolerated
  no-op (npm: explicit "already published" catch; crates: "already
  uploaded" grep on `cargo publish`'s output) rather than a hard failure —
  so re-dispatching `release-type=stable` after a partial failure is safe.

## Policy reference

The release-candidate-then-QA gate is MANDATORY per the internal rule in
`totalreclaw-internal/CLAUDE.md` (shipped 2026-04-18, formalized
2026-04-20). See also the QA automation roadmap at
`totalreclaw-internal/docs/plans/2026-04-20-qa-automation-roadmap.md`
for the progression from Phase 1 (manual dispatch) to Phase 3 (auto-promote).
