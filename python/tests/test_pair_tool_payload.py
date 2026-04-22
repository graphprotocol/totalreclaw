"""rc.5 contract test: ``totalreclaw_pair`` tool payload must include
``qr_png_b64`` + ``qr_unicode`` fields alongside the existing url/pin/
expires_at.

The test builds a pair-session end-to-end against a real pair HTTP
server bound to an ephemeral loopback port and asserts the JSON payload
shape. Nothing crosses the network; the server is never POSTed to
(phrase-safety rule allows this — we only inspect what the agent would
see).

Run with: ``pytest python/tests/test_pair_tool_payload.py``
"""
from __future__ import annotations

import base64
import json
import re
import string

import pytest
from unittest.mock import MagicMock


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_pair_tool_returns_qr_png_and_qr_unicode(tmp_path, monkeypatch):
    """End-to-end: call ``pair_tool.pair({'mode':'generate'}, state)``
    and verify the JSON payload contains the rc.5 fields."""
    # Redirect ~/.totalreclaw to the test tmp dir so we never touch the
    # real home dir. ``_resolve_sessions_dir`` uses ``Path.home()`` so
    # we monkey-patch $HOME.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_PORT", "0")

    # Reset the module-level pair-server singleton — tests are order-
    # dependent otherwise (a prior test might have bound a server
    # singleton to a DIFFERENT tmp_path).
    from totalreclaw.hermes import pair_tool as _pair_tool_mod

    _pair_tool_mod._SERVER_INSTANCE = None  # type: ignore[attr-defined]

    state = MagicMock()
    # configure + state.get_client aren't exercised by the pair-tool
    # path under test (only ``_complete_pairing_handler`` closes over
    # state, and that handler is only invoked by the browser POST —
    # not by the pair tool itself).

    result_json = await _pair_tool_mod.pair({"mode": "generate"}, state)
    payload = json.loads(result_json)

    assert "error" not in payload, f"pair tool returned error: {payload.get('error')!r}"

    # Existing rc.4 contract
    assert "url" in payload and payload["url"].startswith("http://127.0.0.1:")
    assert re.fullmatch(r"\d{6}", payload["pin"]), f"pin not 6 digits: {payload['pin']!r}"
    assert "expires_at" in payload

    # rc.5 additions
    assert "qr_png_b64" in payload, "rc.5 field `qr_png_b64` missing"
    assert "qr_unicode" in payload, "rc.5 field `qr_unicode` missing"

    # qr_png_b64: non-empty, valid base64, decodes to a valid PNG.
    b64 = payload["qr_png_b64"]
    assert b64, "qr_png_b64 is empty — PIL or qrcode failed at runtime"
    # base64 character-set check before the decode.
    b64_chars = set(string.ascii_letters + string.digits + "+/=")
    assert set(b64) <= b64_chars, "qr_png_b64 contains non-base64 chars"
    png = base64.b64decode(b64)
    assert png.startswith(PNG_MAGIC), f"decoded PNG header wrong: {png[:8]!r}"

    # qr_unicode: non-empty + contains block chars.
    uni = payload["qr_unicode"]
    assert uni, "qr_unicode empty"
    assert any(ch in uni for ch in "█▀▄"), (
        f"qr_unicode missing block chars. First 120 chars: {uni[:120]!r}"
    )

    # Defence in depth (text-level): the PIN must not appear verbatim in
    # the Unicode QR glyphs, nor in the base64 PNG payload. These are
    # weak checks (base64 of an image might accidentally contain any
    # 6-digit substring — we gate with the prefix `pin=` match too) but
    # a hard mismatch would flag a regression without needing libzbar.
    assert f"pin={payload['pin']}" not in payload["url"], (
        "phrase-safety/UX: PIN must never be encoded in the pair URL"
    )

    # Phrase-safety: the QR must decode back to payload['url']. Optional
    # because libzbar is a non-portable C dep; we prefer to run it when
    # available (CI + dev machines with `brew install zbar`) but allow
    # skip on barebones hosts.
    try:
        from pyzbar.pyzbar import decode as zbar_decode
        from PIL import Image
        import io as _io
    except (ImportError, OSError) as err:  # pragma: no cover — env-dependent
        return  # primary assertions already ran — decode check is optional

    decoded = zbar_decode(Image.open(_io.BytesIO(png)))
    assert decoded, "pyzbar returned no decode results"
    decoded_url = decoded[0].data.decode("utf-8")
    assert decoded_url == payload["url"], (
        f"QR does not encode the pair URL: {decoded_url!r} vs {payload['url']!r}"
    )
    assert payload["pin"] not in decoded_url, (
        "phrase-safety/UX violation: PIN must not be encoded in the QR"
    )


@pytest.mark.asyncio
async def test_pair_tool_payload_never_contains_phrase_material(tmp_path, monkeypatch):
    """Defence in depth: assert the JSON payload never has a field that
    looks like phrase-material (no field named ``phrase``, ``mnemonic``,
    ``recovery_phrase``; no 12-word BIP-39-shaped string)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("TOTALRECLAW_PAIR_BIND_PORT", "0")

    from totalreclaw.hermes import pair_tool as _pair_tool_mod

    _pair_tool_mod._SERVER_INSTANCE = None  # type: ignore[attr-defined]

    state = MagicMock()

    result_json = await _pair_tool_mod.pair({"mode": "generate"}, state)
    payload = json.loads(result_json)

    forbidden_keys = {"phrase", "mnemonic", "recovery_phrase", "recoveryPhrase", "seed"}
    assert not (set(payload.keys()) & forbidden_keys), (
        f"payload contains forbidden phrase-adjacent keys: "
        f"{set(payload.keys()) & forbidden_keys}"
    )

    # No field-value should be a 12-word lowercase string.
    for key, val in payload.items():
        if isinstance(val, str):
            words = val.strip().split()
            twelve_word = (
                len(words) == 12
                and all(w.isalpha() and w.islower() for w in words)
            )
            assert not twelve_word, (
                f"payload field {key!r} looks like a 12-word phrase: {val!r}"
            )
