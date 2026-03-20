"""
Server-blind observability endpoint for TotalReclaw Server.

GET /v1/metrics — returns per-user operational metrics.
All metrics are derived from in-memory telemetry and DB counts.
No plaintext or encryption keys are ever exposed.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..db import get_db, Database
from ..dependencies import get_current_user
from ..search_telemetry import telemetry_store

router = APIRouter(tags=["observability"])


class UserMetricsResponse(BaseModel):
    """Per-user operational metrics."""
    fact_count: int
    avg_candidates_per_search: float
    max_candidates_hit_rate: float
    p95_search_latency_ms: float


@router.get("/metrics", response_model=UserMetricsResponse)
async def user_metrics(
    user_id: str = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """
    Return per-user operational metrics for server-blind observability.

    Requires authentication (same HKDF auth as other endpoints).

    Metrics:
    - fact_count: Number of active (non-deleted) facts for this user
    - avg_candidates_per_search: Rolling average of total_candidates_matched
      over the last 100 searches. 0 if no searches recorded.
    - max_candidates_hit_rate: Fraction of recent searches where
      total_candidates_matched >= max_candidates requested. 0 if no data.
    - p95_search_latency_ms: P95 of GIN index query time over recent
      searches. 0 if no data.
    """
    # Get fact count from DB
    fact_count = await db.count_active_facts(user_id)

    # Get rolling search telemetry from in-memory store
    telemetry = telemetry_store.get(user_id)

    if telemetry:
        avg_candidates = telemetry.avg_candidates()
        hit_rate = telemetry.max_candidates_hit_rate()
        p95_latency = telemetry.p95_latency_ms()
    else:
        avg_candidates = 0.0
        hit_rate = 0.0
        p95_latency = 0.0

    return UserMetricsResponse(
        fact_count=fact_count,
        avg_candidates_per_search=avg_candidates,
        max_candidates_hit_rate=hit_rate,
        p95_search_latency_ms=p95_latency,
    )
