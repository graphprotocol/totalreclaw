"""
TotalReclaw Store and Search Operations.

Combines crypto, protobuf, relay, LSH, reranker, and ERC-4337 UserOp
construction into high-level operations.
"""
from __future__ import annotations
import base64
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

import json as _json

import totalreclaw_core as _core

from .crypto import (
    DerivedKeys,
    encrypt,
    decrypt,
    generate_blind_indices,
    generate_content_fingerprint,
    encrypt_embedding,
    decrypt_embedding,
)
from .embedding import get_embedding, get_embedding_dims
from .lsh import LSHHasher
from .protobuf import FactPayload, encode_fact_protobuf, encode_tombstone_protobuf
from .relay import RelayClient
from .reranker import RerankerCandidate, RerankerResult, rerank
from .tuning_loop import maybe_write_feedback_for_pin
from .userop import build_and_send_userop
from .claims_helper import (
    build_canonical_claim,
    compute_entity_trapdoors,
    is_digest_blob,
    read_claim_from_blob,
    resolve_claim_format,
)

# GraphQL queries — matching TypeScript search.ts
SEARCH_QUERY = """
  query SearchByBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!) {
    blindIndexes(
      where: { hash_in: $trapdoors, owner: $owner, fact_: { isActive: true } }
      first: $first
      orderBy: id
      orderDirection: desc
    ) {
      id
      fact {
        id
        encryptedBlob
        encryptedEmbedding
        decayScore
        timestamp
        createdAt
        isActive
        contentFp
      }
    }
  }
"""

PAGINATE_QUERY = """
  query PaginateBlindIndex($trapdoors: [String!]!, $owner: Bytes!, $first: Int!, $lastId: String!) {
    blindIndexes(
      where: { hash_in: $trapdoors, owner: $owner, id_gt: $lastId, fact_: { isActive: true } }
      first: $first
      orderBy: id
      orderDirection: asc
    ) {
      id
      fact {
        id
        encryptedBlob
        encryptedEmbedding
        timestamp
        createdAt
        decayScore
        isActive
        contentFp
      }
    }
  }
"""

BROADENED_SEARCH_QUERY = """
  query BroadenedSearch($owner: Bytes!, $first: Int!) {
    facts(
      where: { owner: $owner, isActive: true }
      first: $first
      orderBy: timestamp
      orderDirection: desc
    ) {
      id
      encryptedBlob
      encryptedEmbedding
      timestamp
      createdAt
      decayScore
      isActive
      contentFp
    }
  }
"""

