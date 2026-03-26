#!/bin/bash
set -euo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  echo "Usage: ./scripts/release.sh 1.5.0"
  exit 1
fi

echo "=== Releasing TotalReclaw v$VERSION ==="

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 1. Bump versions in package.json files
echo "Bumping package versions..."
cd client && npm version "$VERSION" --no-git-tag-version && cd ..
cd mcp && npm version "$VERSION" --no-git-tag-version && cd ..
cd skill/plugin && npm version "$VERSION" --no-git-tag-version && cd ../..

# 2. Update skill metadata
echo "Updating skill metadata..."
sed -i '' "s/\"version\": \".*\"/\"version\": \"$VERSION\"/" skill/skill.json
sed -i '' "s/^version: .*/version: $VERSION/" skill/SKILL.md

# 3. Build
echo "Building client..."
cd client && npm run build && cd ..
echo "Building MCP server..."
cd mcp && npm run build && cd ..

# 4. Commit + tag + push
echo "Committing and tagging..."
git add -A
git commit -m "chore: release v$VERSION"
git tag "v$VERSION"
git push origin main --tags

echo ""
echo "=== Tag pushed. GitHub Actions will: ==="
echo "  - Publish to ClawHub automatically"
echo ""
echo "=== Manual steps: ==="
echo "  cd client && npm publish --access public"
echo "  cd mcp && npm publish --access public"
echo "  cd skill/plugin && npm publish --access public"
echo ""
echo "Done!"
