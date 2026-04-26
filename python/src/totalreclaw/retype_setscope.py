"""TotalReclaw — retype / set_scope on-chain operations (Hermes Python parity).

Mirrors ``skill/plugin/retype-setscope.ts`` function-for-function so a
Hermes-issued retype / set_scope produces the same v1.1 on-chain shape
as a plugin-issued one. Cross-client KG parity: a plugin write +
Hermes retype on the same fact id surfaces the new type to either side.

Public API
----------

* :func:`execute_retype` — change a fact's ``type`` (claim ↔ preference, etc.)
* :func:`execute_set_scope` — change a fact's ``scope`` (work ↔ health, etc.)

Both helpers tombstone the existing fact and write a fresh v1.1 ``MemoryClaimV1``
JSON blob with ``superseded_by`` pointing at the old fact id, so cross-device
readers see the correct resolution. The mutation is submitted as a single
atomic ``executeBatch`` UserOp (tombstone v=3 + new fact v=4), matching the
pin/unpin atomic-batch pattern shipped in 2.2.3.

Design notes
------------

* **Why this module is separate from operations.py's pin path.**
  ``_change_claim_status`` in :mod:`totalreclaw.operations` is tightly coupled
  to ``pin_status`` mutations (idempotent short-circuit on matching status,
  feedback-row writes for auto-supersede victims). Retype + set_scope are
  simpler — they don't short-circuit when the new value equals the old (the
  user might be confirming a prior auto-extraction's label) and they never
  write feedback rows. Sharing the transport / crypto deps with pin is still
  useful, so this module reuses ``RelayClient``, ``DerivedKeys``,
  ``build_and_send_userop_batch``, and the ``confirm_indexed`` poller.

* **pin_status preservation (issue #117 / TS PR #114).**
  When a user retype/set_scope's a *pinned* fact, the new fact MUST inherit
  the ``pin_status`` of the source. Without this, a metadata edit silently
  un-pins the fact — auto-resolution can then supersede it. The projector
  below threads ``pin_status`` through the rewrite explicitly.

* **No phrase-safety surfaces.** Mnemonic / encryption-key handling is
  delegated to the caller-supplied :class:`~totalreclaw.crypto.DerivedKeys`.
  No new crypto code; no new userOp construction; no phrase ingress/egress.

* **No new on-chain ABI.** All on-chain calldata flows through the existing
  ``build_and_send_userop_batch`` path. The shared Rust core's
  ``encode_batch_call`` produces the same byte sequence whether the caller
  is the TS plugin or this Python module.
"""
from __future__ import annotations

import base64
import json as _json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .agent.extraction import (
    V0_TO_V1_TYPE,
    VALID_MEMORY_SCOPES,
    VALID_MEMORY_SOURCES,
    VALID_MEMORY_TYPES,
    is_valid_memory_type,
)
from .claims_helper import (
    PROTOBUF_VERSION_V4,
    VALID_PIN_STATUSES,
    build_canonical_claim_v1,
    compute_entity_trapdoors,
)
from .crypto import (
    DerivedKeys,
    decrypt,
    encrypt,
    encrypt_embedding,
    generate_blind_indices,
)
from .embedding import get_embedding
from .lsh import LSHHasher
from .protobuf import FactPayload, encode_fact_protobuf, encode_tombstone_protobuf
from .relay import RelayClient
from .userop import build_and_send_userop_batch

logger = logging.getLogger(__name__)

# GraphQL query — must mirror ``operations.FACT_BY_ID_QUERY`` so a single-source
# fetcher feeds both the pin path and this one without divergence risk.
_FACT_BY_ID_QUERY = """
  query FactById($id: ID!) {
    fact(id: $id) {
      id
      owner
      encryptedBlob
      encryptedEmbedding
      decayScore
      timestamp
      createdAt
      isActive
      contentFp
    }
  }
"""


# ---------------------------------------------------------------------------
# Normalized projector — decrypted plaintext → v1-shape dict for mutation.
# Mirrors ``retype-setscope.ts::projectFromDecrypted`` byte-for-byte semantics.
# ---------------------------------------------------------------------------


