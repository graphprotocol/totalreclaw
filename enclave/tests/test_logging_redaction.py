"""Pins the invariant: no plaintext memory text, query, phrase or token in any
log line (spec §8, PRD-03 §6). Every sentinel in ``SENTINELS`` is fed through
each path a value can take into the log; none may reach the output."""

from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
import sys
import threading
import warnings
from pathlib import Path

import pytest

from tests.support import FAKE_PHRASE, FAKE_TOKEN, MEMORY_TEXT, SENTINELS
from totalreclaw_enclave.logs import configure_logging
from totalreclaw_enclave.settings import SettingsError

pytestmark = pytest.mark.usefixtures("restore_logging")


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _assert_clean(stream: io.StringIO) -> None:
    output = stream.getvalue()
    for sentinel in SENTINELS:
        assert sentinel not in output, sentinel


def test_arguments_are_redacted() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    log = logging.getLogger("totalreclaw_enclave.test")
    log.info("pairing phrase %s", FAKE_PHRASE)
    log.info("bearer %s", FAKE_TOKEN)
    log.info("recall query %s for %d results", MEMORY_TEXT, 8)
    log.warning("mapping %(q)s", {"q": MEMORY_TEXT})
    log.info("status %s", "session-key")
    _assert_clean(stream)
    messages = [line["msg"] for line in _lines(stream)]
    assert messages == [
        "pairing phrase [redacted]",
        "bearer [redacted]",
        "recall query [redacted] for 8 results",
        "mapping [redacted]",
        "status session-key",
    ]


def test_extra_fields_are_redacted() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("totalreclaw_enclave.test").info(
        "tool.call",
        extra={"query": MEMORY_TEXT, "note": FAKE_TOKEN, "count": 3, "tool": "totalreclaw_recall"},
    )
    _assert_clean(stream)
    [line] = _lines(stream)
    assert line["fields"] == {
        "query": "[redacted]",
        "note": "[redacted]",
        "count": 3,
        "tool": "totalreclaw_recall",
    }


def test_exception_messages_are_dropped() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    log = logging.getLogger("totalreclaw_enclave.test")
    try:
        try:
            raise KeyError(FAKE_TOKEN)
        except KeyError as inner:
            raise ValueError(MEMORY_TEXT) from inner
    except ValueError:
        log.exception("tool.failed")
    _assert_clean(stream)
    [line] = _lines(stream)
    exc = line["exc"]
    assert isinstance(exc, dict)
    assert exc["type"] == "ValueError"
    assert "message" not in exc
    assert exc["chain"] == ["KeyError"]
    assert any(frame.startswith("test_logging_redaction.py:") for frame in exc["frames"])


def test_safe_message_errors_keep_their_message() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    try:
        raise SettingsError("ENCLAVE_ENV is required (dev, staging or prod)")
    except SettingsError:
        logging.getLogger("totalreclaw_enclave.test").exception("boot.failed")
    [line] = _lines(stream)
    assert line["exc"]["message"] == "ENCLAVE_ENV is required (dev, staging or prod)"  # type: ignore[index]


def test_secret_shapes_baked_into_the_template_are_scrubbed() -> None:
    # ruff G001-G004 forbid building log templates from values in src/; this
    # is the runtime backstop for the shapes a pattern can recognise.
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    log = logging.getLogger("totalreclaw_enclave.test")
    for template in (
        "phrase: " + FAKE_PHRASE,
        "Authorization: Bearer " + FAKE_TOKEN,
        "retry with token=" + FAKE_TOKEN + " later",
        "raw key 0x" + "ab" * 32,
    ):
        log.info(template)
    _assert_clean(stream)
    assert "ab" * 32 not in stream.getvalue()


def test_non_string_messages_are_replaced() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("totalreclaw_enclave.test").info({"text": MEMORY_TEXT})
    _assert_clean(stream)
    assert _lines(stream)[0]["msg"] == "[redacted: non-string log message]"


def test_third_party_loggers_route_through_the_filter() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("uvicorn.error").error(
        "Exception in ASGI application", exc_info=ValueError(MEMORY_TEXT)
    )
    logging.getLogger("mcp.server.lowlevel").info("request %s", MEMORY_TEXT)
    logging.getLogger("uvicorn.error").info("Started %s", "server", extra={"color_message": MEMORY_TEXT})
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /connect/%s HTTP/1.1" 200', FAKE_TOKEN)
    _assert_clean(stream)
    lines = _lines(stream)
    assert len(lines) == 3  # the access logger is disabled
    assert "fields" not in lines[2]  # color_message dropped


def test_uncaught_exceptions_and_warnings_are_redacted() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    try:
        raise ValueError(MEMORY_TEXT)
    except ValueError:
        sys.excepthook(*sys.exc_info())  # type: ignore[arg-type]

    def worker() -> None:
        raise RuntimeError(FAKE_PHRASE)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn(MEMORY_TEXT, UserWarning, stacklevel=1)
    _assert_clean(stream)
    types = [line.get("exc", {}).get("type") for line in _lines(stream)]  # type: ignore[union-attr]
    assert "ValueError" in types and "RuntimeError" in types


def test_later_basicconfig_calls_do_not_add_handlers() -> None:
    stream = io.StringIO()
    handler = configure_logging("INFO", stream=stream)
    logging.basicConfig(level="DEBUG", format="%(message)s")  # what MCPServer.__init__ does
    assert logging.getLogger().handlers == [handler]


@pytest.mark.parametrize(
    "value",
    [
        "O meu médico é o Dr. Silva",  # non-ASCII prose
        "line one\nline two",  # multi-line
        "x" * 100_000,  # oversized
    ],
)
def test_non_ascii_multiline_and_huge_arguments_are_redacted(value: str) -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("totalreclaw_enclave.test").info("value %s", value)
    [line] = _lines(stream)
    assert line["msg"] == "value [redacted]"


def test_ruff_g_rules_actually_flag_fstring_logging(tmp_path: Path) -> None:
    # G001-G004 (no str.format / % / + / f-string in logging calls) and LOG
    # are what keep plaintext out of log *templates* — but ruff only checks
    # loggers it recognises by name (``logger``, ``log``, the logging module),
    # which is why module loggers here must be called ``logger``. This test
    # runs the real linter on the exact violation it must catch, so a ruff
    # upgrade that stops recognising the rules turns CI red. (The test it
    # replaced only asserted the pyproject select list, which stays green even
    # when the rules match nothing.)
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff is not on PATH")
    snippet = tmp_path / "snippet.py"
    snippet.write_text('import logging\nlogger = logging.getLogger(__name__)\nx = 1\nlogger.info(f"v {x}")\n')
    proc = subprocess.run(
        [ruff, "check", "--select", "G,LOG", "--isolated", str(snippet)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "G004" in proc.stdout + proc.stderr
