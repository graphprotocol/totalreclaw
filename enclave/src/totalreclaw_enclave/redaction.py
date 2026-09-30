"""One definition of "plaintext-shaped", shared by the audit writer (which
refuses) and the log filter (which redacts).

A field is safe when BOTH hold:

1. Its key fully matches ``[a-z][a-z0-9_]{0,39}`` and none of its ``_``-separated
   segments is in ``DENIED_KEY_SEGMENTS`` (names that describe user content,
   credentials or account identity).
2. Its value is ``None``, a ``bool``, an ``int`` within SQLite's 64-bit range,
   a finite ``float``, or a ``str`` that is "slug-shaped": at most 64
   characters from ``[A-Za-z0-9_.:/@+=-]`` (no whitespace, quotes, braces or
   newlines, so prose, JSON and phrases cannot pass) and not secret-shaped:
   no run of 24+ ASCII alphanumerics (hex keys, addresses, hashes, base64
   tokens), no 20+ character token run mixing upper case, lower case and
   digits, no JWT (``eyJ``) prefix.

Consequence for callers: identifiers that must appear in logs or audit rows
are either short (<= 16 chars, e.g. ``ingest_id[:12]``) or contain
separators. ``scrub_text`` is the free-text backstop for log messages; its
secret-shape patterns mirror ``redact_secrets`` in
``python/src/totalreclaw/hermes/qa_bug_report.py:61-118``.
"""

from __future__ import annotations

import math
import re
from typing import Final

REDACTED: Final = "[redacted]"

DENIED_KEY_SEGMENTS: Final = frozenset(
    {
        # user content
        "text", "query", "message", "messages", "content", "memory", "memories",
        "fact", "facts", "prompt", "turn", "turns", "plaintext", "narrative",
        "crystal", "body", "payload", "envelope", "ciphertext",
        # credentials and key material
        "phrase", "mnemonic", "seed", "secret", "password", "token", "tokens",
        "bearer", "cookie", "private", "priv", "key", "bundle", "code",
        "verifier", "mac", "signature", "sig",
        # account identity (log/audit a 16-hex vault_hash, never the address)
        "wallet", "address", "eoa",
    }
)  # fmt: skip

_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_SLUG_RE = re.compile(r"[A-Za-z0-9_.:/@+=-]{0,64}")
_LONG_ALNUM = re.compile(r"[A-Za-z0-9]{24,}")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}")
_INT64 = 2**63 - 1

# Free-text backstop (log messages only). Order: specific before generic.
_BIP39 = re.compile(r"\b[a-z]{3,10}(?:\s+[a-z]{3,10}){11,23}\b")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_QUALIFIED = re.compile(
    r"(?i)\b((?:access_token|refresh_token|token|secret|auth_key|api_key|password|code_verifier)"
    r"\s*[=:]\s*)[^\s&,;]+"
)


def _mixed_token(run: str) -> bool:
    return any(c.isupper() for c in run) and any(c.islower() for c in run) and any(c.isdigit() for c in run)


def key_violation(key: object) -> str | None:
    """Why ``key`` may not name a field, or ``None`` if it may."""
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        return "key must match [a-z][a-z0-9_]{0,39}"
    denied = sorted(set(key.split("_")) & DENIED_KEY_SEGMENTS)
    if denied:
        return f"key segment '{denied[0]}' names user content, a credential or vault identity"
    return None


def value_violation(value: object) -> str | None:
    """Why ``value`` is plaintext-shaped, or ``None`` if it is safe.

    The returned reason never contains the value itself.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return None if -_INT64 - 1 <= value <= _INT64 else "integer outside the 64-bit range"
    if isinstance(value, float):
        return None if math.isfinite(value) else "non-finite float"
    if not isinstance(value, str):
        return f"type {type(value).__name__} is not a scalar"
    if len(value) > 64:
        return "string longer than 64 characters"
    if not _SLUG_RE.fullmatch(value):
        return "string contains whitespace or characters outside [A-Za-z0-9_.:/@+=-]"
    if _LONG_ALNUM.search(value):
        return "string contains a run of 24+ alphanumerics (key, hash, address or token shape)"
    if any(_mixed_token(m.group(0)) for m in _TOKEN_RUN.finditer(value)):
        return "string contains a mixed-case token-shaped run"
    if _JWT.search(value):
        return "string contains a JWT/base64-JSON prefix"
    return None


def scrub_text(text: str) -> str:
    """Replace secret-shaped substrings of free text with ``REDACTED``."""
    out = _BIP39.sub(REDACTED, text)
    out = _BEARER.sub(lambda m: m.group(1) + REDACTED, out)
    out = _QUALIFIED.sub(lambda m: m.group(1) + REDACTED, out)
    out = _JWT.sub(REDACTED, out)
    out = _LONG_ALNUM.sub(REDACTED, out)
    out = _TOKEN_RUN.sub(lambda m: REDACTED if _mixed_token(m.group(0)) else m.group(0), out)
    return out
