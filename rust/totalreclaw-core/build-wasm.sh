#!/usr/bin/env bash
# Build the WASM package for @totalreclaw/core.
#
# Builds wasm-bindgen TWICE from the same crate at the same version:
#   - pkg/nodejs/  (--target nodejs) — existing consumers (plugin, mcp,
#                   nanoclaw) via the bare `@totalreclaw/core` specifier.
#   - pkg/web/     (--target web)    — browser/Vite consumers via the
#                   `@totalreclaw/core/web` subpath (e.g. the vault SPA).
#
# The two builds are composed under a single `pkg/package.json` with an
# `exports` map so `.` resolves to the nodejs build (byte-for-byte
# unchanged for existing consumers) and `./web` resolves to the web build.
# See totalreclaw#500.
#
# wasm-pack emits a per-target package.json with an unscoped
# `name: "totalreclaw-core"`. Both the plugin and the MCP server consume
# it as `@totalreclaw/core`, so this script applies the same rename +
# composition that .github/workflows/npm-publish.yml does in CI. Use this
# script for any local rebuild against the worktree-built pkg.
#
# Usage: ./build-wasm.sh [extra wasm-pack args]

set -euo pipefail

cd "$(dirname "$0")"

rm -rf pkg
wasm-pack build --target nodejs --out-dir pkg/nodejs --features wasm "$@"
wasm-pack build --target web --out-dir pkg/web --features wasm "$@"

node -e "
const fs = require('fs');
const nodejsPkg = JSON.parse(fs.readFileSync('./pkg/nodejs/package.json', 'utf8'));

const composed = {
  name: '@totalreclaw/core',
  description: nodejsPkg.description,
  version: nodejsPkg.version,
  license: nodejsPkg.license,
  repository: nodejsPkg.repository,
  homepage: nodejsPkg.homepage,
  keywords: nodejsPkg.keywords,
  files: ['nodejs/**', 'web/**'],
  main: './nodejs/totalreclaw_core.js',
  types: './nodejs/totalreclaw_core.d.ts',
  exports: {
    '.': {
      types: './nodejs/totalreclaw_core.d.ts',
      default: './nodejs/totalreclaw_core.js'
    },
    './web': {
      types: './web/totalreclaw_core.d.ts',
      default: './web/totalreclaw_core.js'
    }
  }
};

fs.writeFileSync('./pkg/package.json', JSON.stringify(composed, null, 2) + '\n');
// Remove the per-target manifests wasm-pack generated — only the composed
// root manifest above governs resolution + npm packing. Replace pkg/web's
// with a minimal { type: module } marker: the web build's glue file uses
// ESM 'export' syntax, and without a 'type' field on the nearest
// package.json Node has to sniff the file (triggering a
// MODULE_TYPELESS_PACKAGE_JSON perf-overhead warning). The root manifest
// can't declare 'type: module' itself — the nodejs build's glue file is
// CommonJS ('require') and must stay the default type for '.' to resolve
// correctly for existing consumers.
fs.rmSync('./pkg/nodejs/package.json');
fs.writeFileSync('./pkg/web/package.json', JSON.stringify({ type: 'module' }, null, 2) + '\n');
// wasm-pack also drops a per-target '.gitignore' containing a bare '*'.
// npm's packer respects nested .gitignore/.npmignore files even when an
// explicit 'files' field is set on the root manifest, so left in place
// these silently exclude both target dirs from the published tarball
// (verified: 'npm pack' with them present produces a 1-file tarball
// containing only package.json). Strip them so files/** actually ships.
fs.rmSync('./pkg/nodejs/.gitignore', { force: true });
fs.rmSync('./pkg/web/.gitignore', { force: true });
console.log('Composed pkg/package.json -> @totalreclaw/core (nodejs + web)');
"

echo "Done. Consumers can now \`npm install\` against this pkg/ directory."
echo "  - bare specifier '@totalreclaw/core'      -> pkg/nodejs (unchanged)"
echo "  - subpath '@totalreclaw/core/web'          -> pkg/web (new)"
