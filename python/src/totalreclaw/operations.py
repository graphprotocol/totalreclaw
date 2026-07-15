"""
TotalReclaw Store and Search Operations.

Combines crypto, protobuf, relay, LSH, reranker, and ERC-4337 UserOp
construction into high-level operations.
"""
from __future__ import annotations
import base64
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Retrieval v2 Tier 1 source-weighting default. OFF as of 2026-06-08: the
# agent-experienced benchmark (core 2.4.0) showed it tie-or-worse on the shipped
# retrieval path (recall is already saturated, so provenance reweighting only
# reorders). See totalreclaw-internal docs/benchmarks/2026-06-07-agent-experienced-baseline.md.
APPLY_SOURCE_WEIGHTS_DEFAULT = False

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
from .userop import build_and_send_userop, build_and_send_userop_batch, MAX_BATCH_SIZE
from .claims_helper import (
    PROTOBUF_VERSION_V4,
    build_canonical_claim_v1,
    compute_entity_trapdoor,
    compute_entity_trapdoors,
    is_digest_blob,
    is_stub_blob_hex,
    read_blob_unified,
    read_claim_from_blob,
)
from .entity_extract import extract_query_entities
# Taxonomy vocabulary comes from the dependency-free leaf module, NOT
# ``agent.extraction`` — importing the latter here would make the root package
# (``operations`` loads during ``totalreclaw`` bootstrap via ``client``) depend
# on the whole ``agent`` subpackage, the exact root<->agent cycle the leaf was
# extracted to break.
from .memory_types import V0_TO_V1_TYPE, VALID_MEMORY_TYPES

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


@dataclass(frozen=True)
class WalletContext:
    """The stable wallet-identity cluster shared by every on-chain write.

    Groups the credentials + routing that stay constant across all facts in
    a store call — they identify the *wallet and chain*, not the fact
    content: the derived crypto keys, the Smart Account ``owner``, the EOA
    signing pair, an optional distinct ``sender`` Smart Account, and the
    target ``chain_id``.

    Passed as a single argument to :func:`store_fact` / :func:`store_fact_batch`
    so those signatures carry the per-fact content/taxonomy fields
    explicitly and the wallet identity as one cohesive unit, rather than
    interleaving ~6 identity params among the content ones.
    """

    keys: DerivedKeys
    owner: str
    eoa_private_key: Optional[bytes] = None
    eoa_address: Optional[str] = None
    sender: Optional[str] = None
    chain_id: int = 84532

    @property
    def smart_account(self) -> str:
        """The Smart Account to submit the UserOp from.

        Falls back to ``owner`` when a distinct ``sender`` is not set —
        matches the ``sender or owner`` convention the store paths used
        before this cluster was extracted.
        """
        return self.sender or self.owner


class ForgetResult(dict):
    """The result of :meth:`~totalreclaw.client.TotalReclaw.forget`.

    Shares the ``{"success", "fact_id"}`` mapping shape of the sibling
    mutations (``pin_fact`` / ``unpin_fact`` / ``retype`` / ``set_scope``,
    which all return dicts) while staying truthiness-compatible with the
    historic ``bool`` return: ``bool(result)`` is the success flag. Callers
    doing ``if await client.forget(id): ...`` keep working, and new callers
    can read ``result["success"]`` / ``result["fact_id"]`` like the
    siblings.
    """

    def __bool__(self) -> bool:  # noqa: D401 - trivial
        return bool(self.get("success", False))


async def store_fact(
    text: str,
    wallet: WalletContext,
    relay: RelayClient,
    lsh_hasher: Optional[LSHHasher] = None,
    embedding: Optional[list[float]] = None,
    importance: float = 0.5,
    source: str = "python-client",
    agent_id: str = "python-client",
    fact_type: str = "claim",
    entities: Optional[list] = None,
    confidence: float = 0.85,
    extracted_at: Optional[str] = None,
    # v1 taxonomy fields (default path as of totalreclaw 2.0.0)
    provenance: str = "user",
    scope: str = "unspecified",
    reasoning: Optional[str] = None,
    volatility: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
    agent_name: Optional[str] = None,
    data_edge_address: Optional[str] = None,
) -> str:
    """Encrypt and store a fact on-chain via relay.

    As of ``totalreclaw`` 2.0.0 this unconditionally emits a Memory Taxonomy
    v1 JSON blob and tags the outer protobuf wrapper with
    :data:`PROTOBUF_VERSION_V4`. Legacy v0 tokens in ``fact_type`` (e.g.
    ``"fact"``, ``"decision"``, ``"episodic"``) are coerced to v1 via the
    v0→v1 map inside :func:`build_canonical_claim_v1`.

    Parameters
    ----------
    provenance : str, default "user"
        v1 source field — one of user | user-inferred | assistant | external |
        derived. The explicit ``client.remember()`` path defaults to
        ``"user"`` because the caller explicitly typed the text. Auto-
        extraction should pass the source tagged by ``apply_provenance_filter_lax``.
    scope : str, default "unspecified"
        v1 life-domain scope.
    reasoning : str, optional
        "because Y" clause for decision-style claims (v1 claim type).
    volatility : str, optional
        Post-extraction rescored volatility; omitted on explicit tool writes.
    agent_name : str, optional
        Issue #317 — user-given name of *this agent instance* (e.g. "John").
        Persisted in the encrypted blob as additive provenance so a memory
        can render "John (Hermes)". When ``None`` (the default) the store
        path resolves it from ``TOTALRECLAW_AGENT_NAME``; if that is also
        unset the ``agent_name`` key is omitted entirely and the write is
        byte-identical to pre-#317 behaviour.

    Returns
    -------
    str
        The UUID assigned to the stored fact.
    """
    keys = wallet.keys
    owner = wallet.owner
    if wallet.eoa_private_key is None or wallet.eoa_address is None:
        raise ValueError(
            "eoa_private_key and eoa_address are required for UserOp signing"
        )

    smart_account = wallet.smart_account
    fact_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Accept either a 0-1 float (scaled to 1-10) or an already-1-10 value,
    # then clamp into the 1-10 band.
    importance_scaled = int(round(importance * 10)) if importance <= 1 else int(importance)
    importance_int = max(1, min(10, importance_scaled))

    # v1 canonical claim (unconditional). source_agent is carried as
    # ``provenance`` — the v1 `source` field on the claim itself.
    fact_stub = {
        "text": text,
        "type": fact_type,
        "source": provenance,
        "scope": scope,
        "reasoning": reasoning,
        "confidence": confidence,
        "entities": entities,
        "volatility": volatility,
    }
    blob_plaintext = build_canonical_claim_v1(
        fact_stub,
        importance=importance_int,
        created_at=extracted_at or timestamp,
        claim_id=fact_id,
        extra_metadata=extra_metadata,
        # Issue #317 — resolved (explicit arg > TOTALRECLAW_AGENT_NAME > None)
        # inside build_canonical_claim_v1; None omits the key entirely.
        agent_name=agent_name,
    )

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

    # Build protobuf payload — v1 writes ALWAYS tag the outer wrapper v4.
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
        version=PROTOBUF_VERSION_V4,
    )

    protobuf_bytes = encode_fact_protobuf(payload)

    # Build and submit a proper ERC-4337 UserOperation
    await build_and_send_userop(
        sender=smart_account,
        eoa_address=wallet.eoa_address,
        eoa_private_key=wallet.eoa_private_key,
        protobuf_payload=protobuf_bytes,
        relay_url=relay.relay_url,
        auth_key_hex=relay.auth_key_hex or "",
        wallet_address=smart_account,
        chain_id=wallet.chain_id,
        client_id=relay.client_id,
        session_id=relay.session_id,
        data_edge_address=data_edge_address,
    )

    return fact_id


