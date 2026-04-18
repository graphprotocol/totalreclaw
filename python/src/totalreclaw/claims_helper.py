"""
TotalReclaw — Knowledge Graph helpers for the write + read path.

Mirrors ``skill/plugin/claims-helper.ts`` function-for-function in Python so
that client-produced claim blobs are byte-equivalent across TypeScript and
Python. All canonicalization is delegated to ``totalreclaw_core``; this module
only handles category mapping, entity trapdoor hashing, and helpers for
reading already-decrypted blobs.

As of ``totalreclaw`` 2.0.0 / ``@totalreclaw/core`` 2.0.0:

* Memory Taxonomy v1 is the DEFAULT and ONLY write path — no env-var toggles.
  Extraction emits v1 JSON blobs (long-form fields + ``schema_version: "1.0"``).
* The legacy ``TOTALRECLAW_CLAIM_FORMAT=legacy`` fallback has been removed —
  ``build_canonical_claim`` always forwards to :func:`build_canonical_claim_v1`.
* The legacy v0 short-key ``{t, c, cf, i, sa, ea}`` claim format is read-only:
  :func:`read_claim_from_blob` still decodes it transparently for pre-v1
  vault entries, but no Python caller produces it.

The outer protobuf wrapper's ``version`` field MUST be set to
:data:`PROTOBUF_VERSION_V4` (``4``) when storing a v1 payload — see
``operations.py::store_fact``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence

import totalreclaw_core as _core

from .agent.extraction import (
    LEGACY_V0_MEMORY_TYPES,
    V0_TO_V1_TYPE,
    VALID_MEMORY_SCOPES,
    VALID_MEMORY_SOURCES,
    VALID_MEMORY_TYPES,
    VALID_MEMORY_VOLATILITIES,
    is_valid_memory_type,
    normalize_to_v1_type,
)


# ---------------------------------------------------------------------------
# Version + schema markers
# ---------------------------------------------------------------------------

#: Memory Taxonomy v1 schema-version string (matches plugin's V1_SCHEMA_VERSION).
V1_SCHEMA_VERSION: str = "1.0"

#: Outer protobuf wrapper version tags.
#:
#: * ``DEFAULT_PROTOBUF_VERSION`` (3) — legacy callers; inner blob is the
#:   pre-v1 short-key binary envelope.
#: * ``PROTOBUF_VERSION_V4`` (4) — Memory Taxonomy v1; inner blob is a JSON
#:   payload with ``schema_version: "1.0"``.
DEFAULT_PROTOBUF_VERSION: int = 3
PROTOBUF_VERSION_V4: int = 4


def _js_round(x: float) -> int:
    """JavaScript-compatible ``Math.round``: half-to-positive-infinity.

    Python's built-in ``round`` uses banker's rounding (half-to-even), which
    diverges from JavaScript for .5 cases — notably ``0.85 * 10 == 8.5`` which
    Python rounds to 8 but JS rounds to 9. Plugin parity requires matching JS.
    """
    return math.floor(x + 0.5)


# ---------------------------------------------------------------------------
# Feature flags (digest mode only — claim-format gate removed in 2.0.0)
# ---------------------------------------------------------------------------

DigestMode = Literal["on", "off", "template"]


def resolve_digest_mode() -> DigestMode:
    """Resolve ``TOTALRECLAW_DIGEST_MODE`` — "on" (default), "off", "template".

    Digest compilation is orthogonal to the v0/v1 taxonomy split; this
    setting stays. See ``docs/specs/totalreclaw/retrieval-improvements-v3.md``.
    """
    raw = (os.environ.get("TOTALRECLAW_DIGEST_MODE") or "").strip().lower()
    if raw == "off":
        return "off"
    if raw == "template":
        return "template"
    return "on"


# ---------------------------------------------------------------------------
# Category mapping (type → compact Claim category short key for display)
# ---------------------------------------------------------------------------

#: Legacy v0 type → compact category short key. Kept for decoding pre-v1
#: vault entries whose decrypted blob still carries a v0 token string.
TYPE_TO_CATEGORY_V0: Dict[str, str] = {
    "fact": "fact",
    "preference": "pref",
    "decision": "dec",
    "episodic": "epi",
    "goal": "goal",
    "context": "ctx",
    "summary": "sum",
    "rule": "rule",
}

#: v1 type → compact category short key for recall display. Matches the
#: plugin's ``TYPE_TO_CATEGORY_V1``. ``directive`` / ``commitment`` map onto
#: the ``rule`` / ``goal`` display tags so the recall UI is consistent with
#: the pre-v1 category labels the user is used to seeing.
TYPE_TO_CATEGORY_V1: Dict[str, str] = {
    "claim": "claim",
    "preference": "pref",
    "directive": "rule",
    "commitment": "goal",
    "episode": "epi",
    "summary": "sum",
}

#: Backward-compat alias — prefer the versioned maps in new code.
TYPE_TO_CATEGORY: Dict[str, str] = {
    **TYPE_TO_CATEGORY_V0,
    **TYPE_TO_CATEGORY_V1,
}


def map_type_to_category(fact_type: str) -> str:
    """Map any memory type (v1 or legacy v0) to the compact category short key.

    v1 types take priority; unknown tokens fall through to the v0 table for
    pre-v1 vault entries; anything else returns ``"fact"``.
    """
    if fact_type in TYPE_TO_CATEGORY_V1:
        return TYPE_TO_CATEGORY_V1[fact_type]
    return TYPE_TO_CATEGORY_V0.get(fact_type, "fact")


# ---------------------------------------------------------------------------
# Canonical Claim builder (v1 — plugin v3.0.0 / python 2.0.0 default)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Current time as ``YYYY-MM-DDTHH:MM:SS.sssZ`` (same shape as the TS plugin)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def build_canonical_claim_v1(
    fact: Any,
    importance: int,
    created_at: Optional[str] = None,
    superseded_by: Optional[str] = None,
    expires_at: Optional[str] = None,
    claim_id: Optional[str] = None,
) -> str:
    """Build a v1 ``MemoryClaimV1`` JSON blob.

    Mirrors the TS plugin's ``buildCanonicalClaimV1`` function-for-function.

    The pipeline:

      1. Build the full v1 payload object (including plugin-only extras
         like ``volatility`` and ``schema_version``).
      2. Validate the core-required subset through
         :func:`totalreclaw_core.validate_memory_claim_v1` (throws on
         invalid type, source, or missing id).
      3. Re-attach plugin-only extras (``schema_version``, ``volatility``)
         to the validated canonical payload and return as a JSON string.

    The outer protobuf wrapper's ``version`` field must be set to
    :data:`PROTOBUF_VERSION_V4` (4) when storing the returned payload.

    Raises
    ------
    ValueError
        If the fact does not have a valid v1 ``source`` set — v1 requires
        every claim to carry provenance.
    """
    text = _attr(fact, "text")
    fact_type = normalize_to_v1_type(_attr(fact, "type", "claim"))
    source = _attr(fact, "source")
    scope = _attr(fact, "scope", "unspecified")
    reasoning = _attr(fact, "reasoning", None)
    entities = _attr(fact, "entities", None)
    confidence_raw = _attr(fact, "confidence", 0.85)
    volatility = _attr(fact, "volatility", None)

    if not source:
        raise ValueError(
            "build_canonical_claim_v1: fact.source is required (v1 taxonomy mandates provenance)"
        )
    if source not in VALID_MEMORY_SOURCES:
        raise ValueError(f"build_canonical_claim_v1: invalid source {source!r}")

    resolved_id = claim_id or str(uuid.uuid4())
    resolved_created_at = created_at or _now_iso()
    resolved_importance = max(1, min(10, _js_round(float(importance))))
    resolved_confidence = 0.85 if confidence_raw is None else float(confidence_raw)
    resolved_confidence = max(0.0, min(1.0, resolved_confidence))

    # Core-canonical subset sent through validate_memory_claim_v1.
    core_payload: Dict[str, Any] = {
        "id": resolved_id,
        "text": text,
        "type": fact_type,
        "source": source,
        "created_at": resolved_created_at,
        "importance": resolved_importance,
        "confidence": resolved_confidence,
    }

    if scope and scope in VALID_MEMORY_SCOPES:
        core_payload["scope"] = scope
    if isinstance(reasoning, str) and reasoning:
        core_payload["reasoning"] = reasoning[:256]
    if entities:
        ent_list: List[Dict[str, Any]] = []
        for e in list(entities)[:8]:
            name = _attr(e, "name")
            etype = _attr(e, "type")
            role = _attr(e, "role", None)
            if not (isinstance(name, str) and isinstance(etype, str)):
                continue
            entry: Dict[str, Any] = {"name": name, "type": etype}
            if role:
                entry["role"] = role
            ent_list.append(entry)
        if ent_list:
            core_payload["entities"] = ent_list
    if expires_at:
        core_payload["expires_at"] = expires_at
    if superseded_by:
        core_payload["superseded_by"] = superseded_by

    # Attach schema_version BEFORE validation — core's v1 struct requires it.
    core_payload["schema_version"] = V1_SCHEMA_VERSION

    validated = _core.validate_memory_claim_v1(
        json.dumps(core_payload, ensure_ascii=False, separators=(",", ":"))
    )
    canonical = json.loads(validated)
    if not isinstance(canonical, dict):
        raise RuntimeError("validate_memory_claim_v1 did not return an object")

    # Re-attach plugin-only extras not round-tripped by core's validator.
    canonical["schema_version"] = V1_SCHEMA_VERSION
    if volatility and volatility in VALID_MEMORY_VOLATILITIES:
        canonical["volatility"] = volatility

    return json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))


def build_canonical_claim(
    fact: Any,
    importance: int,
    source_agent: str = "",
    extracted_at: Optional[str] = None,
) -> str:
    """Construct a canonical claim JSON string from an ExtractedFact-like object.

    As of ``totalreclaw`` 2.0.0 this unconditionally emits a Memory Taxonomy
    v1 JSON blob (``schema_version == "1.0"``) — forwarded to
    :func:`build_canonical_claim_v1`. The legacy v0 short-key
    ``{t, c, cf, i, sa, ea}`` format is no longer produced on the write path.

    The outer protobuf wrapper's ``version`` field MUST be set to
    :data:`PROTOBUF_VERSION_V4` when storing the returned payload.

    ``source_agent`` is retained on the signature for legacy back-compat but
    is ignored — v1 provenance lives in ``fact.source``. When the input fact
    has no ``source`` set we supply ``"user-inferred"`` as a defensive
    default so a misconfigured extraction path does not drop the write.
    """
    # Defensive: ensure fact.source is populated before v1 validation.
    fact_source = _attr(fact, "source")
    if not fact_source:
        if isinstance(fact, dict):
            fact = dict(fact)
            fact["source"] = "user-inferred"
        else:
            # dataclass: set the attribute in place
            try:
                setattr(fact, "source", "user-inferred")
            except Exception:
                fact = {**_fact_to_dict(fact), "source": "user-inferred"}

    return build_canonical_claim_v1(
        fact,
        importance=importance,
        created_at=extracted_at,
    )


def _fact_to_dict(fact: Any) -> Dict[str, Any]:
    """Last-resort dataclass→dict conversion for the defensive source default."""
    if isinstance(fact, dict):
        return dict(fact)
    out: Dict[str, Any] = {}
    for key in (
        "text", "type", "importance", "action", "confidence", "entities",
        "source", "scope", "reasoning", "volatility",
    ):
        if hasattr(fact, key):
            out[key] = getattr(fact, key)
    return out


# ---------------------------------------------------------------------------
# Legacy ``{text, metadata}`` doc shape — retained for fixture decoding ONLY.
#
# Python 2.0.0 does not produce legacy docs; they exist only so pre-v1
# vault entries remain decodable. Kept here so existing external tests
# that reference ``build_legacy_doc`` still compile, but tagged deprecated.
# ---------------------------------------------------------------------------


def build_legacy_doc(
    fact: Any,
    importance: int,
    source: str,
    created_at: Optional[str] = None,
) -> str:
    """Build a legacy ``{text, metadata}`` doc.

    .. deprecated:: 2.0.0
        No Python caller produces legacy docs; v1 is the default write path.
        This helper is retained only for back-compat decoding tests.
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

