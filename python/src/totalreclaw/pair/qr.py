"""QR encoders for the Hermes pair URL (2.3.1rc5).

Two helpers — both take the pair URL and render it for a display
transport. Neither function touches or accepts recovery-phrase
material; they render the URL that the browser fetches to continue the
pairing flow. The URL itself carries only the session token + gateway
public key (fragment-encoded so it never hits server logs).

Phrase-safety invariant (see
``project_phrase_safety_rule.md`` in the internal repo): the QR payload
is ONLY the pair URL. The 6-digit PIN is a separate out-of-band
secondary channel — the agent prints it in chat text; the browser prompts
for it; it is NEVER baked into the QR image or the URL.

Design notes
------------
* We use the ``qrcode`` library (pure-Python, tiny, pillow optional for
  PNG). Error-correction defaults to ECC=M (15% damage tolerance) which
  gives us comfortable headroom for the ~80-120 character pair URLs
  emitted by the Hermes pair HTTP server (sid + pubkey + hex token).
* Unicode output uses half-block characters (``█▀▄``) via
  ``QRCode.print_ascii`` — one character per two vertical pixels, making
  the QR square-ish in terminal fonts where cell height is ~2x width.
* PNG encoding requires ``Pillow``; we import locally so a barebones
  install without the ``qr`` extra fails at first-call rather than at
  module import (the Hermes plugin module imports
  :mod:`totalreclaw.pair` eagerly and we don't want PIL on the hot
  path if QR is never requested).

Size profile
------------
For a typical pair URL of ~110 characters (sid + pk fragment):

* ``encode_png`` at ``box_size=10, border=4``: ~3-4 KiB raw PNG,
  ~4-5 KiB after base64. Small enough to fit in a tool-call response
  payload without blowing up the LLM context budget.
* ``encode_unicode`` at ``border=2``: ~1.1 KiB string, ~23 lines.
"""
from __future__ import annotations

import io
from typing import Literal

# qrcode is a declared hard dependency in pyproject.toml (2.3.1rc5+);
# Pillow is only pulled in when ``encode_png`` is actually called.

ECC_LEVEL = Literal["L", "M", "Q", "H"]

# Guardrail: the QR standard tops out at version 40 (177x177 modules) =
# ~2953 alphanumeric bytes at ECC-L, less at higher ECC levels. We reject
# payloads that exceed 2KB so callers can't try to shove a phrase-length
# blob in (defense in depth — the pair URL should never approach this).
_MAX_PAYLOAD_BYTES = 2048


class QREncodeError(ValueError):
    """Raised when the payload can't be encoded as a QR code.

    Subclasses :class:`ValueError` so existing try/except ValueError
    blocks around `encode_png` / `encode_unicode` continue to work.
    """


def _ecc_constant(level: ECC_LEVEL):
    """Translate a level literal to the qrcode library's constant."""
    import qrcode

    return {
        "L": qrcode.ERROR_CORRECT_L,
        "M": qrcode.ERROR_CORRECT_M,
        "Q": qrcode.ERROR_CORRECT_Q,
        "H": qrcode.ERROR_CORRECT_H,
    }[level]


def _validate_payload(url: str) -> None:
    """Reject oversized or non-string payloads with a clear error.

    The QR lib's own error (``DataOverflowError``) names the version
    number but not the encoded-byte budget; wrapping it here gives
    callers a stable, documented surface.
    """
    if not isinstance(url, str):
        raise QREncodeError(
            f"url must be a string, got {type(url).__name__}"
        )
    encoded = url.encode("utf-8")
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise QREncodeError(
            f"url too large for QR encoding: {len(encoded)} bytes "
            f"(max {_MAX_PAYLOAD_BYTES}). This limit exists to prevent "
            "accidentally encoding phrase-length blobs; a pair URL "
            "should be ~80-150 bytes."
        )
    if not encoded:
        raise QREncodeError("url must not be empty")


def encode_png(
    url: str,
    *,
    ecc: ECC_LEVEL = "M",
    box_size: int = 10,
    border: int = 4,
) -> bytes:
    """Render ``url`` as a PNG QR code.

    :param url: Pair URL. ONLY the URL — the secondary PIN must NEVER be
        encoded here (phrase-safety invariant).
    :param ecc: Error-correction level; defaults to ``M`` (15%).
    :param box_size: Pixels per module. 10 gives a ~300x300 image for a
        ~25x25 module QR — easy to scan from a phone held 30cm away.
    :param border: Quiet-zone width in modules; the QR standard
        requires at least 4, but 2 is usually enough for modern
        scanners and saves bytes.
    :returns: Raw PNG bytes.
    :raises QREncodeError: if ``url`` is empty, non-string, or exceeds
        the 2 KiB safety cap.
    :raises ImportError: if Pillow is not installed (install with
        ``pip install 'totalreclaw[qr]'`` or ``pip install pillow``).
    """
    _validate_payload(url)

    # Local imports so `totalreclaw.pair` module-load cost stays tiny.
    import qrcode
    from qrcode.image.pil import PilImage

    try:
        qr = qrcode.QRCode(
            version=None,  # auto-select smallest fitting version
            error_correction=_ecc_constant(ecc),
            box_size=max(1, box_size),
            border=max(0, border),
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(image_factory=PilImage, fill_color="black", back_color="white")
    except Exception as err:  # covers DataOverflowError + PIL ImportError fallback
        if "Pillow" in str(err) or "PIL" in str(err):
            raise
        raise QREncodeError(f"QR encoding failed: {err}") from err

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def encode_unicode(
    url: str,
    *,
    ecc: ECC_LEVEL = "M",
    border: int = 2,
) -> str:
    """Render ``url`` as a terminal-ready Unicode QR string.

    Uses half-block glyphs (``█ ▀ ▄``) so each character represents two
    vertical pixels, making the QR roughly square in terminal fonts with
    ~2:1 line-height. The string is newline-delimited and safe to print
    verbatim to any UTF-8 terminal.

    :param url: Pair URL (see :func:`encode_png` for the phrase-safety
        caveat — same rule applies).
    :param ecc: Error-correction level; defaults to ``M``.
    :param border: Quiet-zone width in modules. 2 is tight but usually
        fine for terminal scans since the surrounding text contrasts
        against the QR boundary.
    :returns: Multi-line string.
    :raises QREncodeError: if ``url`` is empty, non-string, or exceeds
        the safety cap.
    """
    _validate_payload(url)

    import qrcode

    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=_ecc_constant(ecc),
            box_size=1,  # irrelevant for ascii/unicode rendering
            border=max(0, border),
        )
        qr.add_data(url)
        qr.make(fit=True)
    except Exception as err:
        raise QREncodeError(f"QR encoding failed: {err}") from err

    buf = io.StringIO()
    # ``invert=False`` renders dark modules as full blocks on a light
    # terminal, which matches the convention of most QR scanners.
    qr.print_ascii(out=buf, invert=False)
    return buf.getvalue()


__all__ = [
    "QREncodeError",
    "encode_png",
    "encode_unicode",
]
