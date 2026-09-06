#!/usr/bin/env bash
# Option E Phase 3 (P3-4) — storage-layout diff CI gate.
#
# `TotalReclawAccount` MUST declare zero plain state variables beyond what
# it inherits from `SimpleAccount` (B2 mitigation: a state variable that
# collides with `owner` at slot 0 is catastrophic and silent, per
# `docs/plans/2026-08-02-option-e-phase3-audit-risk.md` §2). All new
# session-key state lives in ERC-7201 namespaced storage, accessed via a
# fixed-slot inline-assembly getter, which does NOT show up in solc's
# `storageLayout.storage` array — so the strongest possible assertion of
# "no new plain state variables" is: this array is BYTE-IDENTICAL between
# `TotalReclawAccount` and the `SimpleAccount` it subclasses.
#
# `contracts/account/foundry.toml` already sets `extra_output =
# ["storageLayout"]`; this script is what consumes it (the phase3-impl
# spec §3.1/§3.4 note that nothing did, before this).
#
# Usage: contracts/account/script/storage-layout-diff.sh
# Exit 0 = layouts match. Exit 1 = they differ (or either inspect failed).

set -euo pipefail
cd "$(dirname "$0")/.."

echo "Building..."
forge build >/dev/null

echo "Inspecting TotalReclawAccount storage layout..."
forge inspect TotalReclawAccount storageLayout --json > /tmp/tra-storage-layout.json

echo "Inspecting SimpleAccount (the deployed baseline's implementation contract) storage layout..."
forge inspect SimpleAccount storageLayout --json > /tmp/simple-account-storage-layout.json

# Compare only the `.storage` array (slot, offset, type, label) — the
# `.types` dictionary can differ trivially in ordering/naming without any
# real layout difference, so it is not part of the gate.
python3 - <<'PY'
import json
import sys

with open("/tmp/tra-storage-layout.json") as f:
    tra = json.load(f)["storage"]
with open("/tmp/simple-account-storage-layout.json") as f:
    simple = json.load(f)["storage"]

def normalize(entries):
    return [
        {"slot": e["slot"], "offset": e["offset"], "label": e["label"], "type": e["type"]}
        for e in entries
    ]

tra_n = normalize(tra)
simple_n = normalize(simple)

if tra_n != simple_n:
    print("STORAGE LAYOUT MISMATCH — TotalReclawAccount declares state that", file=sys.stderr)
    print("SimpleAccount does not (or vice versa). This is exactly the B2", file=sys.stderr)
    print("failure class (silent slot-0 collision risk) — do not deploy.", file=sys.stderr)
    print("", file=sys.stderr)
    print("TotalReclawAccount:", file=sys.stderr)
    for e in tra_n:
        print(f"  slot {e['slot']:>3} offset {e['offset']} {e['label']}: {e['type']}", file=sys.stderr)
    print("SimpleAccount:", file=sys.stderr)
    for e in simple_n:
        print(f"  slot {e['slot']:>3} offset {e['offset']} {e['label']}: {e['type']}", file=sys.stderr)
    sys.exit(1)

print(f"OK — {len(tra_n)} plain state variable(s), byte-identical between TotalReclawAccount and SimpleAccount:")
for e in tra_n:
    print(f"  slot {e['slot']:>3} offset {e['offset']} {e['label']}: {e['type']}")
PY