# ---------------------------------------------------------------------------
# Hermes parity Gap 3 — batched writes (v2.2.0)
# ---------------------------------------------------------------------------


# ── internal#448: shared byte-capped batching + halve-on-simfail ────────────
# Hoisted VERBATIM from imports/engine.py (rc4 / internal#435) so EVERY
# on-chain write caller — client.remember_batch (auto-extraction,
# recrystallize, import) — inherits the same Pimlico executeBatch
# calldata-size protection. The import engine re-exports the legacy
# underscored names (_estimate_payload_bytes / _group_payloads_by_size /
# _MAX_BATCH_BYTES / IMPORT_MAX_BATCH_SIZE) for back-compat with its tests.

#: Per-blind-index wire cost: a 64-hex-char SHA-256 string field (key + len +
#: 64 bytes ≈ 66; 68 for margin). Blind indices DOMINATE a fact's calldata and
#: scale with the UNIQUE (stemmed) token count, not the char count — the review
#: of PR #461 measured real ``encode_fact_protobuf`` output at ~2× a char-linear
#: estimate for representative prose (~88 indices for a 300-char fact once stems
#: are counted), so we bound the real term by COMPUTING the index count.
_BYTES_PER_BLIND_INDEX = 68
#: Encrypted 640-dim f32 embedding: 2560B packed → base64 (~3416) → XChaCha20
#: ciphertext → base64 string ≈ 4608B on the wire (field 13). Plus 20 LSH
#: bucket indices (one per table). Measured 4611 + 20×66 = 5931; 6060 for margin.
_BYTES_PER_EMBEDDING = 4700 + 20 * _BYTES_PER_BLIND_INDEX
#: Scalar-field + cipher overhead floor (id, timestamp, owner, content_fp,
#: version, decay/is_active, XChaCha20 nonce+tag on the claim blob, ABI slop).
_BYTES_FIXED_OVERHEAD = 620

#: rc4 (internal#435): estimated on-chain calldata-byte ceiling per batched
#: UserOp. Kept comfortably under the observed ~85KB sim-revert cliff (a
#: sim-passing ~67KB op at 15 facts still didn't reliably get INCLUDED on the
#: staging bundler either, so 32KB buys inclusion headroom too). Groups flush
#: when adding the next fact would exceed EITHER this or the count ceiling.
MAX_BATCH_BYTES: int = 32_000

#: Belt-and-braces count ceiling alongside :data:`MAX_BATCH_BYTES`. The byte
#: cap is the real governor; 15 is the conservative upper bound
#: (``userop.MAX_BATCH_SIZE`` stays 30 — 15 ≤ 30, so the core still accepts
#: every group). Hoisted from the import-only ``IMPORT_MAX_BATCH_SIZE`` so all
#: callers share it.
MAX_BATCH_GROUP_COUNT: int = 15


def estimate_payload_bytes(payload: dict) -> int:
    """Estimate a single fact's on-chain executeBatch calldata contribution.

    Bounds the REAL ``encode_fact_protobuf`` output (verified measured-safe,
    ``est ≥ real``, in ``test_batch_sizing_rc4.py``). Terms:

      * fixed overhead (scalar fields + cipher nonce/tag) — ``_BYTES_FIXED_OVERHEAD``;
      * the encrypted claim blob ≈ ciphertext of ``text`` + ``extra_metadata``;
      * the blind indices — the dominant, entropy-dependent term — bounded by
        the ACTUAL ``generate_blind_indices(text)`` count × per-index wire cost
        (a char-linear estimate cannot bound this, hence the PR #461 review
        NO-GO);
      * the encrypted embedding + its LSH indices when an embedding is present.

    ``generate_blind_indices`` is a fast pure Rust call and import is not a
    latency-critical path (the same call happens again during the real store),
    so recomputing it here is cheap. Falls back to a conservative char-based
    index count if the core module is unavailable.
    """
    text = payload.get("text") or ""
    # The encrypted claim blob stores the canonical-claim JSON as UTF-8 BYTES
    # (build_canonical_claim_v1 uses ensure_ascii=False), so the blob term must
    # count UTF-8 bytes, not code points — CJK ≈3B/char, emoji ≈4B/cp (PR #461
    # re-review Finding A). ASCII is unaffected (len == utf-8 length).
    est = _BYTES_FIXED_OVERHEAD + len(text.encode("utf-8"))

    meta = payload.get("extra_metadata")
    if isinstance(meta, dict) and meta:
        # Crystal facts carry key_outcomes / open_threads / topics in the
        # encrypted blob — count them (1.5× for JSON + cipher slack).
        est += int(len(_json.dumps(meta)) * 1.5)

    try:
        n_indices = len(generate_blind_indices(text))
    except Exception:
        # Conservative fallback: ~2 stemmed indices per ~6-char word.
        n_indices = int(len(text) / 3)
    est += n_indices * _BYTES_PER_BLIND_INDEX

    if payload.get("embedding"):
        est += _BYTES_PER_EMBEDDING
    return est


