"""Generate the shared ``userop-batch-v1.json`` parity fixture.

Backs ``tests/parity/userop-batch-parity.test.ts`` (TS/WASM) and
``python/tests/test_userop_batch.py::TestSharedParityFixture`` (Python).
Both load the JSON this script emits and assert that encoding the same
payload set produces the same ``executeBatch`` calldata, which is the
byte-identity guarantee underpinning cross-client vault portability.

The fixture uses a deterministic 15-payload set (the spec-mandated batch
size — also ``MAX_BATCH_SIZE``). Source-of-truth encoder is the Rust
``totalreclaw_core::userop::encode_batch_call``; the Python wrapper
(``totalreclaw_core.encode_batch_call``) and the TS WASM binding
(``encodeBatchCall``) both delegate to it.

Run from the repo root::

    python tests/parity/fixtures/generate-userop-batch-fixture.py
"""
from __future__ import annotations

import json
from pathlib import Path

import totalreclaw_core


# Deterministic 15-fact payload set. Mirrors the ``n15`` entry in
# ``python/tests/fixtures/batch_calldata_vectors.py`` so the existing
# per-size parity test and the new cross-language parity test exercise
# the same input bytes.
PAYLOADS: list[bytes] = [bytes([i] * (5 + i)) for i in range(15)]


def build_fixture() -> dict:
    encoded = totalreclaw_core.encode_batch_call(PAYLOADS)
    return {
        "meta": {
            "version": 1,
            "description": (
                "Cross-language ERC-4337 SimpleAccount.executeBatch "
                "calldata parity fixture. Both Python "
                "(encode_execute_batch_calldata_for_data_edge) and "
                "TS/WASM (encodeBatchCall) MUST produce "
                "expected_calldata_hex when given payloads_hex as input. "
                "Source-of-truth encoder: "
                "rust/totalreclaw-core::userop::encode_batch_call. "
                "Regenerate via: python "
                "tests/parity/fixtures/generate-userop-batch-fixture.py"
            ),
            "selector_executeBatch": "0x47e1da2a",
            "count": len(PAYLOADS),
        },
        "payloads_hex": [p.hex() for p in PAYLOADS],
        "expected_calldata_hex": encoded.hex(),
        "expected_calldata_bytes": len(encoded),
    }


if __name__ == "__main__":
    fixture = build_fixture()
    out_path = Path(__file__).parent / "userop-batch-v1.json"
    out_path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
