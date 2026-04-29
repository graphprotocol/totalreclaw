# Release process

TotalReclaw ships via a **release-candidate (RC) then promote** flow. Every
stable version on npm / PyPI / crates.io / ClawHub was first validated as an
RC against real-user QA on staging.

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
Trigger promote-rc.yml (or dispatch publish-*.yml with release-type=stable)
    │
    ▼
Stable artifacts land on public registries (latest / default tag)
    │
    ▼
Announce (GitHub release, Telegram, website)
```

## Cross-registry version scheme

The RC suffix differs between registries because their pre-release formats
differ. Ship with the table below in front of you.

| Registry   | RC format             | Example            | User install (RC)                                      |
|------------|-----------------------|--------------------|--------------------------------------------------------|
| npm        | `<base>-rc.<N>`       | `2.1.0-rc.1`       | `npm install @totalreclaw/core@rc`                     |
| crates.io  | `<base>-rc.<N>`       | `2.1.0-rc.1`       | `cargo add totalreclaw-core@=2.1.0-rc.1`               |
| PyPI       | `<base>rc<N>` (PEP440)| `2.1.0rc1`         | `pip install totalreclaw==2.1.0rc1`                    |
| ClawHub    | `<base>-rc.<N>`       | `3.1.0-rc.1`       | `clawhub install totalreclaw --version 3.1.0-rc.1`     |

- **npm** uses a `rc` dist-tag (not `latest`). Users who run
  `npm install @totalreclaw/core` without `@rc` get the current stable.
- **crates.io** has no dist-tag system. Cargo's pre-release semver rule
  means `"2.0"` / `"2.1"` / `"^2"` all refuse pre-release versions
  implicitly; users must explicitly pin `=2.1.0-rc.1`.
- **PyPI** `pip install foo` ignores pre-release resolutions by default; users
  pin with `==2.1.0rc1` or pass `--pre`.
- **ClawHub** has no dedicated pre-release channel. RC publishes tag the
  version with `rc` only (no `latest`), so UI / search keeps pointing at the
  last stable release. QA installs with `--version <rc>`.

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

   # ClawHub (provide BASE version; workflow appends -rc.<N>)
   gh workflow run publish-clawhub.yml \
     -f version=3.1.0 \
     -f release-type=rc \
     -f rc-number=1
   ```

3. **Wait for green builds.** The workflows do not touch the stable
   `latest` channel in `rc` mode, so even if a build is flaky, the stable
   surface is untouched.

4. **Run auto-QA** against the published RCs. Use the `qa-totalreclaw`
   skill in `rc-mode`; point it at the RC versions. Reports land in
   `totalreclaw-internal/docs/notes/QA-<integration>-<YYYYMMDD>.md`.

5. **On GO verdict:** trigger `promote-rc.yml` for the npm / ClawHub pieces
   (they ship new artifacts at the stable version). For PyPI and crates.io,
   dispatch `publish-*.yml` with `release-type=stable` — those registries
   have no retag mechanism, so "promote" is a fresh publish of identical
   source at the stable version string.

   ```bash
   gh workflow run promote-rc.yml \
     -f package=core \
     -f rc-version=2.1.0-rc.1
   # (stable-version auto-derived to 2.1.0)
   ```

6. **Announce.** GitHub release, Telegram notification, website update.

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
- **ClawHub**: `clawhub delete totalreclaw@3.1.0-rc.1` (soft-delete;
  owners / moderators can restore).

### Promote fails

The `promote-rc.yml` workflow's `validate-rc-exists` job refuses to run
against an RC version that isn't actually published. Check:

1. Is the `rc-version` input spelled exactly right? (`-rc.1` for
   npm/crates/ClawHub; `rc1` for PyPI.)
2. Did the RC publish succeed? Check the `publish-*.yml` run history.
3. If the RC was yanked/deprecated, it may fail lookup. Republish RC at
   the next rc-number and re-promote.

### Stable rollback

If a stable release ships and is later discovered to be broken:

- **npm**: `npm deprecate @totalreclaw/core@2.1.0 "broken; use 2.0.5"` —
  users get a warning on install. You cannot unpublish after 72 hours.
- **PyPI**: yank via the web UI. Users on `pip install totalreclaw` (no
  pin) will resolve to the previous version.
- **crates.io**: `cargo yank --version 2.1.0 totalreclaw-core`.
- **ClawHub**: republish the previous stable version to restore
  `latest` tag.

## Manual escape hatches

- **Skip RC for urgent hotfixes.** `release-type=stable` is still the
  default. For a true hotfix (e.g. security CVE), you can dispatch
  `release-type=stable` directly with a tested patch. Mark it as a
  hotfix in the announcement so the next feature wave doesn't skip the
  QA gate.
- **Re-run promote as idempotent.** `promote-rc.yml` republishes the
  stable artifact each time. Running it twice with the same inputs
  usually results in npm's "version already exists" branch, which the
  workflow tolerates.

## Policy reference

The release-candidate-then-QA gate is MANDATORY per the internal rule in
`totalreclaw-internal/CLAUDE.md` (shipped 2026-04-18, formalized
2026-04-20). See also the QA automation roadmap at
`totalreclaw-internal/docs/plans/2026-04-20-qa-automation-roadmap.md`
for the progression from Phase 1 (manual dispatch) to Phase 3 (auto-promote).
