"""Re-crystallize / re-key backfill for collapsed on-chain sessions.

**SCAFFOLD — not a runnable on-chain writer.** The dry-run planner
(:func:`plan_recrystallize`) and all *pure* logic (segmentation → session plan,
cost estimation) are implemented and unit-tested. The on-chain write/tombstone
path (:func:`execute_recrystallize`) is a **stub that raises
``NotImplementedError``**. See ``docs/specs/totalreclaw/recrystallize-backfill.md``
for the full design.

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

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Single source of truth for the relay URL (repo forbids duplicate URL literals).
from .relay import _HARDCODED_DEFAULT_URL as _CANONICAL_PROD_URL

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


# ── Fetch + decrypt front-end (STUB — network/crypto path) ────────────────────


async def fetch_and_decrypt_vault(client: Any) -> list[DecryptedFact]:
    """Fetch all active facts for the owner and decrypt them client-side.

    STUB. The real implementation:
      1. Paginates ``facts(where: {owner, isActive: true})`` via
         ``client._relay.query_subgraph`` requesting ``id, encryptedBlob,
         encryptedEmbedding, createdAt, timestamp, decayScore`` (a query wider
         than ``EXPORT_QUERY`` — it must include ``encryptedEmbedding``).
      2. For each fact: ``decrypt(encryptedBlob)`` → ``_decode_raw_blob`` (NOT
         ``read_blob_unified`` — see design §3.1), ``decrypt_embedding`` for the
         vector, and ``createdAt`` → Unix seconds.
      3. Skips digest blobs (``is_digest_blob``) and tombstoned stubs.

    Returns a list of :class:`DecryptedFact` (atomic + Crystals; the caller
    splits them via :func:`split_facts`).
    """
    raise NotImplementedError(
        "fetch_and_decrypt_vault: subgraph fetch + client-side decrypt not "
        "implemented in the scaffold. TODO: reuse the paginated subgraph query "
        "+ crypto path from operations.export_facts, widened to include "
        "encryptedEmbedding and to decode raw metadata via _decode_raw_blob."
    )


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


# ── Dry-run entry point (front-end: fetch is stubbed, planning is real) ───────


async def plan_recrystallize(
    client: Any,
    *,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
) -> RecrystallizePlan:
    """DRY-RUN: fetch + decrypt the vault, re-segment, and estimate cost.

    Writes NOTHING on-chain. This is the default, safe entry point. The pure
    planning half (:func:`build_plan`) is fully implemented and tested; only the
    fetch/decrypt front-end (:func:`fetch_and_decrypt_vault`) is stubbed.
    """
    await client._ensure_address()
    owner = client.wallet_address
    decrypted = await fetch_and_decrypt_vault(client)
    return build_plan(
        owner,
        decrypted,
        gap_seconds=gap_seconds,
        sim_threshold=sim_threshold,
    )


# ── Execute entry point (STUB — on-chain writer, guarded) ─────────────────────


async def execute_recrystallize(
    client: Any,
    plan: RecrystallizePlan,
    *,
    write_side_fix_confirmed: bool = False,
    confirm: bool = False,
    checkpoint: Optional["RecrystallizeCheckpoint"] = None,
) -> None:
    """EXECUTE the backfill: write corrected data, tombstone old data.

    **STUB — raises ``NotImplementedError``.** The on-chain write path is
    intentionally not built in this scaffold.

    When implemented, per corrected session (design §4.3 order):
      1. ``client.remember_batch`` the rewritten facts (fresh ``session_id`` in
         ``extra_metadata``, original embedding reused) in ≤30-fact batches.
      2. Build + write a fresh Crystal (reuse import_engine ``_make_crystal``)
         for multi-fact sessions.
      3. ``confirm_indexed`` the new facts, THEN tombstone the old facts
         (``client.forget`` per-fact, or a future ``forget_batch``).
    Then tombstone every old mixed Crystal.

    Guards (must ALL pass before any write):
      - ``write_side_fix_confirmed`` — attests the target client runs the
        #429/#434 fix (else new writes re-collapse mid-migration; design §8).
      - ``confirm`` — explicit operator go-ahead after reviewing the dry-run.
      - ``checkpoint`` — resumability (design §6); phase-gates each session so a
        re-run never double-writes or double-tombstones.
    """
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
    # TODO(recrystallize): implement the guarded on-chain write/tombstone loop.
    #   - iterate plan.corrected_sessions, phase-gated by `checkpoint`
    #   - client.remember_batch(...) rewritten facts (≤30/batch)
    #   - _make_crystal(...) + write for multi-fact sessions
    #   - confirm_indexed(...) then client.forget(...) old facts
    #   - tombstone plan.old_crystals
    #   - on 403 quota_exceeded: mark checkpoint paused_quota, write, exit clean
    raise NotImplementedError(
        "execute_recrystallize: on-chain write/tombstone path is a scaffold "
        "stub. See docs/specs/totalreclaw/recrystallize-backfill.md §4."
    )


# ── Checkpoint / resumability (STUB persistence; shape is real) ───────────────


@dataclass
class SessionCheckpoint:
    """Per-session progress record. See design §6.1."""

    phase: str = "planned"  # planned | written | tombstoned | done
    old_fact_ids: list[str] = field(default_factory=list)
    new_fact_ids: list[str] = field(default_factory=list)
    crystal_written: bool = False
    old_crystal_ids_tombstoned: list[str] = field(default_factory=list)


@dataclass
class RecrystallizeCheckpoint:
    """Whole-run checkpoint, persisted to
    ``~/.totalreclaw/recrystallize-state/<vault_fingerprint>.json``.

    Persistence (:meth:`save` / :meth:`load`) is a STUB. The dataclass shape is
    final; wire it to the same JSON round-trip pattern as ``import_state.py``.
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
        raise NotImplementedError(
            "RecrystallizeCheckpoint.save: TODO — mirror import_state.write_import_state "
            "(mkdir -p RECRYSTALLIZE_STATE_DIR, json.dumps(asdict(self)))."
        )

    @classmethod
    def load(cls, owner: str) -> Optional["RecrystallizeCheckpoint"]:
        raise NotImplementedError(
            "RecrystallizeCheckpoint.load: TODO — mirror import_state.read_import_state "
            "(read <fingerprint>.json, coerce to dataclass, tolerate legacy keys)."
        )


# ── Small pure helpers ────────────────────────────────────────────────────────


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
        from .import_engine import _uuid7  # type: ignore

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
    p.add_argument("--gap-seconds", type=int, default=DEFAULT_GAP_SECONDS)
    p.add_argument("--sim-threshold", type=float, default=DEFAULT_SIM_THRESHOLD)
    return p


async def _main_async(args: Any) -> int:
    """CLI body. Dry-run is implemented end-to-end EXCEPT the stubbed fetch;
    ``--execute`` is fully stubbed. Kept minimal — this is a scaffold."""
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

    # TODO(recrystallize): construct the client from the recovery-phrase env var,
    # run plan_recrystallize (dry-run), print plan.summary_lines(), and — only if
    # --execute — call execute_recrystallize(... write_side_fix_confirmed=...,
    # confirm=<interactive prompt>). Left as a stub; the on-chain path is not
    # built in this scaffold.
    raise NotImplementedError(
        "recrystallize CLI is a scaffold. The pure planner (build_plan / "
        "estimate_quota_cost) is implemented and unit-tested; wire the "
        "fetch+client front-end before enabling this."
    )


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    import asyncio

    return asyncio.run(_main_async(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