def _project_from_decrypted(decrypted: str) -> Optional[dict]:
    """Project a decrypted blob (v1 long-form OR v0 short-key) into a v1 dict.

    Returns the normalized v1 fields ready to feed into
    :func:`build_canonical_claim_v1`, plus the source claim's ``pin_status``
    (so callers preserve pin state across the rewrite). Returns ``None`` for
    blobs that don't match either recognized shape — caller surfaces a
    descriptive error instead of writing a malformed claim.

    Critical contract: when the source blob has ``pin_status``, the
    projector MUST surface it so :func:`_rewrite_with_mutation` can pass it
    back into :func:`build_canonical_claim_v1`. This is the Python mirror
    of the TS PR #114 fix that surfaced from issue #117 (retype on a pinned
    fact silently un-pinned it).
    """
    try:
        obj = _json.loads(decrypted)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    # 1. v1 JSON payload — long-form fields + ``schema_version`` "1.x"
    schema_version = obj.get("schema_version")
    if (
        isinstance(obj.get("text"), str)
        and isinstance(obj.get("type"), str)
        and isinstance(schema_version, str)
        and schema_version.startswith("1.")
    ):
        v1_type = obj["type"] if obj["type"] in VALID_MEMORY_TYPES else "claim"
        raw_source = obj.get("source")
        v1_source = (
            raw_source
            if isinstance(raw_source, str) and raw_source in VALID_MEMORY_SOURCES
            else "user-inferred"
        )
        raw_scope = obj.get("scope")
        v1_scope = (
            raw_scope
            if isinstance(raw_scope, str) and raw_scope in VALID_MEMORY_SCOPES
            else None
        )
        raw_volatility = obj.get("volatility")
        v1_volatility = (
            raw_volatility
            if isinstance(raw_volatility, str) and raw_volatility
            else None
        )
        raw_reasoning = obj.get("reasoning")
        v1_reasoning = (
            raw_reasoning
            if isinstance(raw_reasoning, str) and raw_reasoning
            else None
        )
        raw_entities = obj.get("entities")
        v1_entities = raw_entities if isinstance(raw_entities, list) else None
        imp_raw = obj.get("importance")
        try:
            importance = (
                int(imp_raw) if isinstance(imp_raw, (int, float)) else 5
            )
        except (ValueError, TypeError):
            importance = 5
        importance = max(1, min(10, importance))
        conf_raw = obj.get("confidence")
        try:
            confidence = (
                float(conf_raw) if isinstance(conf_raw, (int, float)) else 0.85
            )
        except (ValueError, TypeError):
            confidence = 0.85
        confidence = max(0.0, min(1.0, confidence))
        raw_pin_status = obj.get("pin_status")
        pin_status = (
            raw_pin_status
            if isinstance(raw_pin_status, str)
            and raw_pin_status in VALID_PIN_STATUSES
            else None
        )
        return {
            "text": obj["text"],
            "type": v1_type,
            "source": v1_source,
            "scope": v1_scope,
            "volatility": v1_volatility,
            "reasoning": v1_reasoning,
            "entities": v1_entities,
            "importance": importance,
            "confidence": confidence,
            "pin_status": pin_status,
        }

    # 2. v0 short-key blob — upgrade to v1 shape on the fly.
    t_val = obj.get("t")
    c_val = obj.get("c")
    if isinstance(t_val, str) and isinstance(c_val, str):
        v0_type_token = c_val
        v1_type = V0_TO_V1_TYPE.get(v0_type_token, "claim")
        raw_imp = obj.get("i")
        try:
            importance = int(raw_imp) if isinstance(raw_imp, (int, float)) else 5
        except (ValueError, TypeError):
            importance = 5
        importance = max(1, min(10, importance))
        cf_raw = obj.get("cf")
        try:
            confidence = (
                float(cf_raw) if isinstance(cf_raw, (int, float)) else 0.85
            )
        except (ValueError, TypeError):
            confidence = 0.85
        confidence = max(0.0, min(1.0, confidence))
        # v0 short-key blobs have no explicit provenance signal; use the
        # legacy fallback used by the pin path's projection.
        v1_source = "user-inferred"
        # v0 entities → v1 entity shape
        raw_entities = obj.get("e") if isinstance(obj.get("e"), list) else []
        v1_entities: list[dict] = []
        for e in raw_entities:
            if not isinstance(e, dict):
                continue
            name = e.get("n") if isinstance(e.get("n"), str) else ""
            ent_type = e.get("tp") if isinstance(e.get("tp"), str) else "concept"
            if not name:
                continue
            entity: dict = {"name": name, "type": ent_type}
            role = e.get("r") if isinstance(e.get("r"), str) else None
            if role:
                entity["role"] = role
            v1_entities.append(entity)
        return {
            "text": t_val,
            "type": v1_type,
            "source": v1_source,
            "scope": None,
            "volatility": None,
            "reasoning": None,
            "entities": v1_entities or None,
            "importance": importance,
            "confidence": confidence,
            # v0 short-key blobs may carry ``st: "p"`` — treat as pinned.
            "pin_status": "pinned" if obj.get("st") == "p" else None,
        }

    return None