DIGEST_TRAPDOOR: str = hashlib.sha256(b"type:digest").hexdigest()
DIGEST_CATEGORY: str = "dig"
DIGEST_SOURCE_AGENT: str = "hermes-agent-digest"
DIGEST_CLAIM_CAP: int = 200


# ---------------------------------------------------------------------------
# Decrypted blob reader — handles v1 JSON, v0 short-key Claims, and legacy docs
# ---------------------------------------------------------------------------


def is_v1_blob(decrypted: str) -> bool:
    """Heuristic: does a decrypted blob look like a v1 JSON payload?

    Checks for the ``schema_version`` marker plus the long-form ``text`` and
    ``type`` fields. Returns ``False`` on any parse error.
    """
    try:
        obj = json.loads(decrypted)
    except (ValueError, TypeError):
        return False
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("text"), str)
        and isinstance(obj.get("type"), str)
        and isinstance(obj.get("schema_version"), str)
        and obj["schema_version"].startswith("1.")
    )


def read_blob_unified(decrypted: str) -> Dict[str, Any]:
    """Unified decrypted blob reader with v1-first precedence.

    Tries in order:
      1. **v1 JSON** — long-form fields + ``schema_version`` "1.x".
      2. **v0 short-key Claim** — ``{t, c, cf, i, sa, ea, ...}``.
      3. **Legacy plugin doc** — ``{text, metadata: {...}}``.
      4. **Raw text fallback** — unrecognized shape returned as-is.

    Returns the same ``{text, importance, category, metadata}`` shape as the
    plugin's :func:`readClaimFromBlob`. ``metadata`` carries the v1 source
    /scope /volatility /reasoning /schema_version when available, so callers
    applying Tier 1 source-weighted reranking can read the source directly.
    """
    try:
        obj = json.loads(decrypted)
    except (ValueError, TypeError):
        return {
            "text": decrypted,
            "importance": 5,
            "category": "fact",
            "metadata": {},
        }

    if not isinstance(obj, dict):
        return {
            "text": decrypted,
            "importance": 5,
            "category": "fact",
            "metadata": {},
        }

    # 1. v1 JSON payload: long-form fields + schema_version "1.x"
    schema_version = obj.get("schema_version")
    if (
        isinstance(obj.get("text"), str)
        and isinstance(obj.get("type"), str)
        and isinstance(schema_version, str)
        and schema_version.startswith("1.")
    ):
        imp_raw = obj.get("importance")
        if isinstance(imp_raw, (int, float)) and not isinstance(imp_raw, bool):
            importance = max(1, min(10, _js_round(float(imp_raw))))
        else:
            importance = 5
        return {
            "text": obj["text"],
            "importance": importance,
            "category": map_type_to_category(obj["type"]),
            "metadata": {
                "type": obj["type"],
                "source": obj.get("source", "user-inferred") if isinstance(obj.get("source"), str) else "user-inferred",
                "scope": obj.get("scope", "unspecified") if isinstance(obj.get("scope"), str) else "unspecified",
                "volatility": obj.get("volatility", "updatable") if isinstance(obj.get("volatility"), str) else "updatable",
                "reasoning": obj.get("reasoning") if isinstance(obj.get("reasoning"), str) else None,
                "importance": importance / 10,
                "created_at": obj.get("created_at", "") if isinstance(obj.get("created_at"), str) else "",
                "schema_version": schema_version,
            },
        }

    # 2. v0 canonical Claim format: compact short keys {t, c, ...}
    t_val = obj.get("t")
    c_val = obj.get("c")
    if isinstance(t_val, str) and isinstance(c_val, str):
        raw_i = obj.get("i")
        if isinstance(raw_i, (int, float)) and not isinstance(raw_i, bool):
            importance = max(1, min(10, _js_round(float(raw_i))))
        else:
            importance = 5
        return {
            "text": t_val,
            "importance": importance,
            "category": c_val,
            "metadata": {
                "type": c_val,
                "importance": importance / 10,
                "source": obj.get("sa") if isinstance(obj.get("sa"), str) else "auto-extraction",
                "created_at": obj.get("ea") if isinstance(obj.get("ea"), str) else "",
            },
        }

    # 3. Legacy plugin {text, metadata: {importance: 0-1}} format
    legacy_text = obj.get("text")
    if isinstance(legacy_text, str):
        meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        raw_imp = meta.get("importance")
        imp_float = float(raw_imp) if isinstance(raw_imp, (int, float)) and not isinstance(raw_imp, bool) else 0.5
        importance = max(1, min(10, _js_round(imp_float * 10)))
        category = meta.get("type") if isinstance(meta.get("type"), str) else "fact"
        return {
            "text": legacy_text,
            "importance": importance,
            "category": category,
            "metadata": meta,
        }

    # 4. Unrecognized JSON object shape — fall back.
    return {
        "text": decrypted,
        "importance": 5,
        "category": "fact",
        "metadata": {},
    }