FACT_BY_ID_QUERY = """
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

EXPORT_QUERY = """
  query ExportFacts($owner: Bytes!, $first: Int!, $skip: Int!) {
    facts(
      where: { owner: $owner, isActive: true }
      first: $first
      skip: $skip
      orderBy: timestamp
      orderDirection: desc
    ) {
      id
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

DEFAULT_TRAPDOOR_BATCH_SIZE = 5
DEFAULT_PAGE_SIZE = 1000


async def store_fact(
    text: str,
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    lsh_hasher: Optional[LSHHasher] = None,
    embedding: Optional[list[float]] = None,
    importance: float = 0.5,
    source: str = "python-client",
    agent_id: str = "python-client",
    eoa_private_key: Optional[bytes] = None,
    eoa_address: Optional[str] = None,
    sender: Optional[str] = None,
    chain_id: int = 84532,
    fact_type: str = "fact",
    entities: Optional[list] = None,
    confidence: float = 0.85,
    extracted_at: Optional[str] = None,
) -> str:
    """Encrypt and store a fact on-chain via relay.

    KG Phase 1: when ``TOTALRECLAW_CLAIM_FORMAT != 'legacy'`` (default), the
    encrypted blob is a canonical ``Claim`` JSON matching the plugin's format.
    Entity trapdoors are added to blind indices regardless of format so new
    facts are findable via entity-specific search once the read path is KG-aware.

    Returns the fact ID.
    """
    if eoa_private_key is None or eoa_address is None:
        raise ValueError(
            "eoa_private_key and eoa_address are required for UserOp signing"
        )

    smart_account = sender or owner
    fact_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    claim_format = resolve_claim_format()
    importance_int = max(1, min(10, int(round(importance * 10)) if importance <= 1 else int(importance)))
    if claim_format == "claim":
        fact_stub = {
            "text": text,
            "type": fact_type,
            "confidence": confidence,
            "entities": entities,
        }
        blob_plaintext = build_canonical_claim(
            fact_stub,
            importance=importance_int,
            source_agent=source,
            extracted_at=extracted_at or timestamp,
        )
    else:
        blob_plaintext = text

    encrypted_blob = encrypt(blob_plaintext, keys.encryption_key)
    encrypted_hex = base64.b64decode(encrypted_blob).hex()

    word_indices = generate_blind_indices(text)

    lsh_indices: list[str] = []
    if lsh_hasher and embedding:
        lsh_indices = lsh_hasher.hash(embedding)

    entity_trapdoors = compute_entity_trapdoors(entities) if entities else []

    all_indices = word_indices + lsh_indices + entity_trapdoors

    # Content fingerprint
    content_fp = generate_content_fingerprint(text, keys.dedup_key)

    # Encrypt embedding if available
    encrypted_emb: Optional[str] = None
    if embedding:
        encrypted_emb = encrypt_embedding(embedding, keys.encryption_key)

    # Build protobuf payload
    payload = FactPayload(
        id=fact_id,
        timestamp=timestamp,
        owner=owner,
        encrypted_blob=encrypted_hex,
        blind_indices=all_indices,
        decay_score=importance,
        source=source,
        content_fp=content_fp,
        agent_id=agent_id,
        encrypted_embedding=encrypted_emb,
    )

    protobuf_bytes = encode_fact_protobuf(payload)

    # Build and submit a proper ERC-4337 UserOperation
    await build_and_send_userop(
        sender=smart_account,
        eoa_address=eoa_address,
        eoa_private_key=eoa_private_key,
        protobuf_payload=protobuf_bytes,
        relay_url=relay._relay_url,
        auth_key_hex=relay._auth_key_hex or "",
        wallet_address=smart_account,
        chain_id=chain_id,
        client_id=relay._client_id,
    )

    return fact_id


async def search_facts(
    query: str,
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    query_embedding: Optional[list[float]] = None,
    lsh_hasher: Optional[LSHHasher] = None,
    max_candidates: int = 250,
    top_k: int = 8,
) -> list[RerankerResult]:
    """Search for facts using blind indices + reranking."""
    # Normalize owner address to lowercase for subgraph Bytes! type
    owner = owner.lower() if owner else owner

    # Generate search trapdoors from query text
    word_trapdoors = generate_blind_indices(query)

    # Generate LSH trapdoors from embedding (if available)
    lsh_trapdoors: list[str] = []
    if lsh_hasher and query_embedding:
        lsh_trapdoors = lsh_hasher.hash(query_embedding)

    # Combine all trapdoors
    all_trapdoors = word_trapdoors + lsh_trapdoors

    if not all_trapdoors:
        return []

    # Split trapdoors into small batches
    chunks: list[list[str]] = []
    for i in range(0, len(all_trapdoors), DEFAULT_TRAPDOOR_BATCH_SIZE):
        chunks.append(all_trapdoors[i : i + DEFAULT_TRAPDOOR_BATCH_SIZE])

    # Query subgraph via relay
    all_facts: dict[str, dict] = {}

    for chunk in chunks:
        try:
            data = await relay.query_subgraph(
                SEARCH_QUERY,
                {"trapdoors": chunk, "owner": owner, "first": DEFAULT_PAGE_SIZE},
            )
            entries = data.get("data", {}).get("blindIndexes", [])
            for entry in entries:
                fact = entry.get("fact")
                if fact and fact.get("isActive", True) and fact["id"] not in all_facts:
                    all_facts[fact["id"]] = fact

            # Pagination for saturated batches
            if len(entries) >= DEFAULT_PAGE_SIZE:
                last_id = ""
                while len(all_facts) < max_candidates:
                    page_data = await relay.query_subgraph(
                        PAGINATE_QUERY,
                        {
                            "trapdoors": chunk,
                            "owner": owner,
                            "first": DEFAULT_PAGE_SIZE,
                            "lastId": last_id,
                        },
                    )
                    page_entries = page_data.get("data", {}).get("blindIndexes", [])
                    if not page_entries:
                        break
                    for entry in page_entries:
                        fact = entry.get("fact")
                        if (
                            fact
                            and fact.get("isActive", True)
                            and fact["id"] not in all_facts
                        ):
                            all_facts[fact["id"]] = fact
                    if len(page_entries) < DEFAULT_PAGE_SIZE:
                        break
                    last_id = page_entries[-1]["id"]
        except Exception as e:
            logger.warning("Trapdoor batch query failed (owner=%s): %s", owner, e)
            continue

    # Always run broadened search and merge — ensures vocabulary mismatches
    # (e.g., "preferences" vs "prefer") don't cause recall failures.
    # The reranker handles scoring; extra cost is ~1 GraphQL query per recall.
    try:
        data = await relay.query_subgraph(
            BROADENED_SEARCH_QUERY,
            {"owner": owner, "first": min(max_candidates, 1000)},
        )
        facts_list = data.get("data", {}).get("facts", [])
        for fact in facts_list:
            if fact and fact.get("isActive", True) and fact["id"] not in all_facts:
                all_facts[fact["id"]] = fact
    except Exception as e:
        logger.warning("Broadened search failed (owner=%s): %s", owner, e)

    if not all_facts:
        return []

    # Decrypt candidates and build reranker input
    candidates: list[RerankerCandidate] = []
    for fact_id, fact in all_facts.items():
        try:
            encrypted_blob_hex = fact.get("encryptedBlob", "")
            # Subgraph returns 0x-prefixed hex
            if encrypted_blob_hex.startswith("0x"):
                encrypted_blob_hex = encrypted_blob_hex[2:]
            encrypted_b64 = base64.b64encode(bytes.fromhex(encrypted_blob_hex)).decode(
                "ascii"
            )
            decrypted_blob = decrypt(encrypted_b64, keys.encryption_key)
            if is_digest_blob(decrypted_blob):
                continue
            doc = read_claim_from_blob(decrypted_blob)
            text = doc["text"]

            emb: Optional[list[float]] = None
            encrypted_emb = fact.get("encryptedEmbedding")
            if encrypted_emb:
                try:
                    emb = decrypt_embedding(encrypted_emb, keys.encryption_key)
                except Exception:
                    pass

            if emb and len(emb) != get_embedding_dims():
                try:
                    emb = get_embedding(text)
                except Exception:
                    emb = None

            decay_str = fact.get("decayScore", "0.5")
            decay = float(decay_str) if isinstance(decay_str, str) else float(decay_str)

            ts = fact.get("timestamp", "")
            created_at = 0.0
            if ts:
                try:
                    created_at = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    pass

            candidates.append(
                RerankerCandidate(
                    id=fact_id,
                    text=text,
                    embedding=emb,
                    importance=decay,
                    created_at=created_at,
                    category=doc.get("category", "fact"),
                )
            )
        except Exception as e:
            logger.warning("Failed to decrypt candidate %s: %s", fact_id, e)
            continue

    if not candidates:
        return []

    return rerank(query, query_embedding, candidates, top_k=top_k)


async def forget_fact(
    fact_id: str,
    owner: str,
    relay: RelayClient,
    eoa_private_key: Optional[bytes] = None,
    eoa_address: Optional[str] = None,
    sender: Optional[str] = None,
    chain_id: int = 84532,
) -> bool:
    """Write a tombstone on-chain to soft-delete a fact.

    Parameters
    ----------
    eoa_private_key : bytes
        32-byte EOA private key for UserOp signing.
    eoa_address : str
        EOA address that owns the Smart Account.
    sender : str
        Smart Account (CREATE2) address.  Falls back to ``owner`` if not set.
    chain_id : int
        Target chain (84532 = Base Sepolia, 100 = Gnosis).
    """
    if eoa_private_key is None or eoa_address is None:
        raise ValueError(
            "eoa_private_key and eoa_address are required for UserOp signing"
        )

    smart_account = sender or owner
    tombstone_bytes = encode_tombstone_protobuf(fact_id, owner)

    try:
        await build_and_send_userop(
            sender=smart_account,
            eoa_address=eoa_address,
            eoa_private_key=eoa_private_key,
            protobuf_payload=tombstone_bytes,
            relay_url=relay._relay_url,
            auth_key_hex=relay._auth_key_hex or "",
            wallet_address=smart_account,
            chain_id=chain_id,
            client_id=relay._client_id,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Phase 2 Slice 2e — pin_fact / unpin_fact
# ---------------------------------------------------------------------------


# Short-key ⇄ long-name mapping for ClaimStatus (see claims.rs)
_STATUS_SHORT_TO_LONG = {
    "a": "active",
    "s": "superseded",
    "r": "retracted",
    "c": "contradicted",
    "p": "pinned",
}


def _status_long_name(short: str | None) -> str:
    """Translate a ClaimStatus short key to its human-readable name.

    Default (missing / unrecognized) is ``"active"`` — matches the Rust
    ``ClaimStatus::default()``.
    """
    if not short:
        return "active"
    return _STATUS_SHORT_TO_LONG.get(short, "active")


async def _fetch_fact_by_id(
    fact_id: str,
    owner: str,
    relay: RelayClient,
) -> Optional[dict]:
    """Fetch a single fact's on-chain record via the subgraph.

    Returns the raw ``fact`` subgraph object or ``None`` if not found.
    The caller is responsible for checking ``owner`` matches the expected
    smart-account address.
    """
    data = await relay.query_subgraph(FACT_BY_ID_QUERY, {"id": fact_id})
    return data.get("data", {}).get("fact")


def _decrypt_and_parse_claim(
    fact: dict,
    keys: DerivedKeys,
) -> dict:
    """Decrypt a fact's encrypted blob and parse it as a Claim dict.

    Handles both canonical Claim JSON (preferred) and legacy blob shapes
    by delegating to ``totalreclaw_core.parse_claim_or_legacy`` and then
    JSON-loading the normalized output. Raises ``ValueError`` on decrypt
    failure or malformed ciphertext.
    """
    encrypted_hex = fact.get("encryptedBlob", "") or ""
    if encrypted_hex.startswith("0x"):
        encrypted_hex = encrypted_hex[2:]
    if not encrypted_hex:
        raise ValueError("fact has empty encryptedBlob")
    try:
        encrypted_b64 = base64.b64encode(bytes.fromhex(encrypted_hex)).decode("ascii")
        decrypted = decrypt(encrypted_b64, keys.encryption_key)
    except Exception as e:  # pragma: no cover — exercised by bad-cipher paths
        raise ValueError(f"failed to decrypt fact blob: {e}") from e

    # parse_claim_or_legacy always returns a canonical Claim JSON string,
    # normalizing legacy blobs into the canonical short-key shape.
    normalized_json = _core.parse_claim_or_legacy(decrypted)
    claim = _json.loads(normalized_json)
    if not isinstance(claim, dict):
        raise ValueError("parsed claim is not a JSON object")
    return claim


async def _change_claim_status(
    fact_id: str,
    target_status: str,
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    eoa_private_key: Optional[bytes],
    eoa_address: Optional[str],
    sender: Optional[str],
    chain_id: int,
    lsh_hasher: Optional[LSHHasher],
) -> dict:
    """Shared implementation for ``pin_fact`` / ``unpin_fact``.

    Semantics (Phase 2 Slice 2e-python — mirrors the MCP slice-2e tool at
    ``mcp/src/tools/pin.ts``):

    1. Fetch the existing fact by id via the subgraph.
    2. Decrypt + parse as a canonical Claim.
    3. If ``claim["st"]`` already matches ``target_status`` — return a
       ``{idempotent: True}`` result with no on-chain write.
    4. Otherwise, clone the claim, mutate ``st`` (omitting it when the
       target is the default ``"a"`` so the canonical serializer drops
       it), set ``sup`` to the previous fact id, refresh ``ea``, and
       re-canonicalize via ``totalreclaw_core.canonicalize_claim``.
    5. Encrypt, build a new ``FactPayload`` with a fresh UUID. Blind
       indices on the new fact are intentionally **empty** — retrieval
       follows the supersession chain from the old fact's search hits.
       Re-indexing is deferred to a later slice (matches MCP).
    6. Tombstone the old fact and submit the new fact as two sequential
       UserOps. (Matches MCP's batch, but Python's ``build_and_send_userop``
       doesn't currently expose a multi-call shim, so we do two calls.)

    Returns ``{success, fact_id, new_fact_id, previous_status, new_status}``
    plus ``idempotent: True`` on no-op.

    Parameters
    ----------
    target_status : {"p", "a"}
        Compact short-key for the target ``ClaimStatus``.
    """
    if not isinstance(fact_id, str) or not fact_id.strip():
        raise ValueError("fact_id must be a non-empty string")
    if eoa_private_key is None or eoa_address is None:
        raise ValueError(
            "eoa_private_key and eoa_address are required for UserOp signing"
        )
    if target_status not in ("a", "p"):
        raise ValueError(f"unsupported target_status {target_status!r}")

    fact_id = fact_id.strip()
    smart_account = sender or owner
    target_long = _status_long_name(target_status)

    # 1. Fetch
    fact = await _fetch_fact_by_id(fact_id, owner, relay)
    if not fact:
        raise ValueError(f"fact {fact_id} not found")

    # 2. Decrypt + parse
    claim = _decrypt_and_parse_claim(fact, keys)
    current_short = claim.get("st")  # may be None for default Active
    current_long = _status_long_name(current_short)

    # 3. Idempotency guard
    current_is_target = (
        (target_status == "a" and (current_short is None or current_short == "a"))
        or (target_status == "p" and current_short == "p")
    )
    if current_is_target:
        return {
            "success": True,
            "fact_id": fact_id,
            "previous_status": current_long,
            "new_status": target_long,
            "idempotent": True,
        }

    # 4. Build the new canonical claim
    new_claim = dict(claim)  # shallow clone
    if target_status == "a":
        # Active is the canonical default — omit the short key so the
        # serializer produces exactly the same bytes as an "untouched" claim.
        new_claim.pop("st", None)
    else:
        new_claim["st"] = target_status
    new_claim["sup"] = fact_id
    # Refresh the extraction timestamp so downstream consumers can tell
    # this is a new event (matches MCP pin.ts behavior).
    new_claim["ea"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    new_blob_plaintext = _core.canonicalize_claim(
        _json.dumps(new_claim, ensure_ascii=False, separators=(",", ":"))
    )

    # 5. Encrypt + build FactPayload
    encrypted_blob = encrypt(new_blob_plaintext, keys.encryption_key)
    encrypted_hex = base64.b64decode(encrypted_blob).hex()

    new_fact_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    source_tag = "python_pin" if target_status == "p" else "python_unpin"

    # Regenerate trapdoors for the new pinned fact so trapdoor-based recall
    # still surfaces it after the old fact is tombstoned.
    new_claim_text = claim.get("t") if isinstance(claim.get("t"), str) else ""
    new_entities_raw = claim.get("e") if isinstance(claim.get("e"), list) else []
    new_entity_objs: list = []
    for e in new_entities_raw:
        if isinstance(e, dict) and isinstance(e.get("n"), str):
            new_entity_objs.append({"name": e["n"], "type": e.get("tp", "concept")})

    new_word_indices: list[str] = generate_blind_indices(new_claim_text) if new_claim_text else []
    new_lsh_indices: list[str] = []
    new_encrypted_emb: Optional[str] = None
    if new_claim_text:
        try:
            new_embedding = get_embedding(new_claim_text)
            if lsh_hasher and new_embedding:
                new_lsh_indices = lsh_hasher.hash(new_embedding)
            new_encrypted_emb = encrypt_embedding(new_embedding, keys.encryption_key)
        except Exception:
            # Best-effort: if embedding fails, word + entity trapdoors still surface the claim.
            pass
    new_entity_trapdoors = compute_entity_trapdoors(new_entity_objs) if new_entity_objs else []
    new_blind_indices = new_word_indices + new_lsh_indices + new_entity_trapdoors

    payload = FactPayload(
        id=new_fact_id,
        timestamp=timestamp,
        owner=owner,
        encrypted_blob=encrypted_hex,
        blind_indices=new_blind_indices,
        # Pins are top priority (decay=1.0). Unpins revert to 1.0 too —
        # Active claims are always full-decay at write time.
        decay_score=1.0,
        source=source_tag,
        content_fp="",
        agent_id="python-client",
        encrypted_embedding=new_encrypted_emb,
    )

    # 5b. Slice 2f: if this pin/unpin overrides a prior auto-resolution,
    # append a counterexample to ``~/.totalreclaw/feedback.jsonl``. The next
    # digest-compile's tuning loop will consume it and nudge the weights.
    # Voluntary pins (no matching decision row) produce no feedback row.
    try:
        maybe_write_feedback_for_pin(
            fact_id,
            "pinned" if target_status == "p" else "active",
            int(datetime.now(timezone.utc).timestamp()),
        )
    except Exception:
        # Feedback wiring is best-effort — never block the pin operation.
        pass

    # 6. Tombstone old, then write new. Order matters: if the new write
    # fails after a successful tombstone, the caller will see the fact as
    # deleted — acceptable trade-off vs. duplicating the fact live then
    # tombstoning (which would surface both in recall on failure).
    tombstone_bytes = encode_tombstone_protobuf(fact_id, owner)
    await build_and_send_userop(
        sender=smart_account,
        eoa_address=eoa_address,
        eoa_private_key=eoa_private_key,
        protobuf_payload=tombstone_bytes,
        relay_url=relay._relay_url,
        auth_key_hex=relay._auth_key_hex or "",
        wallet_address=smart_account,
        chain_id=chain_id,
        client_id=relay._client_id,
    )

    new_protobuf = encode_fact_protobuf(payload)
    await build_and_send_userop(
        sender=smart_account,
        eoa_address=eoa_address,
        eoa_private_key=eoa_private_key,
        protobuf_payload=new_protobuf,
        relay_url=relay._relay_url,
        auth_key_hex=relay._auth_key_hex or "",
        wallet_address=smart_account,
        chain_id=chain_id,
        client_id=relay._client_id,
    )

    return {
        "success": True,
        "fact_id": fact_id,
        "new_fact_id": new_fact_id,
        "previous_status": current_long,
        "new_status": target_long,
    }


async def pin_fact(
    fact_id: str,
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    eoa_private_key: Optional[bytes] = None,
    eoa_address: Optional[str] = None,
    sender: Optional[str] = None,
    chain_id: int = 84532,
    lsh_hasher: Optional[LSHHasher] = None,
) -> dict:
    """Pin a claim so auto-resolution cannot supersede it.

    Fetches the claim by id, rewrites its canonical blob with
    ``status = pinned`` (short key ``st = "p"``), tombstones the old fact,
    and writes a new fact that references the old one via the ``sup``
    field. Idempotent: pinning an already-pinned claim returns
    ``{idempotent: True}`` with no on-chain write.

    Parameters
    ----------
    fact_id : str
        UUID of the claim to pin.
    keys : DerivedKeys
        Wallet-derived keys for encryption + content fingerprinting.
    owner : str
        Smart Account address (used for the fact's ``owner`` field).
    relay : RelayClient
        Configured relay client for GraphQL + UserOp submission.
    eoa_private_key : bytes
        32-byte EOA private key for UserOp signing.
    eoa_address : str
        EOA address that owns the Smart Account.
    sender : str, optional
        Smart Account (CREATE2) address. Falls back to ``owner`` if unset.
    chain_id : int
        Target chain (84532 = Base Sepolia, 100 = Gnosis).
    lsh_hasher : LSHHasher, optional
        Reserved for a later slice. Phase 2 Slice 2e-python deliberately
        leaves the new fact's blind indices empty so the Python and
        TypeScript paths stay in lockstep; retrieval follows the
        supersession chain instead. Accepted for forward compatibility.

    Returns
    -------
    dict
        ``{success, fact_id, new_fact_id, previous_status, new_status}``
        where ``fact_id`` is the original (now tombstoned) id and
        ``new_fact_id`` is the replacement carrying the pinned status.
        On no-op: ``{success, fact_id, previous_status, new_status, idempotent: True}``
        with no ``new_fact_id`` field.
    """
    return await _change_claim_status(
        fact_id=fact_id,
        target_status="p",
        keys=keys,
        owner=owner,
        relay=relay,
        eoa_private_key=eoa_private_key,
        eoa_address=eoa_address,
        sender=sender,
        chain_id=chain_id,
        lsh_hasher=lsh_hasher,
    )


async def unpin_fact(
    fact_id: str,
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    eoa_private_key: Optional[bytes] = None,
    eoa_address: Optional[str] = None,
    sender: Optional[str] = None,
    chain_id: int = 84532,
    lsh_hasher: Optional[LSHHasher] = None,
) -> dict:
    """Inverse of :func:`pin_fact` — move a pinned claim back to ``active``.

    Same supersession flow: tombstone the old fact, write a new fact with
    the ``st`` field omitted (since Active is the canonical default) and
    ``sup`` set to the previous fact id. Idempotent on already-active claims.
    """
    return await _change_claim_status(
        fact_id=fact_id,
        target_status="a",
        keys=keys,
        owner=owner,
        relay=relay,
        eoa_private_key=eoa_private_key,
        eoa_address=eoa_address,
        sender=sender,
        chain_id=chain_id,
        lsh_hasher=lsh_hasher,
    )


async def export_facts(
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    page_size: int = 1000,
) -> list[dict]:
    """Export all active facts, decrypted."""
    # Normalize owner address to lowercase for subgraph Bytes! type
    owner = owner.lower() if owner else owner

    results: list[dict] = []
    skip = 0

    while True:
        try:
            data = await relay.query_subgraph(
                EXPORT_QUERY,
                {"owner": owner, "first": page_size, "skip": skip},
            )
            facts = data.get("data", {}).get("facts", [])
            if not facts:
                break

            for fact in facts:
                try:
                    encrypted_hex = fact.get("encryptedBlob", "")
                    if encrypted_hex.startswith("0x"):
                        encrypted_hex = encrypted_hex[2:]
                    encrypted_b64 = base64.b64encode(
                        bytes.fromhex(encrypted_hex)
                    ).decode("ascii")
                    decrypted_blob = decrypt(encrypted_b64, keys.encryption_key)
                    if is_digest_blob(decrypted_blob):
                        continue
                    doc = read_claim_from_blob(decrypted_blob)
                    text = doc["text"]

                    # Prefer createdAt (per-fact client timestamp) over
                    # timestamp (block time). Both are BigInt strings from
                    # the subgraph representing Unix seconds.
                    raw_ts = fact.get("createdAt") or fact.get("timestamp") or ""
                    formatted_ts = ""
                    if raw_ts:
                        try:
                            ts_int = int(str(raw_ts))
                            formatted_ts = datetime.fromtimestamp(
                                ts_int, tz=timezone.utc
                            ).strftime("%Y-%m-%d %H:%M:%S UTC")
                        except (ValueError, OSError):
                            formatted_ts = str(raw_ts)

                    results.append(
                        {
                            "id": fact["id"],
                            "text": text,
                            "timestamp": formatted_ts,
                            "importance": float(fact.get("decayScore", "0.5")),
                        }
                    )
                except Exception:
                    continue

            if len(facts) < page_size:
                break
            skip += page_size
        except Exception:
            break

    return results
