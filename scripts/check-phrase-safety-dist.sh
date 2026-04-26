#!/usr/bin/env bash
#
# check-phrase-safety-dist.sh — companion to check-phrase-safety.sh that
# scans COMPILED JavaScript / TypeScript dist artifacts for source-emitted
# recovery-phrase strings inside tool-response payloads.
#
# WHY THIS EXISTS
# ---------------
# `check-phrase-safety.sh` (the markdown-only guard) catches "display the
# phrase" copy in human-facing docs / SKILL.md. That's necessary but not
# sufficient: a separate vector is source code that BUILDS a JSON tool
# response with `recovery_phrase` (or `mnemonic`) as a key. The MCP server
# `totalreclaw_setup` tool used to do exactly that — every agent invoking
# the tool received the user's BIP-39 mnemonic in its LLM context. The
# 3.2.1 hotfix removed the tool, and this script is the regression guard.
#
# WHAT IT SCANS
# -------------
# Every `.js` file under each compiled dist/ tree we ship to a registry:
#   - mcp/dist/                  (npm: @totalreclaw/mcp-server)
#   - skill/plugin/dist/         (npm: @totalreclaw/totalreclaw)
#   - skill-nanoclaw/dist/       (npm: @totalreclaw/skill-nanoclaw)
#   - client/dist/               (npm: @totalreclaw/client)
#
# Build outputs are scanned only if they exist (the script does not invoke
# `npm run build` itself; CI is responsible for ordering).
#
# WHAT IT FLAGS
# -------------
# Any line that emits `recovery_phrase` or `mnemonic` AS AN OBJECT KEY in
# the JS — i.e. the shape that JSON.stringify() writes into a tool result
# payload. Allowed-context filters whitelist legitimate disk persistence
# (`credentials.json` reads / writes, the `SavedCredentials` shape) and
# top-level utility code (`generateMnemonic`, `validateMnemonic`,
# `mnemonicToAccount`, env-var handlers).
#
# Comment-only lines and source-map files are skipped.
#
# Usage:
#   scripts/check-phrase-safety-dist.sh                # scan, exits 0/1
#   PHRASE_SAFETY_DIST_DEBUG=1 scripts/check-phrase-safety-dist.sh
#                                                      # print every match
#
# Exit codes:
#   0 — clean (no violations)
#   1 — at least one phrase-emission shape outside the safety whitelist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Compiled dist trees to scan. We don't scan node_modules (untrusted vendor
# code) or test fixtures — only OUR shipping artifacts.
DIST_PATHS=(
    "mcp/dist"
    "skill/plugin/dist"
    "skill-nanoclaw/dist"
    "client/dist"
)

# Forbidden emission patterns. Each pattern is an extended-regex matched
# against ONE line of compiled JS. We look for the JSON-shape "key:" /
# "key" : / .key = forms — i.e. response-payload shapes.
#
# Comment lines and source-map files are stripped before matching (see
# the awk filter below).
EMISSION_PATTERNS=(
    # `recovery_phrase: <expr>` (JS unquoted key in object literal)
    '\brecovery_phrase[[:space:]]*:'
    # `"recovery_phrase": <expr>` (JS quoted key)
    '"recovery_phrase"[[:space:]]*:'
    # `<obj>.recovery_phrase = <expr>` (assignment to a result object)
    '\.recovery_phrase[[:space:]]*='
    # `mnemonic: <expr>` (unquoted key — needs allow-context filter
    # because legitimate persistence uses this key too)
    '\bmnemonic[[:space:]]*:'
    # `"mnemonic": <expr>`
    '"mnemonic"[[:space:]]*:'
    # `<obj>.mnemonic = <expr>` (assignment)
    '\.mnemonic[[:space:]]*=[^=]'
)

