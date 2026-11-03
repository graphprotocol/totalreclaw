"""Structured JSON logging with a redaction filter (spec §8, PRD-03 §6).

Every record that reaches the root handler is rewritten by
``redact_record`` before formatting:

1. **Arguments.** ``logger.info("x %s", value)``: each argument is kept only
   if ``redaction.value_violation`` accepts it, else replaced by
   ``[redacted]``. The message is then rendered and passed through
   ``redaction.scrub_text`` (phrase / bearer / qualified-secret / key shapes).
2. **Extra fields.** ``extra={...}`` entries are kept only if both the key and
   the value pass the shared field rules; otherwise the value becomes
   ``[redacted]``.
3. **Exceptions.** The exception message is dropped unless the exception is a
   ``SafeMessageError``; the log line keeps the type, the cause chain types
   and ``file:line:function`` frames (no source lines, no locals).
4. **Non-string messages** (``logger.info(obj)``) are replaced entirely.

Plaintext baked into the message *template* (f-strings, ``%``, ``+``,
``.format``) cannot be told apart from a constant, so ruff's ``G`` rules
(pyproject ``[tool.ruff.lint]``) forbid those forms in CI; ``scrub_text``
still catches phrase- and key-shaped content there as a backstop.

uvicorn's access log is disabled (raw paths carry capability ids and query
strings); ``web.RequestLogMiddleware`` logs the route template instead.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import PurePath
from types import TracebackType
from typing import IO, Any, Final

from totalreclaw_enclave.errors import SafeMessageError
from totalreclaw_enclave.redaction import REDACTED, key_violation, scrub_text, value_violation

_STANDARD_ATTRS: Final = frozenset(vars(logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None))) | {
    "message",
    "asctime",
}
_MARK: Final = "_enclave_redacted"
_EXC_ATTR: Final = "_enclave_exc"
_MAX_FRAMES: Final = 30
# uvicorn duplicates its message with ANSI colour codes; drop it outright.
_DROPPED_EXTRAS: Final = ("color_message",)

# Third-party loggers that must route through the root handler only.
_ROUTED_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp", "httpx", "httpx2", "asyncio")


def _safe_arg(value: object) -> object:
    return value if value_violation(value) is None else REDACTED


def _render_message(record: logging.LogRecord) -> str:
    if not isinstance(record.msg, str):
        return "[redacted: non-string log message]"
    template = record.msg
    args = record.args
    if not args:
        return scrub_text(template)
    if isinstance(args, Mapping):
        safe: Any = {k: _safe_arg(v) for k, v in args.items()}
    else:
        safe = tuple(_safe_arg(v) for v in args)
    try:
        rendered = template % safe
    except (TypeError, ValueError, KeyError):
        rendered = f"{template} {REDACTED}"
    return scrub_text(rendered)


def _frames(tb: TracebackType | None) -> list[str]:
    frames = traceback.extract_tb(tb)[-_MAX_FRAMES:]
    return [f"{PurePath(f.filename).name}:{f.lineno}:{f.name}" for f in frames]


def _exception_summary(exc: BaseException) -> dict[str, Any]:
    summary: dict[str, Any] = {"type": type(exc).__qualname__, "frames": _frames(exc.__traceback__)}
    if isinstance(exc, SafeMessageError):
        summary["message"] = scrub_text(str(exc))
    chain: list[str] = []
    seen: set[int] = {id(exc)}
    link = exc.__cause__ or exc.__context__
    while link is not None and id(link) not in seen and len(chain) < 5:
        chain.append(type(link).__qualname__)
        seen.add(id(link))
        link = link.__cause__ or link.__context__
    if chain:
        summary["chain"] = chain
    return summary


def redact_record(record: logging.LogRecord) -> None:
    """Rewrite ``record`` in place so that no plaintext-shaped data remains."""
    if getattr(record, _MARK, False):
        return
    record.msg = _render_message(record)
    record.args = None
    for key in _DROPPED_EXTRAS:
        vars(record).pop(key, None)
    for key in list(vars(record)):
        if key in _STANDARD_ATTRS or key.startswith("_"):
            continue
        value = vars(record)[key]
        if key_violation(key) is not None or value_violation(value) is not None:
            setattr(record, key, REDACTED)
    if record.exc_info and record.exc_info[1] is not None:
        setattr(record, _EXC_ATTR, _exception_summary(record.exc_info[1]))
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    setattr(record, _MARK, True)


class RedactionFilter(logging.Filter):
    """Handler-level filter: rewrites, never drops, a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, fields?, exc?."""

    def format(self, record: logging.LogRecord) -> str:
        redact_record(record)  # idempotent; covers a handler without the filter
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS and not k.startswith("_")}
        if fields:
            payload["fields"] = fields
        exc = getattr(record, _EXC_ATTR, None)
        if exc:
            payload["exc"] = exc
        return json.dumps(payload, ensure_ascii=True, default=lambda _o: REDACTED)


def _log_uncaught(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    logging.getLogger("totalreclaw_enclave.crash").critical(
        "uncaught exception", exc_info=(exc_type, exc, tb)
    )


def _log_uncaught_thread(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is not None:
        _log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)


def _log_warning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: IO[str] | None = None,
    line: str | None = None,
) -> None:
    # Warning text can quote runtime values: keep only category and location.
    logging.getLogger("py.warnings").warning(
        "python.warning",
        extra={"category": category.__name__, "location": f"{PurePath(filename).name}:{lineno}"},
    )


def configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> logging.Handler:
    """Install the single redacting JSON handler on the root logger.

    Replaces any existing root handlers (so a later ``logging.basicConfig`` —
    e.g. the MCP SDK's ``MCPServer.__init__`` — is a no-op), routes the
    third-party loggers through it, disables uvicorn's access log, and sends
    warnings (category and location only) and uncaught exceptions (main
    thread and threads) through the same filter. Call it once at boot, before constructing the app.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in _ROUTED_LOGGERS:
        routed = logging.getLogger(name)
        routed.handlers[:] = []
        routed.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    warnings.showwarning = _log_warning
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_uncaught_thread
    return handler
