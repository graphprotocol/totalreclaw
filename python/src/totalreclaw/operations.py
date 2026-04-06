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
from .userop import build_and_send_userop

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
) -> str:
    """Encrypt and store a fact on-chain via relay.

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

    Returns the fact ID.
    """
    if eoa_private_key is None or eoa_address is None:
        raise ValueError(
            "eoa_private_key and eoa_address are required for UserOp signing"
        )

    smart_account = sender or owner
    fact_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Encrypt the plaintext
    encrypted_blob = encrypt(text, keys.encryption_key)
    # Convert base64 to hex for protobuf
    encrypted_hex = base64.b64decode(encrypted_blob).hex()

    # Generate blind indices (word trapdoors)
    word_indices = generate_blind_indices(text)

    # Generate LSH bucket hashes if embedding available
    lsh_indices: list[str] = []
    if lsh_hasher and embedding:
        lsh_indices = lsh_hasher.hash(embedding)

    all_indices = word_indices + lsh_indices

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
            text = decrypt(encrypted_b64, keys.encryption_key)

            emb: Optional[list[float]] = None
            encrypted_emb = fact.get("encryptedEmbedding")
            if encrypted_emb:
                try:
                    emb = decrypt_embedding(encrypted_emb, keys.encryption_key)
                except Exception:
                    pass

            # Re-embed if stored dimension differs from current model
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
                    text = decrypt(encrypted_b64, keys.encryption_key)
                    results.append(
                        {
                            "id": fact["id"],
                            "text": text,
                            "timestamp": fact.get("timestamp", ""),
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
