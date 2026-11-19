"""H14: recall marks pinned memories; forget reports ``was_pinned``."""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

from totalreclaw.hermes import tools
from totalreclaw.operations import ForgetResult
from totalreclaw.reranker import RerankerResult

PINNED_ID = "655a229d-713b-4984-9add-322dbf2b443a"
PINNED_TEXT = (
    "Tidewater is moving analytics workloads off Postgres to ClickHouse; "
    "Postgres will be retained for transactional workloads only."
)


def _state(client):
    state = MagicMock()
    state.get_client.return_value = client
    state.get_recall_top_k.return_value = 8
    state.get_max_candidate_pool.return_value = 250
    return state


async def _recall(results):
    client = MagicMock()
    client.recall = AsyncMock(return_value=results)
    out = await tools.recall({"query": "analytics"}, _state(client))
    return json.loads(out)["memories"]


async def test_recall_marks_pinned_memory() -> None:
    mems = await _recall([RerankerResult(id=PINNED_ID, text=PINNED_TEXT, pinned=True)])
    assert mems[0]["pinned"] is True


async def test_recall_omits_pinned_key_when_not_pinned() -> None:
    mems = await _recall([RerankerResult(id="x1", text="User likes jazz", pinned=False)])
    assert "pinned" not in mems[0]


def _forget_client(result):
    client = MagicMock()
    client.forget = AsyncMock(return_value=result)
    return client


async def test_forget_reports_was_pinned(caplog) -> None:
    client = _forget_client(ForgetResult(success=True, fact_id=PINNED_ID, pinned=True))
    caplog.set_level(logging.INFO, logger="totalreclaw.hermes.tools")
    out = json.loads(await tools.forget({"fact_id": PINNED_ID}, _state(client)))
    assert out == {"deleted": True, "fact_id": PINNED_ID, "was_pinned": True}
    client.forget.assert_awaited_once()
    assert "forget: tombstoned (pinned=true, via=tool)" in caplog.text
    assert PINNED_ID not in caplog.text


async def test_forget_unpinned_has_no_was_pinned(caplog) -> None:
    client = _forget_client(ForgetResult(success=True, fact_id="x1", pinned=False))
    caplog.set_level(logging.INFO, logger="totalreclaw.hermes.tools")
    out = json.loads(await tools.forget({"fact_id": "x1"}, _state(client)))
    assert out == {"deleted": True, "fact_id": "x1"}
    assert "forget: tombstoned (pinned=false, via=tool)" in caplog.text


async def test_forget_unknown_pin_state_and_mirror(caplog) -> None:
    client = _forget_client(ForgetResult(success=True, fact_id="x1"))
    caplog.set_level(logging.INFO, logger="totalreclaw.hermes.tools")
    out = json.loads(await tools.forget({"fact_id": "x1"}, _state(client), _from_mirror=True))
    assert "was_pinned" not in out
    assert "forget: tombstoned (pinned=unknown, via=mirror)" in caplog.text