class _ProjectedFact:
    """Attribute carrier for :func:`build_canonical_claim_v1`.

    Mirrors the helper used in :mod:`totalreclaw.operations` so the v1
    builder sees a consistent attribute surface regardless of which write
    path constructed the fact.
    """

    __slots__ = (
        "text",
        "type",
        "importance",
        "confidence",
        "source",
        "scope",
        "reasoning",
        "entities",
        "volatility",
    )

    def __init__(self, **kwargs: Any) -> None:
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k))


# ---------------------------------------------------------------------------
# Core: fetch existing → decrypt → mutate → submit batch → confirm-indexed
# ---------------------------------------------------------------------------


async def _rewrite_with_mutation(
    fact_id: str,
    mutate_field: str,
    mutate_value: str,
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    eoa_private_key: Optional[bytes],
    eoa_address: Optional[str],
    sender: Optional[str],
    chain_id: int,
    lsh_hasher: Optional[LSHHasher],
    source_tag: str,
) -> dict:
    """Shared write path for retype + set_scope.

    Mirrors ``retype-setscope.ts::rewriteWithMutation`` step-for-step:

      1. Fetch the existing fact via subgraph.
      2. Decrypt + project to v1 normalized shape (preserving ``pin_status``).
      3. Apply the mutation (``type`` or ``scope``).
      4. Build a fresh v1.1 ``MemoryClaimV1`` JSON blob carrying
         ``superseded_by: <old_fact_id>`` and the preserved ``pin_status``.
      5. Encrypt + encode protobuf v=4 for the new fact, v=3 tombstone for
         the old fact.
      6. Submit BOTH payloads as a single atomic ``executeBatch`` UserOp.
      7. Read-after-write: poll ``confirm_indexed`` for the new fact id; on
         timeout / missing PyO3 bindings surface ``partial: True`` rather
         than failing the operation (the chain write IS acknowledged).

    Returns ``{success, fact_id, new_fact_id, previous_type, new_type,
    previous_scope, new_scope, tx_hash, partial?}`` — same shape as the TS
    plugin's ``RetypeSetScopeResult``.
    """
    if not isinstance(fact_id, str) or not fact_id.strip():
        raise ValueError("fact_id must be a non-empty string")
    if eoa_private_key is None or eoa_address is None:
        raise ValueError(
            "eoa_private_key and eoa_address are required for UserOp signing"
        )
    if mutate_field not in ("type", "scope"):
        raise ValueError(f"unsupported mutate_field {mutate_field!r}")

    fact_id = fact_id.strip()
    smart_account = sender or owner

    # 1. Fetch existing fact.
    data = await relay.query_subgraph(_FACT_BY_ID_QUERY, {"id": fact_id})
    fact = data.get("data", {}).get("fact")
    if not fact:
        return {
            "success": False,
            "fact_id": fact_id,
            "error": f"Fact not found: {fact_id}",
        }

    # 2. Decrypt + project.
    encrypted_hex = fact.get("encryptedBlob", "") or ""
    if encrypted_hex.startswith("0x"):
        encrypted_hex = encrypted_hex[2:]
    if not encrypted_hex:
        return {
            "success": False,
            "fact_id": fact_id,
            "error": "fact has empty encryptedBlob",
        }
    try:
        encrypted_b64 = base64.b64encode(bytes.fromhex(encrypted_hex)).decode(
            "ascii"
        )
        plaintext = decrypt(encrypted_b64, keys.encryption_key)
    except Exception as exc:
        return {
            "success": False,
            "fact_id": fact_id,
            "error": f"Failed to decrypt fact: {exc}",
        }

    current = _project_from_decrypted(plaintext)
    if current is None:
        return {
            "success": False,
            "fact_id": fact_id,
            "error": (
                f"Unrecognized blob shape for fact {fact_id} — cannot retype/rescope"
            ),
        }

    # 3. Apply mutation. previous_X is captured before the mutation so the
    # response matches the TS RetypeSetScopeResult shape.
    previous_type = current["type"]
    previous_scope = current.get("scope") or "unspecified"
    next_state = dict(current)
    next_state[mutate_field] = mutate_value

    # 4. Build new v1.1 canonical claim. Preserve pin_status across the
    # rewrite (issue #117 / TS PR #114).
    new_fact_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    projected = _ProjectedFact(
        text=next_state["text"],
        type=next_state["type"],
        importance=next_state["importance"],
        confidence=next_state["confidence"],
        source=next_state["source"],
        scope=next_state.get("scope"),
        reasoning=next_state.get("reasoning"),
        entities=next_state.get("entities"),
        volatility=next_state.get("volatility"),
    )
    try:
        new_blob_plaintext = build_canonical_claim_v1(
            projected,
            importance=next_state["importance"],
            created_at=timestamp,
            superseded_by=fact_id,
            claim_id=new_fact_id,
            pin_status=next_state.get("pin_status"),
        )
    except Exception as exc:
        return {
            "success": False,
            "fact_id": fact_id,
            "error": f"Failed to build v1 claim blob: {exc}",
        }

    # 5. Encrypt + build payloads.
    try:
        encrypted_blob = encrypt(new_blob_plaintext, keys.encryption_key)
        new_encrypted_hex = base64.b64decode(encrypted_blob).hex()
    except Exception as exc:
        return {
            "success": False,
            "fact_id": fact_id,
            "error": f"Failed to encrypt updated claim: {exc}",
        }

    # Regenerate trapdoors so the renamed/rescoped fact is still
    # discoverable via blind-index search after the old fact tombstones.
    new_text = next_state["text"]
    new_entity_objs = next_state.get("entities") or []
    new_word_indices: list[str] = (
        generate_blind_indices(new_text) if new_text else []
    )
    new_lsh_indices: list[str] = []
    new_encrypted_emb: Optional[str] = None
    if new_text:
        try:
            new_embedding = get_embedding(new_text)
            if lsh_hasher and new_embedding:
                new_lsh_indices = lsh_hasher.hash(new_embedding)
            new_encrypted_emb = encrypt_embedding(
                new_embedding, keys.encryption_key
            )
        except Exception:
            # Best-effort — word + entity trapdoors still surface the claim.
            pass
    new_entity_trapdoors = (
        compute_entity_trapdoors(new_entity_objs) if new_entity_objs else []
    )
    new_blind_indices = (
        new_word_indices + new_lsh_indices + new_entity_trapdoors
    )

    new_payload = FactPayload(
        id=new_fact_id,
        timestamp=timestamp,
        owner=owner,
        encrypted_blob=new_encrypted_hex,
        blind_indices=new_blind_indices,
        decay_score=1.0,
        source=source_tag,
        content_fp="",
        agent_id="python-client",
        encrypted_embedding=new_encrypted_emb,
        version=PROTOBUF_VERSION_V4,
    )

    # 6. Submit tombstone (v=3) + new fact (v=4) as ONE batched UserOp.
    # Same atomic-batch pattern as ``_change_claim_status`` — one nonce,
    # one Pimlico round-trip, atomic on-chain.
    tombstone_bytes = encode_tombstone_protobuf(fact_id, owner)
    new_protobuf = encode_fact_protobuf(new_payload)

    try:
        tx_hash = await build_and_send_userop_batch(
            sender=smart_account,
            eoa_address=eoa_address,
            eoa_private_key=eoa_private_key,
            protobuf_payloads=[tombstone_bytes, new_protobuf],
            relay_url=relay._relay_url,
            auth_key_hex=relay._auth_key_hex or "",
            wallet_address=smart_account,
            chain_id=chain_id,
            client_id=relay._client_id,
            session_id=getattr(relay, "_session_id", None),
        )
    except Exception as exc:
        return {
            "success": False,
            "fact_id": fact_id,
            "error": f"Failed to submit retype/rescope batch: {exc}",
        }

    # 7. Read-after-write — best-effort; missing PyO3 bindings or timeout
    # surface as ``partial: True``, never as a hard failure.
    from .confirm_indexed import confirm_indexed as _confirm_indexed

    try:
        indexed = await _confirm_indexed(new_fact_id, relay, expect="active")
    except Exception:
        indexed = False

    result: dict = {
        "success": True,
        "fact_id": fact_id,
        "new_fact_id": new_fact_id,
        "previous_type": previous_type,
        "new_type": next_state["type"],
        "previous_scope": previous_scope,
        "new_scope": next_state.get("scope") or "unspecified",
    }
    if isinstance(tx_hash, str) and tx_hash:
        result["tx_hash"] = tx_hash
    if not indexed:
        result["partial"] = True
    return result