# Allow-context regex: when one of these tokens is present on the SAME
# line, the line is NOT a tool-response emission and is skipped. This
# whitelists:
#   - SavedCredentials shape (disk-persistence object)
#   - readFileSync / writeFileSync (disk I/O for credentials.json)
#   - generateMnemonic / validateMnemonic / mnemonicToAccount /
#     mnemonicToSeedSync (BIP-39 utility entrypoints; not a payload)
#   - process.env / TOTALRECLAW_RECOVERY_PHRASE (env-var handling, not a
#     payload — this is how the user injects the phrase)
#   - subgraphState.mnemonic / state.mnemonic / parsed.mnemonic (in-memory
#     references that are READING from a stored object, not WRITING the
#     key into a tool response)
#   - description / inputSchema (tool schema metadata; the deleted tool's
#     schema describing `recovery_phrase` as an input is gone, but if any
#     OTHER tool legitimately documents the key in its description it's
#     not a payload emission)
#   - cli/setup (the standalone setup CLI runs in the user's terminal,
#     not in LLM context — see CLAUDE.md phrase-safety rule)
#   - explicit `= undefined` (defensive nulling)
ALLOW_CONTEXT_RE='credentials\.json|writeFileSync|readFileSync|CREDENTIALS_PATH|SavedCredentials|generateMnemonic|validateMnemonic|mnemonicToAccount|mnemonicToSeedSync|process\.env|TOTALRECLAW_RECOVERY_PHRASE|subgraphState\.mnemonic|state\.mnemonic|parsed\.mnemonic|trimmed\.mnemonic|input\?\.\s*mnemonic|input\.mnemonic|description[[:space:]]*:|inputSchema|cli/setup|=[[:space:]]*undefined'

violations=0
debug="${PHRASE_SAFETY_DIST_DEBUG:-0}"

for dist_dir in "${DIST_PATHS[@]}"; do
    if [[ ! -d "$dist_dir" ]]; then
        if [[ "$debug" == "1" ]]; then
            echo "Skipping $dist_dir (not built)"
        fi
        continue
    fi

    while IFS= read -r jsfile; do
        for pat in "${EMISSION_PATTERNS[@]}"; do
            # `grep -nE -H` prints `path:line:matched`. We then strip
            # comment-only lines and allow-context lines.
            while IFS= read -r hit; do
                [[ -n "$hit" ]] || continue
                # `hit` is `path:line:content`. Extract content for
                # context check (everything after the SECOND colon).
                rest_after_path="${hit#*:}"      # strip "path:"
                content="${rest_after_path#*:}"  # strip "line:"

                # Skip comment-only lines (compiled JS may include
                # // ... comments preserved from TS).
                stripped="$(echo "$content" | sed -e 's/^[[:space:]]*//' )"
                case "$stripped" in
                    "//"*|"/*"*|"*"*)
                        if [[ "$debug" == "1" ]]; then
                            echo "  SKIP-COMMENT: $hit"
                        fi
                        continue
                        ;;
                esac

                # Whitelist check.
                if echo "$content" | grep -iqE "$ALLOW_CONTEXT_RE"; then
                    if [[ "$debug" == "1" ]]; then
                        echo "  ALLOW: $hit"
                    fi
                    continue
                fi

                echo "PHRASE-SAFETY-DIST VIOLATION [$pat]: $hit"
                violations=$((violations + 1))
            done < <(grep -nHE "$pat" "$jsfile" 2>/dev/null || true)
        done
    done < <(find "$dist_dir" -type f -name '*.js' 2>/dev/null | sort)
done

if [[ "$violations" -gt 0 ]]; then
    cat <<EOF

Found $violations phrase-emission shape(s) in compiled dist/.

A line of compiled JS is treated as a phrase-safety violation when it builds
\`recovery_phrase\` or \`mnemonic\` AS AN OBJECT KEY into JS source. Such a
shape, returned by an MCP / plugin tool handler, lands the user's BIP-39
recovery phrase inside the LLM's context window — that is forbidden.

Phrase-safety rule (CLAUDE.md): the recovery phrase MUST NEVER cross the
LLM context. Setup must follow the URL-driven install flow documented at
docs/guides/claude-code-setup.md (parity with OpenClaw + Hermes).

If a hit is a legitimate edge case (e.g. a NEW persisted-credential shape),
extend ALLOW_CONTEXT_RE in scripts/check-phrase-safety-dist.sh — do NOT
delete the rule.

EOF
    exit 1
fi

count=0
for dist_dir in "${DIST_PATHS[@]}"; do
    if [[ -d "$dist_dir" ]]; then
        count=$((count + $(find "$dist_dir" -type f -name '*.js' 2>/dev/null | wc -l | tr -d ' ')))
    fi
done
echo "Phrase-safety dist scan: $count compiled .js file(s) clean across ${#DIST_PATHS[@]} dist tree(s)."
exit 0
