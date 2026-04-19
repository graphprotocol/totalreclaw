"""Tests for the Mem0 import adapter.

Mirrors the TypeScript reference test cases in
``skill/plugin/import-adapters/import-adapters.test.ts`` (Mem0Adapter
section) so Python and TS adapters stay feature-identical.

Mem0 is a pre-structured source: the export JSON is a list of already-
atomic memories, so the adapter emits ``facts`` (not ``chunks``) and
the ``ImportEngine`` stores them directly without LLM re-extraction.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Happy-path parsing across the three accepted Mem0 shapes
# ---------------------------------------------------------------------------


class TestMem0ParseShapes:
    def test_api_response_format_results_key(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({
            "results": [
                {"id": "mem-1", "memory": "User prefers dark mode", "categories": ["preference"]},
                {"id": "mem-2", "memory": "User works at Acme Corp", "categories": ["fact"]},
            ]
        })
        result = Mem0Adapter().parse(content=content)
        assert len(result.facts) == 2
        assert result.facts[0].text == "User prefers dark mode"
        assert result.facts[0].type == "preference"
        assert result.facts[0].source == "mem0"
        assert result.facts[1].type == "fact"
        assert result.facts[1].source_id == "mem-2"
        # Pre-structured source → no chunks.
        assert len(result.chunks) == 0

    def test_export_file_format_memories_key(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({
            "export_date": "2026-03-10",
            "memories": [{"id": "mem-1", "memory": "User likes TypeScript"}],
        })
        result = Mem0Adapter().parse(content=content)
        assert len(result.facts) == 1
        assert result.facts[0].text == "User likes TypeScript"

    def test_bare_array_format(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps([
            {"id": "mem-1", "memory": "User prefers Python"},
            {"id": "mem-2", "memory": "User dislikes Java"},
        ])
        result = Mem0Adapter().parse(content=content)
        assert len(result.facts) == 2
        assert result.facts[0].text == "User prefers Python"


# ---------------------------------------------------------------------------
# Validation: empty/short memories dropped, warning surfaced
# ---------------------------------------------------------------------------


class TestMem0Validation:
    def test_skips_empty_and_short_memories(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({
            "results": [
                {"id": "mem-1", "memory": ""},
                {"id": "mem-2", "memory": "Valid fact here"},
                {"id": "mem-3", "memory": "ab"},  # < 3 chars
            ]
        })
        result = Mem0Adapter().parse(content=content)
        assert len(result.facts) == 1
        assert result.facts[0].text == "Valid fact here"
        # Should warn about the 2 skipped entries.
        assert any("2 memories had invalid" in w for w in result.warnings)

    def test_invalid_json_returns_error_not_raises(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        result = Mem0Adapter().parse(content="not json {{{")
        assert len(result.facts) == 0
        assert len(result.errors) > 0
        assert "Failed to parse Mem0 JSON" in result.errors[0]

    def test_missing_content_returns_error(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        result = Mem0Adapter().parse()
        assert len(result.facts) == 0
        assert len(result.errors) > 0
        assert "content" in result.errors[0].lower()

    def test_unrecognized_shape_returns_error(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({"some_key": "some_value"})
        result = Mem0Adapter().parse(content=content)
        assert len(result.facts) == 0
        assert any("Unrecognized Mem0 format" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Category mapping — parity with the TS CATEGORY_MAP
# ---------------------------------------------------------------------------


class TestMem0CategoryMapping:
    def test_category_mapping_matches_ts(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({
            "results": [
                {"id": "1", "memory": "User likes hiking outdoors a lot", "categories": ["like"]},
                {"id": "2", "memory": "User dislikes rainy days", "categories": ["dislike"]},
                {"id": "3", "memory": "Graduated in 2020 with honors", "categories": ["biographical"]},
                {"id": "4", "memory": "Wants to learn Rust next year", "categories": ["objective"]},
                {"id": "5", "memory": "Visited Paris in 2023 on vacation", "categories": ["event"]},
                {"id": "6", "memory": "Chose React over Vue for frontend", "categories": ["decision"]},
                {"id": "7", "memory": "Some item with an unknown category", "categories": ["zzz_unknown"]},
            ]
        })
        result = Mem0Adapter().parse(content=content)
        assert result.facts[0].type == "preference"   # like
        assert result.facts[1].type == "preference"   # dislike
        assert result.facts[2].type == "fact"         # biographical
        assert result.facts[3].type == "goal"         # objective
        assert result.facts[4].type == "episodic"     # event
        assert result.facts[5].type == "decision"     # decision
        assert result.facts[6].type == "fact"         # unknown → default

    def test_metadata_category_fallback(self) -> None:
        """Older Mem0 exports use ``metadata.category`` (singular) instead of
        the top-level ``categories`` array; the adapter must honor both.
        """
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({
            "results": [
                {
                    "id": "m1",
                    "memory": "User prefers vanilla ice cream always",
                    "metadata": {"category": "preference"},
                },
            ]
        })
        result = Mem0Adapter().parse(content=content)
        assert result.facts[0].type == "preference"


# ---------------------------------------------------------------------------
# Importance default + source_id preservation
# ---------------------------------------------------------------------------


class TestMem0FieldDefaults:
    def test_default_importance_is_6(self) -> None:
        """Mem0 has no importance field → adapter must default to 6 so the
        fact clears the ImportEngine's importance >= 6 filter.
        """
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({"results": [{"id": "m1", "memory": "User prefers dark mode everywhere"}]})
        result = Mem0Adapter().parse(content=content)
        assert result.facts[0].importance == 6

    def test_source_timestamp_preserved(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({
            "results": [{
                "id": "m1",
                "memory": "User visited Berlin last month on a trip",
                "metadata": {"updated_at": "2026-03-01T10:00:00Z"},
            }]
        })
        result = Mem0Adapter().parse(content=content)
        assert result.facts[0].source_timestamp == "2026-03-01T10:00:00Z"

    def test_tags_from_categories(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({
            "results": [
                {"id": "m1", "memory": "User prefers tea over coffee", "categories": ["preference", "beverage"]},
            ]
        })
        result = Mem0Adapter().parse(content=content)
        assert "preference" in result.facts[0].tags
        assert "beverage" in result.facts[0].tags


# ---------------------------------------------------------------------------
# File-based parsing (the import_engine hits this path via file_path)
# ---------------------------------------------------------------------------


class TestMem0FileInput:
    def test_file_path_reads_disk(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        content = json.dumps({"memories": [{"id": "m1", "memory": "User likes mountain hiking"}]})
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            tmp_path = f.name
        try:
            result = Mem0Adapter().parse(file_path=tmp_path)
            assert len(result.facts) == 1
            assert result.facts[0].text == "User likes mountain hiking"
        finally:
            os.unlink(tmp_path)

    def test_missing_file_returns_error(self) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        result = Mem0Adapter().parse(file_path="/nonexistent/path/to/mem0.json")
        assert len(result.facts) == 0
        assert len(result.errors) > 0


# ---------------------------------------------------------------------------
# Adapter registry wiring
# ---------------------------------------------------------------------------


class TestMem0RegistryWiring:
    def test_get_adapter_mem0_returns_mem0_adapter(self) -> None:
        from totalreclaw.import_adapters import get_adapter
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        adapter = get_adapter("mem0")
        assert isinstance(adapter, Mem0Adapter)

    def test_list_sources_includes_mem0(self) -> None:
        from totalreclaw.import_adapters import list_sources

        assert "mem0" in list_sources()

    def test_unknown_source_error_lists_mem0(self) -> None:
        from totalreclaw.import_adapters import get_adapter

        with pytest.raises(ValueError) as excinfo:
            get_adapter("bogus")
        assert "mem0" in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end through ImportEngine — golden-file round-trip
# ---------------------------------------------------------------------------


class TestMem0ImportEngineIntegration:
    def test_import_engine_processes_mem0_facts_as_batch(self) -> None:
        """Mem0 adapter + ImportEngine round-trip: 3 facts in → 3 stored.

        Uses a fake client that captures ``remember`` calls; verifies the
        engine routes through the pre-structured-fact path (not LLM
        extraction) and preserves source=import.
        """
        from unittest.mock import AsyncMock, MagicMock
        import asyncio

        from totalreclaw.import_engine import ImportEngine

        fixture = json.dumps({
            "memories": [
                {"id": "m1", "memory": "User prefers dark mode always", "categories": ["preference"]},
                {"id": "m2", "memory": "User works on the TotalReclaw project", "categories": ["fact"]},
                {"id": "m3", "memory": "Decided to use PostgreSQL for the DB", "categories": ["decision"]},
            ]
        })

        fake_client = MagicMock()

        async def _remember(text, **kwargs):
            return f"fact-{text[:20]}"

        fake_client.remember = AsyncMock(side_effect=_remember)

        engine = ImportEngine(client=fake_client, llm_extract=None)

        # Estimate first (matches the tool workflow).
        est = engine.estimate(source="mem0", content=fixture)
        assert est["total_facts"] == 3
        assert est["total_chunks"] == 0
        assert est["has_facts"] is True

        # Process the batch.
        result = asyncio.run(engine.process_batch(
            source="mem0", content=fixture, offset=0, batch_size=25,
        ))
        assert result.success is True
        assert result.facts_stored == 3
        assert result.is_complete is True
        # All three stored with the import engine's source tag.
        assert fake_client.remember.await_count == 3
