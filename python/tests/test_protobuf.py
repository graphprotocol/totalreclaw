"""Tests for the minimal protobuf wire format encoder."""

from __future__ import annotations

import struct

import pytest

from totalreclaw.protobuf import (
    FactPayload,
    _write_bytes,
    _write_double,
    _write_string,
    _write_varint_field,
    encode_fact_protobuf,
    encode_tombstone_protobuf,
    encode_varint,
)


# ---------------------------------------------------------------------------
# Varint encoding
# ---------------------------------------------------------------------------


class TestEncodeVarint:
    """Verify varint encoding matches protobuf base-128 spec."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            (0, b"\x00"),
            (1, b"\x01"),
            (127, b"\x7f"),
            (128, b"\x80\x01"),
            (300, b"\xac\x02"),
            (16384, b"\x80\x80\x01"),
        ],
    )
    def test_known_values(self, value: int, expected: bytes) -> None:
        assert encode_varint(value) == expected

    def test_single_byte_boundary(self) -> None:
        # 0x7F = 127 should be single byte
        result = encode_varint(0x7F)
        assert len(result) == 1
        assert result[0] == 0x7F

    def test_two_byte_boundary(self) -> None:
        # 0x80 = 128 should be two bytes
        result = encode_varint(0x80)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Field tag encoding
# ---------------------------------------------------------------------------


class TestFieldTags:
    """Verify field tag construction: (field_number << 3) | wire_type."""

    def test_string_tag(self) -> None:
        """String field uses wire type 2 (length-delimited)."""
        parts: list[bytes] = []
        _write_string(parts, 1, "hello")
        # First byte should be field tag: (1 << 3) | 2 = 0x0a
        assert parts[0] == encode_varint(0x0A)

    def test_double_tag(self) -> None:
        """Double field uses wire type 1 (64-bit fixed)."""
        parts: list[bytes] = []
        _write_double(parts, 6, 1.0)
        # Field tag: (6 << 3) | 1 = 49 = 0x31
        assert parts[0] == encode_varint(0x31)

    def test_varint_tag(self) -> None:
        """Varint field uses wire type 0."""
        parts: list[bytes] = []
        _write_varint_field(parts, 7, 1)
        # Field tag: (7 << 3) | 0 = 56 = 0x38
        assert parts[0] == encode_varint(0x38)

    def test_bytes_tag(self) -> None:
        """Bytes field uses wire type 2 (length-delimited)."""
        parts: list[bytes] = []
        _write_bytes(parts, 4, b"\xde\xad")
        # Field tag: (4 << 3) | 2 = 34 = 0x22
        assert parts[0] == encode_varint(0x22)

    def test_high_field_number(self) -> None:
        """Field number 13 should produce a two-byte varint tag."""
        parts: list[bytes] = []
        _write_string(parts, 13, "test")
        # Field tag: (13 << 3) | 2 = 106 = 0x6a (single byte, < 128)
        assert parts[0] == encode_varint(0x6A)

    def test_empty_string_skipped(self) -> None:
        """Empty strings should not produce any output."""
        parts: list[bytes] = []
        _write_string(parts, 1, "")
        assert len(parts) == 0


# ---------------------------------------------------------------------------
# Basic fact encoding
# ---------------------------------------------------------------------------


def _make_sample_fact(**overrides: object) -> FactPayload:
    """Create a sample FactPayload with sensible defaults."""
    defaults = dict(
        id="01234567-89ab-cdef-0123-456789abcdef",
        timestamp="2026-03-29T12:00:00.000Z",
        owner="0x2c0CF74B2b76110708CA431796367779e3738250",
        encrypted_blob="deadbeef",
        blind_indices=["abc123", "def456"],
        decay_score=1.0,
        source="python_test",
        content_fp="fp_abc",
        agent_id="test-agent",
    )
    defaults.update(overrides)
    return FactPayload(**defaults)  # type: ignore[arg-type]


class TestEncodeFactProtobuf:
    """Verify full fact encoding produces valid protobuf bytes."""

    def test_produces_bytes(self) -> None:
        fact = _make_sample_fact()
        result = encode_fact_protobuf(fact)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_deterministic(self) -> None:
        """Same input should produce identical output."""
        fact = _make_sample_fact()
        assert encode_fact_protobuf(fact) == encode_fact_protobuf(fact)

    def test_contains_id(self) -> None:
        """The encoded bytes should contain the fact ID as UTF-8."""
        fact = _make_sample_fact()
        result = encode_fact_protobuf(fact)
        assert fact.id.encode("utf-8") in result

    def test_contains_owner(self) -> None:
        fact = _make_sample_fact()
        result = encode_fact_protobuf(fact)
        assert fact.owner.encode("utf-8") in result

    def test_contains_encrypted_blob_bytes(self) -> None:
        """encrypted_blob hex should be decoded to raw bytes in the output."""
        fact = _make_sample_fact(encrypted_blob="cafebabe")
        result = encode_fact_protobuf(fact)
        assert b"\xca\xfe\xba\xbe" in result

    def test_source_not_in_wire_format(self) -> None:
        """In v3, source is encrypted inside field 4, not written as field 9."""
        fact = _make_sample_fact()
        result = encode_fact_protobuf(fact)
        assert b"python_test" not in result

    def test_decay_score_encoding(self) -> None:
        """decay_score=1.0 should appear as a little-endian double."""
        fact = _make_sample_fact(decay_score=1.0)
        result = encode_fact_protobuf(fact)
        packed = struct.pack("<d", 1.0)
        assert packed in result

    def test_is_active_always_true(self) -> None:
        """is_active is hardcoded to 1 (true)."""
        fact = _make_sample_fact()
        result = encode_fact_protobuf(fact)
        # Field 7, wire type 0: tag = (7 << 3) | 0 = 56 = 0x38, value = 1
        tag = encode_varint(0x38)
        val = encode_varint(1)
        assert tag + val in result

    def test_version_always_three(self) -> None:
        """version is hardcoded to 3 (XChaCha20 + encrypted metadata envelope)."""
        fact = _make_sample_fact()
        result = encode_fact_protobuf(fact)
        # Field 8, wire type 0: tag = (8 << 3) | 0 = 64 = 0x40, value = 3
        tag = encode_varint(0x40)
        val = encode_varint(3)
        assert tag + val in result


# ---------------------------------------------------------------------------
# Repeated fields (blind_indices)
# ---------------------------------------------------------------------------


class TestRepeatedFields:
    """Verify repeated string fields produce one tag per element."""

    @staticmethod
    def _count_field(data: bytes, target_field: int) -> int:
        """Count occurrences of a field number by parsing the wire format."""
        count = 0
        offset = 0
        while offset < len(data):
            # Decode tag varint
            result = 0
            shift = 0
            while True:
                byte = data[offset]
                result |= (byte & 0x7F) << shift
                offset += 1
                if not (byte & 0x80):
                    break
                shift += 7
            field_number = result >> 3
            wire_type = result & 0x07
            if field_number == target_field:
                count += 1
            # Skip value
            if wire_type == 0:  # varint
                while data[offset] & 0x80:
                    offset += 1
                offset += 1
            elif wire_type == 1:  # 64-bit
                offset += 8
            elif wire_type == 2:  # length-delimited
                length = 0
                shift = 0
                while True:
                    byte = data[offset]
                    length |= (byte & 0x7F) << shift
                    offset += 1
                    if not (byte & 0x80):
                        break
                    shift += 7
                offset += length
            else:
                raise ValueError(f"Unexpected wire type {wire_type}")
        return count

    def test_multiple_blind_indices(self) -> None:
        fact = _make_sample_fact(blind_indices=["aaa", "bbb", "ccc"])
        result = encode_fact_protobuf(fact)
        count = self._count_field(result, 5)
        assert count == 3, f"Expected 3 occurrences of field 5, got {count}"

    def test_empty_blind_indices(self) -> None:
        """No blind_indices should produce no field 5 entries."""
        fact = _make_sample_fact(blind_indices=[])
        result = encode_fact_protobuf(fact)
        count = self._count_field(result, 5)
        assert count == 0, f"Expected 0 occurrences of field 5, got {count}"

    def test_single_blind_index(self) -> None:
        fact = _make_sample_fact(blind_indices=["only_one"])
        result = encode_fact_protobuf(fact)
        assert b"only_one" in result
        count = self._count_field(result, 5)
        assert count == 1

    def test_blind_index_content(self) -> None:
        """Each blind index string should appear in the encoded output."""
        indices = ["hash_a", "hash_b"]
        fact = _make_sample_fact(blind_indices=indices)
        result = encode_fact_protobuf(fact)
        for idx in indices:
            assert idx.encode("utf-8") in result


# ---------------------------------------------------------------------------
# Optional encrypted_embedding field
# ---------------------------------------------------------------------------


class TestOptionalEncryptedEmbedding:
    """Verify field 13 is only written when present."""

    def test_absent(self) -> None:
        fact = _make_sample_fact(encrypted_embedding=None)
        result = encode_fact_protobuf(fact)
        # Field 13 tag: (13 << 3) | 2 = 106 = 0x6a
        tag = encode_varint(0x6A)
        assert tag not in result

    def test_present(self) -> None:
        fact = _make_sample_fact(encrypted_embedding="encrypted_vec_hex")
        result = encode_fact_protobuf(fact)
        assert b"encrypted_vec_hex" in result
        tag = encode_varint(0x6A)
        assert tag in result

    def test_empty_string_omitted(self) -> None:
        """An empty string should not write the field (protobuf convention)."""
        fact = _make_sample_fact(encrypted_embedding="")
        result = encode_fact_protobuf(fact)
        tag = encode_varint(0x6A)
        assert tag not in result


# ---------------------------------------------------------------------------
# Tombstone encoding
# ---------------------------------------------------------------------------


class TestEncodeTombstoneProtobuf:
    """Verify tombstone payloads for soft-delete."""

    def test_produces_bytes(self) -> None:
        result = encode_tombstone_protobuf("fact-123", "0xOwner")
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_contains_fact_id(self) -> None:
        result = encode_tombstone_protobuf("fact-123", "0xOwner")
        assert b"fact-123" in result

    def test_contains_owner(self) -> None:
        result = encode_tombstone_protobuf("fact-123", "0xOwner")
        assert b"0xOwner" in result

    def test_encrypted_blob_is_tombstone(self) -> None:
        """encrypted_blob should be the raw bytes of 'tombstone'."""
        result = encode_tombstone_protobuf("fact-123", "0xOwner")
        assert b"tombstone" in result

    def test_decay_score_zero(self) -> None:
        """Tombstones should have decay_score=0.0."""
        result = encode_tombstone_protobuf("fact-123", "0xOwner")
        packed_zero = struct.pack("<d", 0.0)
        assert packed_zero in result

    def test_source_not_in_wire_format(self) -> None:
        """In v3, source is encrypted inside field 4, not written as field 9."""
        result = encode_tombstone_protobuf("fact-123", "0xOwner")
        assert b"python_forget" not in result

    def test_agent_id_not_in_wire_format(self) -> None:
        """In v3, agent_id is encrypted inside field 4, not written as field 11."""
        result = encode_tombstone_protobuf("fact-123", "0xOwner")
        assert b"python-client" not in result


# ---------------------------------------------------------------------------
# Round-trip structural verification
# ---------------------------------------------------------------------------


class TestRoundTripStructure:
    """Manually parse the encoded bytes to verify wire format structure.

    We walk through the encoded buffer parsing field tags, wire types, and
    values to confirm the encoder produces valid protobuf wire format.
    """

    @staticmethod
    def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
        """Decode a varint starting at offset. Returns (value, new_offset)."""
        result = 0
        shift = 0
        while True:
            byte = data[offset]
            result |= (byte & 0x7F) << shift
            offset += 1
            if not (byte & 0x80):
                break
            shift += 7
        return result, offset

    def _parse_fields(self, data: bytes) -> list[tuple[int, int, object]]:
        """Parse all fields from protobuf bytes.

        Returns list of (field_number, wire_type, value) tuples.
        """
        fields = []
        offset = 0
        while offset < len(data):
            tag, offset = self._decode_varint(data, offset)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 0:  # varint
                value, offset = self._decode_varint(data, offset)
                fields.append((field_number, wire_type, value))
            elif wire_type == 1:  # 64-bit
                value = struct.unpack("<d", data[offset : offset + 8])[0]
                offset += 8
                fields.append((field_number, wire_type, value))
            elif wire_type == 2:  # length-delimited
                length, offset = self._decode_varint(data, offset)
                value = data[offset : offset + length]
                offset += length
                fields.append((field_number, wire_type, value))
            else:
                raise ValueError(f"Unexpected wire type {wire_type} at offset {offset}")

        return fields

    def test_all_fields_present(self) -> None:
        """A fully populated fact should have all expected field numbers.

        In v3, fields 9 (source) and 11 (agent_id) are no longer written --
        they are encrypted inside field 4.
        """
        fact = _make_sample_fact(encrypted_embedding="enc_emb")
        data = encode_fact_protobuf(fact)
        fields = self._parse_fields(data)

        field_numbers = {f[0] for f in fields}
        # Fields 1-8, 10, 13 expected. Fields 9, 11 removed in v3.
        # Field 12 (sequence_id) is server-assigned.
        expected = {1, 2, 3, 4, 5, 6, 7, 8, 10, 13}
        assert expected.issubset(field_numbers), (
            f"Missing fields: {expected - field_numbers}"
        )
        # Fields 9 and 11 must NOT be present
        assert 9 not in field_numbers, "Field 9 (source) should not be in v3 wire format"
        assert 11 not in field_numbers, "Field 11 (agent_id) should not be in v3 wire format"

    def test_field_wire_types(self) -> None:
        """Verify each field uses the correct wire type."""
        fact = _make_sample_fact(encrypted_embedding="enc_emb")
        data = encode_fact_protobuf(fact)
        fields = self._parse_fields(data)

        expected_wire_types = {
            1: 2,   # id: string (length-delimited)
            2: 2,   # timestamp: string
            3: 2,   # owner: string
            4: 2,   # encrypted_blob: bytes
            5: 2,   # blind_indices: repeated string
            6: 1,   # decay_score: double (64-bit)
            7: 0,   # is_active: bool (varint)
            8: 0,   # version: int32 (varint)
            # 9 (source) and 11 (agent_id) removed in v3
            10: 2,  # content_fp: string
            13: 2,  # encrypted_embedding: string
        }

        for field_number, wire_type, _ in fields:
            if field_number in expected_wire_types:
                assert wire_type == expected_wire_types[field_number], (
                    f"Field {field_number}: expected wire type "
                    f"{expected_wire_types[field_number]}, got {wire_type}"
                )

    def test_field_values(self) -> None:
        """Verify decoded field values match the input."""
        fact = _make_sample_fact(
            id="test-id",
            timestamp="2026-01-01T00:00:00.000Z",
            owner="0xOwner",
            encrypted_blob="aabb",
            blind_indices=["idx1", "idx2"],
            decay_score=0.75,
            source="test_src",
            content_fp="fp123",
            agent_id="agent-1",
            encrypted_embedding="emb_data",
        )
        data = encode_fact_protobuf(fact)
        fields = self._parse_fields(data)

        # Build a dict; for repeated fields, collect into a list
        field_map: dict[int, list[object]] = {}
        for fn, _wt, val in fields:
            field_map.setdefault(fn, []).append(val)

        # String fields
        assert field_map[1] == [b"test-id"]
        assert field_map[2] == [b"2026-01-01T00:00:00.000Z"]
        assert field_map[3] == [b"0xOwner"]
        assert field_map[4] == [b"\xaa\xbb"]
        assert field_map[5] == [b"idx1", b"idx2"]
        assert field_map[6] == [0.75]
        assert field_map[7] == [1]   # is_active = true
        assert field_map[8] == [3]   # version = 3
        # Fields 9 (source) and 11 (agent_id) removed in v3
        assert 9 not in field_map, "Field 9 (source) should not be in v3 wire format"
        assert 11 not in field_map, "Field 11 (agent_id) should not be in v3 wire format"
        assert field_map[10] == [b"fp123"]
        assert field_map[13] == [b"emb_data"]

    def test_no_field_12(self) -> None:
        """Field 12 (sequence_id) should never be set by the client."""
        fact = _make_sample_fact(encrypted_embedding="x")
        data = encode_fact_protobuf(fact)
        fields = self._parse_fields(data)
        field_numbers = {f[0] for f in fields}
        assert 12 not in field_numbers