# ---------------------------------------------------------------------------
# Public entry points — execute_retype / execute_set_scope
# ---------------------------------------------------------------------------


async def execute_retype(
    fact_id: str,
    new_type: str,
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    eoa_private_key: Optional[bytes] = None,
    eoa_address: Optional[str] = None,
    sender: Optional[str] = None,
    chain_id: int = 84532,
    lsh_hasher: Optional[LSHHasher] = None,
) -> dict:
    """Re-type an existing memory.

    Writes a new v1.1 claim with ``type`` changed; tombstones the old fact.
    ``superseded_by`` on the new fact points to the old id so cross-device
    readers see the correct resolution. ``pin_status`` is preserved across
    the rewrite (issue #117 / TS PR #114).

    Returns ``{success, fact_id, new_fact_id, previous_type, new_type,
    previous_scope, new_scope, tx_hash, partial?}``.
    """
    if not is_valid_memory_type(new_type):
        return {
            "success": False,
            "fact_id": fact_id,
            "error": (
                f"Invalid new_type {new_type!r}. "
                f"Must be one of: {', '.join(VALID_MEMORY_TYPES)}."
            ),
        }
    return await _rewrite_with_mutation(
        fact_id=fact_id,
        mutate_field="type",
        mutate_value=new_type,
        keys=keys,
        owner=owner,
        relay=relay,
        eoa_private_key=eoa_private_key,
        eoa_address=eoa_address,
        sender=sender,
        chain_id=chain_id,
        lsh_hasher=lsh_hasher,
        source_tag="python_retype",
    )


