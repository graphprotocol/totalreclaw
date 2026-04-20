"""Tests for pin_fact / unpin_fact (Phase 2 Slice 2e-python).

Covers:
  * operations.pin_fact / unpin_fact round-trip: fetch → parse → mutate → encrypt → write
  * Idempotency: no on-chain write when already in target state
  * Supersession: old fact tombstoned, new fact written with `sup` short-key
  * Validation: empty fact_id raises ValueError
  * Client API: pin_fact / unpin_fact delegate correctly
  * Hermes tool layer: totalreclaw_pin / totalreclaw_unpin with input validation

All tests mock the relay + the on-chain UserOp build path — no network.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

import totalreclaw_core
from totalreclaw.crypto import derive_keys_from_mnemonic, encrypt, decrypt
from totalreclaw.operations import pin_fact, unpin_fact
from totalreclaw.relay import RelayClient

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon about"
)

# Derive EOA credentials for signing path.
from eth_account import Account as _Account

_Account.enable_unaudited_hdwallet_features()
_EOA = _Account.from_mnemonic(TEST_MNEMONIC, account_path="m/44'/60'/0'/0/0")
EOA_ADDRESS = _EOA.address
EOA_PRIVATE_KEY = bytes(_EOA.key)

OWNER = "0x0000000000000000000000000000000000001234"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_claim_json(text: str, status: str | None = None, supersedes: str | None = None) -> str:
    """Build a canonical Claim JSON via totalreclaw_core."""
    claim = {
        "t": text,
        "c": "pref",
        "cf": 0.9,
        "i": 7,
        "sa": "hermes-agent",
        "ea": "2026-04-12T10:00:00.000Z",
    }
    if status is not None:
        claim["st"] = status
    if supersedes is not None:
        claim["sup"] = supersedes
    return totalreclaw_core.canonicalize_claim(
        json.dumps(claim, ensure_ascii=False, separators=(",", ":"))
    )


def _encrypted_hex(plaintext: str, key: bytes) -> str:
    """Return subgraph-style 0x-prefixed hex encrypted blob."""
    b64 = encrypt(plaintext, key)
    return "0x" + base64.b64decode(b64).hex()


def _build_relay_mock(keys, *, fact_id: str, text: str, status: str | None = None) -> AsyncMock:
    """Build a RelayClient mock that returns a single fact on fact-by-id queries."""
    claim_blob = _make_claim_json(text, status=status)
    relay = AsyncMock(spec=RelayClient)
    relay._relay_url = "https://api.totalreclaw.xyz"
    relay._auth_key_hex = "deadbeef"
    relay._client_id = "test"

    async def query(query_str, variables, chain=None):
        # fact-by-id query returns a single fact; everything else returns empty
        if "fact(id" in query_str or "Fact(id" in query_str.lower() or "id: $id" in query_str:
            return {
                "data": {
                    "fact": {
                        "id": fact_id,
                        "owner": OWNER.lower(),
                        "encryptedBlob": _encrypted_hex(claim_blob, keys.encryption_key),
                        "encryptedEmbedding": None,
                        "decayScore": "0.7",
                        "timestamp": "1760000000",
                        "createdAt": "1760000000",
                        "isActive": True,
                        "contentFp": "abc123",
                    }
                }
            }
        return {"data": {}}

    relay.query_subgraph = AsyncMock(side_effect=query)
    return relay


# ---------------------------------------------------------------------------
# pin_fact — low-level operation
# ---------------------------------------------------------------------------


class TestPinFact:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    async def test_pin_rejects_empty_fact_id(self, keys):
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="fact_id"):
            await pin_fact(
                fact_id="",
                keys=keys,
                owner=OWNER,
                relay=relay,
                eoa_private_key=EOA_PRIVATE_KEY,
                eoa_address=EOA_ADDRESS,
            )

    @pytest.mark.asyncio
    async def test_pin_rejects_whitespace_fact_id(self, keys):
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="fact_id"):
            await pin_fact(
                fact_id="   ",
                keys=keys,
                owner=OWNER,
                relay=relay,
                eoa_private_key=EOA_PRIVATE_KEY,
                eoa_address=EOA_ADDRESS,
            )

    @pytest.mark.asyncio
    async def test_pin_requires_eoa_key(self, keys):
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="eoa_private_key"):
            await pin_fact(
                fact_id="fact-123",
                keys=keys,
                owner=OWNER,
                relay=relay,
                eoa_private_key=None,
                eoa_address=None,
            )

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_pin_active_claim_writes_new_fact(self, mock_send, keys):
        mock_send.return_value = "0xabc"
        relay = _build_relay_mock(
            keys, fact_id="old-fact-id", text="Pedro prefers dark mode", status=None
        )

        result = await pin_fact(
            fact_id="old-fact-id",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        assert result["success"] is True
        assert result["previous_status"] == "active"
        assert result["new_status"] == "pinned"
        # Response shape mirrors the MCP slice-2e tool:
        #   fact_id  — the ORIGINAL (now tombstoned) fact id
        #   new_fact_id — the new fact carrying the pinned status
        assert result["fact_id"] == "old-fact-id"
        assert "new_fact_id" in result
        assert result["new_fact_id"] != "old-fact-id"
        assert result.get("idempotent") is not True

        # 2.2.3 atomic pin: ONE batched UserOp carrying BOTH the tombstone
        # and the new pinned fact. Previously this was two sequential
        # ``build_and_send_userop`` calls — the pre-2.2.3 mempool-race bug.
        assert mock_send.await_count == 1
        sent_kwargs = mock_send.await_args.kwargs
        assert len(sent_kwargs["protobuf_payloads"]) == 2, (
            "pin must send tombstone + new fact in a single executeBatch UserOp"
        )

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_pin_already_pinned_is_idempotent(self, mock_send, keys):
        mock_send.return_value = "0xabc"
        relay = _build_relay_mock(
            keys, fact_id="already-pinned", text="Pedro prefers dark mode", status="p"
        )

        result = await pin_fact(
            fact_id="already-pinned",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        assert result["success"] is True
        assert result["idempotent"] is True
        assert result["previous_status"] == "pinned"
        assert result["new_status"] == "pinned"
        assert result["fact_id"] == "already-pinned"
        # No new fact id on idempotent no-op
        assert result.get("new_fact_id") is None
        # No on-chain batch write
        assert mock_send.await_count == 0

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_pin_new_blob_parses_back_as_pinned(self, mock_send, keys):
        """Capture the ciphertext of the new fact (second UserOp), decrypt,
        and verify the canonical claim is a v1.1 MemoryClaimV1 JSON with
        ``pin_status: "pinned"`` and ``superseded_by: <old_fact_id>``.

        Wave 2a (Bug #8, 2026-04-20) flipped the write path from v0
        short-key blobs (``t``, ``c``, ``st``) to v1.1 long-form
        (``text``, ``type``, ``pin_status``) so Python-pinned facts are
        indistinguishable from plugin-pinned facts on the subgraph.
        """
        mock_send.return_value = "0xabc"
        relay = _build_relay_mock(
            keys, fact_id="old-fact-id", text="Sarah loves Django", status=None
        )

        captured_payload_lists: list[list[bytes]] = []

        async def capture(**kwargs):
            captured_payload_lists.append(kwargs["protobuf_payloads"])
            return "0xok"

        mock_send.side_effect = capture

        await pin_fact(
            fact_id="old-fact-id",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        # 2.2.3: exactly ONE batched UserOp carrying both payloads.
        # Index 0 inside the batch = tombstone; index 1 = new-fact write.
        assert len(captured_payload_lists) == 1
        payloads = captured_payload_lists[0]
        assert len(payloads) == 2
        new_payload = payloads[1]

        encrypted_blob_bytes = _extract_protobuf_bytes_field(new_payload, field_number=4)
        assert encrypted_blob_bytes is not None
        encrypted_b64 = base64.b64encode(encrypted_blob_bytes).decode("ascii")
        plaintext = decrypt(encrypted_b64, keys.encryption_key)

        parsed = json.loads(plaintext)
        # v1.1 long-form shape — matches what plugin 3.2.0 emits.
        assert parsed["text"] == "Sarah loves Django"
        assert parsed["type"] == "preference"  # from v0 'pref' category upgrade
        assert parsed["schema_version"].startswith("1.")
        assert parsed["pin_status"] == "pinned"
        assert parsed["superseded_by"] == "old-fact-id"
        # v0 short keys must NOT appear in a v1 blob.
        assert "t" not in parsed
        assert "c" not in parsed
        assert "st" not in parsed
        assert "sup" not in parsed


# ---------------------------------------------------------------------------
# unpin_fact — low-level operation
# ---------------------------------------------------------------------------


class TestUnpinFact:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    async def test_unpin_rejects_empty_fact_id(self, keys):
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="fact_id"):
            await unpin_fact(
                fact_id="",
                keys=keys,
                owner=OWNER,
                relay=relay,
                eoa_private_key=EOA_PRIVATE_KEY,
                eoa_address=EOA_ADDRESS,
            )

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_unpin_pinned_claim_writes_new_fact(self, mock_send, keys):
        mock_send.return_value = "0xabc"
        relay = _build_relay_mock(
            keys, fact_id="pinned-fact", text="Pedro likes clean code", status="p"
        )

        result = await unpin_fact(
            fact_id="pinned-fact",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        assert result["success"] is True
        assert result["previous_status"] == "pinned"
        assert result["new_status"] == "active"
        assert result["fact_id"] == "pinned-fact"
        assert "new_fact_id" in result
        assert result["new_fact_id"] != "pinned-fact"
        assert result.get("idempotent") is not True
        # 2.2.3 atomic unpin: ONE batched UserOp (tombstone + new fact).
        assert mock_send.await_count == 1
        sent_kwargs = mock_send.await_args.kwargs
        assert len(sent_kwargs["protobuf_payloads"]) == 2

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_unpin_already_active_is_idempotent(self, mock_send, keys):
        mock_send.return_value = "0xabc"
        relay = _build_relay_mock(
            keys, fact_id="active-fact", text="Pedro likes clean code", status=None
        )

        result = await unpin_fact(
            fact_id="active-fact",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        assert result["success"] is True
        assert result["idempotent"] is True
        assert result["previous_status"] == "active"
        assert result["new_status"] == "active"
        assert result["fact_id"] == "active-fact"
        assert result.get("new_fact_id") is None
        # No on-chain batch write on idempotent no-op.
        assert mock_send.await_count == 0

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_unpin_new_blob_has_explicit_unpinned_status(self, mock_send, keys):
        """When unpin writes a new v1.1 blob, ``pin_status`` is set
        explicitly to ``"unpinned"`` — matches ``skill/plugin/pin.ts``
        line 550 (``pinStatus = targetStatus === 'pinned' ? 'pinned' : 'unpinned'``).

        Prior to Wave 2a the path emitted a v0 short-key blob that
        OMITTED ``st`` to mean "active" — that behavior is gone.
        """
        mock_send.return_value = "0xabc"
        relay = _build_relay_mock(
            keys, fact_id="pinned-fact", text="Likes TDD", status="p"
        )

        captured_payload_lists: list[list[bytes]] = []

        async def capture(**kwargs):
            captured_payload_lists.append(kwargs["protobuf_payloads"])
            return "0xok"

        mock_send.side_effect = capture

        await unpin_fact(
            fact_id="pinned-fact",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        # 2.2.3: single batched UserOp. Index 1 inside the batch is the new-fact write.
        assert len(captured_payload_lists) == 1
        new_payload = captured_payload_lists[0][1]
        encrypted_blob_bytes = _extract_protobuf_bytes_field(new_payload, field_number=4)
        encrypted_b64 = base64.b64encode(encrypted_blob_bytes).decode("ascii")
        plaintext = decrypt(encrypted_b64, keys.encryption_key)
        parsed = json.loads(plaintext)
        # v1.1 shape: explicit unpinned status on the new blob, plus the
        # superseded_by chain link back to the old pinned fact.
        assert parsed["schema_version"].startswith("1.")
        assert parsed["pin_status"] == "unpinned"
        assert parsed["superseded_by"] == "pinned-fact"
        assert parsed["text"] == "Likes TDD"


# ---------------------------------------------------------------------------
# Client API
# ---------------------------------------------------------------------------


class TestClientPinApi:
    @pytest.mark.asyncio
    async def test_client_pin_delegates_to_operations(self):
        from totalreclaw.client import TotalReclaw

        client = TotalReclaw(recovery_phrase=TEST_MNEMONIC, wallet_address=OWNER)
        client._registered = True  # skip register() call

        with patch(
            "totalreclaw.client.pin_fact", new_callable=AsyncMock
        ) as mock_pin:
            mock_pin.return_value = {
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_status": "active",
                "new_status": "pinned",
            }
            result = await client.pin_fact("old-id")
            assert result["new_status"] == "pinned"
            assert result["new_fact_id"] == "new-id"
            mock_pin.assert_awaited_once()
            call_kwargs = mock_pin.await_args.kwargs
            assert call_kwargs["fact_id"] == "old-id"
            assert call_kwargs["owner"] == OWNER.lower()
        await client.close()

    @pytest.mark.asyncio
    async def test_client_unpin_delegates_to_operations(self):
        from totalreclaw.client import TotalReclaw

        client = TotalReclaw(recovery_phrase=TEST_MNEMONIC, wallet_address=OWNER)
        client._registered = True

        with patch(
            "totalreclaw.client.unpin_fact", new_callable=AsyncMock
        ) as mock_unpin:
            mock_unpin.return_value = {
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_status": "pinned",
                "new_status": "active",
            }
            result = await client.unpin_fact("old-id")
            assert result["new_status"] == "active"
            assert result["new_fact_id"] == "new-id"
            mock_unpin.assert_awaited_once()
        await client.close()


# ---------------------------------------------------------------------------
# Hermes tool wrappers
# ---------------------------------------------------------------------------


class TestHermesPinTool:
    @pytest.mark.asyncio
    async def test_pin_tool_unconfigured(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import pin as pin_tool

        state = PluginState()
        result_str = await pin_tool({"fact_id": "abc"}, state)
        result = json.loads(result_str)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_pin_tool_missing_fact_id(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import pin as pin_tool

        state = PluginState()
        # Pre-populate with a fake client so we get past the unconfigured guard
        state._client = AsyncMock()
        result_str = await pin_tool({}, state)
        result = json.loads(result_str)
        assert "error" in result
        assert "fact_id" in result["error"]

    @pytest.mark.asyncio
    async def test_pin_tool_non_string_fact_id(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import pin as pin_tool

        state = PluginState()
        state._client = AsyncMock()
        result_str = await pin_tool({"fact_id": 12345}, state)
        result = json.loads(result_str)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_pin_tool_success(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import pin as pin_tool

        state = PluginState()
        fake_client = AsyncMock()
        fake_client.pin_fact = AsyncMock(
            return_value={
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_status": "active",
                "new_status": "pinned",
            }
        )
        state._client = fake_client

        result_str = await pin_tool(
            {"fact_id": "old-id", "reason": "user says this is foundational"}, state
        )
        result = json.loads(result_str)
        assert result["pinned"] is True
        assert result["fact_id"] == "old-id"
        assert result["new_fact_id"] == "new-id"
        assert result["new_status"] == "pinned"
        fake_client.pin_fact.assert_awaited_once_with("old-id")

    @pytest.mark.asyncio
    async def test_pin_tool_client_raises(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import pin as pin_tool

        state = PluginState()
        fake_client = AsyncMock()
        fake_client.pin_fact = AsyncMock(side_effect=RuntimeError("boom"))
        state._client = fake_client

        result_str = await pin_tool({"fact_id": "id"}, state)
        result = json.loads(result_str)
        assert "error" in result
        assert "boom" in result["error"]


class TestHermesUnpinTool:
    @pytest.mark.asyncio
    async def test_unpin_tool_success(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import unpin as unpin_tool

        state = PluginState()
        fake_client = AsyncMock()
        fake_client.unpin_fact = AsyncMock(
            return_value={
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_status": "pinned",
                "new_status": "active",
            }
        )
        state._client = fake_client

        result_str = await unpin_tool({"fact_id": "old-id"}, state)
        result = json.loads(result_str)
        assert result["unpinned"] is True
        assert result["fact_id"] == "old-id"
        assert result["new_fact_id"] == "new-id"
        fake_client.unpin_fact.assert_awaited_once_with("old-id")

    @pytest.mark.asyncio
    async def test_unpin_tool_missing_fact_id(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import unpin as unpin_tool

        state = PluginState()
        state._client = AsyncMock()
        result_str = await unpin_tool({"fact_id": ""}, state)
        result = json.loads(result_str)
        assert "error" in result


# ---------------------------------------------------------------------------
# Hermes schemas
# ---------------------------------------------------------------------------


class TestHermesSchemas:
    def test_pin_schema_shape(self):
        from totalreclaw.hermes.schemas import PIN

        assert PIN["name"] == "totalreclaw_pin"
        assert "fact_id" in PIN["parameters"]["properties"]
        assert PIN["parameters"]["required"] == ["fact_id"]

    def test_unpin_schema_shape(self):
        from totalreclaw.hermes.schemas import UNPIN

        assert UNPIN["name"] == "totalreclaw_unpin"
        assert "fact_id" in UNPIN["parameters"]["properties"]
        assert UNPIN["parameters"]["required"] == ["fact_id"]


# ---------------------------------------------------------------------------
# Protobuf extraction helper (test-only, minimal parser)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Slice 2f — feedback wiring on pin/unpin
# ---------------------------------------------------------------------------


def _sample_components(weighted: float) -> dict:
    return {
        "confidence": 0.85,
        "corroboration": 1.0,
        "recency": 0.5,
        "validation": 0.7,
        "weighted_total": weighted,
    }


def _seed_decision_row(state_dir, entry: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "decisions.jsonl"
    existing = p.read_text() if p.exists() else ""
    p.write_text(existing + json.dumps(entry) + "\n", encoding="utf-8")


def _clear_logs(state_dir) -> None:
    for name in ("decisions.jsonl", "feedback.jsonl"):
        p = state_dir / name
        if p.exists():
            p.unlink()


class TestPinFeedbackWiring:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.fixture
    def isolated_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
        return tmp_path

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_pin_loser_writes_feedback(self, mock_send, keys, isolated_state_dir):
        mock_send.return_value = "0xabc"
        _clear_logs(isolated_state_dir)
        _seed_decision_row(
            isolated_state_dir,
            {
                "ts": 1_776_000_000,
                "entity_id": "editor",
                "new_claim_id": "vscode-winner",
                "existing_claim_id": "vim-loser",
                "similarity": 0.5,
                "action": "supersede_existing",
                "reason": "new_wins",
                "winner_score": 0.83,
                "loser_score": 0.73,
                "winner_components": _sample_components(0.83),
                "loser_components": _sample_components(0.73),
                "mode": "active",
            },
        )
        relay = _build_relay_mock(keys, fact_id="vim-loser", text="Pedro uses Vim", status=None)
        result = await pin_fact(
            fact_id="vim-loser",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )
        assert result["success"] is True

        feedback_file = isolated_state_dir / "feedback.jsonl"
        assert feedback_file.exists(), "feedback.jsonl should exist"
        lines = [l for l in feedback_file.read_text().split("\n") if l]
        assert len(lines) == 1
        fb = json.loads(lines[0])
        assert fb["claim_a_id"] == "vim-loser"
        assert fb["claim_b_id"] == "vscode-winner"
        assert fb["formula_winner"] == "b"
        assert fb["user_decision"] == "pin_a"
        assert fb["winner_components"]["weighted_total"] == 0.83
        assert fb["loser_components"]["weighted_total"] == 0.73

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_voluntary_pin_writes_no_feedback(self, mock_send, keys, isolated_state_dir):
        mock_send.return_value = "0xabc"
        _clear_logs(isolated_state_dir)
        _seed_decision_row(
            isolated_state_dir,
            {
                "ts": 1_776_000_000,
                "entity_id": "editor",
                "new_claim_id": "unrelated-a",
                "existing_claim_id": "unrelated-b",
                "similarity": 0.5,
                "action": "supersede_existing",
                "winner_components": _sample_components(0.83),
                "loser_components": _sample_components(0.73),
            },
        )
        relay = _build_relay_mock(keys, fact_id="teal-id", text="My favorite color is teal", status=None)
        result = await pin_fact(
            fact_id="teal-id",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )
        assert result["success"] is True
        feedback_file = isolated_state_dir / "feedback.jsonl"
        if feedback_file.exists():
            assert feedback_file.read_text().strip() == ""

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_unpin_winner_writes_pin_b_feedback(self, mock_send, keys, isolated_state_dir):
        mock_send.return_value = "0xabc"
        _clear_logs(isolated_state_dir)
        _seed_decision_row(
            isolated_state_dir,
            {
                "ts": 1_776_100_000,
                "entity_id": "editor",
                "new_claim_id": "vscode-winner-2",
                "existing_claim_id": "vim-tombstoned",
                "similarity": 0.5,
                "action": "supersede_existing",
                "winner_components": _sample_components(0.83),
                "loser_components": _sample_components(0.73),
            },
        )
        relay = _build_relay_mock(
            keys, fact_id="vscode-winner-2", text="Pedro prefers VS Code", status="p"
        )
        result = await unpin_fact(
            fact_id="vscode-winner-2",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )
        assert result["success"] is True
        feedback_file = isolated_state_dir / "feedback.jsonl"
        content = feedback_file.read_text()
        lines = [l for l in content.split("\n") if l]
        assert len(lines) == 1
        fb = json.loads(lines[0])
        assert fb["user_decision"] == "pin_b"
        assert fb["claim_b_id"] == "vscode-winner-2"

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_idempotent_pin_writes_no_feedback(self, mock_send, keys, isolated_state_dir):
        mock_send.return_value = "0xabc"
        _clear_logs(isolated_state_dir)
        # Even with a matching decision row, an idempotent pin must not emit feedback.
        _seed_decision_row(
            isolated_state_dir,
            {
                "ts": 1_776_000_000,
                "entity_id": "x",
                "new_claim_id": "winner",
                "existing_claim_id": "already-pinned",
                "similarity": 0.5,
                "action": "supersede_existing",
                "winner_components": _sample_components(0.83),
                "loser_components": _sample_components(0.73),
            },
        )
        relay = _build_relay_mock(keys, fact_id="already-pinned", text="foo", status="p")
        result = await pin_fact(
            fact_id="already-pinned",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )
        assert result.get("idempotent") is True
        feedback_file = isolated_state_dir / "feedback.jsonl"
        if feedback_file.exists():
            assert feedback_file.read_text().strip() == ""

    @pytest.mark.asyncio
    @patch("totalreclaw.operations.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_legacy_decision_row_without_components_no_feedback(
        self, mock_send, keys, isolated_state_dir
    ):
        mock_send.return_value = "0xabc"
        _clear_logs(isolated_state_dir)
        # Pre-Slice-2f row — no winner/loser components.
        _seed_decision_row(
            isolated_state_dir,
            {
                "ts": 1_776_000_000,
                "entity_id": "editor",
                "new_claim_id": "winner",
                "existing_claim_id": "old-loser",
                "similarity": 0.5,
                "action": "supersede_existing",
                "reason": "new_wins",
                "winner_score": 0.83,
                "loser_score": 0.73,
            },
        )
        relay = _build_relay_mock(keys, fact_id="old-loser", text="Pedro uses Vim", status=None)
        result = await pin_fact(
            fact_id="old-loser",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )
        assert result["success"] is True
        feedback_file = isolated_state_dir / "feedback.jsonl"
        if feedback_file.exists():
            assert feedback_file.read_text().strip() == ""


def _extract_protobuf_bytes_field(payload: bytes, field_number: int) -> bytes | None:
    """Scan a protobuf payload for a length-delimited field and return its raw bytes.

    Supports wire types 0 (varint), 1 (fixed64), 2 (length-delimited), 5 (fixed32).
    Returns None if the field isn't found.
    """
    i = 0
    n = len(payload)
    while i < n:
        # Decode tag varint
        tag = 0
        shift = 0
        while True:
            b = payload[i]
            i += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        fn = tag >> 3
        wt = tag & 0x07

        if wt == 0:  # varint
            while payload[i] & 0x80:
                i += 1
            i += 1
        elif wt == 1:  # fixed64
            i += 8
        elif wt == 2:  # length-delimited
            length = 0
            shift = 0
            while True:
                b = payload[i]
                i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            value = payload[i : i + length]
            i += length
            if fn == field_number:
                return value
        elif wt == 5:  # fixed32
            i += 4
        else:
            raise ValueError(f"Unsupported wire type {wt}")
    return None
