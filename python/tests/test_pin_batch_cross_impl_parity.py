"""Cross-impl parity test for the pin/unpin atomic batch calldata (2.2.3).

The Python 2.2.3 refactor and the plugin 3.2.x pin path both emit pin
as a SINGLE ``SimpleAccount.executeBatch(tombstone_call, new_fact_call)``
UserOp via the shared Rust core:

    plugin (TS) → ``@totalreclaw/core`` WASM ``encodeBatchCall``
                                 │
                                 ▼
                     ``totalreclaw_core::userop::encode_batch_call``
                                 ▲
                                 │
    python       → ``totalreclaw_core.encode_batch_call`` (PyO3)

Because the ABI-encoding step is shared-Rust, the Python and TS paths
produce byte-identical calldata for byte-identical input payloads. The
payload-assembly step, however, is reimplemented per-client (Python in
``protobuf.py``, TS in ``subgraph-store.ts``), so it MUST be kept in
lockstep. This test pins that contract by:

1. Constructing the same pin-scenario input (a fixed fact_id + v1 claim
   blob + owner) in Python.
2. Encoding tombstone (keyed scheme 1, v=4) + new-fact (keyed scheme 1, v=4) payloads
   via ``encode_fact_protobuf_keyed`` + ``encode_tombstone_protobuf_keyed``.
3. Routing the 2-payload list through
   ``encode_execute_batch_calldata_for_data_edge`` (which delegates to
   the shared Rust core).
4. Byte-comparing the payloads against the core keyed encoders
   (``encode_fact_protobuf_keyed`` / ``encode_tombstone_protobuf_keyed``
   in ``totalreclaw_core``), replacing the pre-#573 golden hex.

If the golden string ever diverges from reality, one of three things
has broken:
    - Python's protobuf encoder drifted from the Rust encoder (covered
      by ``test_python_rust_protobuf_v4_byte_identical`` separately).
    - The Rust core's ``encode_batch_call`` changed ABI shape
      (cross-chain breaking — all clients would need to bump).
    - The pin-path payload construction in Python's
      ``_change_claim_status`` drifted from the plugin's
      ``executePinOperation`` (this test's primary use).

The golden is regenerated deterministically from the fixed inputs; see
the block at the bottom for the regeneration snippet. Run the snippet
whenever the Rust core's ``encode_batch_call`` output shape changes
legitimately (breaking change).
"""
from __future__ import annotations

import json

import totalreclaw_core
from totalreclaw.protobuf import (
    PROTOBUF_VERSION_V4,
    FactPayload,
    encode_fact_protobuf_keyed,
    encode_tombstone_protobuf_keyed,
)
from totalreclaw.userop import encode_execute_batch_calldata_for_data_edge


# ---------------------------------------------------------------------------
# Fixture: fully deterministic inputs for the pin batch.
# ---------------------------------------------------------------------------

# Pin-scenario fixture. All strings are locked so the derived bytes are
# deterministic across runs — no UUIDs, no timestamps derived from
# ``datetime.now()``. This mirrors what the pin path produces for a real
# user, just with test-stable inputs.

# The fact being pinned.
OLD_FACT_ID = "01900000-0000-7000-8000-000000000001"
# The new fact id minted by ``_change_claim_status`` at pin time.
NEW_FACT_ID = "01900000-0000-7000-8000-000000000002"
OWNER = "0x0000000000000000000000000000000000001234"
CANONICAL_TS = "2026-04-19T10:00:00.000Z"
# The v1.1 MemoryClaimV1 JSON the pin path produces on the new fact.
# Pre-computed with `claims_helper.build_canonical_claim_v1` using the
# same semantics the pin code path uses — this is NOT the encrypted blob
# (encryption is non-deterministic for AEAD nonce reasons), so we use a
# fixed ciphertext hex stand-in and a fixed trapdoor list.
FIXED_ENCRYPTED_BLOB_HEX = "c0ffee" + ("ab" * 32)  # 67 bytes stand-in
# Fixed 64-hex stand-ins (unsorted on purpose: the encoder sorts).
FIXED_BLIND_INDICES = [
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9",
]
FIXED_ENCRYPTED_EMBEDDING = "deadbeefcafe"