def group_payloads_by_size(payloads: list, max_count: int, max_bytes: int):
    """Yield successive batch groups respecting BOTH a count and a byte cap.

    A group is flushed before appending a fact whose addition would exceed
    either cap. A single fact larger than ``max_bytes`` still forms its own
    group (never dropped) — the adaptive halving in the store loop is the
    backstop if such a lone op still sim-reverts.
    """
    group: list = []
    group_bytes = 0
    for p in payloads:
        est = estimate_payload_bytes(p)
        if group and (len(group) >= max_count or group_bytes + est > max_bytes):
            yield group
            group = []
            group_bytes = 0
        group.append(p)
        group_bytes += est
    if group:
        yield group


async def _store_group_adaptive(store_fn, group: list) -> tuple[list[str], list[str]]:
    """Store one payload group via ``store_fn``; halve-and-retry on a
    simulation-size revert.

    rc4 (internal#435): Pimlico surfaces an oversized-``executeBatch``
    simulation failure as the catch-all ``-32500 "... reverted during
    simulation ..."``. When a group of >1 fact hits it, split in half and
    retry each half recursively (floor 1). This makes the writer adaptive to
    wherever a given bundler's calldata-size cliff actually sits, on top of
    the static byte cap. A duplicate rejection is swallowed (no ids, no
    error); any other error — or a size revert at the single-fact floor — is
    surfaced into the returned error list so the batch is counted FAILED, not
    stored.

    ``store_fn`` is an ``async (group: list[dict]) -> list[str]`` (the caller's
    single-group store — e.g. a closure over :func:`store_fact_batch`, or the
    client's ``remember_batch``). Returns ``(stored_ids, errors)``.

    A ``-32500`` that is an AA25 (exhausted the userop-layer retry) is NOT a
    size revert — halving it would be pointless, so any AA25-tagged error is
    excluded from the size path (PR #461 review Finding 2).
    """
    try:
        ids = await store_fn(group)
        return list(ids), []
    except Exception as e:
        msg = str(e)
        if '409' in msg or 'duplicate' in msg or 'fingerprint' in msg:
            logger.debug("Batch of %d facts rejected as duplicate", len(group))
            return [], []
        msg_l = msg.lower()
        is_sim_revert = (
            "reverted during simulation" in msg_l
            or ("-32500" in msg and "AA25" not in msg)
        )
        if is_sim_revert and len(group) > 1:
            mid = len(group) // 2
            logger.warning(
                "Batch sim revert on %d facts (%s) — splitting %d/%d and "
                "retrying each half",
                len(group), msg[:120], mid, len(group) - mid,
            )
            ids1, errs1 = await _store_group_adaptive(store_fn, group[:mid])
            ids2, errs2 = await _store_group_adaptive(store_fn, group[mid:])
            return ids1 + ids2, errs1 + errs2
        # Single-fact floor (can't split further) or a non-size error —
        # surface it so the fact is counted failed rather than silently dropped.
        return [], [f"Batch store failed ({len(group)} facts): {msg}"]


async def group_and_store_adaptive(
    store_fn,
    payloads: list,
    max_count: int,
    max_bytes: int,
    *,
    max_errors: Optional[int] = None,
    error_limit_msg: Optional[str] = None,
) -> tuple[list[str], list[str]]:
    """Single source of truth for byte-capped batching + halve-on-simfail.

    Groups ``payloads`` by BOTH ``max_count`` and ``max_bytes`` (via
    :func:`group_payloads_by_size`), then stores each group through
    ``store_fn`` with adaptive halve-on-sim-revert (via
    :func:`_store_group_adaptive`). Returns ``(stored_ids, errors)`` — the ids
    of every successfully stored fact (in input order) and a list of
    per-group error strings (empty on full success).

    Used by :meth:`totalreclaw.TotalReclaw.remember_batch` (so auto-extraction,
    recrystallize, and ad-hoc callers all inherit the protection) and by the
    import engine (which passes ``client.remember_batch`` as ``store_fn`` so
    its existing per-group mock contract — and the rc4 invariant tests — are
    preserved unchanged).

    ``max_errors`` / ``error_limit_msg`` optional cap the collected error
    list (the import engine's 20-error safety valve); when reached, the
    remaining groups are skipped after appending ``error_limit_msg``.
    """
    all_ids: list[str] = []
    all_errors: list[str] = []
    for group in group_payloads_by_size(payloads, max_count, max_bytes):
        ids, errs = await _store_group_adaptive(store_fn, group)
        all_ids.extend(ids)
        if errs:
            all_errors.extend(errs)
            if max_errors is not None and len(all_errors) >= max_errors:
                if error_limit_msg:
                    all_errors.append(error_limit_msg)
                break
    return all_ids, all_errors