#: Back-compat alias — prefer :func:`read_blob_unified` in new code.
def read_claim_from_blob(decrypted_json: str) -> Dict[str, Any]:
    """Back-compat alias for :func:`read_blob_unified`."""
    return read_blob_unified(decrypted_json)


def is_digest_blob(decrypted: str) -> bool:
    """Does this decrypted blob look like a digest claim?"""
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

    Digest claims use the v0 short-key format — they are an internal
    read-path artifact, not a user-facing memory, so they do not need to
    be in the v1 taxonomy.
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

    Given a canonical Claim JSON, return the wrapped Digest object,
    or ``None`` if the claim is not a digest / inner JSON fails to parse.
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
    if not isinstance(digest.get("prompt_text"), str):
        return None
    return digest


# ---------------------------------------------------------------------------
# Staleness + recompile guard
# ---------------------------------------------------------------------------


def hours_since(compiled_at_iso: str, now_ms: int) -> float:
    """Hours between ``compiled_at_iso`` and ``now_ms``."""
    try:
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
    return current_max_created_at_unix > digest_version


def should_recompile(count_new_claims: int, hours_since_compilation: float) -> bool:
    """Recompile guard: 10+ new claims OR 24h+ elapsed."""
    return count_new_claims >= 10 or hours_since_compilation >= 24


# ---------------------------------------------------------------------------
# Entity trapdoors
# ---------------------------------------------------------------------------


def compute_entity_trapdoor(name: str) -> str:
    normalized = _core.normalize_entity_name(name)
    return hashlib.sha256(b"entity:" + normalized.encode("utf-8")).hexdigest()


def compute_entity_trapdoors(
    entities: Optional[Sequence[Any]],
) -> List[str]:
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
