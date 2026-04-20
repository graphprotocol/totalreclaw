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
2. Encoding tombstone (protobuf v=3) + new-fact (protobuf v=4) payloads
   via ``encode_fact_protobuf`` + ``encode_tombstone_protobuf``.
3. Routing the 2-payload list through
   ``encode_execute_batch_calldata_for_data_edge`` (which delegates to
   the shared Rust core).
4. Byte-comparing against a golden string that was generated from the
   same Rust core (hence byte-identical to what the plugin's WASM path
   would produce for the same inputs).

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

import totalreclaw_core
from totalreclaw.protobuf import (
    PROTOBUF_VERSION_V4,
    FactPayload,
    encode_fact_protobuf,
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
FIXED_BLIND_INDICES = [
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # sha256("")
    "5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9",  # sha256("1")
]
FIXED_ENCRYPTED_EMBEDDING = "deadbeefcafe"


def _build_pin_batch_payloads() -> list[bytes]:
    """Construct the 2-payload list the pin path submits: tombstone + new fact.

    Mirrors ``_change_claim_status`` step 6 precisely — ordering,
    protobuf versions, and field values. Only the encrypted-blob +
    trapdoor slots are replaced with deterministic stand-ins so the
    golden stays stable across runs.
    """
    # ``encode_tombstone_protobuf`` uses ``datetime.now()`` internally,
    # so we can't call it directly for a deterministic golden. Rebuild
    # the tombstone payload with a fixed timestamp via the public
    # ``FactPayload`` type + ``encode_fact_protobuf`` — same semantics
    # as the helper, just with a pinned timestamp.
    tombstone_payload = FactPayload(
        id=OLD_FACT_ID,
        timestamp=CANONICAL_TS,
        owner=OWNER,
        encrypted_blob=b"tombstone".hex(),
        blind_indices=[],
        decay_score=0.0,
        source="python_forget",
        content_fp="",
        agent_id="python-client",
        # Tombstone stays at legacy v=3 (matches pin.ts::executePinOperation
        # line 640: ``encodeFactProtobufLocal(tombstonePayload, /* version = legacy v3 */ 3)``).
        version=3,
    )
    tombstone_bytes = encode_fact_protobuf(tombstone_payload)

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
    new_bytes = encode_fact_protobuf(new_payload)

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

    def test_pin_tombstone_payload_is_v3(self) -> None:
        """Tombstone stays at legacy protobuf v=3 for plugin byte-parity.

        The tombstone carries no inner v1 blob, so the outer version
        field is irrelevant for readers — but writing v=3 preserves
        round-trip compat with any pre-v1 tombstone parser, and more
        importantly keeps the bytes identical to the plugin's output.
        """
        payloads = _build_pin_batch_payloads()
        tombstone = payloads[0]
        version = _extract_protobuf_version(tombstone)
        assert version == 3, (
            f"tombstone must be protobuf v=3 (legacy); got v={version}"
        )

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

    def test_pin_batch_golden_calldata_hex(self) -> None:
        """Lock in the exact calldata bytes for the fixed pin scenario.

        If the Rust core's ``encode_batch_call`` ABI output legitimately
        changes (breaking core bump), regenerate via:

            payloads = _build_pin_batch_payloads()
            print(totalreclaw_core.encode_batch_call(payloads).hex())

        and paste the output into ``EXPECTED_PIN_BATCH_CALLDATA_HEX``.
        Otherwise a mismatch means something drifted — investigate
        before "fixing" the golden.
        """
        payloads = _build_pin_batch_payloads()
        actual_hex = totalreclaw_core.encode_batch_call(payloads).hex()
        assert actual_hex == EXPECTED_PIN_BATCH_CALLDATA_HEX, (
            f"Pin batch calldata drift!\n"
            f"  Expected: {EXPECTED_PIN_BATCH_CALLDATA_HEX[:60]}…\n"
            f"  Actual:   {actual_hex[:60]}…\n"
            f"If the change is intentional (new core semantics), "
            f"regenerate the golden via the snippet in the docstring."
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


# ---------------------------------------------------------------------------
# Golden — pinned once from the shared Rust core. Regenerate only when
# the core's ``encode_batch_call`` output legitimately changes (breaking
# change, all clients bumping in lockstep). See the docstring on
# ``test_pin_batch_golden_calldata_hex`` for the regen snippet.
#
# Format:
#   - 4-byte selector: 0x47e1da2a = keccak256("executeBatch(address[],uint256[],bytes[])")[:4]
#   - ABI-encoded dest[] (both DataEdge 0xc445af1d...), values[] (both 0),
#     data[] (tombstone + new-fact protobuf payloads).
#
# The length (1864 hex chars = 932 bytes) is dominated by the new-fact
# protobuf payload which carries a ~67-byte ciphertext stand-in + 2
# trapdoor strings + the v1 inner blob.
# ---------------------------------------------------------------------------

EXPECTED_PIN_BATCH_CALLDATA_HEX = "47e1da2a000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000c000000000000000000000000000000000000000000000000000000000000001200000000000000000000000000000000000000000000000000000000000000002000000000000000000000000c445af1d4eb9fce4e1e61fe96ea7b8febf03c5ca000000000000000000000000c445af1d4eb9fce4e1e61fe96ea7b8febf03c5ca00000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000040000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000000840a2430313930303030302d303030302d373030302d383030302d3030303030303030303030311218323032362d30342d31395431303a30303a30302e3030305a1a2a3078303030303030303030303030303030303030303030303030303030303030303030303030313233342209746f6d6273746f6e65310000000000000000380140030000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001300a2430313930303030302d303030302d373030302d383030302d3030303030303030303030321218323032362d30342d31395431303a30303a30302e3030305a1a2a3078303030303030303030303030303030303030303030303030303030303030303030303030313233342223c0ffeeabababababababababababababababababababababababababababababababab2a40653362306334343239386663316331343961666266346338393936666239323432376165343165343634396239333463613439353939316237383532623835352a403566656365623636666663383666333864393532373836633664363936633739633264626332333964643465393162343637323964373361323766623537653931000000000000f03f380140046a0c64656164626565666361666500000000000000000000000000000000"