async def store_fact_batch(
    facts: list[dict],
    wallet: WalletContext,
    relay: RelayClient,
    lsh_hasher: Optional[LSHHasher] = None,
    source: str = "python-client",
    agent_id: str = "python-client",
    data_edge_address: Optional[str] = None,
) -> list[str]:
    """Encrypt and store N facts in a single batched on-chain UserOp.

    The Python analogue of the TS ``storeFactsBatch`` path. Mirrors
    :func:`store_fact` per-fact (same encryption, same trapdoor
    generation, same canonical v1 JSON claim + protobuf v4 wrapper),
    then wraps all N encoded protobuf payloads into a single
    ``executeBatch`` UserOperation via
    :func:`totalreclaw.userop.build_and_send_userop_batch`.

    Each element of ``facts`` is a dict in the shape produced by
    :class:`totalreclaw.TotalReclaw.remember_batch`::

        {
            "text": str,                      # required
            "importance": float,              # optional, default 0.5
            "embedding": list[float],         # optional
            "fact_type": str,                 # default "claim"
            "entities": list,                 # optional
            "confidence": float,              # default 0.85
            "provenance": str,                # default "user"
            "scope": str,                     # default "unspecified"
            "reasoning": str,                 # optional
            "volatility": str,                # optional
            "extracted_at": str,              # optional ISO timestamp
            "agent_name": str,                # optional (#317); None→env
        }

    Parameters
    ----------
    facts : list[dict]
        1..``MAX_BATCH_SIZE`` (15) fact dicts. Empty or oversized lists
        raise ``ValueError``.
    wallet, relay, lsh_hasher, source, agent_id
        Same semantics as :func:`store_fact`. All facts share the one
        :class:`WalletContext` (signing credentials, owner, and chain),
        relay config — batches do not support mixed owners.

    Returns
    -------
    list[str]
        Pre-assigned UUID fact IDs, one per input fact, in the same
        order as ``facts``. The IDs are stamped into each protobuf
        payload before submission so the subgraph indexes them under
        the same IDs this function returns. Subsequent
        ``client.forget(id)`` / ``client.pin_fact(id)`` calls therefore
        work against these IDs immediately — you don't need to wait
        for any per-fact userOpHash round-trip.

    Raises
    ------
    ValueError
        If ``facts`` is empty, larger than ``MAX_BATCH_SIZE``, missing
        EOA signing credentials, or contains an entry with no/empty
        ``text``.
    RuntimeError
        Propagated from :func:`build_and_send_userop_batch` on
        paymaster or bundler rejection.
    """
    if not facts:
        raise ValueError("store_fact_batch: at least one fact is required")
    if len(facts) > MAX_BATCH_SIZE:
        raise ValueError(
            f"store_fact_batch: batch size {len(facts)} exceeds maximum "
            f"of {MAX_BATCH_SIZE}"
        )
    if wallet.eoa_private_key is None or wallet.eoa_address is None:
        raise ValueError(
            "eoa_private_key and eoa_address are required for UserOp signing"
        )

    keys = wallet.keys
    owner = wallet.owner
    smart_account = wallet.smart_account
    shared_timestamp = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    fact_ids: list[str] = []
    protobuf_payloads: list[bytes] = []

    for idx, raw in enumerate(facts):
        if not isinstance(raw, dict):
            raise ValueError(
                f"store_fact_batch: fact at index {idx} is not a dict"
            )
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                f"store_fact_batch: fact at index {idx} has empty/missing text"
            )

        importance_raw = raw.get("importance", 0.5)
        # Accept either a 1-10 int or a 0-1 float — identical to
        # ``store_fact``'s coercion logic.
        importance_int = max(
            1,
            min(
                10,
                int(round(importance_raw * 10))
                if importance_raw <= 1
                else int(importance_raw),
            ),
        )

        fact_id = str(uuid.uuid4())
        fact_ids.append(fact_id)

        embedding = raw.get("embedding")
        fact_type = raw.get("fact_type") or "claim"
        entities = raw.get("entities")
        confidence = raw.get("confidence", 0.85)
        provenance = raw.get("provenance", "user")
        scope = raw.get("scope", "unspecified")
        reasoning = raw.get("reasoning")
        volatility = raw.get("volatility")
        extracted_at = raw.get("extracted_at")
        # Per-fact typed metadata (#356): session_id + import_source for
        # imports, Crystal fields for session-summary claims. Threaded into the
        # canonical claim's ``metadata`` exactly like the single-fact
        # ``store_fact`` path; absent for normal writes.
        extra_metadata = raw.get("extra_metadata")
        # Issue #317 — per-fact agent-instance name; None falls back to
        # TOTALRECLAW_AGENT_NAME inside build_canonical_claim_v1.
        agent_name = raw.get("agent_name")

        # v1 canonical claim — identical shape to ``store_fact``.
        fact_stub = {
            "text": text,
            "type": fact_type,
            "source": provenance,
            "scope": scope,
            "reasoning": reasoning,
            "confidence": confidence,
            "entities": entities,
            "volatility": volatility,
        }
        blob_plaintext = build_canonical_claim_v1(
            fact_stub,
            importance=importance_int,
            created_at=extracted_at or shared_timestamp,
            claim_id=fact_id,
            extra_metadata=extra_metadata,
            agent_name=agent_name,
        )

        encrypted_blob = encrypt(blob_plaintext, keys.encryption_key)
        encrypted_hex = base64.b64decode(encrypted_blob).hex()

        word_indices = generate_blind_indices(text)

        lsh_indices: list[str] = []
        if lsh_hasher and embedding:
            lsh_indices = lsh_hasher.hash(embedding)

        entity_trapdoors = (
            compute_entity_trapdoors(entities) if entities else []
        )

        all_indices = word_indices + lsh_indices + entity_trapdoors

        content_fp = generate_content_fingerprint(text, keys.dedup_key)

        encrypted_emb: Optional[str] = None
        if embedding:
            encrypted_emb = encrypt_embedding(embedding, keys.encryption_key)

        payload = FactPayload(
            id=fact_id,
            timestamp=shared_timestamp,
            owner=owner,
            encrypted_blob=encrypted_hex,
            blind_indices=all_indices,
            decay_score=importance_raw if importance_raw <= 1 else importance_raw / 10.0,
            source=source,
            content_fp=content_fp,
            agent_id=agent_id,
            encrypted_embedding=encrypted_emb,
            version=PROTOBUF_VERSION_V4,
        )

        protobuf_payloads.append(encode_fact_protobuf(payload))

    # One UserOp for all N facts — ~8s regardless of N, vs 15 × ~4s
    # sequential = 60s for the single-fact path.
    await build_and_send_userop_batch(
        sender=smart_account,
        eoa_address=wallet.eoa_address,
        eoa_private_key=wallet.eoa_private_key,
        protobuf_payloads=protobuf_payloads,
        relay_url=relay.relay_url,
        auth_key_hex=relay.auth_key_hex or "",
        wallet_address=smart_account,
        chain_id=wallet.chain_id,
        client_id=relay.client_id,
        session_id=relay.session_id,
        data_edge_address=data_edge_address,
    )

    return fact_ids


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

    # Entity trapdoors -- orthogonal exact-match signal (#370 read-side).
    # Same hash fn as the write side (claims_helper.compute_entity_trapdoor)
    # so query entities match stored entity-trapdoors byte-for-byte. Heuristic
    # extraction (entity_extract.extract_query_entities); err toward recall --
    # rerank filters false positives. NOTE: widens all_trapdoors (a few extra
    # chunks, larger candidate pool); p95 + pool-cap validated in Task 6.
    entity_trapdoors: list[str] = []
    for _name in extract_query_entities(query):
        _td = compute_entity_trapdoor(_name)
        if _td not in entity_trapdoors:
            entity_trapdoors.append(_td)

    # Combine all trapdoors
    all_trapdoors = word_trapdoors + lsh_trapdoors + entity_trapdoors

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
    skipped_stub_ids: list[str] = []
    for fact_id, fact in all_facts.items():
        try:
            encrypted_blob_hex = fact.get("encryptedBlob", "")
            # Stub / tombstone blobs (encryptedBlob == "0x00" or all-zero
            # hex) are supersede markers the relay writes when a fact is
            # auto-resolved.  Decrypting them always fails with
            # "Encrypted data too short" and pollutes recall logs with a
            # WARN per stale ID per recall — observed on legacy QA vault
            # with ~9 stubs from rc-era pin/supersede flows.  Filter
            # pre-decrypt; emit a single aggregate INFO at the end.
            #
            # Mirrors ``isStubBlob`` in ``skill/plugin/digest-sync.ts``.
            if is_stub_blob_hex(encrypted_blob_hex):
                skipped_stub_ids.append(fact_id)
                continue
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
                except Exception as e:
                    logger.debug("re-embed failed for candidate text %r: %s", text[:40], e)
                    emb = None  # degrade: candidate ranks on BM25 only

            decay = float(fact.get("decayScore", "0.5"))

            ts = fact.get("timestamp", "")
            created_at = 0.0
            if ts:
                try:
                    created_at = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    pass

            # Surface the v1 source from decrypted blob metadata so Tier 1
            # source-weighted reranking can multiply the fused score.
            # Legacy v0 blobs have no v1 source — set None so the reranker
            # applies LEGACY_CLAIM_FALLBACK_WEIGHT (0.85).
            meta = doc.get("metadata", {}) if isinstance(doc.get("metadata"), dict) else {}
            candidate_source: Optional[str] = meta.get("source") if isinstance(meta.get("source"), str) else None

            # #425 — carry a filtered provenance subset through the reranker
            # so recall output can expose where a memory came from (imports
            # tag import_source + session_id since #356/#363).
            # #317 — ``agent_name`` rides the same passthrough so recall can
            # render "John (Hermes)".
            extra_meta = {
                k: meta[k]
                for k in ("import_source", "session_id", "subtype", "agent_name")
                if isinstance(meta.get(k), str) and meta.get(k)
            } or None

            candidates.append(
                RerankerCandidate(
                    id=fact_id,
                    text=text,
                    embedding=emb,
                    importance=decay,
                    created_at=created_at,
                    category=doc.get("category", "fact"),
                    source=candidate_source,
                    metadata=extra_meta,
                )
            )
        except Exception as e:
            logger.warning("Failed to decrypt candidate %s: %s", fact_id, e)
            continue

    if skipped_stub_ids:
        # Aggregate INFO instead of per-fact WARN spam.  Truncate to keep
        # log lines reasonable when a vault has many tombstones.
        sample = ", ".join(skipped_stub_ids[:5])
        suffix = f" (+{len(skipped_stub_ids) - 5} more)" if len(skipped_stub_ids) > 5 else ""
        logger.info(
            "Skipped %d stub/tombstone blob(s) during recall: %s%s",
            len(skipped_stub_ids),
            sample,
            suffix,
        )

    if not candidates:
        return []

    # Retrieval v2 Tier 1 source-weighting: OFF by default as of 2026-06-08
    # (benchmark showed tie-or-worse; see APPLY_SOURCE_WEIGHTS_DEFAULT).
    return rerank(query, query_embedding, candidates, top_k=top_k, apply_source_weights=APPLY_SOURCE_WEIGHTS_DEFAULT)


