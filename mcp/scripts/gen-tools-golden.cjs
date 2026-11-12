#!/usr/bin/env node
/**
 * Regenerates mcp/tests/fixtures/tools-list.golden.json from the BUILT
 * TOOL_MANIFEST (mcp/dist/dispatch.js).
 *
 * The golden pins what `tools/list` returns on the wire — names, order,
 * descriptions, inputSchema, annotations — so an MCP SDK upgrade (DEP-15)
 * or a transport change cannot silently alter the tool surface. It is the
 * JSON serialisation of TOOL_MANIFEST, which is byte-identical to what the
 * v1 SDK (1.30.1) put on the wire at public main 17ffb82.
 *
 * Regenerate ONLY when a PR intentionally changes a tool definition, and
 * say so in the PR body:
 *   cd mcp && npm run build && node scripts/gen-tools-golden.cjs
 */
'use strict';

const fs = require('fs');
const path = require('path');

const { TOOL_MANIFEST } = require('../dist/dispatch.js');

const out = path.join(__dirname, '..', 'tests', 'fixtures', 'tools-list.golden.json');
fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, JSON.stringify(TOOL_MANIFEST, null, 2) + '\n');
console.log(`wrote ${TOOL_MANIFEST.length} tools to tests/fixtures/tools-list.golden.json`);
