"""Tests for retype / set_scope (Hermes Python parity, rc.23 / issue #150).

Mirrors ``skill/plugin/retype-setscope.test.ts`` so the Hermes Python and
the OpenClaw plugin TS paths exercise the same surface: input validation,
v1.1 blob shape, supersession chain, ``pin_status`` preservation, and
client + tool wrappers.

Coverage:
  * ``validate_retype_args`` / ``validate_set_scope_args`` accept and reject
    well- / ill-formed inputs (camelCase + snake_case parity with TS)
  * ``execute_retype`` happy path: fetch → decrypt → mutate → batch submit
  * ``execute_set_scope`` happy path
  * ``pin_status`` preservation across a retype/set_scope rewrite
    (regression shield for issue #117 / TS PR #114)
  * Error paths: missing fact, malformed blob, invalid type/scope
  * Atomic batch: tombstone + new fact in ONE ``executeBatch`` UserOp
  * Client API: ``client.retype`` / ``client.set_scope`` delegate correctly
  * Hermes tool layer: ``totalreclaw_retype`` / ``totalreclaw_set_scope``
  * Hermes plugin manifest parity (plugin.yaml advertises both tools)

All tests mock the relay + the on-chain UserOp build path — no network.

These tests are designed to FAIL on the rc.22 baseline (no Python
retype/set_scope module) and PASS on this rc.23 branch.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import totalreclaw_core
from totalreclaw.claims_helper import build_canonical_claim_v1
from totalreclaw.crypto import derive_keys_from_mnemonic, encrypt, decrypt
from totalreclaw.relay import RelayClient
from totalreclaw.retype_setscope import (
    execute_retype,
    execute_set_scope,
    validate_retype_args,
    validate_set_scope_args,
)


TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon about"
)

from eth_account import Account as _Account

_Account.enable_unaudited_hdwallet_features()
_EOA = _Account.from_mnemonic(TEST_MNEMONIC, account_path="m/44'/60'/0'/0/0")
EOA_ADDRESS = _EOA.address
EOA_PRIVATE_KEY = bytes(_EOA.key)

OWNER = "0x0000000000000000000000000000000000001234"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_v1_blob(
    *,
    fact_id: str,
    text: str,
    fact_type: str = "claim",
    source: str = "user",
    scope: str | None = None,
    pin_status: str | None = None,
    importance: int = 7,
    confidence: float = 0.9,
) -> str:
    """Produce a v1.1 ``MemoryClaimV1`` JSON blob matching the production builder."""

    class _Fact:
        pass

    fact = _Fact()
    fact.text = text
    fact.type = fact_type
    fact.source = source
    fact.scope = scope
    fact.reasoning = None
    fact.entities = None
    fact.confidence = confidence
    fact.volatility = None
    return build_canonical_claim_v1(
        fact,
        importance=importance,
        created_at="2026-04-25T10:00:00.000Z",
        claim_id=fact_id,
        pin_status=pin_status,
    )


def _encrypted_hex(plaintext: str, key: bytes) -> str:
    b64 = encrypt(plaintext, key)
    return "0x" + base64.b64decode(b64).hex()


def _build_relay_mock(
    keys, *, fact_id: str, blob: str, fetch_returns_null: bool = False
) -> AsyncMock:
    relay = AsyncMock(spec=RelayClient)
    relay._relay_url = "https://api-staging.totalreclaw.xyz"
    relay._auth_key_hex = "deadbeef"
    relay._client_id = "test"

    async def query(query_str, variables, chain=None):
        if "fact(id" in query_str or "id: $id" in query_str:
            if fetch_returns_null:
                return {"data": {"fact": None}}
            return {
                "data": {
                    "fact": {
                        "id": fact_id,
                        "owner": OWNER.lower(),
                        "encryptedBlob": _encrypted_hex(blob, keys.encryption_key),
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


def _extract_protobuf_bytes_field(payload: bytes, field_number: int) -> bytes | None:
    """Minimal protobuf-wire scanner — find a length-delimited field by number."""
    i = 0
    n = len(payload)
    while i < n:
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
        if wt == 0:
            while payload[i] & 0x80:
                i += 1
            i += 1
        elif wt == 1:
            i += 8
        elif wt == 2:
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
        elif wt == 5:
            i += 4
        else:
            raise ValueError(f"Unsupported wire type {wt}")
    return None


# ---------------------------------------------------------------------------
# validate_retype_args / validate_set_scope_args — input parsing
# ---------------------------------------------------------------------------


class TestValidateRetypeArgs:
    def test_accepts_snake_case(self):
        r = validate_retype_args({"fact_id": "abc-123", "new_type": "preference"})
        assert r["ok"] is True
        assert r["fact_id"] == "abc-123"
        assert r["new_type"] == "preference"

    def test_accepts_camel_case(self):
        r = validate_retype_args({"factId": "abc-123", "newType": "claim"})
        assert r["ok"] is True
        assert r["new_type"] == "claim"

    def test_rejects_invalid_type(self):
        r = validate_retype_args({"fact_id": "abc", "new_type": "banana"})
        assert r["ok"] is False
        assert "new_type" in r["error"]

    def test_rejects_missing_fact_id(self):
        r = validate_retype_args({"new_type": "claim"})
        assert r["ok"] is False
        assert "fact_id" in r["error"]

    def test_rejects_empty_fact_id(self):
        r = validate_retype_args({"fact_id": "", "new_type": "claim"})
        assert r["ok"] is False
        assert "fact_id" in r["error"]

    def test_rejects_null(self):
        r = validate_retype_args(None)
        assert r["ok"] is False


class TestValidateSetScopeArgs:
    def test_accepts_snake_case(self):
        r = validate_set_scope_args({"fact_id": "abc", "new_scope": "work"})
        assert r["ok"] is True
        assert r["new_scope"] == "work"

    def test_accepts_health_scope(self):
        r = validate_set_scope_args({"fact_id": "abc", "new_scope": "health"})
        assert r["ok"] is True

    def test_rejects_invalid_scope(self):
        r = validate_set_scope_args({"fact_id": "abc", "new_scope": "banana"})
        assert r["ok"] is False
        assert "new_scope" in r["error"]

    def test_rejects_empty_fact_id(self):
        r = validate_set_scope_args({"fact_id": "", "new_scope": "work"})
        assert r["ok"] is False


# ---------------------------------------------------------------------------
# execute_retype / execute_set_scope — integration-style with mock deps
# ---------------------------------------------------------------------------


class TestExecuteRetype:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    async def test_retype_rejects_invalid_type(self, keys):
        relay = AsyncMock(spec=RelayClient)
        result = await execute_retype(
            fact_id="abc",
            new_type="banana",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
        )
        assert result["success"] is False
        assert "Invalid new_type" in result["error"]

    @pytest.mark.asyncio
    async def test_retype_requires_eoa_key(self, keys):
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="eoa_private_key"):
            await execute_retype(
                fact_id="abc",
                new_type="preference",
                keys=keys,
                owner=OWNER,
                relay=relay,
                eoa_private_key=None,
                eoa_address=None,
            )

    @pytest.mark.asyncio
    async def test_retype_rejects_empty_fact_id(self, keys):
        relay = AsyncMock(spec=RelayClient)
        with pytest.raises(ValueError, match="fact_id"):
            await execute_retype(
                fact_id="",
                new_type="preference",
                keys=keys,
                owner=OWNER,
                relay=relay,
                eoa_private_key=EOA_PRIVATE_KEY,
                eoa_address=EOA_ADDRESS,
            )

    @pytest.mark.asyncio
    @patch("totalreclaw.retype_setscope.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_retype_happy_path_claim_to_preference(self, mock_send, keys):
        """Happy path — retype an existing v1 claim to a preference.

        Mirrors the TS plugin test "executeRetype: success" + companions.
        Asserts the response shape (previous_type, new_type, fact_id,
        new_fact_id) and that the on-chain submission was a single batched
        UserOp carrying tombstone + new fact.
        """
        mock_send.return_value = "0xfeedfeed"

        existing_blob = _build_v1_blob(
            fact_id="old-fact-id",
            text="I prefer PostgreSQL over MySQL",
            fact_type="claim",
        )
        relay = _build_relay_mock(keys, fact_id="old-fact-id", blob=existing_blob)

        result = await execute_retype(
            fact_id="old-fact-id",
            new_type="preference",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        assert result["success"] is True
        assert result["fact_id"] == "old-fact-id"
        assert result["new_fact_id"] != "old-fact-id"
        assert result["previous_type"] == "claim"
        assert result["new_type"] == "preference"

        # Single atomic executeBatch UserOp carrying tombstone + new fact.
        assert mock_send.await_count == 1
        sent_kwargs = mock_send.await_args.kwargs
        assert len(sent_kwargs["protobuf_payloads"]) == 2

    @pytest.mark.asyncio
    @patch("totalreclaw.retype_setscope.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_retype_returns_error_on_missing_fact(self, mock_send, keys):
        relay = _build_relay_mock(
            keys, fact_id="missing", blob="", fetch_returns_null=True
        )
        result = await execute_retype(
            fact_id="missing",
            new_type="preference",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
        )
        assert result["success"] is False
        assert "Fact not found" in result["error"]
        # No on-chain write attempted.
        assert mock_send.await_count == 0

    @pytest.mark.asyncio
    @patch("totalreclaw.retype_setscope.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_retype_new_blob_is_v1_with_supersedes(self, mock_send, keys):
        """Extract the new fact's ciphertext, decrypt, and verify the v1.1
        canonical shape: ``schema_version`` "1.x", new ``type``, and
        ``superseded_by`` pointing at the old fact id.
        """
        mock_send.return_value = "0xabc"
        existing_blob = _build_v1_blob(
            fact_id="old-fact-id",
            text="Sarah loves Django",
            fact_type="claim",
        )
        relay = _build_relay_mock(keys, fact_id="old-fact-id", blob=existing_blob)

        captured: list[list[bytes]] = []

        async def capture(**kwargs):
            captured.append(kwargs["protobuf_payloads"])
            return "0xok"

        mock_send.side_effect = capture

        await execute_retype(
            fact_id="old-fact-id",
            new_type="preference",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        assert len(captured) == 1
        payloads = captured[0]
        assert len(payloads) == 2
        # Index 1 = the new-fact write.
        encrypted_blob_bytes = _extract_protobuf_bytes_field(payloads[1], field_number=4)
        assert encrypted_blob_bytes is not None
        encrypted_b64 = base64.b64encode(encrypted_blob_bytes).decode("ascii")
        plaintext = decrypt(encrypted_b64, keys.encryption_key)

        parsed = json.loads(plaintext)
        assert parsed["text"] == "Sarah loves Django"
        assert parsed["type"] == "preference"
        assert parsed["schema_version"].startswith("1.")
        assert parsed["superseded_by"] == "old-fact-id"
        # v0 short keys must NOT appear.
        assert "t" not in parsed
        assert "c" not in parsed

    @pytest.mark.asyncio
    @patch("totalreclaw.retype_setscope.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_retype_preserves_pin_status(self, mock_send, keys):
        """Regression shield for issue #117 / TS PR #114.

        When the user retypes a *pinned* fact, the rewrite MUST inherit
        ``pin_status: "pinned"`` from the source. Without this, a metadata
        edit silently un-pins the fact and auto-resolution can supersede
        it on the next conflicting write.
        """
        mock_send.return_value = "0xabc"
        existing_blob = _build_v1_blob(
            fact_id="pinned-fact",
            text="My favorite color is blue",
            fact_type="claim",
            pin_status="pinned",
        )
        # Sanity-check the fixture itself.
        assert json.loads(existing_blob)["pin_status"] == "pinned"

        relay = _build_relay_mock(keys, fact_id="pinned-fact", blob=existing_blob)

        captured: list[list[bytes]] = []

        async def capture(**kwargs):
            captured.append(kwargs["protobuf_payloads"])
            return "0xok"

        mock_send.side_effect = capture

        await execute_retype(
            fact_id="pinned-fact",
            new_type="preference",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        encrypted_blob_bytes = _extract_protobuf_bytes_field(
            captured[0][1], field_number=4
        )
        plaintext = decrypt(
            base64.b64encode(encrypted_blob_bytes).decode("ascii"),
            keys.encryption_key,
        )
        parsed = json.loads(plaintext)
        assert parsed["pin_status"] == "pinned", (
            "retype must preserve pin_status — issue #117 regression"
        )
        assert parsed["type"] == "preference"
        assert parsed["superseded_by"] == "pinned-fact"


class TestExecuteSetScope:
    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    async def test_set_scope_rejects_invalid_scope(self, keys):
        relay = AsyncMock(spec=RelayClient)
        result = await execute_set_scope(
            fact_id="abc",
            new_scope="banana",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
        )
        assert result["success"] is False
        assert "Invalid new_scope" in result["error"]

    @pytest.mark.asyncio
    @patch("totalreclaw.retype_setscope.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_set_scope_happy_path(self, mock_send, keys):
        mock_send.return_value = "0xabc"
        existing_blob = _build_v1_blob(
            fact_id="abc-456",
            text="My manager is Alice",
            fact_type="claim",
        )
        relay = _build_relay_mock(keys, fact_id="abc-456", blob=existing_blob)

        result = await execute_set_scope(
            fact_id="abc-456",
            new_scope="work",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        assert result["success"] is True
        assert result["new_scope"] == "work"
        # Default scope from build_canonical_claim_v1 is "unspecified" when
        # the caller omits it (the v1 type guard treats no-scope as
        # equivalent to "unspecified").
        assert result["previous_scope"] == "unspecified"
        assert mock_send.await_count == 1
        assert len(mock_send.await_args.kwargs["protobuf_payloads"]) == 2

    @pytest.mark.asyncio
    @patch("totalreclaw.retype_setscope.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_set_scope_preserves_pin_status(self, mock_send, keys):
        """Same #117 regression shield, on the scope axis."""
        mock_send.return_value = "0xabc"
        existing_blob = _build_v1_blob(
            fact_id="pinned-fact",
            text="Morning runs",
            fact_type="commitment",
            pin_status="pinned",
        )
        relay = _build_relay_mock(keys, fact_id="pinned-fact", blob=existing_blob)

        captured: list[list[bytes]] = []

        async def capture(**kwargs):
            captured.append(kwargs["protobuf_payloads"])
            return "0xok"

        mock_send.side_effect = capture

        await execute_set_scope(
            fact_id="pinned-fact",
            new_scope="health",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )

        encrypted_blob_bytes = _extract_protobuf_bytes_field(
            captured[0][1], field_number=4
        )
        plaintext = decrypt(
            base64.b64encode(encrypted_blob_bytes).decode("ascii"),
            keys.encryption_key,
        )
        parsed = json.loads(plaintext)
        assert parsed["pin_status"] == "pinned"
        assert parsed["scope"] == "health"
        assert parsed["superseded_by"] == "pinned-fact"