async def forget_fact(
    fact_id: str,
    owner: str,
    relay: RelayClient,
    eoa_private_key: Optional[bytes] = None,
    eoa_address: Optional[str] = None,
    sender: Optional[str] = None,
    chain_id: int = 84532,
    data_edge_address: Optional[str] = None,
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
            relay_url=relay.relay_url,
            auth_key_hex=relay.auth_key_hex or "",
            wallet_address=smart_account,
            chain_id=chain_id,
            client_id=relay.client_id,
            session_id=relay.session_id,
            data_edge_address=data_edge_address,
        )
        # Read-after-write: wait until the subgraph has flipped the fact's
        # isActive bit. Forget keeps its bool return contract — a False here
        # would mean the chain write itself failed, which is NOT what a
        # confirm-indexed timeout signals. We log the timeout for
        # observability but still return True (chain write succeeded).
        from .confirm_indexed import confirm_indexed as _confirm_indexed
        try:
            indexed = await _confirm_indexed(fact_id, relay, expect="inactive")
            if not indexed:
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "forget_fact: chain write succeeded for %s but subgraph "
                    "indexer did not confirm tombstone within timeout; "
                    "follow-up recall/export may briefly show stale state",
                    fact_id,
                )
        except Exception:
            # Confirm helper failures must NEVER mask a successful chain write.
            pass
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


# v0 short-key category → v0 memory-type token. Inverse of
# ``TYPE_TO_CATEGORY_V0`` in ``claims_helper.py``. Used by the pin/unpin
# path to upgrade a legacy short-key source blob into a v1 ``type`` field
# on the fly — mirrors ``skill/plugin/pin.ts::projectToV1``.
_V0_CATEGORY_TO_V0_TYPE: dict[str, str] = {
    "fact": "fact",
    "pref": "preference",
    "dec": "decision",
    "epi": "episodic",
    "goal": "goal",
    "ctx": "context",
    "sum": "summary",
    "rule": "rule",
    "ent": "fact",   # entity records fall back to "fact" on round-trip
    "dig": "summary",
    "claim": "claim",
}


