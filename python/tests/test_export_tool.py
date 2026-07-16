"""Tests for the ``totalreclaw_export`` Hermes tool (internal#468).

Regression coverage for the large-vault export defect: the tool used to
return the entire decrypted vault inline (``{"count", "facts": [...]}``),
which blew the agent context/output budget on a 472-fact (~119KB) vault.
The result was silently truncated and the agent then *confabulated*
``PROVENANCE: Not present`` — even though the underlying data is correct
(``operations.export_facts`` stamps ``source`` / ``import_source`` /
``session_id`` / ``agent_name`` on every entry that carries them).

The fix:
  - always derive an accurate provenance summary from the real fact data
    (counts, not LLM prose);
  - inline the facts only when the serialized payload is small (explicit
    byte threshold); otherwise write the full dump to a file under the
    TotalReclaw state dir and return the path + summary.

No network, no LLM. Disk writes are redirected to a tmp state dir via
``TOTALRECLAW_STATE_DIR`` (the same override ``tuning_loop.resolve_state_dir``
honours).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totalreclaw.hermes import schemas, tools


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _fact(
    fid: str,
    *,
    type: str = "claim",
    text: str | None = None,
    source: str | None = None,
    import_source: str | None = None,
    session_id: str | None = None,
    agent_name: str | None = None,
) -> dict:
    """Build an export-shaped fact dict (mirrors ``operations.export_facts``)."""
    entry: dict = {
        "id": fid,
        "text": text or f"fact {fid}",
        "timestamp": "",
        "importance": 0.5,
        "type": type,
    }
    # Provenance keys are only present when non-empty (same as the real
    # export path), so omit them entirely when not provided.
    if source:
        entry["source"] = source
    if import_source:
        entry["import_source"] = import_source
    if session_id:
        entry["session_id"] = session_id
    if agent_name:
        entry["agent_name"] = agent_name
    return entry


def _make_state_with_facts(facts: list[dict]):
    """Return a PluginState whose client.export_all() yields *facts*."""
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            from totalreclaw.hermes.state import PluginState

            state = PluginState()
    fake_client = MagicMock()
    fake_client.export_all = AsyncMock(return_value=facts)
    state._client = fake_client
    return state


# ---------------------------------------------------------------------------
# _summarize_export — pure, data-derived provenance summary
# ---------------------------------------------------------------------------


class TestSummarizeExport:
    def test_empty_vault(self):
        s = tools._summarize_export([])
        assert s["total_facts"] == 0
        assert s["by_type"] == {}
        assert s["provenance_present"] is False
        prov = s["provenance"]
        assert prov["with_import_source"] == 0
        assert prov["with_session_id"] == 0
        assert prov["with_agent_name"] == 0
        assert prov["distinct_sessions"] == 0

    def test_mixed_provenance_counts_derived_from_data(self):
        """The exact scenario the agent misreported as 'Not present':
        a mix of import_source / session_id / agent_name / manual source."""
        facts = [
            _fact("1", type="claim", import_source="chatgpt", session_id="s1", agent_name="Crystal"),
            _fact("2", type="preference", import_source="chatgpt", session_id="s1"),
            _fact("3", type="claim", import_source="claude", session_id="s2", agent_name="Crystal"),
            _fact("4", type="directive", source="manual"),
            _fact("5", type="claim", agent_name="Atlas", session_id="s2"),
        ]
        s = tools._summarize_export(facts)

        assert s["total_facts"] == 5
        assert s["by_type"] == {"claim": 3, "preference": 1, "directive": 1}

        # Provenance IS present — pins the fix against the confabulation.
        assert s["provenance_present"] is True

        prov = s["provenance"]
        assert prov["with_import_source"] == 3      # facts 1, 2, 3
        assert prov["with_source"] == 1             # fact 4
        assert prov["with_session_id"] == 4         # facts 1, 2, 3, 5
        assert prov["with_agent_name"] == 3         # facts 1, 3, 5
        assert prov["distinct_sessions"] == 2       # s1, s2

        # Per-client / per-agent breakdowns derived from actual values.
        assert prov["by_import_source"] == {"chatgpt": 2, "claude": 1}
        assert prov["by_agent_name"] == {"Crystal": 2, "Atlas": 1}
        assert prov["by_source"] == {"manual": 1}

    def test_default_type_when_missing(self):
        """A fact with no ``type`` collapses to the export default ``fact``."""
        facts = [{"id": "x", "text": "t"}, {"id": "y", "text": "t", "type": "claim"}]
        s = tools._summarize_export(facts)
        assert s["by_type"] == {"fact": 1, "claim": 1}

    def test_provenance_absent_when_no_field_anywhere(self):
        facts = [_fact("1"), _fact("2", type="preference")]
        s = tools._summarize_export(facts)
        assert s["provenance_present"] is False
        assert s["provenance"]["with_import_source"] == 0
        assert s["provenance"]["by_agent_name"] == {}


# ---------------------------------------------------------------------------
# export_all — large vault → file + summary (no inline facts)
# ---------------------------------------------------------------------------


class TestExportLargeVaultToFile:
    @pytest.mark.asyncio
    async def test_large_export_written_to_file_with_accurate_summary(self, tmp_path, monkeypatch):
        # Force the file path with a tiny threshold so 3 small facts overflow it.
        monkeypatch.setattr(tools, "EXPORT_INLINE_MAX_BYTES", 4)
        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))

        facts = [
            _fact("1", import_source="chatgpt", session_id="s1", agent_name="Crystal"),
            _fact("2", import_source="chatgpt", session_id="s1"),
            _fact("3", import_source="claude", session_id="s2", agent_name="Atlas"),
        ]
        state = _make_state_with_facts(facts)

        raw = await tools.export_all({}, state)
        result = json.loads(raw)

        # The full facts MUST NOT come back inline — that was the bug.
        assert "facts" not in result
        assert result["count"] == 3
        assert result["inline"] is False

        # Summary is derived from the real data, accurate.
        assert result["summary"]["total_facts"] == 3
        assert result["summary"]["provenance_present"] is True
        assert result["summary"]["provenance"]["by_import_source"] == {"chatgpt": 2, "claude": 1}
        assert result["summary"]["provenance"]["by_agent_name"] == {"Crystal": 1, "Atlas": 1}

        # A real file was written under the state dir and contains the full dump.
        export_path = Path(result["export_path"])
        assert export_path.exists()
        assert export_path.is_absolute()
        assert str(export_path).startswith(str(tmp_path))
        on_disk = json.loads(export_path.read_text())
        assert on_disk["count"] == 3
        assert [f["id"] for f in on_disk["facts"]] == ["1", "2", "3"]
        assert on_disk["summary"]["total_facts"] == 3

    @pytest.mark.asyncio
    async def test_export_file_and_dir_permissions_locked_down(self, tmp_path, monkeypatch):
        """The dump is the entire decrypted vault in cleartext — it must follow
        the repo's sensitive-file convention (dir 0700, file 0600, no
        world-readable window), mirroring credentials.json / session_store."""
        import os as _os
        import stat as _stat

        monkeypatch.setattr(tools, "EXPORT_INLINE_MAX_BYTES", 4)
        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
        state = _make_state_with_facts([_fact("1"), _fact("2")])

        result = json.loads(await tools.export_all({}, state))
        export_path = Path(result["export_path"])

        file_mode = _stat.S_IMODE(_os.stat(export_path).st_mode)
        dir_mode = _stat.S_IMODE(_os.stat(export_path.parent).st_mode)
        assert file_mode == 0o600, f"export file mode {oct(file_mode)} != 0600"
        assert dir_mode == 0o700, f"exports dir mode {oct(dir_mode)} != 0700"
        # No .tmp staging file left behind (atomic replace completed).
        assert not list(export_path.parent.glob("*.tmp"))

    @pytest.mark.asyncio
    async def test_file_write_failure_does_not_truncate_or_crash(self, tmp_path, monkeypatch):
        """If the dump can't be saved to disk, the tool must NOT fall back to
        dumping the whole vault inline (that re-introduces the truncation bug).
        It returns the accurate summary + an error instead."""
        monkeypatch.setattr(tools, "EXPORT_INLINE_MAX_BYTES", 4)
        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(tools, "_write_export_file", _boom)

        facts = [_fact("1", import_source="chatgpt"), _fact("2")]
        state = _make_state_with_facts(facts)

        result = json.loads(await tools.export_all({}, state))

        assert "facts" not in result          # never inline the big payload
        assert result["inline"] is False
        assert "export_path" not in result
        assert "error" in result              # surfaced, not swallowed
        # The summary is still accurate so the agent isn't left blind.
        assert result["summary"]["total_facts"] == 2
        assert result["summary"]["provenance"]["with_import_source"] == 1


# ---------------------------------------------------------------------------
# export_all — small vault → inline facts + summary
# ---------------------------------------------------------------------------


class TestExportSmallVaultInline:
    @pytest.mark.asyncio
    async def test_small_export_inlined_with_summary(self, tmp_path, monkeypatch):
        # Default threshold is large enough that 2 tiny facts stay inline.
        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))

        facts = [
            _fact("1", type="claim", import_source="chatgpt", agent_name="Crystal"),
            _fact("2", type="preference"),
        ]
        state = _make_state_with_facts(facts)

        result = json.loads(await tools.export_all({}, state))

        # Backward-compatible shape: count + facts present, plus the new summary.
        assert result["inline"] is True
        assert result["count"] == 2
        assert [f["id"] for f in result["facts"]] == ["1", "2"]
        assert result["summary"]["total_facts"] == 2
        assert result["summary"]["by_type"] == {"claim": 1, "preference": 1}
        assert result["summary"]["provenance_present"] is True

    @pytest.mark.asyncio
    async def test_not_configured_returns_error(self):
        from totalreclaw.hermes.state import PluginState

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "exists", return_value=False):
                state = PluginState()
        # No client configured.
        result = json.loads(await tools.export_all({}, state))
        assert "error" in result


# ---------------------------------------------------------------------------
# schema — backward compatible
# ---------------------------------------------------------------------------


class TestExportSchema:
    def test_name_and_empty_required_params(self):
        assert schemas.EXPORT["name"] == "totalreclaw_export"
        params = schemas.EXPORT["parameters"]
        assert params["type"] == "object"
        # No required params → existing no-arg calls keep working.
        assert params.get("required", []) == []

    def test_description_advertises_summary_and_file_behavior(self):
        """The agent must know large exports are summarized + saved to a file,
        so it stops confabulating 'provenance not present'."""
        desc = schemas.EXPORT["description"].lower()
        assert "provenance" in desc
        assert "file" in desc or "path" in desc
        assert "summary" in desc
