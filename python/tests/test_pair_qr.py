"""Tests for the rc.5 QR encoder helpers in ``totalreclaw.pair.qr``.

Covers:
    * PNG encoding emits a valid PNG (magic-header bytes) and round-trips
      through a decoder (``pyzbar`` when installed; soft-skip otherwise).
    * Unicode encoding emits a non-empty block-character string that
      matches a stable golden snapshot.
    * Oversized payloads raise ``QREncodeError``.
    * Non-string / empty payloads raise ``QREncodeError``.

Run with: ``pytest python/tests/test_pair_qr.py``
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from totalreclaw.pair.qr import QREncodeError, encode_png, encode_unicode

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# A stable pair URL used for both PNG + Unicode tests. The exact shape
# matches what ``totalreclaw.hermes.pair_tool.pair`` assembles.
SAMPLE_URL = (
    "http://127.0.0.1:47321/pair/"
    "abc123def456abc123def456abc123de"
    "#pk=Nq7v3pQ8kL_wY1rZ-aXmPqT9yCvB6jH2kLgFeRzK"
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "qr_snapshots"


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

def test_encode_png_emits_valid_png_header():
    png = encode_png(SAMPLE_URL)
    assert png.startswith(PNG_MAGIC), f"expected PNG magic, got {png[:8]!r}"
    # Sanity: a 25x25-module QR at box_size=10 border=4 is roughly
    # 330x330 pixels — well above 300 bytes and well below 10 KiB.
    assert 500 < len(png) < 10_000, f"PNG size {len(png)} out of range"


def test_encode_png_round_trips_via_pyzbar():
    """Decode the PNG back to the URL. Soft-skips if pyzbar/libzbar are
    not installed (the dev extra declares both)."""
    try:
        from pyzbar.pyzbar import decode as zbar_decode
        from PIL import Image
    except (ImportError, OSError) as err:  # pragma: no cover — env-dependent
        pytest.skip(f"pyzbar/Pillow not available: {err}")

    png = encode_png(SAMPLE_URL)
    img = Image.open(io.BytesIO(png))
    decoded = zbar_decode(img)
    assert decoded, "pyzbar returned no decode results"
    payload = decoded[0].data.decode("utf-8")
    assert payload == SAMPLE_URL, f"round-trip mismatch: {payload!r}"


def test_encode_png_respects_box_size_and_border():
    small = encode_png(SAMPLE_URL, box_size=4, border=2)
    big = encode_png(SAMPLE_URL, box_size=12, border=4)
    assert len(big) > len(small), "bigger box_size should yield larger PNG"


# ---------------------------------------------------------------------------
# Unicode
# ---------------------------------------------------------------------------

def test_encode_unicode_uses_block_chars():
    s = encode_unicode(SAMPLE_URL)
    assert s, "unicode QR must not be empty"
    assert any(ch in s for ch in "█▀▄"), (
        "expected at least one block-glyph (qrcode.print_ascii output). "
        f"First 120 chars: {s[:120]!r}"
    )
    # Should be multi-line (23 lines for a ~25-module QR with border=2).
    assert s.count("\n") >= 10, f"expected >=10 newlines, got {s.count(chr(10))}"


def test_encode_unicode_matches_golden_snapshot():
    """Regression guard — if the qrcode library changes its ASCII
    rendering, we want to see it diff instead of silently drifting."""
    golden_path = FIXTURE_DIR / "sample_pair_url.txt"
    actual = encode_unicode(SAMPLE_URL)

    if not golden_path.exists():
        # First-run materialisation. Writing the fixture here keeps the
        # test authoritative without asking contributors to run an
        # out-of-band snapshot script.
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")

    expected = golden_path.read_text(encoding="utf-8")
    assert actual == expected, (
        "QR unicode snapshot drifted. If this is intentional, delete "
        f"{golden_path} and re-run."
    )


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def test_encode_png_rejects_oversized_url():
    # 2049-char URL deliberately exceeds the 2 KiB cap.
    oversized = "http://x/" + ("a" * 2050)
    with pytest.raises(QREncodeError, match="too large"):
        encode_png(oversized)


def test_encode_unicode_rejects_oversized_url():
    oversized = "http://x/" + ("a" * 2050)
    with pytest.raises(QREncodeError, match="too large"):
        encode_unicode(oversized)


def test_encode_png_rejects_empty():
    with pytest.raises(QREncodeError, match="empty"):
        encode_png("")


def test_encode_unicode_rejects_empty():
    with pytest.raises(QREncodeError, match="empty"):
        encode_unicode("")


def test_encode_png_rejects_non_string():
    with pytest.raises(QREncodeError, match="must be a string"):
        encode_png(None)  # type: ignore[arg-type]


def test_encode_unicode_rejects_non_string():
    with pytest.raises(QREncodeError, match="must be a string"):
        encode_unicode(12345)  # type: ignore[arg-type]