def _project_source_to_v1(
    source_claim: dict, default_source_agent: str,
) -> dict:
    """Project a decrypted claim (v0 short-key OR v1 long-form) into the
    v1-shape dict needed to drive :func:`build_canonical_claim_v1`.

    Mirrors ``skill/plugin/pin.ts::projectToV1`` function-for-function so
    a Python-produced pinned blob is byte-equivalent to a plugin-produced
    pinned blob for the same input fact. The pin path in 2.2.2 always
    emits a v1.1 blob regardless of what the existing fact's blob shape
    was; v0 sources are upgraded on the fly per the legacy-mapping table.

    Unknown or missing fields fall back to defensible defaults:
      * ``source`` → ``"user-inferred"`` (Tier 1 reranker doesn't grant
        these the "user" trust boost; correct for legacy blobs with no
        explicit provenance signal)
      * ``type`` → ``"claim"``
      * ``importance`` → 5
      * ``confidence`` → 0.85

    Returns a dict with keys matching the :func:`build_canonical_claim_v1`
    attribute names.
    """
    # v1 source: schema_version "1.x" + long-form fields.
    schema_version = source_claim.get("schema_version")
    if (
        isinstance(source_claim.get("text"), str)
        and isinstance(source_claim.get("type"), str)
        and isinstance(schema_version, str)
        and schema_version.startswith("1.")
    ):
        v1_type = source_claim["type"]
        if v1_type not in VALID_MEMORY_TYPES:
            v1_type = "claim"
        imp_raw = source_claim.get("importance")
        try:
            importance = int(imp_raw) if isinstance(imp_raw, (int, float)) else 5
        except (ValueError, TypeError):
            importance = 5
        importance = max(1, min(10, importance))
        conf_raw = source_claim.get("confidence")
        try:
            confidence = float(conf_raw) if isinstance(conf_raw, (int, float)) else 0.85
        except (ValueError, TypeError):
            confidence = 0.85
        raw_source = source_claim.get("source")
        v1_source = raw_source if isinstance(raw_source, str) and raw_source else "user-inferred"
        raw_scope = source_claim.get("scope")
        v1_scope = raw_scope if isinstance(raw_scope, str) and raw_scope else None
        raw_volatility = source_claim.get("volatility")
        v1_volatility = raw_volatility if isinstance(raw_volatility, str) and raw_volatility else None
        raw_reasoning = source_claim.get("reasoning")
        v1_reasoning = raw_reasoning if isinstance(raw_reasoning, str) and raw_reasoning else None
        v1_entities = source_claim.get("entities") if isinstance(source_claim.get("entities"), list) else None
        return {
            "text": source_claim["text"],
            "type": v1_type,
            "source": v1_source,
            "scope": v1_scope,
            "volatility": v1_volatility,
            "reasoning": v1_reasoning,
            "entities": v1_entities,
            "importance": importance,
            "confidence": confidence,
        }

    # v0 short-key source: {t, c, cf, i, sa, ea, ...} → upgrade to v1.
    text = source_claim.get("t") if isinstance(source_claim.get("t"), str) else ""
    v0_category = source_claim.get("c") if isinstance(source_claim.get("c"), str) else "fact"
    v0_type_token = _V0_CATEGORY_TO_V0_TYPE.get(v0_category, "fact")
    v1_type = V0_TO_V1_TYPE.get(v0_type_token, "claim")
    imp_raw = source_claim.get("i")
    try:
        importance = int(imp_raw) if isinstance(imp_raw, (int, float)) else 5
    except (ValueError, TypeError):
        importance = 5
    importance = max(1, min(10, importance))
    cf_raw = source_claim.get("cf")
    try:
        confidence = float(cf_raw) if isinstance(cf_raw, (int, float)) else 0.85
    except (ValueError, TypeError):
        confidence = 0.85

    # v0 `sa` was "source agent" (e.g. "openclaw-plugin"), not v1 provenance.
    # Heuristic upgrade — matches ``projectToV1`` in the TS plugin.
    sa = source_claim.get("sa") if isinstance(source_claim.get("sa"), str) else default_source_agent
    sa_lower = sa.lower() if isinstance(sa, str) else ""
    if "derived" in sa_lower or "digest" in sa_lower or "consolidat" in sa_lower:
        v1_source = "derived"
    elif "assistant" in sa_lower:
        v1_source = "assistant"
    elif "extern" in sa_lower or "mem0" in sa_lower or "import" in sa_lower:
        v1_source = "external"
    else:
        v1_source = "user-inferred"

    # v0 entities → v1 entity shape (short keys n/tp/r → long name/type/role).
    raw_entities = source_claim.get("e") if isinstance(source_claim.get("e"), list) else []
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
        "text": text,
        "type": v1_type,
        "source": v1_source,
        "scope": None,
        "volatility": None,
        "reasoning": None,
        "entities": v1_entities or None,
        "importance": importance,
        "confidence": confidence,
    }


