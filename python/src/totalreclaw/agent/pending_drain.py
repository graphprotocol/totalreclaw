"""Cross-process drain queue for messages whose lifecycle extraction was
prevented by interpreter shutdown.

Background — issue #148
-----------------------
``hermes chat -q "<msg>"`` runs each turn in a fresh process. The Hermes
plugin's ``on_session_finalize`` hook fires from ``hermes_cli``'s
``atexit`` chain. By that point Python's ``concurrent.futures.thread``
module has already set its process-global ``_shutdown`` flag, so any
``ThreadPoolExecutor.submit`` (including the ones inside ``httpx``'s
anyio backend) raises::

    RuntimeError: cannot schedule new futures after interpreter shutdown

The persistent sync-loop runner detects this and raises
``InterpreterShutdownError``. Lifecycle hooks catch the error and
``enqueue_messages`` the unprocessed buffer here instead of dropping it.
On the next ``on_session_start`` (a healthy interpreter), the plugin
calls ``drain_pending`` and re-runs auto-extract on the recovered
messages so no facts are lost.

File layout
-----------
``~/.totalreclaw/.pending_extract.jsonl`` — append-only JSONL keyed by
owner address. Each line carries::

    {"owner": "<eoa-or-sa-hex>",
     "queued_at": "<iso8601>",
     "messages": [{"role": "...", "content": "..."}, ...]}

Owner-keying lets a multi-account host (rare today; possible with future
profile-switching) drain only its own batches. The file lives in
``~/.totalreclaw`` rather than ``~/.hermes`` so non-Hermes integrations
(LangChain, custom agents) share the same recovery surface.

Failure semantics
-----------------
- Disk write failures are logged and swallowed. The lifecycle hook still
  surfaces a quota warning so the user knows extraction was lost; we do
  not want a disk-full condition to crash the chat process.
- Drain reads the entire file into memory, returns the matching batches,
  and atomically rewrites the file with non-matching ones. If the rewrite
  fails the original file is preserved (next drain will re-attempt).
- Malformed JSONL lines are dropped on drain with a debug log; they would
  block all future drains otherwise.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def _pending_path() -> Path:
    return Path.home() / ".totalreclaw" / ".pending_extract.jsonl"


def enqueue_messages(
    owner: str,
    messages: list[dict],
    path: Optional[Path] = None,
) -> bool:
    """Append a batch of unprocessed messages to the pending queue.

    Returns ``True`` on success, ``False`` if the disk write fails. A
    ``False`` return is logged at WARNING; callers may use it to decide
    whether to surface a more aggressive user-visible warning.

    Empty ``messages`` is a no-op (returns ``True``).
    """
    if not messages:
        return True

    pending = path or _pending_path()
    try:
        pending.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "owner": owner or "",
            "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "messages": messages,
        }
        with pending.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            pending.chmod(0o600)
        except OSError:
            pass
        return True
    except Exception as exc:
        logger.warning(
            "TotalReclaw: failed to enqueue pending extraction batch (%d msgs): %s",
            len(messages), exc,
        )
        return False


def drain_pending(
    owner: str,
    path: Optional[Path] = None,
) -> list[list[dict]]:
    """Atomically read + remove all batches matching ``owner``.

    Returns a list of message-lists (one per enqueued batch, in arrival
    order). Non-matching batches stay in the file. The file is removed
    when empty.

    Drain on a missing or empty file returns ``[]`` without error — both
    conditions are normal (no prior shutdown loss, or another process
    already drained).
    """
    pending = path or _pending_path()
    if not pending.exists():
        return []

    keep_lines: list[str] = []
    drained: list[list[dict]] = []
    try:
        raw = pending.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("TotalReclaw: failed to read pending queue: %s", exc)
        return []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            logger.debug("TotalReclaw: skipping malformed pending line: %r", line[:80])
            continue
        if not isinstance(record, dict):
            continue

        rec_owner = record.get("owner", "") or ""
        rec_msgs = record.get("messages") or []
        if not isinstance(rec_msgs, list):
            continue

        if rec_owner == (owner or ""):
            drained.append(rec_msgs)
        else:
            keep_lines.append(line)

    if not drained:
        return []

    try:
        if keep_lines:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                dir=str(pending.parent), prefix=".pending_extract.", suffix=".tmp",
                delete=False,
            )
            try:
                tmp.write("\n".join(keep_lines) + "\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            finally:
                tmp.close()
            os.replace(tmp.name, str(pending))
            try:
                pending.chmod(0o600)
            except OSError:
                pass
        else:
            pending.unlink()
    except Exception as exc:
        logger.warning(
            "TotalReclaw: drained %d batch(es) but failed to rewrite queue file: %s",
            len(drained), exc,
        )

    return drained


def has_pending(owner: str, path: Optional[Path] = None) -> bool:
    """Cheap predicate: is there at least one queued batch for ``owner``?

    Used by ``on_session_start`` to skip the (slightly costly) drain path
    when the file is missing. Treats read errors as "no pending" rather
    than raising.
    """
    pending = path or _pending_path()
    if not pending.exists():
        return False
    try:
        raw = pending.read_text(encoding="utf-8")
    except Exception:
        return False
    needle = f'"owner": "{owner or ""}"'
    return needle in raw