# ---------------------------------------------------------------------------
# Cross-client KG parity — plugin write + Hermes retype/set_scope, then
# plugin re-read sees the new values.
#
# The TS plugin and the Python module both go through the same Rust core
# canonicalization. The test below verifies the on-chain JSON shape
# (the only artifact a re-reading client sees) is what either client
# would emit.
# ---------------------------------------------------------------------------


class TestCrossClientKGParity:
    """Cross-client knowledge-graph parity (issue #149 companion).

    The TS plugin (``skill/plugin``) and Hermes Python (this module) both
    write claims through ``totalreclaw_core::validate_memory_claim_v1``,
    so byte-identical canonicalization is enforced upstream. This test
    pins the *retype/set_scope* surface specifically — verifying the
    Python-rewritten claim has the EXACT v1.1 shape a TS-plugin reader
    expects on round-trip.
    """

    @pytest.fixture
    def keys(self):
        return derive_keys_from_mnemonic(TEST_MNEMONIC)

    @pytest.mark.asyncio
    @patch("totalreclaw.retype_setscope.build_and_send_userop_batch", new_callable=AsyncMock)
    async def test_plugin_write_then_hermes_retype_then_setscope_roundtrip(
        self, mock_send, keys
    ):
        """Simulate: plugin stores fact A (claim/unspecified) → Hermes
        retypes A to preference → Hermes sets A's scope to health →
        re-read final v1 blob matches what plugin would emit.

        The chain of two rewrites is what a real session looks like when
        the user disambiguates a fact in two messages. ``superseded_by``
        chains correctly: first rewrite ``superseded_by: <plugin_id>``,
        second rewrite ``superseded_by: <retype_id>``.
        """
        mock_send.return_value = "0xabc"

        # Plugin-emitted v1 blob (claim, scope=unspecified, no pin_status).
        plugin_blob = _build_v1_blob(
            fact_id="plugin-fact-id",
            text="I like dark mode in code editors",
            fact_type="claim",
        )

        # Hermes retype — relay returns the plugin blob.
        relay = _build_relay_mock(keys, fact_id="plugin-fact-id", blob=plugin_blob)
        retype_captured: list[list[bytes]] = []

        async def capture_retype(**kwargs):
            retype_captured.append(kwargs["protobuf_payloads"])
            return "0xretype"

        mock_send.side_effect = capture_retype

        retype_result = await execute_retype(
            fact_id="plugin-fact-id",
            new_type="preference",
            keys=keys,
            owner=OWNER,
            relay=relay,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )
        assert retype_result["success"] is True
        retype_id = retype_result["new_fact_id"]

        # Decrypt the retyped blob and feed it back as the "current" blob
        # for the second rewrite (set_scope). This mimics a re-fetch from
        # the subgraph after the retype indexed.
        retype_blob_bytes = _extract_protobuf_bytes_field(
            retype_captured[0][1], field_number=4
        )
        retype_plaintext = decrypt(
            base64.b64encode(retype_blob_bytes).decode("ascii"),
            keys.encryption_key,
        )
        retype_parsed = json.loads(retype_plaintext)

        # Verify post-retype shape.
        assert retype_parsed["type"] == "preference"
        assert retype_parsed["text"] == "I like dark mode in code editors"
        assert retype_parsed["superseded_by"] == "plugin-fact-id"
        assert retype_parsed["schema_version"].startswith("1.")

        # Now Hermes set_scope on the retyped fact.
        relay2 = _build_relay_mock(keys, fact_id=retype_id, blob=retype_plaintext)
        setscope_captured: list[list[bytes]] = []

        async def capture_setscope(**kwargs):
            setscope_captured.append(kwargs["protobuf_payloads"])
            return "0xsetscope"

        mock_send.side_effect = capture_setscope

        setscope_result = await execute_set_scope(
            fact_id=retype_id,
            new_scope="creative",
            keys=keys,
            owner=OWNER,
            relay=relay2,
            eoa_private_key=EOA_PRIVATE_KEY,
            eoa_address=EOA_ADDRESS,
            sender=OWNER,
        )
        assert setscope_result["success"] is True

        # Decrypt + verify the final blob.
        final_bytes = _extract_protobuf_bytes_field(
            setscope_captured[0][1], field_number=4
        )
        final_plaintext = decrypt(
            base64.b64encode(final_bytes).decode("ascii"),
            keys.encryption_key,
        )
        final = json.loads(final_plaintext)

        # Final claim is what a TS plugin re-read would surface.
        assert final["text"] == "I like dark mode in code editors"
        assert final["type"] == "preference"  # carried over from retype
        assert final["scope"] == "creative"  # set by set_scope
        assert final["superseded_by"] == retype_id
        assert final["schema_version"].startswith("1.")
        # Source preserved across both rewrites.
        assert final["source"] == "user"


