#!/bin/bash
# Post-publish verification — run after npm publish to verify packages work
set -e

echo "=== TotalReclaw Post-Publish Verification ==="

# 1. Verify packages are on npm with correct versions
echo ""
echo "Checking published versions..."
CLIENT_VER=$(npm view @totalreclaw/client version 2>/dev/null)
MCP_VER=$(npm view @totalreclaw/mcp-server version 2>/dev/null)
PLUGIN_VER=$(npm view @totalreclaw/totalreclaw version 2>/dev/null)
echo "  @totalreclaw/client: $CLIENT_VER"
echo "  @totalreclaw/mcp-server: $MCP_VER"
echo "  @totalreclaw/totalreclaw: $PLUGIN_VER"

# 2. Verify MCP server can be invoked
echo ""
echo "Verifying MCP server binary..."
npx --yes @totalreclaw/mcp-server@latest --help 2>/dev/null | head -3 || echo "  WARNING: MCP server --help failed (may need stdin)"

# 3. Verify client library imports
echo ""
echo "Verifying client library imports..."
node -e "
  const pkg = require('@totalreclaw/client/package.json');
  console.log('  Client version:', pkg.version);
  console.log('  Description:', pkg.description.slice(0, 60) + '...');
" 2>/dev/null || echo "  WARNING: Could not import client (may need install)"

# 4. Verify embedding model ID is correct in published package
echo ""
echo "Checking embedding model configuration..."
TEMP_DIR=$(mktemp -d)
cd "$TEMP_DIR"
npm init -y --silent >/dev/null 2>&1
npm install @totalreclaw/client@latest --silent 2>/dev/null
node -e "
  const fs = require('fs');
  const path = require('path');
  // Check for Qwen3 model reference in the compiled output
  const indexPath = path.join('node_modules', '@totalreclaw', 'client', 'dist', 'embedding', 'onnx.js');
  if (fs.existsSync(indexPath)) {
    const content = fs.readFileSync(indexPath, 'utf8');
    if (content.includes('Qwen3')) {
      console.log('  Qwen3 embedding model reference found');
    } else if (content.includes('bge-small')) {
      console.log('  ERROR: Still references old BGE model!');
      process.exit(1);
    } else {
      console.log('  WARNING: Could not verify model reference');
    }
  } else {
    console.log('  WARNING: onnx.js not found at expected path');
  }
" 2>/dev/null
cd - >/dev/null
rm -rf "$TEMP_DIR"

echo ""
echo "=== Verification Complete ==="
