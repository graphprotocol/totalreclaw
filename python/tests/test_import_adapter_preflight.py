"""Tests for the file-size + RAM preflight checks added in imp-5.

Covers all four file-based adapters: Claude, ChatGPT, Gemini, Mem0.
Uses monkeypatching (pytest's built-in) to avoid creating 500MB files.
"""
from __future__ import annotations

import os
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Sentinel path prefix used in size/RAM tests so the mock knows which calls
# to intercept vs. delegate to the real os.stat.
_FAKE_PATH_PREFIX = '/tmp/totalreclaw-preflight-fake-'


def _make_fake_stat(size_bytes: int):
    from unittest.mock import MagicMock
    m = MagicMock()
    m.st_size = size_bytes
    # Make st_mode look like a regular file (0o100644) so any S_ISDIR() checks
    # on the mock return False rather than blowing up.
    m.st_mode = 0o100644
    return m


def _patched_stat(size_bytes: int):
    """Return a drop-in replacement for os.stat that fakes size for preflight paths."""
    _real_stat = os.stat

    def _mock(*args, **kwargs):
        path = str(args[0]) if args else str(kwargs.get('path', ''))
        if _FAKE_PATH_PREFIX in path:
            return _make_fake_stat(size_bytes)
        return _real_stat(*args, **kwargs)

    return _mock


def _fake_path(suffix: str) -> str:
    return f'{_FAKE_PATH_PREFIX}{suffix}'


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
        from totalreclaw.imports.adapters.claude_adapter import ClaudeAdapter
        monkeypatch.setattr(os, 'stat', _patched_stat(501 * 1024 * 1024))
        result = ClaudeAdapter().parse(file_path=_fake_path('claude.txt'))
        self._assert_size_error(result, 'Claude')
        assert '501' in result.errors[0]

    def test_chatgpt_adapter_rejects_oversized_file(self, monkeypatch) -> None:
        from totalreclaw.imports.adapters.chatgpt_adapter import ChatGPTAdapter
        monkeypatch.setattr(os, 'stat', _patched_stat(600 * 1024 * 1024))
        result = ChatGPTAdapter().parse(file_path=_fake_path('chatgpt.json'))
        self._assert_size_error(result, 'ChatGPT')

    def test_gemini_adapter_rejects_oversized_file(self, monkeypatch) -> None:
        from totalreclaw.imports.adapters.gemini_adapter import GeminiAdapter
        monkeypatch.setattr(os, 'stat', _patched_stat(510 * 1024 * 1024))
        result = GeminiAdapter().parse(file_path=_fake_path('gemini.html'))
        self._assert_size_error(result, 'Gemini')

    def test_mem0_adapter_rejects_oversized_file(self, monkeypatch) -> None:
        from totalreclaw.imports.adapters.mem0_adapter import Mem0Adapter
        monkeypatch.setattr(os, 'stat', _patched_stat(520 * 1024 * 1024))
        result = Mem0Adapter().parse(file_path=_fake_path('mem0.json'))
        self._assert_size_error(result, 'Mem0')

    def test_error_message_names_actual_size(self, monkeypatch) -> None:
        from totalreclaw.imports.adapters.claude_adapter import ClaudeAdapter
        monkeypatch.setattr(os, 'stat', _patched_stat(501 * 1024 * 1024))
        result = ClaudeAdapter().parse(file_path=_fake_path('claude-size.txt'))
        assert '501' in result.errors[0], (
            f'Error should name actual size (501MB): {result.errors[0]}'
        )


# ---------------------------------------------------------------------------
# RAM preflight
# ---------------------------------------------------------------------------

class TestRamPreflight:
    def test_claude_adapter_rejects_on_low_ram(self, monkeypatch) -> None:
        import psutil
        from totalreclaw.imports.adapters.claude_adapter import ClaudeAdapter

        # 10MB file, only 1MB free (< 2x = 20MB needed)
        monkeypatch.setattr(os, 'stat', _patched_stat(10 * 1024 * 1024))
        mock_vm = type('VM', (), {'available': 1 * 1024 * 1024})()
        monkeypatch.setattr(psutil, 'virtual_memory', lambda: mock_vm)

        result = ClaudeAdapter().parse(file_path=_fake_path('claude-lowram.txt'))
        assert len(result.errors) > 0, 'Low RAM must return error'
        assert 'memory' in result.errors[0].lower(), (
            f'Error must mention memory: {result.errors[0]}'
        )

    def test_ram_error_names_available_and_needed(self, monkeypatch) -> None:
        import psutil
        from totalreclaw.imports.adapters.chatgpt_adapter import ChatGPTAdapter

        # 100MB file, 50MB free → needs 200MB
        monkeypatch.setattr(os, 'stat', _patched_stat(100 * 1024 * 1024))
        mock_vm = type('VM', (), {'available': 50 * 1024 * 1024})()
        monkeypatch.setattr(psutil, 'virtual_memory', lambda: mock_vm)

        result = ChatGPTAdapter().parse(file_path=_fake_path('chatgpt-lowram.json'))
        assert len(result.errors) > 0
        err = result.errors[0]
        assert '50' in err, f'Error should name available RAM (50MB): {err}'
        assert '200' in err, f'Error should name needed RAM (200MB): {err}'


# ---------------------------------------------------------------------------
# Normal-sized files pass preflight (end-to-end with real temp file)
# ---------------------------------------------------------------------------

class TestPreflightPassthrough:
    def test_claude_normal_file_passes_preflight(self) -> None:
        from totalreclaw.imports.adapters.claude_adapter import ClaudeAdapter

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
        from totalreclaw.imports.adapters.mem0_adapter import Mem0Adapter

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
