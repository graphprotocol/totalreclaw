"""``activate-memory-provider`` must leave Hermes' config.yaml valid.

Hermes 0.22's stock config.yaml has comments and blank lines inside the
``memory:`` block. The old regex treated the block as ending at the first
blank line, so ``user_profile_enabled`` (further down) was not found and a
second copy was appended. Hermes refuses a file with a duplicate key and
runs on default settings, dropping the user's model/provider. The same
regex also hid a ``provider:`` key that Hermes itself writes at the end of
the block, so ``memory-status`` reported ``none``.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from totalreclaw.hermes import install_memory_provider as imp

# The memory block of Hermes 0.22.5's config.yaml after any `hermes config
# set` (Hermes' own rewrite strips the trailing spaces on blank lines).
_HERMES_022_CONFIG = """\
_config_version: 51
model:
  provider: zai
  default: glm-5-turbo

# =============================================================================
# Persistent Memory
# =============================================================================
# Bounded curated memory injected into the system prompt every session.
#
memory:
  # Agent's personal notes: environment facts, conventions, things learned
  memory_enabled: true

  # User profile: preferences, communication style, expectations
  user_profile_enabled: true

  # External prefetch only: opt in to head/tail previews using hooks.output_spill.
  prefetch_spill_enabled: false

  # Character limits (~2.75 chars per token, model-independent)
  memory_char_limit: 2200   # ~800 tokens
  user_char_limit: 1375     # ~500 tokens

  # Periodic memory nudge: remind the agent to consider saving memories
  nudge_interval: 10        # Nudge every 10 user turns (0 = disabled)

# =============================================================================
# Session Continuity (Messaging Platforms)
# =============================================================================
max_concurrent_sessions:

