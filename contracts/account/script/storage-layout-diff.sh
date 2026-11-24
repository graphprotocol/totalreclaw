#!/usr/bin/env bash
# Option E Phase 3 (P3-4) — storage-layout diff CI gate.
#
# `TotalReclawAccount` MUST declare zero plain state variables beyond what
# it inherits from `SimpleAccount` (B2 mitigation: a state variable that
# collides with `owner` at slot 0 is catastrophic and silent, per
# `docs/plans/2026-08-02-option-e-phase3-audit-risk.md` §2). All new
# session-key state lives in ERC-7201 namespaced storage, accessed via a
# fixed-slot inline-assembly getter. Such storage sits at keccak-derived
# slots (~2**255) that cannot collide with the sequential slots 0..k that
# `owner` occupies, so it is NOT part of the collision class this gate
# guards. Older forge omitted it from `storageLayout.storage` entirely;
# newer forge lists it — so we filter namespaced entries out explicitly and
# assert the remaining PLAIN sequential layout is BYTE-IDENTICAL between
# `TotalReclawAccount` and the `SimpleAccount` it subclasses. (Relying on
# namespaced storage being absent was a toolchain-version assumption that
# broke; filtering is version-independent.)
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

# Compare only the PLAIN sequential `.storage` entries (slot, offset, type,
# label). The `.types` dictionary can differ trivially in ordering/naming
# without any real layout difference, so it is not part of the gate; and
# ERC-7201 namespaced entries are excluded (see header) because they live at
# keccak-derived slots that cannot collide with slot 0.
python3 - <<'PY'
import json
import sys

with open("/tmp/tra-storage-layout.json") as f:
    tra = json.load(f)["storage"]
with open("/tmp/simple-account-storage-layout.json") as f:
    simple = json.load(f)["storage"]

# An ERC-7201 namespaced slot is a keccak-derived value (~2**255); a plain
# sequential state variable is at a small slot (0, 1, 2, ...). Exclude the
# namespaced ones — by the "erc7201" type marker newer forge emits, and,
# defensively, by slot magnitude — so the gate compares only the plain
# layout that could collide with `owner`.
_NAMESPACE_SLOT_FLOOR = 1 << 64

def is_plain(e):
    if "erc7201" in e.get("type", ""):
        return False
    try:
        return int(e["slot"]) < _NAMESPACE_SLOT_FLOOR
    except (KeyError, ValueError):
        return True

def normalize(entries):
    return [
        {"slot": e["slot"], "offset": e["offset"], "label": e["label"], "type": e["type"]}
        for e in entries
        if is_plain(e)
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