# ---------------------------------------------------------------------------
# Client API
# ---------------------------------------------------------------------------


class TestClientRetypeApi:
    @pytest.mark.asyncio
    async def test_client_retype_delegates_to_module(self):
        from totalreclaw.client import TotalReclaw

        client = TotalReclaw(recovery_phrase=TEST_MNEMONIC, wallet_address=OWNER)
        client._registered = True

        with patch(
            "totalreclaw.client.execute_retype", new_callable=AsyncMock
        ) as mock_retype:
            mock_retype.return_value = {
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_type": "claim",
                "new_type": "preference",
                "previous_scope": "unspecified",
                "new_scope": "unspecified",
            }
            result = await client.retype("old-id", "preference")
            assert result["new_type"] == "preference"
            assert result["new_fact_id"] == "new-id"
            mock_retype.assert_awaited_once()
            kwargs = mock_retype.await_args.kwargs
            assert kwargs["fact_id"] == "old-id"
            assert kwargs["new_type"] == "preference"
            assert kwargs["owner"] == OWNER.lower()
        await client.close()

    @pytest.mark.asyncio
    async def test_client_set_scope_delegates_to_module(self):
        from totalreclaw.client import TotalReclaw

        client = TotalReclaw(recovery_phrase=TEST_MNEMONIC, wallet_address=OWNER)
        client._registered = True

        with patch(
            "totalreclaw.client.execute_set_scope", new_callable=AsyncMock
        ) as mock_setscope:
            mock_setscope.return_value = {
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_type": "claim",
                "new_type": "claim",
                "previous_scope": "unspecified",
                "new_scope": "health",
            }
            result = await client.set_scope("old-id", "health")
            assert result["new_scope"] == "health"
            mock_setscope.assert_awaited_once()
        await client.close()


