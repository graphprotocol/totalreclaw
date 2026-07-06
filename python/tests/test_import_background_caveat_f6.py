"""F6 — background import needs a long-lived Hermes process.

A background import spawns an asyncio.Task that outlives the tool call. Under a
one-shot ``hermes chat -q`` invocation the process exits before the task
finishes, so nothing is stored. This is a copy-only guard: the background-ack
message and the IMPORT_FROM schema description must carry the caveat so the
agent tells the user (same style as the pair tool's long-lived-process note).
"""
from __future__ import annotations

from totalreclaw.hermes.schemas import IMPORT_FROM


def test_import_from_schema_description_has_long_lived_caveat():
    desc = IMPORT_FROM["description"].lower()
    assert "long-lived" in desc or "long lived" in desc
    assert "one-shot" in desc or "one shot" in desc


def test_import_from_schema_mentions_gateway_or_daemon():
    desc = IMPORT_FROM["description"].lower()
    assert "gateway" in desc or "daemon" in desc
