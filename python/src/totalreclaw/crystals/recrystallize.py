"""Re-crystallize / re-key backfill for collapsed on-chain sessions.

Both the dry-run planner (:func:`plan_recrystallize`, pure segmentation +
cost estimation) and the guarded on-chain write/tombstone path
(:func:`execute_recrystallize`) are implemented + validated against staging
(#438). See ``docs/specs/totalreclaw/recrystallize-backfill.md`` for the full
design and §12 for usage. Not yet run against any real user vault (Phase B/C,
gated on Pedro).

Why this exists
---------------
A Hermes write-side bug collapsed every conversation in a chat into ONE on-chain
``session_id``, so a vault holds many unrelated conversations mis-grouped under a
few giant ``session_id``s, plus mixed "Crystals" (a Crystal = a ``type=summary``
claim with ``metadata.subtype=session_crystal`` summarizing a session). The
write-side fix (#429 + #434) stops NEW writes from collapsing but leaves existing
on-chain data mis-grouped. This backfill re-segments the vault into coherent
sessions and re-keys the data:

    decrypt vault → segment atomic facts (centroid-walk) → for each coherent
    session: write facts with a fresh session_id + a fresh Crystal, then
    tombstone the old facts + old mixed Crystals.

Ordering (hard dependency)
--------------------------
This MUST run AFTER the write-side fix is live for the target client, else live
auto-extraction re-collapses new writes while the backfill repairs old ones.
:func:`execute_recrystallize` refuses to run unless ``write_side_fix_confirmed``
is set.

Safety
------
- Dry-run (:func:`plan_recrystallize`) is the DEFAULT and writes nothing.
- All testing hits STAGING only (``api-staging.totalreclaw.xyz``). Prod requires
  an explicit opt-in in the CLI (see :func:`build_arg_parser`).
- Quota unit = memories *written*, counted per-fact by the relay, and tombstones
  count too (relay ``extractFactCount`` is payload-agnostic). Cost formula:
  ``2·F + S_multi + C_old`` (see :func:`estimate_quota_cost`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Single source of truth for the relay URL (repo forbids duplicate URL literals).
from totalreclaw.relay import _HARDCODED_DEFAULT_URL as _CANONICAL_PROD_URL

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

#: Marker for a Crystal (session-summary) claim, mirrored from import_engine.
METADATA_SUBTYPE_SESSION_CRYSTAL = "session_crystal"

#: Centroid-walk segmentation defaults — identical to the import path so backfill
#: grouping matches what a fixed live run would broadly produce.
DEFAULT_GAP_SECONDS = 1800  # 30-minute hard time boundary
DEFAULT_SIM_THRESHOLD = 0.55  # validated on real Gemini data (#368)

#: A session needs >= this many facts to warrant a fresh Crystal. Singletons are
#: re-keyed without a Crystal (mirrors import singleton semantics).
MIN_FACTS_FOR_CRYSTAL = 2

#: Crystal importance — anchored "high" per the v1 rubric, mirrored from
#: import_engine._CRYSTAL_IMPORTANCE so backfilled Crystals score like imports.
CRYSTAL_IMPORTANCE = 8

#: Provenance for a re-derived Crystal. A Crystal is a summary the tool derived
#: from the vault's own facts, so ``derived`` is the correct v1 MemorySource
#: (not ``external``, which imports use for provider-sourced data).
CRYSTAL_PROVENANCE = "derived"

#: Page size for the paginated subgraph fetch (mirrors operations.export_facts).
FETCH_PAGE_SIZE = 1000

#: Max inner calls per executeBatch UserOp (core 2.5.5, #392 Part 2). Batching
#: cuts UserOp count (Pimlico cost) but NOT quota cost (quota is per-fact).
MAX_BATCH_SIZE = 30

#: Checkpoint dir — sibling of the import-state precedent
#: (~/.totalreclaw/import-state/). See import_state.py.
RECRYSTALLIZE_STATE_DIR: Path = Path.home() / ".totalreclaw" / "recrystallize-state"

#: Hard project rule: all testing hits staging, never production. Both URLs are
#: sourced from the ONE canonical site (`relay._HARDCODED_DEFAULT_URL`) rather
#: than re-baked here — the repo forbids duplicate URL literals (see
#: `test_no_other_hardcoded_default_url_sites`). Staging is the same host with
#: the `api-staging` subdomain; also overridable via `TOTALRECLAW_SERVER_URL`.
PRODUCTION_RELAY_URL = _CANONICAL_PROD_URL
STAGING_RELAY_URL = PRODUCTION_RELAY_URL.replace("://api.", "://api-staging.")


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class DecryptedFact:
    """A single active fact fetched from the subgraph and decrypted client-side.

    Carries the fields the standard ``export``/``recall`` queries drop — notably
    the raw ``metadata`` dict (``session_id`` / ``subtype``) which
    ``read_blob_unified`` whitelists away for v1 blobs (see design §3.1), plus
    the decrypted embedding needed for re-segmentation.
    """

    fact_id: str
    text: str
    #: Decrypted 640d Harrier embedding (L2-normalised), or None if unavailable.
    embedding: Optional[list[float]]
    #: Unix seconds (per-fact ``createdAt``, falling back to block ``timestamp``).
    created_at: float
    importance: float
    fact_type: str
    provenance: str
    #: Raw ``metadata`` dict decoded straight from the blob JSON (NOT via
    #: ``read_blob_unified``). Holds ``session_id`` / ``subtype`` / ``import_source``.
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Entities carried on the v1 claim, if any (re-attached on rewrite).
    entities: Optional[list[dict[str, Any]]] = None

    @property
    def old_session_id(self) -> Optional[str]:
        sid = self.metadata.get("session_id")
        return sid if isinstance(sid, str) else None

    @property
    def is_crystal(self) -> bool:
        return self.metadata.get("subtype") == METADATA_SUBTYPE_SESSION_CRYSTAL


@dataclass
class CorrectedSession:
    """One coherent session produced by re-segmentation.

    ``fresh_session_id`` is minted once (or read back from the checkpoint on a
    resume) and stamped onto every rewritten fact + the fresh Crystal.
    """

    fresh_session_id: str
    #: The atomic facts (from the input vault) assigned to this session, in time
    #: order.
    facts: list[DecryptedFact]

    @property
    def needs_crystal(self) -> bool:
        return len(self.facts) >= MIN_FACTS_FOR_CRYSTAL


@dataclass
class QuotaEstimate:
    """Quota cost breakdown for a backfill plan.

    Quota unit = memories written, per-fact, tombstones included. See design §5.
    Formula: ``total = 2·F + S_multi + C_old``.
    """

    atomic_facts: int  # F
    old_crystals: int  # C_old
    multi_fact_sessions: int  # S_multi
    singleton_sessions: int

    @property
    def writes_new(self) -> int:
        """Fresh facts written = every atomic fact rewritten + one Crystal per
        multi-fact session."""
        return self.atomic_facts + self.multi_fact_sessions

    @property
    def tombstones(self) -> int:
        """Old facts + old mixed Crystals tombstoned."""
        return self.atomic_facts + self.old_crystals

    @property
    def total_quota_cost(self) -> int:
        """2·F + S_multi + C_old (see design §5.1)."""
        return self.writes_new + self.tombstones

    def userops_estimate(self, batch_size: int = MAX_BATCH_SIZE) -> int:
        """Approximate UserOp count with executeBatch batching (Pimlico cost,
        NOT quota). Excludes confirm/retry overhead."""
        return _ceil_div(self.writes_new, batch_size) + _ceil_div(
            self.tombstones, batch_size
        )


@dataclass
class RecrystallizePlan:
    """The full dry-run plan: corrected sessions + quota estimate.

    Returned by :func:`plan_recrystallize`. Contains everything a human needs to
    review before authorizing an ``--execute`` run; writing this out (as JSON or
    a printed summary) is the dry-run deliverable.
    """

    owner: str
    corrected_sessions: list[CorrectedSession]
    old_crystals: list[DecryptedFact]
    estimate: QuotaEstimate

    def summary_lines(self) -> list[str]:
        """Human-readable dry-run report lines (printed by the CLI)."""
        e = self.estimate
        lines = [
            f"Vault owner: {self.owner}",
            f"Atomic facts (F):            {e.atomic_facts}",
            f"Old mixed Crystals (C_old):  {e.old_crystals}",
            f"Corrected sessions (total):  {len(self.corrected_sessions)}",
            f"  - multi-fact (S_multi):    {e.multi_fact_sessions}  (each gets a fresh Crystal)",
            f"  - singleton:               {e.singleton_sessions}  (re-keyed, no Crystal)",
            "",
            f"Writes (new facts + Crystals): {e.writes_new}",
            f"Tombstones (old facts + Crystals): {e.tombstones}",
            f"TOTAL QUOTA COST (2·F + S_multi + C_old): {e.total_quota_cost} memories",
            f"~UserOps (batched, Pimlico cost only): {e.userops_estimate()}",
        ]
        return lines


# ── Pure logic: segmentation → plan → estimate (IMPLEMENTED + tested) ─────────


def build_corrected_sessions(
    atomic_facts: list[DecryptedFact],
    *,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
    session_id_factory: Optional[Any] = None,
) -> list[CorrectedSession]:
    """Re-segment atomic facts into coherent sessions (pure, deterministic).

    Sorts facts chronologically, runs the centroid-walk segmenter
    (:func:`totalreclaw.session_segmentation.segment_sessions`, core-hoisted),
    and wraps each returned index-group as a :class:`CorrectedSession` with a
    freshly-minted ``session_id``.

    Parameters
    ----------
    atomic_facts:
        Decrypted NON-Crystal facts. Facts without an embedding are still
        included (segmenter tolerates a zero-ish vector), but grouping quality
        degrades — the caller should prefer to backfill embeddings first.
    gap_seconds, sim_threshold:
        Segmenter knobs (defaults match the import path).
    session_id_factory:
        Callable returning a fresh unique id (UUIDv7 in production). Injected so
        tests get deterministic ids and a *resume* can supply ids read back from
        the checkpoint instead of minting new ones. Defaults to
        :func:`_default_session_id`.

    Returns
    -------
    list[CorrectedSession]
        One entry per coherent session, in chronological order.
    """
    if not atomic_facts:
        return []

    mint = session_id_factory or _default_session_id

    # Segmenter assumes time order.
    ordered = sorted(atomic_facts, key=lambda f: f.created_at)

    # Import lazily so the pure planner has no hard dependency at module import
    # time (keeps the scaffold importable without the core wheel).
    from .session_segmentation import segment_sessions

    timestamps: list[Optional[float]] = [f.created_at for f in ordered]
    embeddings: list[list[float]] = [
        f.embedding if f.embedding else [] for f in ordered
    ]

    # Facts missing an embedding can't be centroid-compared. If ANY are missing
    # we fall back to a time-gap-only grouping for those; here, defensively, we
    # substitute a zero vector so segment_sessions still returns contiguous
    # groups (the segmenter treats a below-eps norm as non-splitting). Grouping
    # quality is a documented open question (design §10.1).
    dim = _first_embedding_dim(embeddings)
    embeddings = [e if e else [0.0] * dim for e in embeddings]

    groups = segment_sessions(
        timestamps=timestamps,
        embeddings=embeddings,
        gap_seconds=gap_seconds,
        sim_threshold=sim_threshold,
    )

    sessions: list[CorrectedSession] = []
    for idx_group in groups:
        facts = [ordered[i] for i in idx_group]
        sessions.append(CorrectedSession(fresh_session_id=mint(), facts=facts))
    return sessions


def estimate_quota_cost(
    corrected_sessions: list[CorrectedSession],
    old_crystals: list[DecryptedFact],
) -> QuotaEstimate:
    """Compute the quota cost breakdown for a plan (pure). See design §5.1."""
    atomic_facts = sum(len(s.facts) for s in corrected_sessions)
    multi = sum(1 for s in corrected_sessions if s.needs_crystal)
    singleton = sum(1 for s in corrected_sessions if not s.needs_crystal)
    return QuotaEstimate(
        atomic_facts=atomic_facts,
        old_crystals=len(old_crystals),
        multi_fact_sessions=multi,
        singleton_sessions=singleton,
    )


def split_facts(
    decrypted: list[DecryptedFact],
) -> tuple[list[DecryptedFact], list[DecryptedFact]]:
    """Partition decrypted facts into ``(atomic_facts, old_crystals)`` (pure)."""
    atomic = [f for f in decrypted if not f.is_crystal]
    crystals = [f for f in decrypted if f.is_crystal]
    return atomic, crystals


def build_plan(
    owner: str,
    decrypted: list[DecryptedFact],
    *,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
    session_id_factory: Optional[Any] = None,
) -> RecrystallizePlan:
    """Assemble a full dry-run plan from already-decrypted facts (pure).

    This is the testable core of the dry-run path — no network, no crypto.
    :func:`plan_recrystallize` wraps it with the fetch+decrypt front-end.
    """
    atomic, old_crystals = split_facts(decrypted)
    sessions = build_corrected_sessions(
        atomic,
        gap_seconds=gap_seconds,
        sim_threshold=sim_threshold,
        session_id_factory=session_id_factory,
    )
    estimate = estimate_quota_cost(sessions, old_crystals)
    return RecrystallizePlan(
        owner=owner,
        corrected_sessions=sessions,
        old_crystals=old_crystals,
        estimate=estimate,
    )


# ── Fetch + decrypt front-end (network/crypto path) ──────────────────────────


#: A widened export query — like ``operations.EXPORT_QUERY`` but the fact
#: fields the backfill additionally needs are ``encryptedEmbedding`` (to reuse
#: the original vector on rewrite) and ``createdAt`` (the segmenter's ordering
#: key). All the standard export/recall queries already return these on the
#: subgraph object; the export path just drops them post-decrypt.
_FETCH_QUERY = """
  query RecrystallizeFetch($owner: Bytes!, $first: Int!, $skip: Int!) {
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


def _subgraph_ts_to_unix(raw: Any) -> float:
    """Coerce a subgraph ``createdAt``/``timestamp`` BigInt string to Unix secs.

    Both are BigInt strings of Unix seconds. Returns ``0.0`` on any parse
    failure (a fact with no usable timestamp still segments, just with a
    degenerate ordering key — the segmenter tolerates ties).
    """
    if raw in (None, ""):
        return 0.0
    try:
        return float(int(str(raw)))
    except (ValueError, TypeError):
        return 0.0


async def fetch_and_decrypt_vault(client: Any) -> list[DecryptedFact]:
    """Fetch all active facts for the owner and decrypt them client-side.

    Paginates ``facts(where: {owner, isActive: true})`` via
    ``client._relay.query_subgraph`` (widened over ``export`` to include
    ``encryptedEmbedding`` + ``createdAt``), then for each fact:

      1. ``decrypt(encryptedBlob)`` → the raw v1 JSON blob.
      2. :func:`_decode_raw_blob` for the stored ``metadata`` dict (NOT
         ``read_blob_unified``, which whitelists ``metadata`` away — design
         §3.1), so ``session_id`` / ``subtype`` / ``import_source`` survive.
      3. ``read_blob_unified`` for the display fields the raw blob doesn't
         canonicalize (``text`` / ``importance`` / ``category``).
      4. ``decrypt_embedding`` for the 640d vector (reused on rewrite so LSH
         trapdoors + search are unchanged).

    Skips digest blobs (``is_digest_blob``) and tombstone stubs
    (``is_stub_blob_hex``) — neither is a re-keyable atomic fact or Crystal.

    Returns a list of :class:`DecryptedFact` (atomic + Crystals; the caller
    splits them via :func:`split_facts`). ``client._ensure_address`` /
    ``_ensure_registered`` must have run first (``plan_recrystallize`` does).
    """
    # Local imports keep the pure planner importable without the crypto stack.
    from totalreclaw.crypto import decrypt, decrypt_embedding
    from totalreclaw.claims_helper import (
        is_digest_blob,
        is_stub_blob_hex,
        read_blob_unified,
    )

    keys = client._keys
    relay = client._relay
    owner = client.wallet_address.lower()

    out: list[DecryptedFact] = []
    skip = 0
    while True:
        data = await relay.query_subgraph(
            _FETCH_QUERY,
            {"owner": owner, "first": FETCH_PAGE_SIZE, "skip": skip},
        )
        facts = data.get("data", {}).get("facts", []) if isinstance(data, dict) else []
        if not facts:
            break

        for fact in facts:
            try:
                encrypted_hex = fact.get("encryptedBlob", "") or ""
                if is_stub_blob_hex(encrypted_hex):
                    continue
                if encrypted_hex.startswith("0x"):
                    encrypted_hex = encrypted_hex[2:]
                if not encrypted_hex:
                    continue
                encrypted_b64 = base64.b64encode(
                    bytes.fromhex(encrypted_hex)
                ).decode("ascii")
                decrypted_blob = decrypt(encrypted_b64, keys.encryption_key)
                if is_digest_blob(decrypted_blob):
                    continue

                # Raw metadata (session_id / subtype / import_source) — design §3.1.
                metadata = _decode_raw_blob(decrypted_blob)
                # Canonical display fields the raw blob doesn't normalize.
                doc = read_blob_unified(decrypted_blob)
                text = doc.get("text") or ""
                if not text:
                    continue
                importance = float(doc.get("importance", 5))
                fact_type = doc.get("category", "claim")

                # v1 provenance lives in the blob's whitelisted metadata; the
                # raw metadata dict (session_id etc.) does not carry ``source``.
                doc_meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
                provenance = (
                    doc_meta.get("source")
                    if isinstance(doc_meta.get("source"), str)
                    else "user-inferred"
                )

                # Entities off the raw v1 blob (re-attached verbatim on rewrite).
                entities = None
                try:
                    raw_obj = json.loads(decrypted_blob)
                    if isinstance(raw_obj, dict) and isinstance(
                        raw_obj.get("entities"), list
                    ):
                        entities = raw_obj["entities"]
                except (ValueError, TypeError):
                    pass

                embedding: Optional[list[float]] = None
                enc_emb = fact.get("encryptedEmbedding")
                if enc_emb:
                    try:
                        embedding = decrypt_embedding(enc_emb, keys.encryption_key)
                    except Exception:
                        embedding = None

                created_at = _subgraph_ts_to_unix(
                    fact.get("createdAt") or fact.get("timestamp")
                )

                out.append(
                    DecryptedFact(
                        fact_id=fact["id"],
                        text=text,
                        embedding=embedding,
                        created_at=created_at,
                        importance=importance,
                        fact_type=fact_type,
                        provenance=provenance,
                        metadata=metadata,
                        entities=entities,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "recrystallize: skipped undecryptable fact %s: %s",
                    fact.get("id"),
                    exc,
                )
                continue

        if len(facts) < FETCH_PAGE_SIZE:
            break
        skip += FETCH_PAGE_SIZE

    return out


def _decode_raw_blob(decrypted_json: str) -> dict[str, Any]:
    """Decode a decrypted blob to its raw dict, preserving ``metadata``.

    Unlike ``claims_helper.read_blob_unified`` (which, for v1 blobs, rebuilds
    ``metadata`` from a whitelist and DROPS the stored ``metadata`` dict — design
    §3.1), this returns the blob's own top-level ``metadata`` so ``session_id`` /
    ``subtype`` survive. Falls back to an empty dict on any parse failure.
    """
    try:
        obj = json.loads(decrypted_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    meta = obj.get("metadata")
    return meta if isinstance(meta, dict) else {}


# ── Crystal (re)builder + quota detection (write-path helpers) ────────────────


def _is_quota_exhausted_error(exc: BaseException) -> bool:
    """True if ``exc`` is (or wraps) a relay 403 quota-exceeded response.

    The bundler submit path calls ``resp.raise_for_status()``, so a quota
    rejection surfaces as an ``httpx.HTTPStatusError`` with a 403 status. We
    also match on a ``"quota"`` substring in the message as a belt-and-braces
    guard for relays that signal quota differently. Used to convert a mid-run
    403 into a clean ``paused_quota`` stop (design §6.2) rather than a crash.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 403:
        return True
    msg = str(exc).lower()
    return "quota" in msg and ("exceed" in msg or "403" in msg)


async def _build_crystal_text(
    session: "CorrectedSession",
    llm_completion: Optional[Callable[[str], Any]],
) -> tuple[str, dict[str, Any]]:
    """Produce ``(text, extra_metadata)`` for a corrected session's Crystal.

    Unlike the import engine, the backfill has **no conversation transcript**
    (turns aren't on-chain) — only the re-segmented *facts*. So the summary
    prompt is fact-only. Behaviour mirrors ``import_engine._make_crystal``:

      * If an ``llm_completion`` is wired, one call returns
        ``{"title", "summary", ...}``; the title becomes the Crystal text.
      * On no LLM / bad JSON / empty title, fall back to
        ``_derive_title_from_facts`` (highest-importance fact, truncated).

    The returned ``extra_metadata`` carries the Crystal marker
    (``subtype=session_crystal``) + the **fresh** ``session_id`` so backfilled
    Crystals key exactly like the fixed live write-side path.
    """
    facts_for_title = [
        {"text": f.text, "importance": f.importance, "type": f.fact_type}
        for f in session.facts
    ]

    title: Optional[str] = None
    summary: Optional[str] = None
    if llm_completion is not None:
        try:
            from totalreclaw.import_engine import _extract_json_object

            prompt = _recrystallize_crystal_prompt(facts_for_title)
            raw = await llm_completion(prompt)
            data = _extract_json_object(raw) if raw else None
            if isinstance(data, dict):
                t = (data.get("title") or "").strip()
                title = t[:60] if t else None
                s = (data.get("summary") or "").strip()
                summary = s or None
        except Exception as exc:  # never let a Crystal failure abort the run
            logger.debug("recrystallize: Crystal LLM call failed: %s", exc)

    if not title:
        from totalreclaw.import_engine import _derive_title_from_facts

        title = _derive_title_from_facts(facts_for_title)

    meta: dict[str, Any] = {
        "subtype": METADATA_SUBTYPE_SESSION_CRYSTAL,
        "session_id": session.fresh_session_id,
        "session_title": title,
    }
    if summary:
        meta["session_summary"] = summary
    # Preserve the provider provenance if the source facts came from an import.
    for f in session.facts:
        src = f.metadata.get("import_source")
        if isinstance(src, str) and src:
            meta["import_source"] = src
            break
    return title[:512], meta


def _recrystallize_crystal_prompt(facts: list[dict[str, Any]]) -> str:
    """Fact-only Crystal summary prompt (no transcript — see §3 caveat).

    Analogue of ``import_engine._crystal_prompt`` with the transcript half
    removed: the backfill only has extracted facts to summarize.
    """
    sorted_facts = sorted(
        facts, key=lambda f: f.get("importance", 5), reverse=True
    )
    fact_lines = "\n".join(
        f"- [{f.get('type', 'fact')}] (importance={f.get('importance', 5)}) "
        f"{f.get('text', '')}"
        for f in sorted_facts[:20]
    )[:2000]
    return (
        "You are given the EXTRACTED FACTS from a single coherent session "
        "(the conversation transcript is unavailable). Generate a compact JSON "
        "object summarizing the session.\n"
        "Return ONLY JSON, no prose. Schema:\n"
        '{"title": "<=60-char human headline", '
        '"summary": "<=200-char one-line gist"}\n\n'
        f"Extracted facts (primary signal for the title):\n{fact_lines}\n"
    )


# ── Dry-run entry point (front-end: fetch is stubbed, planning is real) ───────


async def plan_recrystallize(
    client: Any,
    *,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
) -> RecrystallizePlan:
    """DRY-RUN: fetch + decrypt the vault, re-segment, and estimate cost.

    Writes NOTHING on-chain. This is the default, safe entry point; the pure
    planning half (:func:`build_plan`) is unit-tested and the fetch/decrypt
    front-end (:func:`fetch_and_decrypt_vault`) reads + decrypts the vault.
    """
    await client._ensure_address()
    # Subgraph queries need the auth key registered (else the relay 401s).
    await client._ensure_registered()
    owner = client.wallet_address
    decrypted = await fetch_and_decrypt_vault(client)
    return build_plan(
        owner,
        decrypted,
        gap_seconds=gap_seconds,
        sim_threshold=sim_threshold,
    )


# ── Execute entry point (on-chain writer, guarded) ───────────────────────────


class QuotaPaused(Exception):
    """Internal signal: a relay 403 quota-exceeded stopped the run cleanly.

    Not an error — the CLI catches it, reports "resume next month", and exits
    0. The checkpoint is already persisted as ``paused_quota`` when this is
    raised (design §6.2).
    """


async def _rewrite_session_facts(
    client: Any,
    session: "CorrectedSession",
) -> list[str]:
    """Write the re-keyed facts for one session; return their new ids.

    Each old fact is rewritten identically (text / importance / fact_type /
    provenance / entities / original embedding) with the **fresh**
    ``session_id`` stamped into ``extra_metadata`` and ``import_source``
    preserved. Batched ``remember_batch`` up to :data:`MAX_BATCH_SIZE` per
    ``executeBatch`` UserOp (design §4.1).
    """
    new_ids: list[str] = []
    fact_dicts: list[dict[str, Any]] = []
    for f in session.facts:
        extra_metadata: dict[str, Any] = {"session_id": session.fresh_session_id}
        import_source = f.metadata.get("import_source")
        if isinstance(import_source, str) and import_source:
            extra_metadata["import_source"] = import_source
        fact_dicts.append(
            {
                "text": f.text,
                "importance": f.importance,
                "embedding": f.embedding,
                "fact_type": f.fact_type,
                "entities": f.entities,
                "provenance": f.provenance,
                "extra_metadata": extra_metadata,
            }
        )

    for start in range(0, len(fact_dicts), MAX_BATCH_SIZE):
        batch = fact_dicts[start : start + MAX_BATCH_SIZE]
        ids = await client.remember_batch(batch, source="recrystallize")
        new_ids.extend(ids)
    return new_ids


async def _write_crystal(
    client: Any,
    session: "CorrectedSession",
    llm_completion: Optional[Callable[[str], Any]],
) -> Optional[str]:
    """Build + write one fresh Crystal for a multi-fact session; return its id."""
    text, meta = await _build_crystal_text(session, llm_completion)
    return await client.remember(
        text,
        importance=CRYSTAL_IMPORTANCE,
        source="recrystallize",
        fact_type="summary",
        provenance=CRYSTAL_PROVENANCE,
        extra_metadata=meta,
    )


async def execute_recrystallize(
    client: Any,
    plan: RecrystallizePlan,
    *,
    write_side_fix_confirmed: bool = False,
    confirm: bool = False,
    checkpoint: Optional["RecrystallizeCheckpoint"] = None,
    llm_completion: Optional[Callable[[str], Any]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> "RecrystallizeCheckpoint":
    """EXECUTE the backfill: write corrected data, tombstone old data.

    Per corrected session, in the safe **write-new → confirm-indexed →
    tombstone-old** order (design §4.3) so an interruption never loses data —
    at worst it leaves a duplicate the resume then cleans up:

      1. ``client.remember_batch`` the rewritten facts (fresh ``session_id`` in
         ``extra_metadata``, original embedding reused) in ≤30-fact batches.
      2. For a multi-fact session, build + write a fresh Crystal
         (:func:`_build_crystal_text`).
      3. ``confirm_indexed`` the last new fact, THEN ``client.forget`` each old
         fact (per-fact tombstone; design §4.1 — no batch-delete exists).

    Finally, every old mixed Crystal is tombstoned (design §4.2).

    Resumability (design §6): every mutation advances the per-session
    checkpoint phase (``planned → written → tombstoned → done``) and persists
    it. On a re-run, a session already ``written`` skips its writes; already
    ``done`` is skipped entirely. Fresh ``session_id``s are read back from the
    checkpoint so a resume never double-mints. A relay 403 quota-exceeded marks
    the checkpoint ``paused_quota``, persists it, and raises :class:`QuotaPaused`
    (a clean stop, not a failure).

    Guards (must ALL pass before any write):
      - ``write_side_fix_confirmed`` — attests the target client runs the
        #429/#434 fix (else new writes re-collapse mid-migration; design §8).
      - ``confirm`` — explicit operator go-ahead after reviewing the dry-run.
      - ``plan`` — an explicit plan artifact from the dry-run planner is
        REQUIRED; ``plan=None`` refuses (safety rail).

    Parameters
    ----------
    checkpoint : RecrystallizeCheckpoint, optional
        Resume state. If ``None``, a fresh checkpoint is created (and returned)
        so the caller can persist / inspect it.
    llm_completion : callable, optional
        ``async (prompt) -> str`` used to summarize each multi-fact Crystal.
        When absent, Crystals fall back to a fact-derived title (no LLM call).
    progress : callable, optional
        ``(str) -> None`` per-item progress sink (design safety rail: per-item
        progress logging). Defaults to a module-logger INFO line.

    Returns
    -------
    RecrystallizeCheckpoint
        The final checkpoint (``status=completed`` on a full run).
    """
    if plan is None:
        raise RuntimeError(
            "execute_recrystallize refused: a plan artifact is required. Run "
            "plan_recrystallize (dry-run) first and pass its result."
        )
    if not write_side_fix_confirmed:
        raise RuntimeError(
            "execute_recrystallize refused: write_side_fix_confirmed is False. "
            "The write-side session_id fix (#429/#434) MUST be live for the "
            "target client first, else live auto-extraction re-collapses new "
            "writes while the backfill repairs old ones (design §8)."
        )
    if not confirm:
        raise RuntimeError(
            "execute_recrystallize refused: confirm is False. Review the dry-run "
            "plan (plan_recrystallize) and pass confirm=True to proceed."
        )

    def _emit(msg: str) -> None:
        if progress is not None:
            progress(msg)
        else:
            logger.info("recrystallize: %s", msg)

    if checkpoint is None:
        checkpoint = RecrystallizeCheckpoint(
            owner=plan.owner,
            started_at=_now_iso(),
            last_updated=_now_iso(),
            status="running",
        )
    else:
        checkpoint.status = "running"
        checkpoint.quota_exhausted_at = None

    from totalreclaw.confirm_indexed import confirm_indexed as _confirm_indexed

    # Resume index: map each already-planned session's OLD-fact-id set to its
    # stored checkpoint. Segmentation is deterministic, so a re-derived plan
    # produces the SAME old-fact-id groups; matching on that set (not on the
    # freshly-minted session_id, which differs run-to-run) lets a resume reuse
    # the prior fresh session_id + progress without double-minting (§6.2).
    _by_old_ids: dict[frozenset[str], tuple[str, SessionCheckpoint]] = {
        frozenset(sc.old_fact_ids): (sid, sc)
        for sid, sc in checkpoint.sessions.items()
        if sid != _OLD_CRYSTALS_KEY
    }

    try:
        total = len(plan.corrected_sessions)
        for i, session in enumerate(plan.corrected_sessions, start=1):
            old_id_set = frozenset(f.fact_id for f in session.facts)
            # Exact session_id hit (same plan object re-passed), else match on
            # the deterministic old-fact-id set (re-derived plan on resume).
            sc = checkpoint.sessions.get(session.fresh_session_id)
            if sc is None and old_id_set in _by_old_ids:
                prior_sid, sc = _by_old_ids[old_id_set]
                session.fresh_session_id = prior_sid
            if sc is None:
                sc = SessionCheckpoint(
                    old_fact_ids=[f.fact_id for f in session.facts],
                )
                checkpoint.sessions[session.fresh_session_id] = sc

            if sc.phase == "done":
                _emit(f"[{i}/{total}] session {session.fresh_session_id} already done — skip")
                continue

            # 1. Write new facts (each sub-step persisted so a resume never
            #    re-mints work that already landed — mint cost is quota-billed).
            if sc.phase == "planned":
                if not sc.facts_written:
                    _emit(
                        f"[{i}/{total}] writing {len(session.facts)} facts → "
                        f"session {session.fresh_session_id}"
                    )
                    sc.new_fact_ids = await _rewrite_session_facts(client, session)
                    sc.facts_written = True
                    checkpoint.save()  # persist BEFORE the Crystal write
                if session.needs_crystal and not sc.crystal_written:
                    crystal_id = await _write_crystal(client, session, llm_completion)
                    if crystal_id:
                        sc.new_fact_ids.append(crystal_id)
                    sc.crystal_written = True
                    checkpoint.save()
                sc.phase = "written"
                checkpoint.save()

            # 2. Confirm the new data is indexed, THEN tombstone the old facts.
            #    ``tombstoned`` means a prior run crashed mid-tombstone — re-enter
            #    and finish the remaining (skip-already-tombstoned) forgets.
            if sc.phase in ("written", "tombstoned"):
                if sc.phase == "written" and sc.new_fact_ids:
                    await _confirm_indexed(sc.new_fact_ids[-1], client._relay, expect="active")
                remaining = [
                    fid for fid in sc.old_fact_ids if fid not in sc.old_fact_ids_tombstoned
                ]
                for old_id in remaining:
                    await client.forget(old_id)
                    sc.old_fact_ids_tombstoned.append(old_id)
                    sc.phase = "tombstoned"
                    checkpoint.save()
                sc.phase = "done"
                checkpoint.save()
                _emit(f"[{i}/{total}] session {session.fresh_session_id} done")

        # 3. Tombstone every old mixed Crystal (design §4.2). Tracked under a
        #    synthetic session key so a resume skips already-tombstoned ones.
        crystal_sc = checkpoint.sessions.get(_OLD_CRYSTALS_KEY)
        if crystal_sc is None:
            crystal_sc = SessionCheckpoint(
                phase="written",
                old_fact_ids=[c.fact_id for c in plan.old_crystals],
            )
            checkpoint.sessions[_OLD_CRYSTALS_KEY] = crystal_sc
        if crystal_sc.phase != "done":
            remaining_crystals = [
                cid
                for cid in crystal_sc.old_fact_ids
                if cid not in crystal_sc.old_crystal_ids_tombstoned
            ]
            for j, cid in enumerate(remaining_crystals, start=1):
                _emit(f"tombstoning old Crystal [{j}/{len(remaining_crystals)}] {cid}")
                await client.forget(cid)
                crystal_sc.old_crystal_ids_tombstoned.append(cid)
                checkpoint.save()
            crystal_sc.phase = "done"
            checkpoint.save()

        checkpoint.status = "completed"
        checkpoint.save()
        _emit("recrystallize complete")
        return checkpoint

    except Exception as exc:
        if _is_quota_exhausted_error(exc):
            checkpoint.status = "paused_quota"
            checkpoint.quota_exhausted_at = _now_iso()
            checkpoint.save()
            _emit(
                "quota exhausted — checkpoint saved as paused_quota; "
                "resume after the monthly quota resets"
            )
            raise QuotaPaused(str(exc)) from exc
        checkpoint.status = "failed"
        checkpoint.save()
        raise


#: Synthetic checkpoint key under which old-Crystal tombstones are tracked
#: (they don't belong to any corrected session).
_OLD_CRYSTALS_KEY = "__old_crystals__"


# ── Checkpoint / resumability (atomic JSON persistence) ──────────────────────


@dataclass
class SessionCheckpoint:
    """Per-session progress record. See design §6.1.

    ``old_fact_ids_tombstoned`` tracks which of this session's ``old_fact_ids``
    have already been tombstoned, so a crash mid-tombstone resumes without a
    double-forget. ``old_crystal_ids_tombstoned`` is used only by the synthetic
    old-Crystals bucket (:data:`_OLD_CRYSTALS_KEY`) to track old-Crystal
    tombstones the same way.
    """

    phase: str = "planned"  # planned | written | tombstoned | done
    old_fact_ids: list[str] = field(default_factory=list)
    new_fact_ids: list[str] = field(default_factory=list)
    #: Set once the atomic-fact ``remember_batch`` writes have all landed for
    #: this session. Persisted BEFORE the (separate) Crystal write so a crash
    #: between the two never re-mints the facts on resume (they're expensive +
    #: quota-billed). ``crystal_written`` guards the Crystal the same way.
    facts_written: bool = False
    crystal_written: bool = False
    old_fact_ids_tombstoned: list[str] = field(default_factory=list)
    old_crystal_ids_tombstoned: list[str] = field(default_factory=list)


@dataclass
class RecrystallizeCheckpoint:
    """Whole-run checkpoint, persisted to
    ``~/.totalreclaw/recrystallize-state/<vault_fingerprint>.json``.

    Persistence (:meth:`save` / :meth:`load`) mirrors the JSON round-trip
    pattern in ``import_state.py`` (atomic temp-file + ``os.replace``, tolerant
    coercion of unknown keys on load).
    """

    owner: str
    started_at: str
    last_updated: str
    status: str = "running"  # running | paused_quota | completed | failed
    sessions: dict[str, SessionCheckpoint] = field(default_factory=dict)
    quota_exhausted_at: Optional[str] = None

    @staticmethod
    def fingerprint(owner: str) -> str:
        """Stable per-vault checkpoint key (owner-address hash)."""
        return hashlib.sha256(owner.lower().encode("utf-8")).hexdigest()[:16]

    def path(self) -> Path:
        return RECRYSTALLIZE_STATE_DIR / f"{self.fingerprint(self.owner)}.json"

    def save(self) -> None:
        """Persist the checkpoint atomically. Mirrors ``import_state``.

        Stamps ``last_updated`` and writes ``<fingerprint>.json`` under
        :data:`RECRYSTALLIZE_STATE_DIR`. Nested :class:`SessionCheckpoint`
        dataclasses round-trip via :func:`dataclasses.asdict`. The write goes
        to a temp file then ``os.replace`` so a crash mid-write never leaves a
        truncated (unparseable) checkpoint — the resume guard depends on it.
        """
        import os

        RECRYSTALLIZE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.last_updated = datetime.now(timezone.utc).isoformat()
        target = self.path()
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, target)

    @classmethod
    def load(cls, owner: str) -> Optional["RecrystallizeCheckpoint"]:
        """Load a checkpoint for ``owner``, or ``None`` if none / unreadable.

        Tolerant to legacy / unknown top-level keys (mirrors
        ``import_state._coerce_state``) so a schema addition never orphans an
        in-flight run. Nested ``sessions`` are coerced back into
        :class:`SessionCheckpoint` instances.
        """
        path = RECRYSTALLIZE_STATE_DIR / f"{cls.fingerprint(owner)}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        allowed = {f.name for f in _dc_fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in allowed}

        raw_sessions = kwargs.get("sessions") or {}
        sessions: dict[str, SessionCheckpoint] = {}
        sc_allowed = {f.name for f in _dc_fields(SessionCheckpoint)}
        if isinstance(raw_sessions, dict):
            for sid, sc in raw_sessions.items():
                if isinstance(sc, dict):
                    sessions[sid] = SessionCheckpoint(
                        **{k: v for k, v in sc.items() if k in sc_allowed}
                    )
        kwargs["sessions"] = sessions
        return cls(**kwargs)


# ── Small pure helpers ────────────────────────────────────────────────────────


def _dc_fields(cls_or_instance: Any):
    """Return the dataclass fields for a class or instance (thin wrapper)."""
    from dataclasses import fields as _fields

    return _fields(cls_or_instance)


def _now_iso() -> str:
    """Current UTC time as an ISO 8601 string (checkpoint timestamps)."""
    return datetime.now(timezone.utc).isoformat()


def _ceil_div(n: int, d: int) -> int:
    """Ceiling division; ``_ceil_div(0, d) == 0`` and ``d <= 0`` yields 0."""
    if d <= 0 or n <= 0:
        return 0
    return (n + d - 1) // d


def _first_embedding_dim(embeddings: list[list[float]]) -> int:
    """Dimension of the first non-empty embedding (fallback 640, Harrier dims)."""
    for e in embeddings:
        if e:
            return len(e)
    return 640


def _default_session_id() -> str:
    """Mint a fresh session id (UUIDv7 in production; uuid4 fallback here)."""
    try:  # reuse the import engine's UUIDv7 minter when available
        from totalreclaw.import_engine import _uuid7  # type: ignore

        return _uuid7()
    except Exception:
        import uuid

        return str(uuid.uuid4())


# ── CLI (thin; dry-run default, staging default) ──────────────────────────────


def build_arg_parser() -> Any:
    """Build the operator CLI arg parser.

    Dry-run is the DEFAULT; ``--execute`` opts into writes. Staging is the
    DEFAULT relay; ``--i-understand-this-is-production`` is required to target
    prod (hard project rule: tests hit staging only).
    """
    import argparse

    p = argparse.ArgumentParser(
        prog="totalreclaw-recrystallize",
        description=(
            "Re-key a collapsed on-chain vault into coherent sessions + fresh "
            "Crystals. DRY-RUN by default (writes nothing). STAGING by default."
        ),
    )
    p.add_argument(
        "--recovery-phrase-env",
        default="TOTALRECLAW_RECOVERY_PHRASE",
        help="Env var holding the BIP-39 recovery phrase (never a CLI arg).",
    )
    p.add_argument(
        "--server-url",
        default=STAGING_RELAY_URL,
        help=f"Relay URL (default staging: {STAGING_RELAY_URL}).",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Perform on-chain writes/tombstones. Omit for a dry-run (default).",
    )
    p.add_argument(
        "--write-side-fix-confirmed",
        action="store_true",
        help="Attest the target client runs the #429/#434 write-side fix. "
        "Required for --execute.",
    )
    p.add_argument(
        "--i-understand-this-is-production",
        action="store_true",
        help="Required to target the production relay. Testing must use staging.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (scripted runs). "
        "Still requires --execute and --write-side-fix-confirmed.",
    )
    p.add_argument("--gap-seconds", type=int, default=DEFAULT_GAP_SECONDS)
    p.add_argument("--sim-threshold", type=float, default=DEFAULT_SIM_THRESHOLD)
    return p


def _recovery_phrase_from_env(env_var: str) -> Optional[str]:
    """Read the BIP-39 recovery phrase from ``env_var`` (never a CLI arg)."""
    import os

    phrase = (os.environ.get(env_var) or "").strip()
    return phrase or None


async def _main_async(args: Any) -> int:
    """CLI body — dry-run by default, ``--execute`` opts into on-chain writes.

    Flow: construct the client from the recovery-phrase env var → run the
    dry-run planner → print ``plan.summary_lines()``. If ``--execute`` (and the
    guards pass), confirm interactively (unless ``--yes``) and run
    :func:`execute_recrystallize`, resuming from any existing checkpoint.

    The recovery phrase is read ONLY from the env var (never a CLI arg) and is
    never printed. Returns a process exit code (0 = success / clean quota
    pause; non-zero on refusal or error).
    """
    # Guard: prod requires explicit opt-in.
    if (
        args.server_url == PRODUCTION_RELAY_URL
        and not args.i_understand_this_is_production
    ):
        logger.error(
            "Refusing to target production (%s) without "
            "--i-understand-this-is-production. Use staging (%s).",
            PRODUCTION_RELAY_URL,
            STAGING_RELAY_URL,
        )
        return 2

    phrase = _recovery_phrase_from_env(args.recovery_phrase_env)
    if not phrase:
        logger.error(
            "No recovery phrase in env var %s. Set it (never pass the phrase "
            "as a CLI arg).",
            args.recovery_phrase_env,
        )
        return 2

    from totalreclaw import TotalReclaw

    client = TotalReclaw(
        recovery_phrase=phrase,
        server_url=args.server_url,
        is_test=(args.server_url != PRODUCTION_RELAY_URL),
        suppress_welcome=True,
    )
    try:
        plan = await plan_recrystallize(
            client,
            gap_seconds=args.gap_seconds,
            sim_threshold=args.sim_threshold,
        )
        for line in plan.summary_lines():
            print(line)

        if not args.execute:
            print("\n(dry-run — no writes. Re-run with --execute to apply.)")
            return 0

        if not args.write_side_fix_confirmed:
            logger.error(
                "--execute requires --write-side-fix-confirmed (attest the "
                "target client runs the #429/#434 write-side fix)."
            )
            return 2

        if not args.yes:
            print(
                f"\nAbout to rewrite {plan.estimate.atomic_facts} facts + "
                f"tombstone {plan.estimate.tombstones} on {args.server_url}."
            )
            answer = input("Type 'yes' to proceed: ").strip().lower()
            if answer != "yes":
                print("Aborted.")
                return 1

        checkpoint = RecrystallizeCheckpoint.load(plan.owner)
        try:
            await execute_recrystallize(
                client,
                plan,
                write_side_fix_confirmed=True,
                confirm=True,
                checkpoint=checkpoint,
                progress=lambda m: print(m, flush=True),
            )
        except QuotaPaused:
            print(
                "\nPaused on quota. Re-run the same command after the monthly "
                "quota resets to resume from the checkpoint."
            )
            return 0
        return 0
    finally:
        await client.close()


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    import asyncio

    return asyncio.run(_main_async(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