group_sessions_per_user: true
"""

# What versions before this fix produced from the config above.
_CORRUPTED_BY_OLD_VERSION = _HERMES_022_CONFIG.replace(
    "  memory_enabled: true\n",
    "  memory_enabled: false\n  provider: totalreclaw\n  user_profile_enabled: false\n",
)


class _DupLoader(yaml.SafeLoader):
    pass


def _no_dups(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    assert len(keys) == len(set(keys)), f"duplicate keys in mapping: {keys}"
    return loader.construct_mapping(node, deep=deep)


_DupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dups)


def _parse(text: str) -> dict:
    return yaml.load(text, Loader=_DupLoader)


def _write(home: Path, text: str) -> Path:
    cfg = imp.config_path(home)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_bytes(text.encode("utf-8"))
    return cfg


def test_activation_on_stock_hermes_022_config_stays_valid(tmp_path):
    cfg = _write(tmp_path, _HERMES_022_CONFIG)
    imp.install_and_activate(hermes_home=tmp_path, activate=True)
    body = cfg.read_text(encoding="utf-8")
    data = _parse(body)  # no duplicate keys anywhere
    assert data["memory"]["provider"] == "totalreclaw"
    assert data["memory"]["memory_enabled"] is False
    assert data["memory"]["user_profile_enabled"] is False
    assert body.count("user_profile_enabled") == 1
    # Untouched settings and every comment survive.
    assert data["model"] == {"provider": "zai", "default": "glm-5-turbo"}
    assert data["memory"]["nudge_interval"] == 10
    for line in _HERMES_022_CONFIG.splitlines():
        if line.lstrip().startswith("#"):
            assert line in body
    assert "  memory_char_limit: 2200   # ~800 tokens\n" in body
    # The new key goes after the block's last entry, before the next section.
    assert body.index("  provider: totalreclaw") < body.index("# Session Continuity")
    assert imp.read_active_provider(tmp_path) == "totalreclaw"


def test_activation_is_idempotent(tmp_path):
    cfg = _write(tmp_path, _HERMES_022_CONFIG)
    imp.install_and_activate(hermes_home=tmp_path, activate=True)
    first = cfg.read_text(encoding="utf-8")
    imp.install_and_activate(hermes_home=tmp_path, activate=True)
    assert cfg.read_text(encoding="utf-8") == first


def test_activation_repairs_config_corrupted_by_older_version(tmp_path):
    with pytest.raises(AssertionError):
        _parse(_CORRUPTED_BY_OLD_VERSION)  # sanity: the fixture IS broken
    cfg = _write(tmp_path, _CORRUPTED_BY_OLD_VERSION)
    imp.install_and_activate(hermes_home=tmp_path, activate=True)
    body = cfg.read_text(encoding="utf-8")
    data = _parse(body)
    assert data["memory"]["user_profile_enabled"] is False
    assert data["memory"]["provider"] == "totalreclaw"
    assert body.count("user_profile_enabled") == 1
    assert body.count("provider: totalreclaw") == 1
    assert "  # User profile: preferences, communication style, expectations\n" in body


def test_memory_status_reads_provider_hermes_put_at_block_end(tmp_path):
    # `hermes config set memory.provider totalreclaw` appends the key at the
    # end of the mapping, after blank lines and even after the next
    # section's column-0 comments.
    text = _HERMES_022_CONFIG.replace(
        "max_concurrent_sessions:\n",
        "  provider: totalreclaw\nmax_concurrent_sessions:\n",
    )
    assert _parse(text)["memory"]["provider"] == "totalreclaw"
    _write(tmp_path, text)
    assert imp.read_active_provider(tmp_path) == "totalreclaw"

    from totalreclaw.hermes.cli import main

    assert main(["memory-status", "--hermes-home", str(tmp_path)]) == 0


def test_memory_status_cli_reports_totalreclaw(tmp_path, capsys):
    from totalreclaw.cli import main

    _write(tmp_path, _HERMES_022_CONFIG)
    imp.install_and_activate(hermes_home=tmp_path, activate=True)
    capsys.readouterr()
    rc = main(["memory-status", "--hermes-home", str(tmp_path)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"provider": "totalreclaw"}


def test_existing_provider_replaced_in_place_with_comment(tmp_path):
    cfg = _write(tmp_path, "memory:\n  provider: 'honcho'   # was honcho\n  memory_enabled: true\n")
    imp.set_active_provider("totalreclaw", hermes_home=tmp_path)
    body = cfg.read_text(encoding="utf-8")
    assert body.splitlines()[1] == "  provider: totalreclaw   # was honcho"
    assert _parse(body)["memory"] == {"provider": "totalreclaw", "memory_enabled": True}


def test_quoted_provider_reads_back(tmp_path):
    _write(tmp_path, 'memory:\n  provider: "Honcho"  # remote\n')
    assert imp.read_active_provider(tmp_path) == "honcho"


def test_nested_provider_key_is_not_the_memory_provider(tmp_path):
    cfg = _write(
        tmp_path,
        "memory:\n  honcho:\n    provider: cloud\n  memory_enabled: true\n",
    )
    assert imp.read_active_provider(tmp_path) == ""
    imp.set_active_provider("totalreclaw", hermes_home=tmp_path)
    data = _parse(cfg.read_text(encoding="utf-8"))
    assert data["memory"]["honcho"] == {"provider": "cloud"}
    assert data["memory"]["provider"] == "totalreclaw"


def test_four_space_indent_is_respected(tmp_path):
    cfg = _write(tmp_path, "memory:\n    memory_enabled: true\nother: 1\n")
    imp.install_and_activate(hermes_home=tmp_path, activate=True)
    body = cfg.read_text(encoding="utf-8")
    assert "    provider: totalreclaw\n" in body
    assert _parse(body)["memory"]["provider"] == "totalreclaw"


def test_empty_inline_memory_mapping_becomes_a_block(tmp_path):
    cfg = _write(tmp_path, "memory: {}  # nothing yet\nother: 1\n")
    imp.set_active_provider("totalreclaw", hermes_home=tmp_path)
    body = cfg.read_text(encoding="utf-8")
    assert body.startswith("memory:  # nothing yet\n  provider: totalreclaw\n")
    assert _parse(body) == {"memory": {"provider": "totalreclaw"}, "other": 1}


def test_flow_mapping_is_refused_and_file_left_alone(tmp_path):
    original = "memory: {provider: honcho}\n"
    cfg = _write(tmp_path, original)
    with pytest.raises(imp.HermesConfigEditError):
        imp.set_active_provider("totalreclaw", hermes_home=tmp_path)
    assert cfg.read_text(encoding="utf-8") == original


def test_crlf_line_endings_preserved(tmp_path):
    cfg = _write(tmp_path, "memory:\r\n  memory_enabled: true\r\nother: 1\r\n")
    imp.install_and_activate(hermes_home=tmp_path, activate=True)
    raw = cfg.read_bytes().decode("utf-8")
    assert "\n" not in raw.replace("\r\n", "")
    assert _parse(raw)["memory"]["provider"] == "totalreclaw"


def test_no_memory_block_appends_one(tmp_path):
    cfg = _write(tmp_path, "model:\n  provider: zai")  # no trailing newline
    imp.install_and_activate(hermes_home=tmp_path, activate=True)
    data = _parse(cfg.read_text(encoding="utf-8"))
    assert data["model"] == {"provider": "zai"}
    assert data["memory"] == {
        "provider": "totalreclaw",
        "memory_enabled": False,
        "user_profile_enabled": False,
    }


def test_write_keeps_file_mode_and_leaves_no_temp_file(tmp_path):
    cfg = _write(tmp_path, _HERMES_022_CONFIG)
    os.chmod(cfg, 0o600)
    imp.install_and_activate(hermes_home=tmp_path, activate=True)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert sorted(p.name for p in cfg.parent.iterdir() if p.is_file()) == ["config.yaml"]


def test_invalid_result_is_never_written(tmp_path, monkeypatch):
    cfg = _write(tmp_path, _HERMES_022_CONFIG)
    monkeypatch.setattr(
        imp, "_set_memory_key", lambda text, key, value: text + "memory:\n  x: 1\n"
    )
    with pytest.raises(imp.HermesConfigEditError, match="duplicate key"):
        imp.set_active_provider("totalreclaw", hermes_home=tmp_path)
    assert cfg.read_text(encoding="utf-8") == _HERMES_022_CONFIG
