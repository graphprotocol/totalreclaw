"""Regression shield: first-run must not emit anything to stdout in
agent contexts.

Added 2.3.1rc9 (2026-04-23).

Context
-------

rc.8's Hermes auto-QA run with the Git-plugin install path surfaced a
chat-breaker + a phrase-safety violation, both rooted in
``totalreclaw.onboarding.maybe_emit_welcome``:

1. **Chat-breaker.** When ``~/.totalreclaw/credentials.json`` was
   missing (the clean-machine case that every fresh user hits),
   ``import totalreclaw.hermes`` wrote a multi-paragraph welcome
   banner to stdout. The QA harness invokes ``hermes chat -q`` and
   parses ``session_id`` from the response — the banner dominated
   stdout, the parser failed, every chat step in the scenario failed,
   the run returned NO-GO.

2. **Phrase-safety violation.** The banner suggested
   ``Run: totalreclaw setup``. That CLI runs an interactive prompt
   that echoes the recovery phrase to stdout. In an agent-driven
   context (Hermes chat, OpenClaw chat, etc.) the agent reads stdout
   back into its LLM context — which means the phrase crosses the
   LLM boundary. That's a vault-compromise-class violation of the
   absolute rule in ``project_phrase_safety_rule.md``: "recovery
   phrase MUST NEVER cross the LLM context in ANY form."

Fix: ``maybe_emit_welcome`` is a no-op as of 2.3.1rc9. Agent-driven
setup flows through the ``totalreclaw_pair`` tool (browser-side
crypto, phrase-safe); user-in-terminal setup happens OUTSIDE any
agent context via ``totalreclaw setup`` directly.

This test is the regression shield for both problems.
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

from totalreclaw import onboarding


class TestNoStdoutOnFirstRunInAgentContext:
    """Simulate an agent-context first-run and assert zero stdout."""

    def setup_method(self) -> None:
        onboarding._reset_for_tests()

    def test_maybe_emit_welcome_writes_nothing_in_agent_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent context: ``sys.stdout.isatty()`` returns False + no
        credentials file.

        Under those conditions ``maybe_emit_welcome`` MUST NOT write
        anything to stdout (or to the passed-in stream, or anywhere
        else observable by an agent harness).
        """
        # No credentials file → first-run.
        creds = tmp_path / "does-not-exist.json"
        sentinel = tmp_path / ".welcome_shown"

        # Redirect the default stream so we can observe it — and
        # separately redirect sys.stdout so even if the function
        # ignored the explicit stream arg and reached for stdout, we'd
        # catch it.
        fake_default = StringIO()
        fake_stdout = StringIO()
        monkeypatch.setattr(onboarding, "_default_stream", lambda: fake_default)
        monkeypatch.setattr(sys, "stdout", fake_stdout)

        # Simulate "piped to another process" (the agent harness case).
        # ``sys.stdout.isatty()`` on a StringIO returns False already,
        # but we lock it in explicitly so the test expresses the
        # agent-context invariant.
        assert fake_stdout.isatty() is False

        # Point the sentinel at tmp so we never touch the real home.
        original_sentinel = onboarding._WELCOME_SENTINEL_PATH
        onboarding._WELCOME_SENTINEL_PATH = sentinel
        try:
            result = onboarding.maybe_emit_welcome(credentials_path=creds)
        finally:
            onboarding._WELCOME_SENTINEL_PATH = original_sentinel

        # Contract:
        assert result is False, (
            "maybe_emit_welcome must be a no-op in agent contexts "
            "(returns False, never True)"
        )
        assert fake_default.getvalue() == "", (
            "maybe_emit_welcome wrote to its default stream — it must "
            "not emit anything on first-run in an agent context"
        )
        assert fake_stdout.getvalue() == "", (
            "maybe_emit_welcome wrote to sys.stdout — it must not emit "
            "anything on first-run in an agent context"
        )

    def test_maybe_emit_welcome_writes_nothing_when_explicit_stream_passed(
        self, tmp_path: Path
    ) -> None:
        """Even with an explicit ``stream`` arg and first-run conditions,
        the function writes nothing.

        This is the direct harness-compat scenario: an agent runtime
        passes its own buffer in hopes of capturing the banner
        separately. The no-op must honour the contract regardless of
        where the stream points.
        """
        creds = tmp_path / "nope.json"
        buf = StringIO()

        result = onboarding.maybe_emit_welcome(
            credentials_path=creds,
            relay_url="https://api-staging.totalreclaw.xyz",
            stream=buf,
            use_sentinel=False,
        )

        assert result is False
        assert buf.getvalue() == ""

    def test_import_totalreclaw_does_not_write_to_stdout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Module import must be side-effect-free on stdout.

        Historically ``totalreclaw.hermes.register(ctx)`` called
        ``maybe_emit_welcome()``, which in turn wrote the banner. The
        no-op fix means any subsequent caller of register() also
        inherits the silence guarantee — but the strongest shield is
        to assert that bare ``import totalreclaw`` / ``import
        totalreclaw.onboarding`` write nothing.

        We capture stdout during a fresh module reload to approximate
        a first-time import.
        """
        # Simulate no-credentials first-run env.
        monkeypatch.setattr(
            onboarding,
            "CANONICAL_CREDENTIALS_PATH",
            tmp_path / "absent-credentials.json",
        )

        buf = StringIO()
        with mock.patch.object(sys, "stdout", buf):
            # Re-run the module load side effects (no-op today, but
            # future regressions that add a module-level print would
            # get caught).
            import importlib
            importlib.reload(onboarding)

        assert buf.getvalue() == "", (
            "Importing totalreclaw.onboarding wrote to stdout — module "
            "load must be silent in agent contexts"
        )

    def test_no_print_statement_at_module_init_time(self) -> None:
        """AST scan: no top-level ``print(...)`` / ``sys.stdout.write``
        at module-body scope in ``onboarding.py``.

        Module-level prints would fire on every ``import`` regardless
        of the ``maybe_emit_welcome`` gate. This scan is a belt-and-
        suspenders shield: even if the gate is correct today, a future
        patch that sneaks a ``print(...)`` onto the module body would
        break agent contexts and fail this assertion.
        """
        import ast
        import inspect

        source = inspect.getsource(onboarding)
        tree = ast.parse(source)

        for node in tree.body:
            # Skip imports, constants, function / class definitions —
            # those don't emit at import time on their own.
            if isinstance(
                node,
                (
                    ast.Import,
                    ast.ImportFrom,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.Assign,
                    ast.AnnAssign,
                    ast.AugAssign,
                ),
            ):
                continue
            # ``from __future__ import annotations``, docstring, etc.
            # ``ast.Str`` was removed in Python 3.14; ``ast.Constant``
            # covers string / number / None literals in 3.8+.
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            if isinstance(node, ast.If):
                # Allow ``if TYPE_CHECKING:`` and similar pure type-only
                # branches. If a regression adds ``if x: print(y)`` at
                # module scope, catch that below.
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and _is_stdout_call(sub):
                        pytest.fail(
                            "Module-level ``print`` / stdout write "
                            "detected in totalreclaw.onboarding — this "
                            "will fire on every import and break agent "
                            "contexts. Wrap it in a function."
                        )
                continue

            # Anything else at module scope that contains a stdout
            # call is a regression.
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and _is_stdout_call(sub):
                    pytest.fail(
                        "Module-level ``print`` / stdout write "
                        "detected in totalreclaw.onboarding — this "
                        "will fire on every import and break agent "
                        "contexts."
                    )


def _is_stdout_call(call: "ast.Call") -> bool:
    """Return True if ``call`` is a ``print(...)`` or
    ``sys.stdout.write(...)`` / ``sys.stderr.write(...)`` invocation."""
    import ast as _ast

    func = call.func
    if isinstance(func, _ast.Name) and func.id == "print":
        return True
    if isinstance(func, _ast.Attribute):
        if func.attr == "write" and isinstance(func.value, _ast.Attribute):
            inner = func.value
            if inner.attr in ("stdout", "stderr"):
                if isinstance(inner.value, _ast.Name) and inner.value.id == "sys":
                    return True
    return False


class TestBannerCopyIsPhraseSafe:
    """Even though the banner is no longer emitted, the copy constants
    are still exported (the CLI wizard imports them). Assert they don't
    carry phrase-unsafe CLI hints that a future regression could
    re-emit.
    """

    def test_local_instructions_no_cli_run_hint(self) -> None:
        li = onboarding.LOCAL_MODE_INSTRUCTIONS
        assert "Run: totalreclaw setup" not in li
        assert "Run: hermes setup" not in li

    def test_remote_instructions_no_cli_run_hint(self) -> None:
        r = onboarding.REMOTE_MODE_INSTRUCTIONS
        assert "Run: totalreclaw setup" not in r
        assert "Run: hermes setup" not in r

    def test_pair_flow_is_the_documented_setup_path(self) -> None:
        """The instructions must route users to the pair flow (which
        runs through an agent tool + browser-side crypto) rather than
        a shell CLI that would echo the phrase."""
        for copy in (
            onboarding.LOCAL_MODE_INSTRUCTIONS,
            onboarding.REMOTE_MODE_INSTRUCTIONS,
        ):
            # Either an explicit "Set up TotalReclaw" agent prompt or
            # a reference to QR / pairing is fine. The failure case is
            # "no mention of the pair flow at all."
            assert (
                "Set up TotalReclaw" in copy
                or "QR" in copy
                or "pair" in copy.lower()
            ), f"instructions lost the pair-flow hint: {copy!r}"
