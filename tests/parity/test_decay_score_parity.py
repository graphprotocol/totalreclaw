"""graphprotocol/totalreclaw#789 -- on-chain decayScore parity (Python leg).

Fixture ``tests/parity/fixtures/decay-score-v1.json``: 0 = tombstone, 1.0 =
live, no importance. Asserts the PyO3 core function (when the installed core
has it), the client wrapper and its fallback all return the fixture value,
and the fixture threshold equals the subgraph's.

Siblings: ``rust/totalreclaw-core/tests/decay_score_parity.rs`` (Rust),
``tests/parity/decay-score-parity.test.ts`` (TS).

Run (CI ``python-tests`` job, local core wheel installed)::

    cd python && python -m pytest ../tests/parity/test_decay_score_parity.py -v
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import totalreclaw_core

import totalreclaw.operations as ops

HERE = Path(__file__).resolve().parent
FIXTURE = json.loads((HERE / "fixtures" / "decay-score-v1.json").read_text())
MAPPING = (HERE.parents[1] / "subgraph" / "src" / "mapping.ts").read_text()


@pytest.mark.skipif(
    not hasattr(totalreclaw_core, "live_decay_score"),
    reason="installed totalreclaw-core predates live_decay_score",
)
def test_core_matches_fixture():
    assert totalreclaw_core.live_decay_score() == FIXTURE["live"]


def test_wrapper_matches_fixture():
    assert ops.live_decay_score() == FIXTURE["live"]


def test_fallback_matches_fixture():
    class _NoFn:
        pass

    with patch.object(ops, "_core", _NoFn()):
        assert ops.live_decay_score() == FIXTURE["live"]


def test_threshold_matches_subgraph():
    m = re.search(r'INACTIVE_THRESHOLD\s*=\s*BigDecimal\.fromString\("([0-9.]+)"\)', MAPPING)
    assert m is not None
    assert float(m.group(1)) == FIXTURE["subgraph_threshold"]
    assert "decoded.decayScore.ge(INACTIVE_THRESHOLD)" in MAPPING
    assert FIXTURE["live"] >= FIXTURE["subgraph_threshold"]
    assert FIXTURE["tombstone"] == 0
