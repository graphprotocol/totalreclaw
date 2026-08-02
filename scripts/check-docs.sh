#!/usr/bin/env bash
# check-docs.sh — guard visitor-facing docs against retired-product drift.
#
# Two checks:
#   1. Retired-token scan: greps the "current-fact" doc surfaces (README,
#      docs/guides, top-level docs, package READMEs/SKILL.md, skill.json)
#      for tokens that describe the pre-2026-06 product (dual-chain /
#      Base Sepolia / unlimited tiers / deleted tools / removed env vars).
#      A hit is allowed only when the same line frames it as historical
#      ("retired", "removed", "no longer", "legacy", ...).
#   2. Relative-link check: every relative markdown link in those surfaces
#      (plus docs/specs) must resolve to a real file; root-absolute links
#      (/client/...) are errors — they break on github.com.
#
# Out of scope BY DESIGN: docs/specs historical flow docs carry a
# banner instead of being rewritten (only their links are checked);
# CHANGELOGs legitimately narrate history; CLAUDE.md is agent-facing and
# narrates retirements; code/comments are not docs.
#
# The 2026-07-15 docs audit found ~40 instances that this grep would have
# caught at PR time. See PR #525/#532/#533 for the cleanup this locks in.

set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

FAIL=0

# ---------------------------------------------------------------------------
# 1. Retired-token scan
# ---------------------------------------------------------------------------

CURRENT_FACT_FILES=$(ls -1 \
  README.md \
  CONTRIBUTING.md \
  docs/*.md \
  docs/guides/*.md \
  python/README.md \
  rust/totalreclaw-memory/README.md \
  rust/totalreclaw-core/README.md \
  client/README.md \
  mcp/README.md \
  skill-nanoclaw/README.md \
  skill/plugin/README.md \
  skill/plugin/SKILL.md \
  skill/plugin/skill.json \
  2>/dev/null)

# Lines matching these markers may mention a retired token (historical framing).
HISTORICAL_MARKERS='retired|removed|no longer|legacy|historical|predates|deprecated|was on|used to|old |pre-2026|pre-v1|blocker|migration'

# token|allow_historical(1/0)|explanation
# allow_historical=1: the mention passes when the line itself OR any of the
# EIGHT lines above it carry historical framing. Eight is deliberate: real
# "these vars were removed" lists put items 6-7 lines below their marker
# sentence (v1-migration.md, beta-tester guide). Trade-off accepted: a
# current-fact claim sitting within 8 lines below a Legacy/Retired heading
# would be masked — tolerable for a tripwire; don't widen further.
RETIRED_TOKENS=(
  'Base Sepolia|1|retired testnet presented as current (single-chain Gnosis since 2026-06-05)'
  '84532|1|retired Base Sepolia chain id'
  'TOTALRECLAW_CHAIN_ID|1|env var removed in v1'
  'TOTALRECLAW_EMBEDDING_MODEL|1|env var removed in v1'
  'totalreclaw_migrate|1|tool deleted in mcp-server 3.4.0'
  'totalreclaw_setup|1|tool removed; the real tool is totalreclaw_pair'
  'unlimited memories|0|no tier is unlimited (Free 250/mo, Pro 1,500/mo)'
  'unlimited imports|0|imports are metered by the monthly memory quota'
  '\$3\.99|0|hardcoded price; link totalreclaw.xyz/pricing instead'
  'placeholder —|0|unfilled placeholder in shipped docs'
  'placeholder --|0|unfilled placeholder in shipped docs'
  'all-MiniLM|0|retired embedding model (Harrier-OSS-v1-270M since v1)'
)

for spec in "${RETIRED_TOKENS[@]}"; do
  token="${spec%%|*}"
  rest="${spec#*|}"
  allow_hist="${rest%%|*}"
  why="${rest#*|}"

  while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    file="${hit%%:*}"
    lineno="${hit#*:}"; lineno="${lineno%%:*}"
    if [ "$allow_hist" = "1" ]; then
      start=$(( lineno > 8 ? lineno - 8 : 1 ))
      if sed -n "${start},${lineno}p" "$file" | grep -qiE "$HISTORICAL_MARKERS"; then
        continue
      fi
    fi
    echo "RETIRED TOKEN: $hit"
    echo "  -> $why"
    FAIL=1
  done < <(grep -inE "$token" $CURRENT_FACT_FILES 2>/dev/null)
done

# ---------------------------------------------------------------------------
# 2. Relative-link check (includes docs/specs — banners don't excuse 404s)
# ---------------------------------------------------------------------------

LINK_SCOPE=$(printf '%s\n' $CURRENT_FACT_FILES; find docs/specs -name '*.md' 2>/dev/null)

python3 - $LINK_SCOPE <<'PY'
import os, re, sys

link_re = re.compile(r'\[[^\]]*\]\(([^)\s]+)\)')
repo_root = os.path.realpath(os.getcwd())
fail = False
for path in sys.argv[1:]:
    if not path.endswith('.md'):
        continue
    try:
        text = open(path, encoding='utf-8').read()
    except OSError:
        continue
    # Ignore links inside code: fenced blocks first, then inline spans
    # (e.g. instructions that literally say "no `[text](url)` wrapping").
    text = re.sub(r'```.*?```', '', text, flags=re.S)
    text = re.sub(r'`[^`\n]*`', '', text)
    base = os.path.dirname(path)
    for target in link_re.findall(text):
        if target.startswith(('http://', 'https://', 'mailto:', '#', '//')):
            continue
        if target.startswith('/'):
            print(f"ROOT-ABSOLUTE LINK: {path}: ({target}) — breaks on github.com; use a repo-relative path")
            fail = True
            continue
        clean = target.split('#')[0]
        if not clean:
            continue
        resolved = os.path.normpath(os.path.join(base, clean))
        if os.path.commonpath([repo_root, os.path.realpath(resolved)]) != repo_root:
            print(f"LINK ESCAPES REPO: {path}: ({target}) resolves outside the repository")
            fail = True
            continue
        if not os.path.exists(resolved):
            print(f"BROKEN LINK: {path}: ({target}) -> {resolved} does not exist")
            fail = True
sys.exit(1 if fail else 0)
PY
[ $? -ne 0 ] && FAIL=1

# ---------------------------------------------------------------------------

if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo "check-docs: FAILED — visitor-facing docs contradict the shipped product."
  echo "Fix the lines above (or add historical framing like 'retired'/'removed'"
  echo "if the mention is genuinely historical)."
  exit 1
fi

echo "check-docs: OK"
