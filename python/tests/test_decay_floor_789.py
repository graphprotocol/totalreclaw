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
    # active iff decayScore >= threshold: the comparison direction matters too
    assert "decoded.decayScore.ge(INACTIVE_THRESHOLD)" in MAPPING.read_text()


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


@pytest.mark.parametrize("raw,want", [(0.2, 0.3), (0.0, 0.3), (-1, 0.3), (1, 1), (8, 8), (0.7, 0.7), (float("nan"), 5), ("x", 5)])
def test_live_decay_floor(raw, want):
    from totalreclaw.operations import live_decay_floor
    assert live_decay_floor(raw) == pytest.approx(want)


@pytest.mark.asyncio
@pytest.mark.parametrize("imp,want", [(0.2, 0.3), (8, 8)])
async def test_store_fact_floors_live_write(imp, want):
    from totalreclaw.operations import store_fact
    wallet, relay = _wallet_relay()
    seen = {}
    with patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock), \
         patch("totalreclaw.operations.encode_fact_protobuf",
               side_effect=lambda p: seen.setdefault("decay", p.decay_score) and b"x"):
        await store_fact(text="a fact", wallet=wallet, relay=relay, importance=imp)
    assert seen["decay"] == pytest.approx(want)


@pytest.mark.parametrize("crystal_importance,want", [(2, 0.3), (10, 1.0)])
def test_session_crystal_through_real_lifecycle(crystal_importance, want):
    from unittest.mock import MagicMock
    from totalreclaw.agent.debrief import Crystal
    from totalreclaw.agent.lifecycle import session_debrief
    from totalreclaw.operations import store_fact

    wallet, relay = _wallet_relay()
    seen = {}

    class _Client:
        async def remember(self, text, source="python-client", **fields):
            with patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock), \
                 patch("totalreclaw.operations.encode_fact_protobuf",
                       side_effect=lambda p: seen.update(decay=p.decay_score) or b"x"):
                return await store_fact(text=text, wallet=wallet, relay=relay, source=source, **fields)

    state = MagicMock()
    state.is_configured.return_value = True
    state.get_client.return_value = _Client()
    state.session_id = "s1"
    state.get_all_messages.return_value = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(8)
    ]
    crystal = Crystal(
        narrative="User planned a Lisbon trip and booked an aisle seat.",
        key_outcomes=["Lisbon flight booked"],
        importance=crystal_importance,
        topics_discussed=["travel"],
    )
    with patch("totalreclaw.agent.lifecycle.generate_crystal", new=AsyncMock(return_value=crystal)), \
         patch("totalreclaw.agent.lifecycle.crystal_gate_passes", return_value=True), \
         patch("totalreclaw.embedding.get_embedding", return_value=None):
        ids = session_debrief(state, stored_fact_texts=[])
    assert ids
    assert seen["decay"] == pytest.approx(want)
