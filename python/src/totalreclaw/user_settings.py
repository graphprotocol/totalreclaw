"""User-facing TotalReclaw settings that can change at runtime.

Stored as a small JSON object at ``~/.totalreclaw/settings.json`` (the
``TOTALRECLAW_STATE_DIR`` override is honoured, same as every other
disk-touching helper). Unlike env vars, a setting written here takes effect
on the next read without restarting the agent: readers load the file each
time they need a value, and the file is tiny.

Today the only key is ``extraction_model`` (set by the
``totalreclaw_set_extraction_model`` Hermes tool and the ``totalreclaw
extraction-model`` CLI). Nothing secret may ever be stored here: no
recovery phrase, no API key.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"

#: Key for the user's extraction-model choice. Absent = automatic.
EXTRACTION_MODEL_KEY = "extraction_model"


def settings_path() -> Path:
    """``<state_dir>/settings.json``; honours ``TOTALRECLAW_STATE_DIR``."""
    override = os.environ.get("TOTALRECLAW_STATE_DIR")
    base = Path(override) if override else Path.home() / ".totalreclaw"
    return base / SETTINGS_FILENAME


def load_settings() -> dict[str, Any]:
    """Return the settings object, or ``{}`` when missing or unreadable.

    A corrupt file is treated as empty (and logged at DEBUG) so a bad edit
    can never break extraction; the next write replaces it.
    """
    path = settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        logger.debug("user_settings: cannot read %s (%s)", path, e)
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        logger.debug("user_settings: %s is not valid JSON (%s)", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def get_setting(key: str) -> Optional[Any]:
    return load_settings().get(key)


def set_setting(key: str, value: Optional[Any]) -> None:
    """Set ``key`` to ``value`` (``None`` removes it) and write atomically.

    Write-to-temp + ``os.replace`` so a concurrent reader (the Hermes
    gateway reading on its next turn while the CLI writes) never sees a
    half-written file. Raises ``OSError`` if the state dir is not writable;
    callers surface that to the user.
    """
    path = settings_path()
    data = load_settings()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{SETTINGS_FILENAME}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def get_extraction_model() -> Optional[str]:
    """The user's extraction-model choice, or ``None`` for automatic."""
    value = get_setting(EXTRACTION_MODEL_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def set_extraction_model(model: Optional[str]) -> Optional[str]:
    """Persist the extraction-model choice; empty / ``"default"`` / ``"auto"``
    clears it. Returns the stored value (``None`` when cleared)."""
    cleaned = (model or "").strip()
    if cleaned.lower() in ("", "default", "auto", "automatic", "none", "reset"):
        set_setting(EXTRACTION_MODEL_KEY, None)
        return None
    set_setting(EXTRACTION_MODEL_KEY, cleaned)
    return cleaned
