"""Tests for ``totalreclaw.pair.crypto`` (2.3.1rc4 phrase-safety port).

Parity with the TypeScript gateway: a ciphertext produced by the Python
side with the same (sk, pk, sid) as a Node-side test vector must decrypt
back to the original plaintext. End-to-end round trips are tested here;
cross-language vectors live in the TS test file (``pair-crypto.test.ts``)
and match the constants we emit.
"""
from __future__ import annotations

import base64

import pytest

from totalreclaw.pair.crypto import (
    AEAD_KEY_BYTES,
    AEAD_NONCE_BYTES,
    AEAD_TAG_BYTES,
    HKDF_INFO,
    X25519_KEY_BYTES,
    _b64url_decode,
    _b64url_encode,
    _hkdf_sha256,
    aead_decrypt,
    aead_encrypt_with_session_key,
    compare_secondary_codes_ct,
    compute_shared_secret,
    decrypt_pairing_payload,
    derive_aead_key_from_ecdh,
    derive_public_from_private,
    derive_session_keys,
    encrypt_pairing_payload,
    generate_gateway_keypair,
)


class TestConstants:
    def test_parity_with_ts_module(self):
        """These values MUST equal the TS module's exports. Any drift
        breaks cross-stack ciphertext interop."""
        assert HKDF_INFO == "totalreclaw-pair-v1"
        assert AEAD_KEY_BYTES == 32
        assert AEAD_NONCE_BYTES == 12
        assert AEAD_TAG_BYTES == 16
        assert X25519_KEY_BYTES == 32


class TestBase64url:
    def test_encode_no_padding(self):
        """Python's urlsafe_b64 emits ``=`` padding; we strip it for
        Node ``Buffer.toString('base64url')`` parity."""
        raw = b"\x01" * 32
        encoded = _b64url_encode(raw)
        assert "=" not in encoded
        assert len(encoded) == 43  # 32 bytes → 43 chars unpadded

    def test_decode_accepts_optional_padding(self):
        raw = b"\x02" * 16
        encoded_unpadded = _b64url_encode(raw)
        encoded_padded = encoded_unpadded + "=" * ((-len(encoded_unpadded)) % 4)
        assert _b64url_decode(encoded_unpadded) == raw
        assert _b64url_decode(encoded_padded) == raw

    def test_roundtrip(self):
        raw = bytes(range(256))
        assert _b64url_decode(_b64url_encode(raw)) == raw


class TestKeypairGeneration:
    def test_generate_keypair_has_correct_sizes(self):
        kp = generate_gateway_keypair()
        assert len(_b64url_decode(kp.sk_b64)) == X25519_KEY_BYTES
        assert len(_b64url_decode(kp.pk_b64)) == X25519_KEY_BYTES

    def test_keypairs_are_unique(self):
        kp_a = generate_gateway_keypair()
        kp_b = generate_gateway_keypair()
        assert kp_a.sk_b64 != kp_b.sk_b64
        assert kp_a.pk_b64 != kp_b.pk_b64

    def test_derive_public_matches_keypair(self):
        kp = generate_gateway_keypair()
        derived = derive_public_from_private(kp.sk_b64)
        assert derived == kp.pk_b64


class TestECDH:
    def test_shared_secret_is_symmetric(self):
        """Swapped (local, remote) pairs MUST produce the same secret."""
        a = generate_gateway_keypair()
        b = generate_gateway_keypair()
        s1 = compute_shared_secret(a.sk_b64, b.pk_b64)
        s2 = compute_shared_secret(b.sk_b64, a.pk_b64)
        assert s1 == s2
        assert len(s1) == X25519_KEY_BYTES

    def test_shared_secret_differs_with_different_peers(self):
        a = generate_gateway_keypair()
        b = generate_gateway_keypair()
        c = generate_gateway_keypair()
        assert compute_shared_secret(a.sk_b64, b.pk_b64) != compute_shared_secret(a.sk_b64, c.pk_b64)


class TestHKDF:
    def test_rfc5869_vector(self):
        """Spot-check against the RFC 5869 Test Case 1.

        IKM  = 0x0b*22
        salt = 0x00..0x0c
        info = 0xf0..0xf9
        L    = 42 → PRK, OKM from RFC.
        """
        ikm = b"\x0b" * 22
        salt = bytes(range(0x0D))
        info = bytes(range(0xF0, 0xFA))
        okm = _hkdf_sha256(salt=salt, ikm=ikm, info=info, length=42)
        expected = bytes.fromhex(
            "3cb25f25faacd57a90434f64d0362f2a"
            "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865"
        )
        assert okm == expected


