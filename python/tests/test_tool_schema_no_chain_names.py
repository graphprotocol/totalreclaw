"""Guard (#373, rc6 pre-stable QA): no user-facing tool-schema description
names a chain.

Post-ops-1 (single-chain Gnosis, totalreclaw-internal#283 closed) BOTH tiers
run on Gnosis — the user never needs the chain, and naming it is both noise and
(in the upgrade copy) factually wrong ("Pro routes to Gnosis instead of Base
Sepolia" is false now). The 2.4.4rc6 pre-stable QA (#373, blocker) found
"Gnosis mainnet" + "Base Sepolia" leaking into the ``totalreclaw_upgrade`` and
``totalreclaw_remember`` descriptions. Pin every schema description clean: cite
``totalreclaw_status`` for tier/quota, never a chain name.
"""
from __future__ import annotations

import re

from totalreclaw.hermes import schemas as _schemas

_CHAIN = re.compile(r"\b(Gnosis|Base\s+Sepolia|mainnet|testnet)\b", re.IGNORECASE)


def _iter_schema_dicts():
    """Yield (var_name, schema_dict) for every tool-schema dict in the module."""
    for var_name in dir(_schemas):
        obj = getattr(_schemas, var_name)
        if isinstance(obj, dict) and "description" in obj and "name" in obj:
            yield var_name, obj


def test_no_tool_description_names_a_chain():
    offenders = []
    for var_name, schema in _iter_schema_dicts():
        desc = schema.get("description", "") or ""
        m = _CHAIN.search(desc)
        if m:
            offenders.append(f"{var_name} ({schema.get('name')}): {m.group()!r}")
    assert not offenders, (
        "User-facing tool-schema descriptions must not name a chain "
        "(post-ops-1 single-chain; cite totalreclaw_status for tier/quota): "
        + "; ".join(offenders)
    )


def test_guard_actually_inspects_descriptions():
    """Sanity: the guard sees a non-trivial number of schemas (so a refactor
    that empties the introspection doesn't make the guard vacuously pass)."""
    found = list(_iter_schema_dicts())
    assert len(found) >= 8, (
        f"Expected to inspect the full tool surface; only found {len(found)} "
        "schema dicts — did the schemas module move or change shape?"
    )
