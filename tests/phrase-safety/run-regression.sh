#!/usr/bin/env bash
#
# Regression tests for `scripts/check-phrase-safety.sh`. Each fixture is a
# plain markdown file. The test suite plants each fixture into the repo
# (under `docs/.phrase-safety-fixtures/`), runs the guard, and asserts the
# expected exit code (1 for violation fixtures, 0 for clean fixtures).
# After each run the planted fixture is removed.
#
# This exists because static-analysis guards regress silently: a tweak to
# the regex or whitelist that breaks the guard would otherwise sail through
# CI as long as the rest of the repo is clean.
#
# Usage:
#   tests/phrase-safety/run-regression.sh            # run all cases
#   tests/phrase-safety/run-regression.sh --debug    # leak guard's stdout
#
# Exit:
#   0 — every case behaved as expected
#   1 — at least one case mismatched

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures"
PLANT_DIR="$REPO_ROOT/docs/.phrase-safety-fixtures"
GUARD="$REPO_ROOT/scripts/check-phrase-safety.sh"

debug=0
if [[ "${1:-}" == "--debug" ]]; then
    debug=1
fi

if [[ ! -x "$GUARD" ]]; then
    echo "FAIL: guard script missing or not executable: $GUARD"
    exit 1
fi

if [[ ! -d "$FIXTURES_DIR" ]]; then
    echo "FAIL: fixtures dir missing: $FIXTURES_DIR"
    exit 1
fi

# Cleanup helper — always removes the planted dir, even on script failure.
cleanup() {
    rm -rf "$PLANT_DIR"
}
trap cleanup EXIT

failures=0
total=0

# Each fixture file's basename starts with `fail_` (must trigger the guard)
# or `pass_` (must NOT trigger). We run them one at a time so a failing
# case doesn't mask later cases.
for fx in "$FIXTURES_DIR"/*.md; do
    name="$(basename "$fx")"
    total=$((total + 1))

    expected_exit=1
    case "$name" in
        fail_*) expected_exit=1 ;;
        pass_*) expected_exit=0 ;;
        *)
            echo "FAIL: fixture $name must start with fail_ or pass_"
            failures=$((failures + 1))
            continue
            ;;
    esac

    rm -rf "$PLANT_DIR"
    mkdir -p "$PLANT_DIR"
    cp "$fx" "$PLANT_DIR/$name"

    # Add the planted file to the guard's scope by appending it via
    # PHRASE_SAFETY_EXTRA_PATHS — unsupported today, so we just rely on
    # the guard's `find docs -type f *.md` walk. The planted directory
    # is excluded by NAME from typical doc browsing because it starts
    # with a dot.

    set +e
    if [[ "$debug" == "1" ]]; then
        "$GUARD"
    else
        "$GUARD" >/dev/null 2>&1
    fi
    actual_exit=$?
    set -e

    if [[ "$actual_exit" -ne "$expected_exit" ]]; then
        echo "FAIL ($name): expected exit $expected_exit, got $actual_exit"
        if [[ "$debug" == "1" ]]; then
            echo "    Fixture content:"
            sed 's/^/      | /' "$fx"
        fi
        failures=$((failures + 1))
    else
        echo "PASS ($name): exit $actual_exit as expected"
    fi
done

cleanup

echo ""
if [[ "$failures" -gt 0 ]]; then
    echo "$failures of $total phrase-safety regression case(s) FAILED."
    exit 1
fi

echo "All $total phrase-safety regression case(s) passed."
exit 0
