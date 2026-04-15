"""
TotalReclaw — Knowledge Graph helpers for the write + read path (Phase 1).

Mirrors ``skill/plugin/claims-helper.ts`` function-for-function in Python so
that client-produced Claim blobs are byte-identical across TypeScript and
Python. All canonicalization is delegated to ``totalreclaw_core``; this module
only handles feature flags, category mapping, entity trapdoor hashing, and
helpers for reading already-decrypted blobs.

Canonical Claim schema uses compact short keys (``t, c, cf, i, sa, ea, e, ...``).
The field order the core serializer emits matches the Rust golden tests and the
plugin's reference test vectors.
"""
from __future__ import annotations

import hashlib
import json
import math
import os


def _js_round(x: float) -> int:
    """JavaScript-compatible ``Math.round``: half-to-positive-infinity.

    Python's built-in ``round`` uses banker's rounding (half-to-even), which
    diverges from JavaScript for .5 cases — notably ``0.85 * 10 == 8.5`` which
    Python rounds to 8 but JS rounds to 9. Plugin parity requires matching JS.
    """
    return math.floor(x + 0.5)

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence

import totalreclaw_core as _core


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

ClaimFormat = Literal["claim", "legacy"]
DigestMode = Literal["on", "off", "template"]


def resolve_claim_format() -> ClaimFormat:
    """Resolve ``TOTALRECLAW_CLAIM_FORMAT`` — "claim" (default) or "legacy".

    - ``claim`` (default, unset, or unknown): new canonical Claim blob.
    - ``legacy``: old ``{text, metadata}`` doc shape; entity trapdoors are
      still added to blind indices even in legacy mode.

    Read on every call so tests can toggle via env without module reload.
    """
    raw = (os.environ.get("TOTALRECLAW_CLAIM_FORMAT") or "").strip().lower()
    return "legacy" if raw == "legacy" else "claim"


def resolve_digest_mode() -> DigestMode:
    """Resolve ``TOTALRECLAW_DIGEST_MODE`` — "on" (default), "off", "template".

    - ``on`` (default, unset, or unknown): digest injection + LLM compilation
      when an LLM is configured; template fallback otherwise.
    - ``off``: legacy individual-fact recall path, no digest injection.
    - ``template``: digest injection but skip LLM entirely.
    """
    raw = (os.environ.get("TOTALRECLAW_DIGEST_MODE") or "").strip().lower()
    if raw == "off":
        return "off"
    if raw == "template":
        return "template"
    return "on"


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

TYPE_TO_CATEGORY: Dict[str, str] = {
    "fact": "fact",
    "preference": "pref",
    "decision": "dec",
    "episodic": "epi",
    "goal": "goal",
    "context": "ctx",
    "summary": "sum",
    # Phase 2.2: rule = reusable operational rule, gotcha, debugging shortcut, or
    # convention the user wants to remember for next time. Distinct from decision
    # (which has reasoning for a specific choice) and preference (personal taste).
    "rule": "rule",
}


def map_type_to_category(fact_type: str) -> str:
    """Map an ExtractedFact type string → the compact Claim category short key.

    Unknown types fall back to ``"fact"`` so a bad LLM response never crashes
    the write path.
    """
    return TYPE_TO_CATEGORY.get(fact_type, "fact")


