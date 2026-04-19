"""Generate batch-call calldata fixtures from the Rust core reference.

Run this script to (re)generate ``batch_calldata_vectors.json`` whenever
the Rust ``encode_batch_call`` ABI contract changes. The baked fixture
backs the byte-match parity test at ``tests/test_userop_batch.py``.

Usage::

    cd python
    python tests/fixtures/batch_calldata_vectors.py

The generator uses deterministic, fully-specified input payloads so the
fixture is reproducible on any host. The expected bytes come straight
from ``totalreclaw_core.encode_batch_call`` — the Rust implementation
also drives the TypeScript `encodeBatchCalls` path via the shared Rust
core, so matching the Rust bytes is equivalent to matching the TS
bytes. See ``rust/totalreclaw-core/src/userop.rs::encode_batch_call``
and ``skill/plugin/store.ts`` for the other side of the contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import totalreclaw_core


# ---------------------------------------------------------------------------
# Deterministic payload sets
# ---------------------------------------------------------------------------
#
# We exercise the full supported range: N=1 (single-call fast path),
# N=3/5/10 (typical extraction cycles), and N=15 (MAX_BATCH_SIZE). Each
# payload is fully specified as a hex byte string — no RNG, no time
# dependence — so the expected calldata is reproducible on every run.

PAYLOAD_SETS: dict[str, list[bytes]] = {
    "n1": [bytes([1, 2, 3, 4, 5])],
    "n3": [b"fact one", b"fact two", b"fact three"],
    "n5": [
        b"a" * 32,
        b"b" * 64,
        b"c" * 128,
        b"d" * 256,
        b"e" * 100,
    ],
    "n10": [bytes([i] * (10 + i)) for i in range(10)],
    "n15": [bytes([i] * (5 + i)) for i in range(15)],
}


def build_fixture() -> dict:
    """Build the full fixture dict — keys are batch-size labels, values
    carry the input payloads + expected calldata bytes."""
    out: dict = {}
    for label, payloads in PAYLOAD_SETS.items():
        encoded = totalreclaw_core.encode_batch_call(payloads)
        out[label] = {
            "count": len(payloads),
            "payloads_hex": [p.hex() for p in payloads],
            "expected_calldata_hex": encoded.hex(),
            "expected_calldata_bytes": len(encoded),
        }
    return out


if __name__ == "__main__":
    fixture = build_fixture()
    out_path = Path(__file__).parent / "batch_calldata_vectors.json"
    out_path.write_text(json.dumps(fixture, indent=2, sort_keys=True))
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
