"""#789 -- on-chain decayScore: 0 = tombstone, 1.0 = live, no importance.

Every live write path (store_fact, store_fact_batch) writes the constant;
tombstones keep writing exactly 0; the constant stays above the subgraph's
active threshold.
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from totalreclaw.operations import LIVE_DECAY_SCORE, live_decay_score
from totalreclaw.protobuf import encode_tombstone_protobuf

ROOT = Path(__file__).resolve().parents[2]
MAPPING = ROOT / "subgraph" / "src" / "mapping.ts"
FIXTURE = ROOT / "tests" / "parity" / "fixtures" / "decay-score-v1.json"


def test_live_value_is_one():
    assert LIVE_DECAY_SCORE == 1.0
    assert live_decay_score() == 1.0
    assert live_decay_score() == json.loads(FIXTURE.read_text())["live"]


def test_threshold_matches_subgraph():
    text = MAPPING.read_text()
    m = re.search(r'INACTIVE_THRESHOLD\s*=\s*BigDecimal\.fromString\("([0-9.]+)"\)', text)
    assert m is not None
    assert float(m.group(1)) == json.loads(FIXTURE.read_text())["subgraph_threshold"] == 0.3
    # active iff decayScore >= threshold: the comparison direction matters too
    assert "decoded.decayScore.ge(INACTIVE_THRESHOLD)" in text
    assert live_decay_score() >= float(m.group(1))


def test_fallback_when_core_lacks_function():
    import totalreclaw.operations as ops

    class _NoFn:
        pass

    with patch.object(ops, "_core", _NoFn()):
        assert ops.live_decay_score() == 1.0


def test_tombstone_still_writes_zero():
    raw = encode_tombstone_protobuf("fid", "0x" + "1" * 40)
    assert b"\x31" + struct.pack("<d", 0.0) in raw
    assert struct.pack("<d", 1.0) not in raw


def _wallet_relay():
    from totalreclaw.crypto import derive_keys_from_mnemonic
    from totalreclaw.operations import WalletContext
    from totalreclaw.relay import RelayClient

    keys = derive_keys_from_mnemonic(
        "abandon abandon abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon about"
    )
    wallet = WalletContext(
        keys=keys, owner="0x1234", eoa_private_key=bytes(32),
        eoa_address="0x9858EfFD232B4033E47d90003D41EC34EcaEda94", sender="0x1234",
    )
    return wallet, AsyncMock(spec=RelayClient)


@pytest.mark.asyncio
@pytest.mark.parametrize("imp", [1, 2, 0.2, 5, 8, 10, 1.0])
async def test_store_fact_writes_constant(imp):
    from totalreclaw.operations import store_fact

    wallet, relay = _wallet_relay()
    seen = {}
    with patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock), \
         patch("totalreclaw.operations.encode_fact_protobuf",
               side_effect=lambda p: seen.setdefault("decay", p.decay_score) and b"x"):
        await store_fact(text="a fact", wallet=wallet, relay=relay, importance=imp)
    assert seen["decay"] == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("imp", [1, 2, 2.0, 0.2, 6, 8, 10])
async def test_store_fact_batch_writes_constant(imp):
    import totalreclaw_core

    if not hasattr(totalreclaw_core, "validate_memory_claim_v1"):
        pytest.skip("core lacks validate_memory_claim_v1")
    from totalreclaw.operations import store_fact_batch

    wallet, relay = _wallet_relay()
    captured = []
    with patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock), \
         patch("totalreclaw.operations.encode_fact_protobuf",
               side_effect=lambda p: captured.append(p) or b""):
        await store_fact_batch(facts=[{"text": "a fact", "importance": imp}], wallet=wallet, relay=relay)
    assert len(captured) == 1
    assert captured[0].decay_score == 1.0