# ---------------------------------------------------------------------------
# Hermes tool wrappers
# ---------------------------------------------------------------------------


class TestHermesRetypeTool:
    @pytest.mark.asyncio
    async def test_retype_tool_unconfigured(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import retype as retype_tool

        state = PluginState()
        result = json.loads(
            await retype_tool({"fact_id": "abc", "new_type": "preference"}, state)
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_retype_tool_invalid_args(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import retype as retype_tool

        state = PluginState()
        state._client = AsyncMock()
        result = json.loads(
            await retype_tool({"fact_id": "abc", "new_type": "banana"}, state)
        )
        assert "error" in result
        assert "new_type" in result["error"]

    @pytest.mark.asyncio
    async def test_retype_tool_success(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import retype as retype_tool

        state = PluginState()
        fake_client = AsyncMock()
        fake_client.retype = AsyncMock(
            return_value={
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_type": "claim",
                "new_type": "preference",
                "previous_scope": "unspecified",
                "new_scope": "unspecified",
                "tx_hash": "0xabc",
            }
        )
        state._client = fake_client
        result = json.loads(
            await retype_tool(
                {"fact_id": "old-id", "new_type": "preference"}, state
            )
        )
        assert result["retyped"] is True
        assert result["fact_id"] == "old-id"
        assert result["new_type"] == "preference"
        assert result["tx_hash"] == "0xabc"
        fake_client.retype.assert_awaited_once_with("old-id", "preference")

    @pytest.mark.asyncio
    async def test_retype_tool_surfaces_partial(self):
        """When confirm-indexed times out, the result.partial flag must
        propagate through the tool wrapper so the agent surfaces it.
        """
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import retype as retype_tool

        state = PluginState()
        fake_client = AsyncMock()
        fake_client.retype = AsyncMock(
            return_value={
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_type": "claim",
                "new_type": "preference",
                "previous_scope": "unspecified",
                "new_scope": "unspecified",
                "partial": True,
            }
        )
        state._client = fake_client
        result = json.loads(
            await retype_tool({"fact_id": "x", "new_type": "preference"}, state)
        )
        assert result["partial"] is True


class TestHermesSetScopeTool:
    @pytest.mark.asyncio
    async def test_set_scope_tool_success(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import set_scope as set_scope_tool

        state = PluginState()
        fake_client = AsyncMock()
        fake_client.set_scope = AsyncMock(
            return_value={
                "success": True,
                "fact_id": "old-id",
                "new_fact_id": "new-id",
                "previous_type": "claim",
                "new_type": "claim",
                "previous_scope": "unspecified",
                "new_scope": "health",
            }
        )
        state._client = fake_client
        result = json.loads(
            await set_scope_tool(
                {"fact_id": "old-id", "new_scope": "health"}, state
            )
        )
        assert result["scope_set"] is True
        assert result["new_scope"] == "health"
        fake_client.set_scope.assert_awaited_once_with("old-id", "health")

    @pytest.mark.asyncio
    async def test_set_scope_tool_invalid_scope(self):
        from totalreclaw.hermes.state import PluginState
        from totalreclaw.hermes.tools import set_scope as set_scope_tool

        state = PluginState()
        state._client = AsyncMock()
        result = json.loads(
            await set_scope_tool(
                {"fact_id": "abc", "new_scope": "banana"}, state
            )
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Hermes plugin manifest parity — plugin.yaml advertises both tools and
# register() wires them. Mirrors test_hermes_plugin_manifest_parity.py's
# enforcement model.
# ---------------------------------------------------------------------------


class TestHermesManifestParity:
    """plugin.yaml ↔ register() parity for retype + set_scope."""

    def test_manifest_advertises_retype_and_set_scope(self):
        import yaml
        from pathlib import Path

        plugin_yaml = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "totalreclaw"
            / "hermes"
            / "plugin.yaml"
        )
        data = yaml.safe_load(plugin_yaml.read_text())
        provides = data.get("provides_tools", [])
        assert "totalreclaw_retype" in provides, (
            "plugin.yaml must advertise totalreclaw_retype — agents read "
            "this list to discover available tools."
        )
        assert "totalreclaw_set_scope" in provides

    def test_register_wires_retype_and_set_scope(self):
        import os
        from pathlib import Path
        from unittest.mock import patch

        from totalreclaw.hermes import register

        ctx = MagicMock()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                register(ctx)

        registered = {
            call.kwargs["name"] for call in ctx.register_tool.call_args_list
        }
        assert "totalreclaw_retype" in registered
        assert "totalreclaw_set_scope" in registered


# ---------------------------------------------------------------------------
# Hermes schemas — shape sanity check
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_retype_schema_shape(self):
        from totalreclaw.hermes.schemas import RETYPE
        from totalreclaw.agent.extraction import VALID_MEMORY_TYPES

        assert RETYPE["name"] == "totalreclaw_retype"
        props = RETYPE["parameters"]["properties"]
        assert "fact_id" in props
        assert "new_type" in props
        assert set(props["new_type"]["enum"]) == set(VALID_MEMORY_TYPES)
        assert RETYPE["parameters"]["required"] == ["fact_id", "new_type"]

    def test_set_scope_schema_shape(self):
        from totalreclaw.hermes.schemas import SET_SCOPE
        from totalreclaw.agent.extraction import VALID_MEMORY_SCOPES

        assert SET_SCOPE["name"] == "totalreclaw_set_scope"
        props = SET_SCOPE["parameters"]["properties"]
        assert "fact_id" in props
        assert "new_scope" in props
        assert set(props["new_scope"]["enum"]) == set(VALID_MEMORY_SCOPES)
        assert SET_SCOPE["parameters"]["required"] == ["fact_id", "new_scope"]


# ---------------------------------------------------------------------------
# Natural-language surface — assert that the schema descriptions cover the
# key NL phrases the agent should map to retype / set_scope.
# ---------------------------------------------------------------------------


class TestNaturalLanguageSurface:
    """The schema descriptions are the agent's prompt for tool selection.

    These checks pin specific natural-language triggers in the description
    so a future refactor doesn't accidentally drop them — silently
    breaking agent routing for the common phrasings.
    """

    def test_retype_description_covers_preference_pivot_phrases(self):
        from totalreclaw.hermes.schemas import RETYPE

        desc = RETYPE["description"].lower()
        # "Actually, my pasta preference should be a preference, not a claim"
        assert "preference" in desc
        # Generic disambiguation triggers.
        assert "directive" in desc or "commitment" in desc
        # Phrase-routing hint that triggers natural-language matching.
        assert "not a claim" in desc or "claim" in desc

    def test_set_scope_description_covers_filing_phrases(self):
        from totalreclaw.hermes.schemas import SET_SCOPE

        desc = SET_SCOPE["description"].lower()
        # "Put that under health"
        assert "health" in desc
        # "File this under work"
        assert "work" in desc
        # "File that under" or "put that under" — the canonical NL hooks.
        assert "under" in desc