class TestSessionKeys:
    def test_session_keys_length(self):
        shared = b"\x33" * X25519_KEY_BYTES
        keys = derive_session_keys(shared, "deadbeef" * 4)
        assert len(keys.k_enc) == AEAD_KEY_BYTES

    def test_sid_binding_changes_key(self):
        """Different sid → different key (enforces HKDF salt binding)."""
        shared = b"\x33" * X25519_KEY_BYTES
        a = derive_session_keys(shared, "session-a").k_enc
        b = derive_session_keys(shared, "session-b").k_enc
        assert a != b

    def test_reject_wrong_shared_length(self):
        with pytest.raises(ValueError):
            derive_session_keys(b"\x00" * 16, "sid")

    def test_reject_empty_sid(self):
        with pytest.raises(ValueError):
            derive_session_keys(b"\x00" * 32, "")


class TestRoundTrip:
    def test_encrypt_decrypt_known_plaintext(self):
        """Gateway encrypts → gateway decrypts back. Sanity round-trip."""
        gw = generate_gateway_keypair()
        dev = generate_gateway_keypair()
        sid = "abcdef" * 5  # 30 chars
        plaintext = b"abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"

        # Device encrypts for gateway.
        nonce_b64, ct_b64 = encrypt_pairing_payload(
            sk_local_b64=dev.sk_b64,
            pk_remote_b64=gw.pk_b64,
            sid=sid,
            plaintext=plaintext,
        )

        # Gateway decrypts with its sk and the device's pk.
        recovered = decrypt_pairing_payload(
            sk_gateway_b64=gw.sk_b64,
            pk_device_b64=dev.pk_b64,
            sid=sid,
            nonce_b64=nonce_b64,
            ciphertext_b64=ct_b64,
        )
        assert recovered == plaintext

    def test_tamper_in_ciphertext_fails(self):
        """Flipped bit in ct MUST make AEAD tag check fail."""
        from cryptography.exceptions import InvalidTag

        gw = generate_gateway_keypair()
        dev = generate_gateway_keypair()
        sid = "deadbeef" * 4

        nonce_b64, ct_b64 = encrypt_pairing_payload(
            sk_local_b64=dev.sk_b64,
            pk_remote_b64=gw.pk_b64,
            sid=sid,
            plaintext=b"some plaintext",
        )

        # Flip a byte in the middle of the ciphertext.
        raw = bytearray(_b64url_decode(ct_b64))
        raw[5] ^= 0x01
        tampered = _b64url_encode(bytes(raw))

        with pytest.raises(InvalidTag):
            decrypt_pairing_payload(
                sk_gateway_b64=gw.sk_b64,
                pk_device_b64=dev.pk_b64,
                sid=sid,
                nonce_b64=nonce_b64,
                ciphertext_b64=tampered,
            )

    def test_sid_mismatch_fails(self):
        """AEAD additional-data binding: decrypt under wrong sid rejects."""
        from cryptography.exceptions import InvalidTag

        gw = generate_gateway_keypair()
        dev = generate_gateway_keypair()

        nonce_b64, ct_b64 = encrypt_pairing_payload(
            sk_local_b64=dev.sk_b64,
            pk_remote_b64=gw.pk_b64,
            sid="session-A" * 4,
            plaintext=b"secret",
        )

        with pytest.raises(InvalidTag):
            decrypt_pairing_payload(
                sk_gateway_b64=gw.sk_b64,
                pk_device_b64=dev.pk_b64,
                sid="session-B" * 4,
                nonce_b64=nonce_b64,
                ciphertext_b64=ct_b64,
            )

    def test_wrong_gateway_key_fails(self):
        """Decrypt under a different gateway private key must fail."""
        from cryptography.exceptions import InvalidTag

        gw = generate_gateway_keypair()
        other = generate_gateway_keypair()
        dev = generate_gateway_keypair()
        sid = "s" * 16

        nonce_b64, ct_b64 = encrypt_pairing_payload(
            sk_local_b64=dev.sk_b64,
            pk_remote_b64=gw.pk_b64,
            sid=sid,
            plaintext=b"payload",
        )

        with pytest.raises(InvalidTag):
            decrypt_pairing_payload(
                sk_gateway_b64=other.sk_b64,
                pk_device_b64=dev.pk_b64,
                sid=sid,
                nonce_b64=nonce_b64,
                ciphertext_b64=ct_b64,
            )


class TestPinCompare:
    def test_matching_pins_return_true(self):
        assert compare_secondary_codes_ct("123456", "123456") is True

    def test_mismatched_pins_return_false(self):
        assert compare_secondary_codes_ct("123456", "123457") is False

    def test_different_lengths_return_false(self):
        assert compare_secondary_codes_ct("123456", "12345") is False

    def test_non_str_returns_false(self):
        assert compare_secondary_codes_ct("123456", None) is False  # type: ignore[arg-type]
        assert compare_secondary_codes_ct(None, "123456") is False  # type: ignore[arg-type]