class _ProjectedFact:
    """Lightweight attribute-carrier used to feed ``build_canonical_claim_v1``.

    That builder accepts either dicts or attribute-based objects (see
    ``claims_helper._attr``). Keeping this as a tiny local class avoids
    having to hand-craft a dict with every taxonomy field the builder
    reads — the builder silently falls back on missing attributes.
    """

    __slots__ = (
        "text", "type", "importance", "confidence", "source",
        "scope", "reasoning", "entities", "volatility",
    )

    def __init__(self, **kwargs):
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k))


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
    data_edge_address: Optional[str] = None,
) -> dict:
    """Shared implementation for ``pin_fact`` / ``unpin_fact``.

    Semantics (Wave 2a, 2026-04-20 — see also Bug #8 in
    ``docs/notes/QA-hermes-RC-2.2.1-20260420.md``):

    1. Fetch the existing fact by id via the subgraph.
    2. Decrypt + parse. Detect whether the source blob is v1 (long-form
       fields + ``schema_version``) or v0 (short-key).
    3. Idempotency guard — if the parsed status already matches
       ``target_status``, return a no-op result with no on-chain write.
       For v1 sources the status comes from ``pin_status``; for v0
       sources it comes from the short-key ``st`` sentinel.
    4. **New in 2.2.2** — project the source blob into v1 shape via
       :func:`_project_source_to_v1` (upgrading v0 → v1 on the fly when
       necessary), then build a fresh v1.1 ``MemoryClaimV1`` JSON blob
       with ``pin_status`` set ("pinned" for pin; "unpinned" for
       explicit unpin), ``superseded_by`` pointing at the old fact id,
       and a fresh claim id. Matches ``skill/plugin/pin.ts`` byte-for-byte.
    5. Encrypt the new blob and build a ``FactPayload`` carrying it,
       with ``version=PROTOBUF_VERSION_V4`` so the outer protobuf wrapper
       tags the write as v1 taxonomy.
    6. **New in 2.2.3** — submit tombstone (at default v=3; matches
       plugin behavior) + new fact (at v=4) as a SINGLE batched UserOp
       via :func:`totalreclaw.userop.build_and_send_userop_batch` (which
       wraps both calls in ``SimpleAccount.executeBatch(...)``). Prior to
       2.2.3 the helper issued two sequential ``build_and_send_userop``
       calls (nonces N and N+1), which raced against a Pimlico mempool
       quirk: the bundler occasionally accepted the nonce-N+1 op,
       returned a hash, and then never propagated it — leaving the user
       with a tombstoned old fact but no pinned replacement. Batching
       collapses the race (one nonce, one submission) and makes the pin
       atomic on-chain.

    Prior to 2.2.2 this helper emitted a short-key blob at the default
    protobuf v=3 for the new fact, which violated the v1 on-chain
    contract: the subgraph showed a v=3 tombstone with no companion v=4
    pinned claim. That was the QA Finding #8 ship-stopper.

    Returns ``{success, fact_id, new_fact_id, previous_status, new_status}``
    plus ``idempotent: True`` on no-op.

    Parameters
    ----------
    target_status : {"p", "a"}
        Compact short-key for the target ``ClaimStatus``. ``"p"`` means
        pin (emit ``pin_status: "pinned"``); ``"a"`` means unpin (emit
        ``pin_status: "unpinned"`` on the new v1 blob).
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

    # 2. Decrypt + parse. We need the raw decrypted blob (not the
    # normalized-to-short-key output of ``_decrypt_and_parse_claim``) so
    # v1 sources can round-trip their long-form fields into the new v1
    # blob. Parse twice on the v0 path: once to get the short-key dict
    # for idempotency + projection, once through the raw plaintext so
    # v1 blobs preserve their long-form metadata.
    encrypted_hex_src = fact.get("encryptedBlob", "") or ""
    if encrypted_hex_src.startswith("0x"):
        encrypted_hex_src = encrypted_hex_src[2:]
    if not encrypted_hex_src:
        raise ValueError("fact has empty encryptedBlob")
    encrypted_b64_src = base64.b64encode(bytes.fromhex(encrypted_hex_src)).decode("ascii")
    try:
        decrypted_plaintext = decrypt(encrypted_b64_src, keys.encryption_key)
    except Exception as e:
        raise ValueError(f"failed to decrypt fact blob: {e}") from e

    # Try to parse the raw plaintext. v1 blobs come out as long-form
    # dicts with schema_version; v0 blobs come out as short-key dicts.
    try:
        raw_claim_obj = _json.loads(decrypted_plaintext)
    except (ValueError, TypeError):
        raw_claim_obj = None

    is_v1_source = (
        isinstance(raw_claim_obj, dict)
        and isinstance(raw_claim_obj.get("text"), str)
        and isinstance(raw_claim_obj.get("type"), str)
        and isinstance(raw_claim_obj.get("schema_version"), str)
        and raw_claim_obj["schema_version"].startswith("1.")
    )

    # Derive ``current_status`` + short-key projection for idempotency.
    if is_v1_source:
        source_claim_for_projection = raw_claim_obj
        current_pin_status = raw_claim_obj.get("pin_status")
        if current_pin_status == "pinned":
            current_short = "p"
            current_long = "pinned"
        else:
            current_short = None
            current_long = "active"
    else:
        # v0 short-key path — delegate to the existing normalizer so
        # legacy {text, metadata} blobs coerce into {t, c, ...}.
        source_claim_for_projection = _decrypt_and_parse_claim(fact, keys)
        current_short = source_claim_for_projection.get("st")
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

    # 4. Build the new v1.1 canonical claim. ALWAYS v1 on the pin path —
    # matches ``skill/plugin/pin.ts::executePinOperation`` step 4.
    v1_view = _project_source_to_v1(
        source_claim_for_projection,
        default_source_agent="python-client",
    )

    # Preserve the source blob's ``metadata`` dict across the pin/unpin
    # rewrite (internal#470 / Finding #7). ``_project_source_to_v1`` returns
    # only the v1 provenance fields, so without re-attaching ``metadata``
    # the session_id / import_source stamp (core #463) is dropped and the
    # superseding active fact comes back with ``session_id: null``. Only v1
    # sources carry the field; v0 short-key blobs predate it.
    carried_metadata = (
        raw_claim_obj.get("metadata")
        if is_v1_source and isinstance(raw_claim_obj.get("metadata"), dict)
        else None
    )

    new_fact_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    pin_status_wire = "pinned" if target_status == "p" else "unpinned"

    projected_fact = _ProjectedFact(
        text=v1_view["text"],
        type=v1_view["type"],
        importance=v1_view["importance"],
        confidence=v1_view["confidence"],
        source=v1_view["source"],
        scope=v1_view["scope"],
        reasoning=v1_view["reasoning"],
        entities=v1_view["entities"],
        volatility=v1_view["volatility"],
    )

    new_blob_plaintext = build_canonical_claim_v1(
        projected_fact,
        importance=v1_view["importance"],
        created_at=timestamp,
        superseded_by=fact_id,
        claim_id=new_fact_id,
        pin_status=pin_status_wire,
        extra_metadata=carried_metadata,
    )

    # 5. Encrypt + build FactPayload at protobuf v=4.
    encrypted_blob = encrypt(new_blob_plaintext, keys.encryption_key)
    encrypted_hex = base64.b64decode(encrypted_blob).hex()

    source_tag = "python_pin" if target_status == "p" else "python_unpin"

    # Regenerate trapdoors for the new pinned fact so trapdoor-based
    # recall still surfaces it after the old fact is tombstoned.
    new_claim_text = v1_view["text"]
    new_entity_objs = v1_view["entities"] or []

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
        # Bug #8 (Wave 2a): the new v1.1 blob MUST ride a v=4 outer
        # protobuf. Pre-2.2.2 this fell through to DEFAULT_PROTOBUF_VERSION
        # (v=3), leaving the on-chain fact with an inner v0 short-key
        # blob and a v=3 wrapper — unreadable by decoders that gate on
        # the v=4 flag.
        version=PROTOBUF_VERSION_V4,
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

    # 6. Submit tombstone + new fact as ONE batched UserOp via
    # ``SimpleAccount.executeBatch(...)``. Collapses the pre-2.2.3 flow's
    # two sequential ``build_and_send_userop`` calls (two nonces, two
    # Pimlico round-trips, two paymaster sponsorships, back-to-back
    # mempool submissions) into a single atomic UserOp.
    #
    # Why batch the pin path specifically:
    #   1. Atomicity — either both the tombstone AND the new v1 pinned
    #      blob land in the same block, or neither does. Pre-2.2.3 could
    #      land the tombstone first and then have the new fact stick in
    #      Pimlico's mempool forever, leaving the user with an
    #      effectively-forgotten fact that still surfaces as pinned in
    #      the UX. This was observed on staging during the Hermes 2.2.2
    #      QA pass (internal repo, issue #17).
    #   2. Pimlico mempool race — same-SA back-to-back UserOps at
    #      nonce N and N+1 occasionally trip a Pimlico quirk where the
    #      second op is accepted (hash returned) but never leaves the
    #      mempool. One UserOp = no race.
    #   3. Gas — paymaster counts the pin as 1 UserOp instead of 2. Base
    #      tx cost amortized over both calls.
    #   4. Nonce safety — the AA25 retry collapse noted in
    #      :func:`build_and_send_userop_batch` applies here too: O(1)
    #      retry vs O(n) for the prior sequential flow.
    #
    # Ordering within the batch is preserved: DataEdge emits one
    # ``Log(bytes)`` event per call, and the subgraph indexes each by
    # ``txHash + logIndex`` (ascending). Tombstone at index 0, new fact
    # at index 1 — identical on-chain shape to the pre-2.2.3 two-UserOp
    # flow from the subgraph's point of view.
    #
    # The outer protobuf versioning from the pre-2.2.3 flow is preserved
    # verbatim: tombstone at v=3 (legacy; matches ``skill/plugin/pin.ts``
    # byte-for-byte), new fact at v=4 (v1 taxonomy — Bug #8 fix from
    # 2.2.2 still required).
    tombstone_bytes = encode_tombstone_protobuf(fact_id, owner)
    new_protobuf = encode_fact_protobuf(payload)

    await build_and_send_userop_batch(
        sender=smart_account,
        eoa_address=eoa_address,
        eoa_private_key=eoa_private_key,
        protobuf_payloads=[tombstone_bytes, new_protobuf],
        relay_url=relay.relay_url,
        auth_key_hex=relay.auth_key_hex or "",
        wallet_address=smart_account,
        chain_id=chain_id,
        client_id=relay.client_id,
        session_id=relay.session_id,
        data_edge_address=data_edge_address,
    )

    # Read-after-write: poll the subgraph until the new (pinned/unpinned)
    # fact id is indexed and active. On timeout we still report success
    # — the chain write IS acknowledged — but flag ``partial=True`` so a
    # follow-up ``client.export()`` / ``client.recall()`` doesn't surprise
    # the caller with stale state.
    from .confirm_indexed import confirm_indexed as _confirm_indexed

    indexed = await _confirm_indexed(new_fact_id, relay, expect="active")

    result = {
        "success": True,
        "fact_id": fact_id,
        "new_fact_id": new_fact_id,
        "previous_status": current_long,
        "new_status": target_long,
    }
    if not indexed:
        result["partial"] = True
    return result


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
    data_edge_address: Optional[str] = None,
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
        data_edge_address=data_edge_address,
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
    data_edge_address: Optional[str] = None,
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
        data_edge_address=data_edge_address,
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

                    entry = {
                        "id": fact["id"],
                        "text": text,
                        "timestamp": formatted_ts,
                        "importance": float(fact.get("decayScore", "0.5")),
                        "type": doc.get("category", "fact"),
                    }
                    # #425 — export carries provenance: source +
                    # import_source + session_id (tagged on write since
                    # #356/#363 but previously dropped here).
                    # #317 — agent_name rides the same provenance passthrough.
                    doc_meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
                    for key in ("source", "import_source", "session_id", "agent_name"):
                        val = doc_meta.get(key)
                        if isinstance(val, str) and val:
                            entry[key] = val
                    results.append(entry)
                except Exception:
                    continue

            if len(facts) < page_size:
                break
            skip += page_size
        except Exception:
            break

    return results


async def find_existing_content_fps(
    keys: DerivedKeys,
    owner: str,
    relay: RelayClient,
    fps: list[str],
) -> set[str]:
    """Return the subset of *fps* that already exist as ACTIVE facts on-chain.

    #422 — the on-chain pivot silently dropped write-path dedup: the client
    computes ``content_fp`` and the subgraph stores it, but nothing ever
    CHECKED it (the old relay-store 409 never happens on the bundler path).
    This is the read half of client-side pre-write dedup: callers skip facts
    whose fingerprint is already live, saving the duplicate AND its gas.

    Fails open (empty set) on any query error — dedup is best-effort and
    must never block a store.
    """
    if not fps:
        return set()
    owner = owner.lower() if owner else owner
    query = """
    query($owner: Bytes!, $fps: [String!]!) {
      facts(where: {owner: $owner, contentFp_in: $fps, isActive: true}, first: 1000) {
        contentFp
      }
    }
    """
    try:
        data = await relay.query_subgraph(query, {"owner": owner, "fps": fps})
        return {
            f["contentFp"]
            for f in data.get("data", {}).get("facts", [])
            if f.get("contentFp")
        }
    except Exception as e:
        logger.warning("Pre-write dedup query failed (fail-open): %s", e)
        return set()
