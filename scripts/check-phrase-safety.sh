#!/usr/bin/env bash
#
# check-phrase-safety.sh — repository-level guard against accidental
# recovery-phrase-display copy in human-facing artifacts.
#
# WHY THIS EXISTS
# ---------------
# The TotalReclaw recovery phrase is the user's only identity — it derives
# every key in the system. The hard rule (CLAUDE.md, internal repo) is:
#
#     The recovery phrase MUST NEVER cross the LLM context. The QR-pair
#     flow is the ONLY agent-facilitated setup path. Forbidden: agent-
#     shell-invoked phrase-generating CLIs, --emit-phrase via tool,
#     phrase in agent responses.
#
# We've been bitten twice by this rule slipping into a SKILL.md or guide
# during a doc rewrite — each time the safety language was REPLACED with
# instruction copy that told the agent to display the phrase. This script
# is a static grep that fails CI when any human-facing artifact contains
# a phrase-DISPLAY pattern that isn't covered by an explicit safety
# whitelist (e.g. "NEVER display", "do not display", security warnings).
#
# Scope: every SKILL.md (plugin, hermes, nanoclaw), every README and root
# markdown, all of `docs/`. Excluded: `node_modules/`, `dist/`, `.git/`,
# `archive/`, the script + tests themselves, and `target/` build artifacts.
#
# Usage:
#   scripts/check-phrase-safety.sh                # scans repo, exits 0/1
#   PHRASE_SAFETY_DEBUG=1 scripts/check-phrase-safety.sh
#                                                 # prints every match
#
# Exit codes:
#   0 — clean (no violations)
#   1 — at least one phrase-display pattern outside the safety whitelist
#
# This is intentionally a bash + grep script (zero deps). It runs in CI in
# under 200ms on a clean checkout.

set -euo pipefail

# Resolve the repo root regardless of where this script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Files in scope. We keep this as an explicit list so CI cost stays bounded
# even as the repo grows. Each entry is a path or a `find` pattern.
SCOPE_PATHS=(
    "README.md"
    "CHANGELOG-public.md"
    "CONTRIBUTING.md"
    "skill/SKILL.md"
    "skill/plugin/SKILL.md"
    "skill/plugin/CLAWHUB.md"
    "skill/plugin/README.md"
    "skill/plugin/CHANGELOG.md"
    "skill-nanoclaw/SKILL.md"
    "skill-nanoclaw/mcp/SKILL.md"
    "skill-nanoclaw/CHANGELOG.md"
    "skill-nanoclaw/README.md"
    "python/src/totalreclaw/hermes/SKILL.md"
)

# Add every markdown file under docs/ (excluding archive subfolders).
while IFS= read -r f; do
    SCOPE_PATHS+=("$f")
done < <(find docs -type f \( -name '*.md' -o -name '*.mdx' \) ! -path 'docs/archive/*' 2>/dev/null | sort)

# Phrase-DISPLAY patterns. Case-insensitive. Each pattern matches an
# instruction-shape that would render a phrase to the user / agent. The
# whitelist below subtracts safety language ("NEVER display", "do not
# show", etc) from the same line.
#
# Patterns are kept TIGHT — we don't fish for "phrase" alone (too noisy);
# we look for verbs that imply displaying / printing / showing / revealing.
DISPLAY_PATTERNS=(
    'display[[:space:]]+(the[[:space:]]+)?(recovery[[:space:]]+)?phrase'
    'show[[:space:]]+(the[[:space:]]+)?(recovery[[:space:]]+)?phrase'
    'show[[:space:]]+(the[[:space:]]+)?mnemonic'
    'print[[:space:]]+(the[[:space:]]+)?(recovery[[:space:]]+)?phrase'
    'print[[:space:]]+(the[[:space:]]+)?mnemonic'
    'echo[[:space:]]+(the[[:space:]]+)?(recovery[[:space:]]+)?phrase'
    'reveal[[:space:]]+(the[[:space:]]+)?(recovery[[:space:]]+)?phrase'
    'output[[:space:]]+(the[[:space:]]+)?(recovery[[:space:]]+)?phrase'
    'paste[[:space:]]+(the[[:space:]]+)?phrase[[:space:]]+(in|into)[[:space:]]+chat'
    'paste[[:space:]]+(your[[:space:]]+)?(recovery[[:space:]]+)?phrase[[:space:]]+here'
    '--emit-phrase'
    '--print-phrase'
    'emit[_-]phrase'
)

# Whitelist regex: when one of these tokens is present on the SAME line
# (case-insensitive), the line is treated as legitimate safety copy and
# does not count as a violation.
#
# Examples of legitimate lines this whitelists:
#   - "NEVER display the recovery phrase in chat"
#   - "Do not show the mnemonic"
#   - "MUST NEVER print the phrase to stdout"
#   - "Never paste your recovery phrase here"
#   - "WARNING: showing the phrase here would compromise it"
#   - "tells the user it is compromised" (post-violation guidance)
WHITELIST_TOKENS=(
    'never'
    'forbidden'
    'do[[:space:]]+not'
    "don't"
    'must[[:space:]]+not'
    'must[[:space:]]+never'
    'cannot'
    'compromised'
    'warn(ing)?'
    'leak'
    'forbid(den)?'
    'avoid'
    'security[[:space:]]+(rule|notice|warning)'
    'phrase[[:space:]]+safety'
    'hard[[:space:]]+rule'
    'critical[[:space:]]+safety'
    'no[[:space:]]+(part|portion)[[:space:]]+of'
    'never[[:space:]]+echo'
)

# Build whitelist alternation once.
WHITELIST_RE="$(IFS='|'; echo "${WHITELIST_TOKENS[*]}")"

violations=0
debug="${PHRASE_SAFETY_DEBUG:-0}"

for f in "${SCOPE_PATHS[@]}"; do
    [[ -f "$f" ]] || continue
    for pat in "${DISPLAY_PATTERNS[@]}"; do
        # `grep -inE -H` prints `path:line:matched-content` for every hit.
        # We then strip whitelisted lines.
        while IFS= read -r line; do
            [[ -n "$line" ]] || continue
            # Whitelist check: does the matched line contain a safety token?
            content="${line#*:*:}"
            if echo "$content" | grep -iqE "$WHITELIST_RE"; then
                if [[ "$debug" == "1" ]]; then
                    echo "WHITELISTED: $line"
                fi
                continue
            fi
            echo "PHRASE-SAFETY VIOLATION: $line"
            violations=$((violations + 1))
        done < <(grep -inHE "$pat" "$f" 2>/dev/null || true)
    done
done

if [[ "$violations" -gt 0 ]]; then
    echo ""
    echo "Found $violations phrase-display pattern(s) without safety qualifier."
    echo "Phrase-safety rule (CLAUDE.md): the recovery phrase must never cross"
    echo "the LLM context. Replace any 'display/show/print/echo/reveal phrase'"
    echo "instruction with the QR-pair flow (\`totalreclaw_pair\` tool) OR add"
    echo "an explicit safety qualifier on the same line (\"NEVER display ...\")."
    echo ""
    echo "If a hit is a legitimate edge case, expand WHITELIST_TOKENS in"
    echo "scripts/check-phrase-safety.sh — do not delete the rule."
    exit 1
fi

echo "Phrase-safety check: $((${#SCOPE_PATHS[@]})) artifact(s) clean."
exit 0
