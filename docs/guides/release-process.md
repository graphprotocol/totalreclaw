# Release process

TotalReclaw ships via a **release-candidate (RC) then promote** flow. Every
stable version on npm / PyPI / crates.io / ClawHub was first validated as an
RC against real-user QA on staging.

This guide is for maintainers. Users install stable artifacts via the
integration-specific setup guides (`openclaw-setup.md`, `hermes-setup.md`,
etc.) and don't need to know about RCs.

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

7. **Run the post-stable doc-cleanup checklist below.** Mandatory ceremony — same session as the promote, not deferred.

## After stable promote (MANDATORY post-publish checklist)

Run this immediately after every stable promote completes (npm + PyPI +
ClawHub all live on the `latest` channel). Same session as the promote, not
deferred to a follow-up task. The release ceremony isn't done until this
checklist clears.

1. **Bump `release-pipeline.md`.** Set Production version + status to
   `promoted` for each surface that just shipped, clear the Latest RC
   cell, append a Recent-history row dated today. Internal repo
   `docs/release-pipeline.md`.

2. **Drop `@rc` / `--pre` from default install paths in user-facing
   guides.** Stable is the new canonical install. Audit
   `docs/guides/openclaw-setup.md`, `docs/guides/hermes-setup.md`,
   `docs/guides/claude-code-setup.md`, `docs/guides/feature-comparison.md`,
   `docs/guides/client-setup-v1.md`, `docs/guides/beta-tester-guide-detailed.md`,
   and any guide referencing the just-shipped surfaces. `@rc` /
   `==<version>rc<N>` examples move to a clearly-labeled "Installing
   release candidates (advanced)" subsection at the bottom of each guide
   for power users. The default paste-this prompt and the canonical install
   commands MUST default to stable, NOT `@rc`.

3. **Audit + archive stale plans / specs.** Anything from the just-shipped
   RC cycle that is now SHIPPED should be marked SHIPPED inline or moved
   to `archive/plans/`. Anything redundant with a shipped spec can be
   deleted. When in doubt, leave + flag — don't delete ambiguous items.
   This sweep covers BOTH repos: public (`docs/plans/`, `docs/specs/`) and
   internal (`docs/plans/`, `docs/notes/`, `research/`).

4. **CHANGELOG dated entry.** Add a `## [X.Y.Z] - YYYY-MM-DD` block to
   `CHANGELOG-public.md` summarizing what shipped. Group by surface
   (plugin / python / mcp / core / nanoclaw). Cross-link to PR numbers
   where useful.

5. **`ROADMAP.md` update.** Move the just-shipped track from "In flight"
   to "Shipped — current stable" with the new version pins, surface
   what's next in the "Next" section, and bump the wave-status date at
   the top of the "Now / Next" block.

6. **Cross-repo verification.** Skim both repos one more time for stale
   `@rc` references, outdated version pins, or broken cross-links between
   the public guides and internal trackers. A rough `grep -rn @rc
   docs/guides/` in the public repo is a useful sanity check; `grep -rn
   "Latest RC" docs/release-pipeline.md` confirms cleared cells in the
   internal tracker.

These steps were done manually for the 3.3.1 + 2.3.1 + 3.2.1 stable
promote on 2026-04-27. From that promote forward, this checklist is
mandatory ceremony — skipping any step (especially #2 and #3) leaves the
docs misaligned with the published artifacts and forces a follow-up
cleanup PR. Future improvement: a post-stable workflow trigger that runs
the audit + opens the cleanup PR automatically (tracked as a Phase 3+
extension to the autonomous QA pipeline at
`totalreclaw-internal/docs/operations/autonomous-qa-pipeline.md`).

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
