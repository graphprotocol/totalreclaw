# mcp/skill-clawhub/ — ClawHub publish source for the MCP-first skill

This directory bundles the MCP-first skill artifacts that get published to
ClawHub under the `totalreclaw` slug for `openclaw skills install totalreclaw`
to resolve.

**Source of truth for SKILL.md is `mcp/SKILL.md`**, which also ships inside
the npm tarball. The ClawHub publish workflow copies it here at publish time
to keep the two surfaces in lockstep.

## Contents

- `skill.json` — ClawHub manifest (slug `totalreclaw`, version pulled from
  `mcp/package.json`, install instructions point at `openclaw mcp set
  totalreclaw …`).
- `SKILL.md` — symlink (or copy at publish time) of `../SKILL.md`.

## Publish

The `.github/workflows/publish-clawhub.yml` workflow has a `skill-source`
input: `plugin` (default — legacy) or `mcp` (this directory). When MCP is
selected, the workflow:

1. Copies `mcp/SKILL.md` → `mcp/skill-clawhub/SKILL.md`
2. Stamps the version from `mcp/package.json` into `skill.json`
3. Runs `clawhub publish ./mcp/skill-clawhub --slug totalreclaw …`

Until the workflow is wired (rc.2 ships scaffolding only), the existing
plugin-bundled SKILL.md remains the published source. The mcp-first SKILL.md
ships *only* in the npm tarball via the `files: [..., "SKILL.md"]` entry in
`mcp/package.json` — agents that install via `openclaw mcp set totalreclaw
'{"command":"npx","args":["-y","@totalreclaw/mcp-server"]}'` get it from the
tarball directly.
