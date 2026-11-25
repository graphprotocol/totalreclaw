"""#704: importance scale handling (1-10 tool scale vs legacy 0-1 floats)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.crypto import derive_keys_from_mnemonic
from totalreclaw.operations import WalletContext, normalize_importance, store_fact
from totalreclaw.relay import RelayClient

from tests.test_operations import EOA_ADDRESS, EOA_PRIVATE_KEY, TEST_MNEMONIC


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, 1),       # int 1 is 1 on the 1-10 scale (was 10)
        (1.0, 1),     # float 1.0 likewise (the tool never sends 100%)
        (0.7, 7),     # legacy 0-1 float
        (0.5, 5),
        (5, 5),
        (10, 10),
        (10.0, 10),
        (0, 1),       # clamped
        (99, 10),     # clamped
        (True, 5),    # bool is not a score
        ("x", 5),
        (None, 5),
    ],
)
def test_normalize_importance(value, expected):
    assert normalize_importance(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("given,stored", [(1, 1), (0.7, 7), (5, 5), (10, 10), (1.0, 1)])
async def test_store_fact_blob_importance_and_active_decay_score(given, stored):
    keys = derive_keys_from_mnemonic(TEST_MNEMONIC)
    relay = AsyncMock(spec=RelayClient)
    relay._relay_url = "https://api-staging.totalreclaw.xyz"
    wallet = WalletContext(
        keys=keys, owner="0x1234", eoa_private_key=EOA_PRIVATE_KEY,
        eoa_address=EOA_ADDRESS, sender="0x1234",
    )
    seen = {}

    import totalreclaw.operations as ops

    real_build = ops.build_canonical_claim_v1

    def _build(*a, **kw):
        seen["importance"] = kw.get("importance")
        return real_build(*a, **kw)

    def _encode(payload):
        seen["decay"] = payload.decay_score
        return b"x"

    with patch("totalreclaw.operations.build_and_send_userop", new_callable=AsyncMock), \
         patch("totalreclaw.operations.build_canonical_claim_v1", side_effect=_build), \
         patch("totalreclaw.operations.encode_fact_protobuf", side_effect=_encode):
        await store_fact(text="Test fact", wallet=wallet, relay=relay, importance=given)

    assert seen["importance"] == stored
    # decayScore < 0.3 marks a fact inactive in the subgraph: it must stay
    # at or above that for every explicit importance (decayScore is unchanged).
    assert seen["decay"] >= 0.3


def _configured_state():
    from totalreclaw.hermes.tools import PluginState  # noqa: F401
    state = MagicMock()
    state.is_configured.return_value = True
    state.get_client.return_value = MagicMock()
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("given,passed", [(1, 1), (5, 5), (10, 10), (7, 7), (0, 1), (99, 10)])
async def test_remember_tool_passes_1_to_10_int(given, passed):
    from totalreclaw.hermes import tools

    state = MagicMock()
    state.is_configured.return_value = True
    client = MagicMock()
    client.remember = AsyncMock(return_value="fid")
    state.get_client.return_value = client

    with patch.object(tools, "_embed_for_remember", MagicMock(return_value=[0.1] * 4)), \
         patch("totalreclaw.write_gate.record_write", MagicMock()):
        raw = await tools.remember({"text": "likes tea", "importance": given, "force": True}, state)

    payload = json.loads(raw)
    assert payload.get("stored") is True, raw
    kwargs = client.remember.await_args.kwargs
    assert kwargs["importance"] == passed
    assert isinstance(kwargs["importance"], int)
    assert payload["importance"] == passed


@pytest.mark.asyncio
@pytest.mark.parametrize("default,expected", [(None, 7), (7, 7), (8, 8), (1, 1), (10, 10)])
async def test_import_progress_note_not_stored_at_10(default, expected):
    from totalreclaw.hermes import tools

    state = MagicMock()
    state.is_configured.return_value = True
    seen = {}

    async def _fake_remember(args, st, **kw):
        seen.update(args)
        return "{}"

    with patch.object(tools, "remember", _fake_remember):
        if default is None:
            await tools._persist_import_memory(state, text="n")
        else:
            await tools._persist_import_memory(state, text="n", importance=default)
    assert seen["importance"] == expected
    assert normalize_importance(seen["importance"]) == expected