async def execute_set_scope(
    fact_id: str,
    new_scope: str,
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    eoa_private_key: Optional[bytes] = None,
    eoa_address: Optional[str] = None,
    sender: Optional[str] = None,
    chain_id: int = 84532,
    lsh_hasher: Optional[LSHHasher] = None,
) -> dict:
    """Re-scope an existing memory.

    Writes a new v1.1 claim with ``scope`` changed; tombstones the old fact.
    Inverse-equivalent of :func:`execute_retype` for the scope axis.

    Returns the same shape as :func:`execute_retype`.
    """
    if new_scope not in VALID_MEMORY_SCOPES:
        return {
            "success": False,
            "fact_id": fact_id,
            "error": (
                f"Invalid new_scope {new_scope!r}. "
                f"Must be one of: {', '.join(VALID_MEMORY_SCOPES)}."
            ),
        }
    return await _rewrite_with_mutation(
        fact_id=fact_id,
        mutate_field="scope",
        mutate_value=new_scope,
        keys=keys,
        owner=owner,
        relay=relay,
        eoa_private_key=eoa_private_key,
        eoa_address=eoa_address,
        sender=sender,
        chain_id=chain_id,
        lsh_hasher=lsh_hasher,
        source_tag="python_set_scope",
    )


# ---------------------------------------------------------------------------
# Validation helpers — used by Hermes tool wrappers + any external CLI
# ---------------------------------------------------------------------------


def validate_retype_args(args: Any) -> dict:
    """Validate raw tool arguments for ``totalreclaw_retype``.

    Returns ``{"ok": True, "fact_id": ..., "new_type": ...}`` on accept,
    or ``{"ok": False, "error": ...}`` on reject. Mirrors the TS plugin's
    ``validateRetypeArgs`` shape so cross-client tests pin the same surface.
    """
    if not isinstance(args, dict):
        return {
            "ok": False,
            "error": "totalreclaw_retype requires an object argument.",
        }
    fact_id = args.get("fact_id") or args.get("factId")
    if not isinstance(fact_id, str) or not fact_id.strip():
        return {
            "ok": False,
            "error": "fact_id is required and must be a non-empty string.",
        }
    new_type = args.get("new_type") or args.get("newType") or args.get("type")
    if not isinstance(new_type, str) or not is_valid_memory_type(new_type):
        return {
            "ok": False,
            "error": (
                f"new_type must be one of: {', '.join(VALID_MEMORY_TYPES)}"
            ),
        }
    return {"ok": True, "fact_id": fact_id.strip(), "new_type": new_type}


def validate_set_scope_args(args: Any) -> dict:
    """Validate raw tool arguments for ``totalreclaw_set_scope``."""
    if not isinstance(args, dict):
        return {
            "ok": False,
            "error": "totalreclaw_set_scope requires an object argument.",
        }
    fact_id = args.get("fact_id") or args.get("factId")
    if not isinstance(fact_id, str) or not fact_id.strip():
        return {
            "ok": False,
            "error": "fact_id is required and must be a non-empty string.",
        }
    new_scope = (
        args.get("new_scope") or args.get("newScope") or args.get("scope")
    )
    if not isinstance(new_scope, str) or new_scope not in VALID_MEMORY_SCOPES:
        return {
            "ok": False,
            "error": (
                f"new_scope must be one of: {', '.join(VALID_MEMORY_SCOPES)}"
            ),
        }
    return {"ok": True, "fact_id": fact_id.strip(), "new_scope": new_scope}