# ---------------------------------------------------------------------------
# Canonical Claim builder
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Current time as ``YYYY-MM-DDTHH:MM:SS.sssZ`` (same shape as plugin)."""
    # Plugin uses ``new Date().toISOString()`` → millisecond precision + ``Z``.
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def build_canonical_claim(
    fact: Any,
    importance: int,
    source_agent: str,
    extracted_at: Optional[str] = None,
) -> str:
    """Construct a canonical Claim JSON string from an ExtractedFact-like object.

    The input ``fact`` can be either an ``ExtractedFact`` dataclass-like object
    (with ``text``, ``type``, optional ``confidence``, optional ``entities``) or
    a plain dict with the same keys. The output is byte-identical to what the
    plugin's TypeScript ``buildCanonicalClaim`` produces for the same logical
    claim — field order, default omission rules, everything.

    Encrypt the returned string directly; do not re-stringify it.
    """
    text = _attr(fact, "text")
    fact_type = _attr(fact, "type")
    confidence = _attr(fact, "confidence", 0.85)
    if confidence is None:
        confidence = 0.85
    entities = _attr(fact, "entities", None)

    claim: Dict[str, Any] = {
        "t": text,
        "c": map_type_to_category(fact_type),
        "cf": confidence,
        "i": importance,
        "sa": source_agent,
        "ea": extracted_at if extracted_at is not None else _now_iso(),
    }

    if entities:
        e_list: List[Dict[str, Any]] = []
        for e in entities:
            name = _attr(e, "name")
            etype = _attr(e, "type")
            role = _attr(e, "role", None)
            entry: Dict[str, Any] = {"n": name, "tp": etype}
            if role:
                entry["r"] = role
            e_list.append(entry)
        if e_list:
            claim["e"] = e_list

    # json.dumps preserves insertion order for dicts — the canonical_claim
    # core function reparses and re-emits in the core's own canonical order
    # anyway, so intermediate dict ordering is irrelevant to correctness.
    return _core.canonicalize_claim(json.dumps(claim, ensure_ascii=False, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Legacy ``{text, metadata}`` doc shape (pre-KG fallback)
# ---------------------------------------------------------------------------


def build_legacy_doc(
    fact: Any,
    importance: int,
    source: str,
    created_at: Optional[str] = None,
) -> str:
    """Build the legacy ``{text, metadata}`` document.

    Kept so the ``TOTALRECLAW_CLAIM_FORMAT=legacy`` fallback emits blobs the
    existing ``parseClaimOrLegacy`` path has always handled. Output must be
    byte-identical to the plugin's ``buildLegacyDoc``.
    """
    text = _attr(fact, "text")
    fact_type = _attr(fact, "type")
    doc: Dict[str, Any] = {
        "text": text,
        "metadata": {
            "type": fact_type,
            "importance": importance / 10,
            "source": source,
            "created_at": created_at if created_at is not None else _now_iso(),
        },
    }
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Digest helpers (Stage 3b read path)
# ---------------------------------------------------------------------------

#: Plain SHA-256("type:digest") as hex. The ``type:`` namespace prefix keeps
#: it distinct from any user word trapdoor. Uses plain SHA-256 (not HMAC) so
#: it lives in the existing ``blind_indices`` array alongside word/stem trapdoors.
DIGEST_TRAPDOOR: str = hashlib.sha256(b"type:digest").hexdigest()

#: Compact category short key for digest claims (ClaimCategory::Digest).
DIGEST_CATEGORY: str = "dig"

#: Distinctive source marker so operators can grep for Python-origin digest writes.
DIGEST_SOURCE_AGENT: str = "hermes-agent-digest"

#: Hard ceiling on claim count for LLM-assisted digest compilation.
#: Above this, the template path is forced to keep token cost bounded.
DIGEST_CLAIM_CAP: int = 200


# ---------------------------------------------------------------------------
# Decrypted blob reader — handles both new Claim and legacy shapes
# ---------------------------------------------------------------------------


def read_claim_from_blob(decrypted_json: str) -> Dict[str, Any]:
    """Read a decrypted blob as a logical ``{text, importance, category, metadata}``.

    Handles:

      * new canonical Claim format with compact short keys (``t, c, i, ...``)
      * legacy plugin ``{text, metadata: {importance: 0-1}}`` format
      * malformed JSON and other edge cases

    Output is intentionally the same shape as the plugin's ``readClaimFromBlob``
    so search / export / re-rank code can be written once and share.
    """
    try:
        obj = json.loads(decrypted_json)
    except (ValueError, TypeError):
        return {
            "text": decrypted_json,
            "importance": 5,
            "category": "fact",
            "metadata": {},
        }

    if not isinstance(obj, dict):
        return {
            "text": decrypted_json,
            "importance": 5,
            "category": "fact",
            "metadata": {},
        }

    # New canonical Claim format: short keys
    t = obj.get("t")
    c = obj.get("c")
    if isinstance(t, str) and isinstance(c, str):
        raw_i = obj.get("i")
        if isinstance(raw_i, (int, float)) and not isinstance(raw_i, bool):
            importance = max(1, min(10, _js_round(float(raw_i))))
        else:
            importance = 5
        sa = obj.get("sa")
        ea = obj.get("ea")
        return {
            "text": t,
            "importance": importance,
            "category": c,
            "metadata": {
                "type": c,
                "importance": importance / 10,
                "source": sa if isinstance(sa, str) else "auto-extraction",
                "created_at": ea if isinstance(ea, str) else "",
            },
        }

    # Legacy plugin {text, metadata: {importance: 0-1}} format
    legacy_text = obj.get("text")
    if isinstance(legacy_text, str):
        meta = obj.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
        raw_imp = meta.get("importance")
        if isinstance(raw_imp, (int, float)) and not isinstance(raw_imp, bool):
            imp_float = float(raw_imp)
        else:
            imp_float = 0.5
        importance = max(1, min(10, _js_round(imp_float * 10)))
        category = meta.get("type") if isinstance(meta.get("type"), str) else "fact"
        return {
            "text": legacy_text,
            "importance": importance,
            "category": category,
            "metadata": meta,
        }

    # Fallback: unrecognized JSON object shape.
    return {
        "text": decrypted_json,
        "importance": 5,
        "category": "fact",
        "metadata": {},
    }


def is_digest_blob(decrypted: str) -> bool:
    """Does this decrypted blob look like a digest claim?

    Returns True only for canonical Claim JSON with ``c == "dig"``.
    Returns False for legacy docs, malformed JSON, or any other shape.
    """
    try:
        obj = json.loads(decrypted)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and obj.get("c") == DIGEST_CATEGORY


def build_digest_claim(
    digest_json: str,
    compiled_at: str,
) -> str:
    """Wrap a Digest JSON as a canonical Claim with category ``dig``.

    Stores the raw Digest JSON as the claim's ``t`` field. Reader path is
    ``parse_claim_or_legacy → extract_digest_from_claim``. Digest claims
    deliberately carry no entity refs — otherwise entity trapdoors would
    surface the digest blob in normal recall queries.
    """
    claim: Dict[str, Any] = {
        "t": digest_json,
        "c": DIGEST_CATEGORY,
        "cf": 1.0,
        "i": 10,
        "sa": DIGEST_SOURCE_AGENT,
        "ea": compiled_at,
    }
    return _core.canonicalize_claim(
        json.dumps(claim, ensure_ascii=False, separators=(",", ":"))
    )


def extract_digest_from_claim(canonical_claim_json: str) -> Optional[Dict[str, Any]]:
    """Inverse of :func:`build_digest_claim`.

    Given a canonical Claim JSON (as returned by ``parse_claim_or_legacy``),
    return the wrapped Digest object, or ``None`` if the claim is not a
    digest or the inner JSON fails to parse.
    """
    try:
        claim = json.loads(canonical_claim_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(claim, dict):
        return None
    if claim.get("c") != DIGEST_CATEGORY:
        return None
    t = claim.get("t")
    if not isinstance(t, str):
        return None
    try:
        digest = json.loads(t)
    except (ValueError, TypeError):
        return None
    if not isinstance(digest, dict):
        return None
    # Minimal shape check: a Digest must at least have prompt_text.
    if not isinstance(digest.get("prompt_text"), str):
        return None
    return digest


# ---------------------------------------------------------------------------
# Staleness + recompile guard
# ---------------------------------------------------------------------------


def hours_since(compiled_at_iso: str, now_ms: int) -> float:
    """Hours between ``compiled_at_iso`` and ``now_ms``.

    Returns ``float('inf')`` if the timestamp is unparseable (forces a
    recompile, the safe default). Returns 0 for future dates (clock-skew
    defensive).
    """
    try:
        # Python's fromisoformat handles +00:00 but not 'Z' before 3.11;
        # we're on >=3.11 per pyproject but normalize anyway.
        normalized = compiled_at_iso
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        then_dt = datetime.fromisoformat(normalized)
        if then_dt.tzinfo is None:
            then_dt = then_dt.replace(tzinfo=timezone.utc)
        then_ms = int(then_dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return math.inf
    delta_ms = now_ms - then_ms
    if delta_ms <= 0:
        return 0.0
    return delta_ms / (1000 * 60 * 60)


def is_digest_stale(digest_version: int, current_max_created_at_unix: int) -> bool:
    """The digest is stale if new claims have been written since compilation.

    Both inputs are Unix seconds. Equal or regressing values (clock skew,
    empty vault) return False — we only recompile on strictly-newer evidence.
    """
    return current_max_created_at_unix > digest_version


def should_recompile(count_new_claims: int, hours_since_compilation: float) -> bool:
    """Recompile guard (plan §15.10): 10+ new claims OR 24h+ elapsed."""
    return count_new_claims >= 10 or hours_since_compilation >= 24


# ---------------------------------------------------------------------------
# Entity trapdoors
# ---------------------------------------------------------------------------


def compute_entity_trapdoor(name: str) -> str:
    """Compute a single entity trapdoor: ``sha256("entity:" + normalized)`` as hex.

    Uses plain SHA-256 (not HMAC) — the same primitive as word/stem
    trapdoors in ``rust/totalreclaw-core/src/blind.rs`` — so entity
    trapdoors live in the same ``blind_indices`` array and are findable
    by the existing search pipeline. The ``entity:`` prefix namespaces the
    hash so a user named "postgresql" never collides with the word
    trapdoor for the token "postgresql".
    """
    normalized = _core.normalize_entity_name(name)
    return hashlib.sha256(b"entity:" + normalized.encode("utf-8")).hexdigest()


def compute_entity_trapdoors(
    entities: Optional[Sequence[Any]],
) -> List[str]:
    """Compute trapdoors for every entity on a fact, deduplicated.

    Returns an empty list when the input is ``None`` or empty. Input can be
    a list of dicts (``{"name": ..., "type": ...}``) or dataclass instances.
    """
    if not entities:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for e in entities:
        name = _attr(e, "name", None)
        if not isinstance(name, str) or not name:
            continue
        td = compute_entity_trapdoor(name)
        if td not in seen:
            seen.add(td)
            out.append(td)
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a dataclass-like object or a dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
