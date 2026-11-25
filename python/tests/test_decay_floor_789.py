"""#789 (interim) — live batch writes are floored at the subgraph's 0.3 active
threshold; tombstones stay at decay 0."""
from __future__ import annotations

import re
import struct
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import totalreclaw_core

from totalreclaw.operations import ACTIVE_DECAY_THRESHOLD, live_batch_decay_score
from totalreclaw.protobuf import encode_tombstone_protobuf

MAPPING = Path(__file__).resolve().parents[2] / "subgraph" / "src" / "mapping.ts"


def test_threshold_matches_subgraph():
    m = re.search(r'INACTIVE_THRESHOLD\s*=\s*BigDecimal\.fromString\("([0-9.]+)"\)', MAPPING.read_text())
    assert m is not None
    assert float(m.group(1)) == ACTIVE_DECAY_THRESHOLD == 0.3


@pytest.mark.parametrize(
    "raw,want",
    [(2, 0.3), (0.2, 0.3), (0, 0.3), (-5, 0.3), (1.5, 0.3), (8, 0.8), (3, 0.3), (7, 0.7), (0.7, 0.7), (float("nan"), 0.5), ("x", 0.5)],
)
def test_live_batch_decay_score(raw, want):
    assert live_batch_decay_score(raw) == pytest.approx(want)


def test_tombstone_still_writes_zero():
    raw = encode_tombstone_protobuf("fid", "0x" + "1" * 40)
    assert b"\x31" + struct.pack("<d", 0.0) in raw
    assert struct.pack("<d", 0.3) not in raw


@pytest.mark.asyncio
@pytest.mark.parametrize("imp,want", [(2, 0.3), (0.2, 0.3), (8, 0.8)])
async def test_store_fact_batch_floors_live_facts(imp, want):
    if not hasattr(totalreclaw_core, "validate_memory_claim_v1"):
        pytest.skip("core lacks validate_memory_claim_v1")
    from totalreclaw.crypto import derive_keys_from_mnemonic
    from totalreclaw.operations import WalletContext, store_fact_batch
    from totalreclaw.relay import RelayClient

    keys = derive_keys_from_mnemonic(
        "abandon abandon abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon about"
    )
    relay = AsyncMock(spec=RelayClient)
    wallet = WalletContext(
        keys=keys, owner="0x1234", eoa_private_key=bytes(32),
        eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94", sender="0x1234",
    )
    captured = []
    with patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock), \
         patch("totalreclaw.operations.encode_fact_protobuf",
               side_effect=lambda p: captured.append(p) or b"") :
        await store_fact_batch(facts=[{"text": "a fact", "importance": imp}], wallet=wallet, relay=relay)
    assert len(captured) == 1
    assert captured[0].decay_score == pytest.approx(want)