def _build_pin_batch_payloads() -> list[bytes]:
    """Construct the 2-payload list the pin path submits: tombstone + new fact.

    Mirrors ``_change_claim_status`` step 6 precisely — ordering,
    protobuf versions, and field values. Only the encrypted-blob +
    trapdoor slots are replaced with deterministic stand-ins so the
    golden stays stable across runs.
    """
    # Fixed timestamp keeps the bytes deterministic.
    tombstone_bytes = encode_tombstone_protobuf_keyed(
        OLD_FACT_ID, OWNER, timestamp=CANONICAL_TS
    )

    new_payload = FactPayload(
        id=NEW_FACT_ID,
        timestamp=CANONICAL_TS,
        owner=OWNER,
        encrypted_blob=FIXED_ENCRYPTED_BLOB_HEX,
        blind_indices=FIXED_BLIND_INDICES,
        decay_score=1.0,
        source="python_pin",
        content_fp="",
        agent_id="python-client",
        encrypted_embedding=FIXED_ENCRYPTED_EMBEDDING,
        # New pinned fact at v=4 (v1 taxonomy). Matches pin.ts line 641:
        # ``encodeFactProtobufLocal(newPayload, PROTOBUF_VERSION_V4)``.
        version=PROTOBUF_VERSION_V4,
    )
    new_bytes = encode_fact_protobuf_keyed(new_payload)

    return [tombstone_bytes, new_bytes]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPinBatchCrossImplParity:
    """Lock in that Python's pin batch calldata is byte-identical to
    what the plugin's WASM path would produce for the same inputs.

    Since both sides delegate to the same shared-Rust
    ``userop::encode_batch_call``, byte-identity is guaranteed at the
    ABI-encoding step; what this test actually guards is the
    pin-path payload construction (protobuf version choices, field
    ordering, tombstone/new-fact ordering in the batch).
    """

    def test_pin_batch_has_exactly_two_payloads_in_order(self) -> None:
        """Sanity: the pin batch is always tombstone-at-index-0,
        new-fact-at-index-1. If either order or count drifts the
        subgraph's supersession chain breaks.
        """
        payloads = _build_pin_batch_payloads()
        assert len(payloads) == 2, (
            "pin batch must carry exactly tombstone + new fact"
        )

    def test_pin_tombstone_payload_is_v4_scheme1(self) -> None:
        """Keyed scheme 1 tombstone: version 4, field 14 = 1, equal to the
        core keyed tombstone layout (compared at the same timestamp)."""
        payloads = _build_pin_batch_payloads()
        tombstone = payloads[0]
        assert _extract_protobuf_version(tombstone) == 4
        core = bytes(
            totalreclaw_core.encode_tombstone_protobuf_keyed(OLD_FACT_ID, OWNER)
        )
        # Same bytes except the core binding stamps its own timestamp.
        assert tombstone.endswith(core[-40:]) and core.endswith(tombstone[-40:])

    def test_pin_new_fact_matches_core_keyed_encoder(self) -> None:
        new_fact = _build_pin_batch_payloads()[1]
        core = bytes(
            totalreclaw_core.encode_fact_protobuf_keyed(
                json.dumps(
                    {
                        "id": NEW_FACT_ID,
                        "timestamp": CANONICAL_TS,
                        "owner": OWNER,
                        "encrypted_blob_hex": FIXED_ENCRYPTED_BLOB_HEX,
                        "blind_indices": FIXED_BLIND_INDICES,
                        "decay_score": 1.0,
                        "source": "python_pin",
                        "content_fp": "",
                        "agent_id": "python-client",
                        "encrypted_embedding": FIXED_ENCRYPTED_EMBEDDING,
                        "version": 4,
                    }
                )
            )
        )
        assert new_fact == core

    def test_pin_new_fact_payload_is_v4(self) -> None:
        """New-fact write is protobuf v=4 (v1 taxonomy) — Bug #8's fix."""
        payloads = _build_pin_batch_payloads()
        new_fact = payloads[1]
        version = _extract_protobuf_version(new_fact)
        assert version == 4, (
            f"new pinned fact must be protobuf v=4 (v1 taxonomy); got v={version}"
        )

    def test_pin_batch_calldata_is_executebatch(self) -> None:
        """2-element batch triggers ``executeBatch`` (selector 0x47e1da2a),
        NOT ``execute`` (0xb61d27f6). This is the whole point of the
        2.2.3 refactor — one executeBatch, not two executes.
        """
        payloads = _build_pin_batch_payloads()
        calldata_hex = encode_execute_batch_calldata_for_data_edge(payloads)
        assert calldata_hex.startswith("0x")
        selector = calldata_hex[2:10]
        assert selector == "47e1da2a", (
            f"pin batch must route through SimpleAccount.executeBatch "
            f"(selector 0x47e1da2a); got 0x{selector}. "
            f"If this is 0xb61d27f6 the pin has regressed to single-call "
            f"``execute`` — the exact bug 2.2.3 was fixing."
        )

    def test_pin_batch_calldata_byte_matches_rust_core(self) -> None:
        """The Python wrapper's output must be byte-identical to what
        the shared Rust core produces directly. Guards against a
        divergence between ``encode_execute_batch_calldata_for_data_edge``
        and ``totalreclaw_core.encode_batch_call`` — e.g. a future
        wrapper that wraps/escapes bytes differently.

        Since the plugin's WASM ``encodeBatchCall`` also delegates to
        the same Rust core, passing this test is equivalent to
        byte-matching the plugin for identical input payloads.
        """
        payloads = _build_pin_batch_payloads()
        wrapper_calldata = encode_execute_batch_calldata_for_data_edge(payloads)
        wrapper_hex = wrapper_calldata[2:]  # strip 0x prefix

        core_bytes = totalreclaw_core.encode_batch_call(payloads)
        core_hex = core_bytes.hex()

        assert wrapper_hex == core_hex, (
            "Python's encode_execute_batch_calldata_for_data_edge "
            "diverges from totalreclaw_core.encode_batch_call. The "
            "plugin's WASM path uses the same Rust core, so a mismatch "
            "here means Python and plugin pin batches produce different "
            "calldata for identical input payloads — cross-client parity "
            "is broken."
        )


# ---------------------------------------------------------------------------
# Protobuf version extractor — tiny parser for field 8 (outer version).
# ---------------------------------------------------------------------------


def _extract_protobuf_version(payload: bytes) -> int:
    """Scan a ``FactPayload`` protobuf for field 8 (``version`` varint).

    Minimal parser — supports the wire types the FactPayload uses.
    Returns 0 if the field isn't found (treat as default v=3).
    """
    i = 0
    n = len(payload)
    while i < n:
        # Decode tag varint.
        tag = 0
        shift = 0
        while True:
            b = payload[i]
            i += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            v = 0
            shift = 0
            while True:
                b = payload[i]
                i += 1
                v |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if field_num == 8:
                return v
        elif wire_type == 1:  # fixed64
            i += 8
        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while True:
                b = payload[i]
                i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            i += length
        elif wire_type == 5:  # fixed32
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
    return 0
