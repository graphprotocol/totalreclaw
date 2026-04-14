#!/usr/bin/env bash
# Build the WASM package for @totalreclaw/core.
#
# wasm-pack emits pkg/package.json with an unscoped `name: "totalreclaw-core"`.
# Both the plugin and the MCP server consume it as `@totalreclaw/core`, so this
# script applies the same rename that .github/workflows/npm-publish.yml does
# in CI. Use this script for any local rebuild against the worktree-built pkg.
#
# Usage: ./build-wasm.sh [extra wasm-pack args]

set -euo pipefail

cd "$(dirname "$0")"

wasm-pack build --target nodejs --out-dir pkg --features wasm "$@"

node -e "
const fs = require('fs');
const path = './pkg/package.json';
const pkg = JSON.parse(fs.readFileSync(path, 'utf8'));
pkg.name = '@totalreclaw/core';
fs.writeFileSync(path, JSON.stringify(pkg, null, 2) + '\n');
console.log('Patched pkg/package.json name -> @totalreclaw/core');
"

echo "Done. Consumers can now \`npm install\` against this pkg/ directory."
