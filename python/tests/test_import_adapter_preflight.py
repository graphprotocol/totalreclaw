"""Tests for the file-size + RAM preflight checks added in imp-5.

Covers all four file-based adapters: Claude, ChatGPT, Gemini, Mem0.
Uses monkeypatching (pytest's built-in) to avoid creating 500MB files.
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stat(size_bytes: int):
    """Return a real os.stat_result with the given size, reusing /dev/null's stat."""
    real = os.stat('/dev/null')
    # os.stat_result is immutable but we can build a fake via os.stat_result((mode,...))
    # Simpler: use unittest.mock.MagicMock
    from unittest.mock import MagicMock
    m = MagicMock()
    m.st_size = size_bytes
    return m


# ---------------------------------------------------------------------------
# 500MB hard cap
# ---------------------------------------------------------------------------

class TestFileSizeCap:
    def _assert_size_error(self, result, adapter_name: str) -> None:
        assert len(result.errors) > 0, f'{adapter_name}: oversized file must return error'
        assert '500MB' in result.errors[0], (
            f'{adapter_name}: error must mention 500MB cap, got: {result.errors[0]}'
        )
        assert len(result.facts) == 0
        assert len(result.chunks) == 0

    def test_claude_adapter_rejects_oversized_file(self, monkeypatch) -> None:
        from totalreclaw.import_adapters.claude_adapter import ClaudeAdapter
        monkeypatch.setattr(os, 'stat', lambda _: _make_stat(501 * 1024 * 1024))
        result = ClaudeAdapter().parse(file_path='/tmp/fake-large.txt')
        self._assert_size_error(result, 'Claude')
        assert '501' in result.errors[0] or '500MB' in result.errors[0]

    def test_chatgpt_adapter_rejects_oversized_file(self, monkeypatch) -> None:
        from totalreclaw.import_adapters.chatgpt_adapter import ChatGPTAdapter
        monkeypatch.setattr(os, 'stat', lambda _: _make_stat(600 * 1024 * 1024))
        result = ChatGPTAdapter().parse(file_path='/tmp/fake-large.json')
        self._assert_size_error(result, 'ChatGPT')

    def test_gemini_adapter_rejects_oversized_file(self, monkeypatch) -> None:
        from totalreclaw.import_adapters.gemini_adapter import GeminiAdapter
        monkeypatch.setattr(os, 'stat', lambda _: _make_stat(510 * 1024 * 1024))
        result = GeminiAdapter().parse(file_path='/tmp/fake-large.html')
        self._assert_size_error(result, 'Gemini')

    def test_mem0_adapter_rejects_oversized_file(self, monkeypatch) -> None:
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter
        monkeypatch.setattr(os, 'stat', lambda _: _make_stat(520 * 1024 * 1024))
        result = Mem0Adapter().parse(file_path='/tmp/fake-large.json')
        self._assert_size_error(result, 'Mem0')

    def test_error_message_names_actual_size(self, monkeypatch) -> None:
        from totalreclaw.import_adapters.claude_adapter import ClaudeAdapter
        monkeypatch.setattr(os, 'stat', lambda _: _make_stat(501 * 1024 * 1024))
        result = ClaudeAdapter().parse(file_path='/tmp/fake-large.txt')
        # Error should name the actual file size (501.0MB)
        assert '501' in result.errors[0], (
            f'Error should name actual size: {result.errors[0]}'
        )


# ---------------------------------------------------------------------------
# RAM preflight
# ---------------------------------------------------------------------------

class TestRamPreflight:
    def test_claude_adapter_rejects_on_low_ram(self, monkeypatch) -> None:
        import psutil
        from totalreclaw.import_adapters.claude_adapter import ClaudeAdapter

        # 10MB file, but only 1MB free (< 2x = 20MB needed)
        monkeypatch.setattr(os, 'stat', lambda _: _make_stat(10 * 1024 * 1024))
        mock_vm = type('VM', (), {'available': 1 * 1024 * 1024})()
        monkeypatch.setattr(psutil, 'virtual_memory', lambda: mock_vm)

        result = ClaudeAdapter().parse(file_path='/tmp/fake-low-mem.txt')
        assert len(result.errors) > 0, 'Low RAM must return error'
        assert 'memory' in result.errors[0].lower(), (
            f'Error must mention memory: {result.errors[0]}'
        )

    def test_ram_error_names_available_and_needed(self, monkeypatch) -> None:
        import psutil
        from totalreclaw.import_adapters.chatgpt_adapter import ChatGPTAdapter

        # 100MB file, 50MB free → needs 200MB
        monkeypatch.setattr(os, 'stat', lambda _: _make_stat(100 * 1024 * 1024))
        mock_vm = type('VM', (), {'available': 50 * 1024 * 1024})()
        monkeypatch.setattr(psutil, 'virtual_memory', lambda: mock_vm)

        result = ChatGPTAdapter().parse(file_path='/tmp/fake-low-mem.json')
        assert len(result.errors) > 0
        err = result.errors[0]
        assert '50' in err, f'Error should name available RAM (50MB): {err}'
        assert '200' in err, f'Error should name needed RAM (200MB): {err}'


# ---------------------------------------------------------------------------
# Normal-sized files pass preflight (end-to-end with real temp file)
# ---------------------------------------------------------------------------

class TestPreflightPassthrough:
    def test_claude_normal_file_passes_preflight(self) -> None:
        from totalreclaw.import_adapters.claude_adapter import ClaudeAdapter

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write('User prefers dark mode\nUser works remotely\n')
            path = f.name

        try:
            result = ClaudeAdapter().parse(file_path=path)
            assert len(result.errors) == 0, f'Normal file should have no errors: {result.errors}'
            assert len(result.chunks) > 0, 'Normal file should produce chunks'
        finally:
            os.unlink(path)

    def test_mem0_normal_file_passes_preflight(self) -> None:
        import json
        from totalreclaw.import_adapters.mem0_adapter import Mem0Adapter

        data = {'results': [{'id': '1', 'memory': 'User prefers TypeScript', 'categories': ['preference']}]}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(data, f)
            path = f.name

        try:
            result = Mem0Adapter().parse(file_path=path)
            assert len(result.errors) == 0, f'Normal file should have no errors: {result.errors}'
            assert len(result.facts) == 1
        finally:
            os.unlink(path)
