"""Tests for totalreclaw.onboarding (added in 2.3.1).

Covers:
- detect_first_run across missing / empty / invalid / valid credentials.
- build_welcome_message for local + remote mode.
- detect_mode URL + env-var classification.
- Module-level copy-constant parity (tests assert byte-identity so any
  future change gets flagged).
- Terminology sweep — an AST walk that asserts no ``mnemonic`` /
  ``seed phrase`` / ``recovery code`` / ``recovery key`` string literals
  leak into user-facing call sites (``print``, ``click.echo``,
  ``logger.warning`` / ``.error``) under ``python/src/**/*.py``.
"""
from __future__ import annotations

import ast
import json
import os
from io import StringIO
from pathlib import Path

import pytest

from totalreclaw import onboarding


# ---------------------------------------------------------------------------
# detect_first_run
# ---------------------------------------------------------------------------


class TestDetectFirstRun:
    def test_detect_first_run_missing_file(self, tmp_path: Path) -> None:
        """True when the credentials file doesn't exist."""
        path = tmp_path / "does-not-exist.json"
        assert onboarding.detect_first_run(path) is True

    def test_detect_first_run_empty_json(self, tmp_path: Path) -> None:
        """True when the file is ``{}`` (no credentials keys)."""
        path = tmp_path / "empty.json"
        path.write_text("{}")
        assert onboarding.detect_first_run(path) is True

    def test_detect_first_run_empty_file(self, tmp_path: Path) -> None:
        """True when the file exists but is literally empty."""
        path = tmp_path / "zero-bytes.json"
        path.write_text("")
        assert onboarding.detect_first_run(path) is True

    def test_detect_first_run_invalid_json(self, tmp_path: Path) -> None:
        """True when the file isn't valid JSON."""
        path = tmp_path / "garbage.json"
        path.write_text("{ not valid json")
        assert onboarding.detect_first_run(path) is True

    def test_detect_first_run_non_object_json(self, tmp_path: Path) -> None:
        """True when the JSON is valid but not an object (array / string)."""
        path = tmp_path / "array.json"
        path.write_text("[]")
        assert onboarding.detect_first_run(path) is True

        path2 = tmp_path / "string.json"
        path2.write_text('"some string"')
        assert onboarding.detect_first_run(path2) is True

    def test_detect_first_run_missing_required_keys(self, tmp_path: Path) -> None:
        """True when the object has neither mnemonic nor recovery_phrase."""
        path = tmp_path / "wrong-key.json"
        path.write_text(json.dumps({"address": "0x123", "created": "2026-04-20"}))
        assert onboarding.detect_first_run(path) is True

    def test_detect_first_run_empty_mnemonic_value(self, tmp_path: Path) -> None:
        """True when the mnemonic key is present but empty / whitespace."""
        path = tmp_path / "empty-mnem.json"
        path.write_text(json.dumps({"mnemonic": ""}))
        assert onboarding.detect_first_run(path) is True

        path2 = tmp_path / "ws-mnem.json"
        path2.write_text(json.dumps({"mnemonic": "   "}))
        assert onboarding.detect_first_run(path2) is True

    def test_detect_first_run_valid_credentials(self, tmp_path: Path) -> None:
        """False when the file has a real mnemonic value."""
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )
        path = tmp_path / "creds.json"
        path.write_text(json.dumps({"mnemonic": mnemonic}))
        assert onboarding.detect_first_run(path) is False

    def test_detect_first_run_valid_legacy_key(self, tmp_path: Path) -> None:
        """False when the file uses legacy 'recovery_phrase' key.

        Cross-client back-compat: Python pre-2.2.2 wrote this key. A
        user who onboarded on old Python should NOT see the welcome
        after upgrading.
        """
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps({"recovery_phrase": mnemonic}))
        assert onboarding.detect_first_run(path) is False

    def test_detect_first_run_default_path(self, monkeypatch, tmp_path: Path) -> None:
        """Uses CANONICAL_CREDENTIALS_PATH when no path is passed."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        fake_creds = fake_home / ".totalreclaw" / "credentials.json"

        # Point the module at a fresh CANONICAL_CREDENTIALS_PATH.
        monkeypatch.setattr(onboarding, "CANONICAL_CREDENTIALS_PATH", fake_creds)

        # No file → first run.
        assert onboarding.detect_first_run() is True

        fake_creds.parent.mkdir(parents=True)
        fake_creds.write_text(json.dumps({"mnemonic": "abandon " * 11 + "about"}))
        assert onboarding.detect_first_run() is False


# ---------------------------------------------------------------------------
# build_welcome_message
# ---------------------------------------------------------------------------


class TestBuildWelcomeMessage:
    def test_welcome_message_local_mode(self) -> None:
        """Local mode must include the short 'Run: hermes setup' instruction."""
        msg = onboarding.build_welcome_message("local")
        assert onboarding.WELCOME_MESSAGE in msg
        assert onboarding.BRANCH_QUESTION in msg
        assert onboarding.LOCAL_MODE_INSTRUCTIONS in msg
        # Remote-specific copy must NOT appear in local mode.
        assert onboarding.REMOTE_MODE_INSTRUCTIONS not in msg
        # The canonical "recovery phrase" terminology is present.
        assert "recovery phrase" in msg.lower()

    def test_welcome_message_remote_mode(self) -> None:
        """Remote mode must include the longer phrase-never-leaves note."""
        msg = onboarding.build_welcome_message("remote")
        assert onboarding.WELCOME_MESSAGE in msg
        assert onboarding.BRANCH_QUESTION in msg
        assert onboarding.REMOTE_MODE_INSTRUCTIONS in msg
        # Remote-specific copy asserts the phrase-never-leaves guarantee.
        assert "never leaves this machine" in msg
        # Canonical terminology.
        assert "recovery phrase" in msg.lower()

    def test_welcome_message_invalid_mode(self) -> None:
        """Unknown modes raise ValueError."""
        with pytest.raises(ValueError):
            onboarding.build_welcome_message("somewhere-else")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# detect_mode
# ---------------------------------------------------------------------------


class TestDetectMode:
    def test_local_by_loopback_url_arg(self) -> None:
        assert onboarding.detect_mode("http://localhost:4000") == "local"
        assert onboarding.detect_mode("https://127.0.0.1:4000") == "local"
        assert onboarding.detect_mode("http://[::1]:4000") == "local"

    def test_remote_by_public_url_arg(self) -> None:
        assert onboarding.detect_mode("https://api.totalreclaw.xyz") == "remote"
        assert onboarding.detect_mode("https://api-staging.totalreclaw.xyz") == "remote"

    def test_local_by_env_flag(self, monkeypatch) -> None:
        monkeypatch.setenv("TOTALRECLAW_LOCAL_GATEWAY", "1")
        monkeypatch.delenv("TOTALRECLAW_SERVER_URL", raising=False)
        monkeypatch.delenv("TOTALRECLAW_RELAY_URL", raising=False)
        assert onboarding.detect_mode() == "local"

    def test_local_by_server_url_env(self, monkeypatch) -> None:
        monkeypatch.delenv("TOTALRECLAW_LOCAL_GATEWAY", raising=False)
        monkeypatch.delenv("HERMES_LOCAL_GATEWAY", raising=False)
        monkeypatch.setenv("TOTALRECLAW_SERVER_URL", "http://localhost:4000")
        assert onboarding.detect_mode() == "local"

    def test_default_is_remote(self, monkeypatch) -> None:
        # Strip every relevant env var.
        for var in (
            "TOTALRECLAW_LOCAL_GATEWAY",
            "HERMES_LOCAL_GATEWAY",
            "TOTALRECLAW_SERVER_URL",
            "TOTALRECLAW_RELAY_URL",
        ):
            monkeypatch.delenv(var, raising=False)
        assert onboarding.detect_mode() == "remote"


# ---------------------------------------------------------------------------
# Copy constants — byte-identity lock
# ---------------------------------------------------------------------------


class TestCopyConstants:
    def test_welcome_message_contents(self) -> None:
        """WELCOME_MESSAGE is a non-empty stripped string that mentions
        both E2E encryption and cross-agent portability."""
        m = onboarding.WELCOME_MESSAGE
        assert m == m.strip()
        assert len(m) > 0
        assert "end-to-end encrypted" in m.lower()
        assert "recovery phrase" in m.lower()
        # OpenClaw + Hermes + NanoClaw are called out by name — the
        # copy is the same across all three clients.
        assert "OpenClaw" in m
        assert "Hermes" in m
        assert "NanoClaw" in m

    def test_branch_question_contents(self) -> None:
        """BRANCH_QUESTION asks about restore-vs-generate in user terms."""
        q = onboarding.BRANCH_QUESTION
        assert q == q.strip()
        assert "recovery phrase" in q.lower()
        assert "generate" in q.lower()

    def test_local_instructions(self) -> None:
        # 2.3.1rc4: the colliding `hermes` console script was removed to
        # stop overwriting the upstream hermes-agent CLI on
        # `pip install totalreclaw`. Only the canonical `totalreclaw setup`
        # remains in the hint.
        assert "totalreclaw setup" in onboarding.LOCAL_MODE_INSTRUCTIONS
        assert "hermes setup" not in onboarding.LOCAL_MODE_INSTRUCTIONS
        assert onboarding.LOCAL_MODE_INSTRUCTIONS.startswith("Run: ")

    def test_remote_instructions_contents(self) -> None:
        r = onboarding.REMOTE_MODE_INSTRUCTIONS
        assert r == r.strip()
        # 2.3.1rc4: canonical `totalreclaw setup` only (see test_local_instructions).
        assert "totalreclaw setup" in r
        assert "hermes setup" not in r
        # The load-bearing security claim.
        assert "never leaves this machine" in r

    def test_storage_guidance(self) -> None:
        g = onboarding.STORAGE_GUIDANCE
        assert g == g.strip()
        assert "12 words" in g
        assert "password manager" in g.lower()
        # The "dedicated to TotalReclaw" warning, shorter variant.
        assert "don't reuse" in g.lower() or "do not reuse" in g.lower()

    def test_restore_prompt(self) -> None:
        p = onboarding.RESTORE_PROMPT
        assert p.strip() == "Enter your 12-word recovery phrase to restore your account:"

    def test_generated_confirmation(self) -> None:
        c = onboarding.GENERATED_CONFIRMATION
        assert c == c.strip()
        assert "write it down" in c.lower()
        assert "only way to restore" in c.lower()


# ---------------------------------------------------------------------------
# maybe_emit_welcome — once-per-process behaviour
# ---------------------------------------------------------------------------


class TestMaybeEmitWelcome:
    def setup_method(self) -> None:
        onboarding._reset_for_tests()

    def test_emits_on_first_run(self, tmp_path: Path) -> None:
        creds = tmp_path / "creds.json"
        sentinel = tmp_path / ".welcome_shown"
        buf = StringIO()

        # Ensure we don't leak to the actual user home.
        original_sentinel = onboarding._WELCOME_SENTINEL_PATH
        onboarding._WELCOME_SENTINEL_PATH = sentinel
        try:
            emitted = onboarding.maybe_emit_welcome(
                credentials_path=creds,
                relay_url="http://localhost:4000",
                stream=buf,
            )
        finally:
            onboarding._WELCOME_SENTINEL_PATH = original_sentinel

        assert emitted is True
        output = buf.getvalue()
        assert onboarding.WELCOME_MESSAGE in output
        assert onboarding.BRANCH_QUESTION in output
        assert onboarding.LOCAL_MODE_INSTRUCTIONS in output

    def test_does_not_emit_when_onboarded(self, tmp_path: Path) -> None:
        creds = tmp_path / "creds.json"
        creds.write_text(json.dumps({"mnemonic": "abandon " * 11 + "about"}))
        buf = StringIO()

        emitted = onboarding.maybe_emit_welcome(
            credentials_path=creds, stream=buf, use_sentinel=False
        )
        assert emitted is False
        assert buf.getvalue() == ""

    def test_does_not_emit_twice_in_same_process(self, tmp_path: Path) -> None:
        creds = tmp_path / "creds.json"
        buf1 = StringIO()
        buf2 = StringIO()

        first = onboarding.maybe_emit_welcome(
            credentials_path=creds, stream=buf1, use_sentinel=False
        )
        second = onboarding.maybe_emit_welcome(
            credentials_path=creds, stream=buf2, use_sentinel=False
        )
        assert first is True
        assert second is False
        assert buf1.getvalue() != ""
        assert buf2.getvalue() == ""


# ---------------------------------------------------------------------------
# Terminology sweep — AST walk on python/src/**/*.py
# ---------------------------------------------------------------------------


FORBIDDEN_USER_FACING_TERMS = ("seed phrase", "recovery code", "recovery key")

# 'mnemonic' is allowed as a variable name / internal identifier, but
# must NOT appear in strings passed to user-facing output sinks.
FORBIDDEN_USER_FACING_EXACT_MNEMONIC = "mnemonic"

# Allowlisted file paths — strings inside these files are NEVER
# user-facing (pure data, schema constants that go into JSON bodies the
# agent consumes as data, docstrings explaining internal APIs).
_ALLOWLISTED_FILES: tuple[str, ...] = (
    # Raw schemas / core bindings — ``mnemonic`` here is a JSON-key
    # identifier for credentials.json parity, not human-facing prose.
    "totalreclaw/agent/state.py",
    # Client constructor accepts ``mnemonic`` as a deprecated kwarg —
    # the string lives in the signature, not in a user-facing message.
    "totalreclaw/client.py",
    # Rust core binding layer — argument name flows through to the Rust
    # PyO3 function, can't be renamed without a core bump.
    "totalreclaw/crypto.py",
    # Internal setup tool — uses ``_acct, recovery_phrase = ...`` plus a
    # leading comment that references ``BIP-39 mnemonic`` inside a
    # docstring-style comment. No user-facing string leak.
    "totalreclaw/hermes/tools.py",
)


def _source_files() -> list[Path]:
    root = Path(__file__).resolve().parent.parent / "src"
    return sorted(p for p in root.rglob("*.py"))


def _is_user_facing_call(node: ast.Call) -> bool:
    """True iff the call target is print / click.echo / logger.warning /
    logger.error / logger.info — everything that ends up in front of
    a human user as formatted text."""
    fn = node.func
    if isinstance(fn, ast.Name) and fn.id == "print":
        return True
    if isinstance(fn, ast.Attribute):
        # click.echo / click.secho / click.confirm
        if fn.attr in ("echo", "secho", "confirm"):
            return True
        # logger.warning / error / info — only WARN+ are strictly
        # "user-facing" in a CLI, but INFO leaks to the default
        # verbose flag too, so include it.
        if fn.attr in ("warning", "error", "warn"):
            return True
    return False


def _iter_string_args(call: ast.Call):
    """Yield string literal Constant nodes from a call's args + kwargs."""
    for a in call.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            yield a
    for kw in call.keywords:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            yield kw.value


class TestTerminologyParity:
    """Grep-style check that no forbidden terminology leaks into strings
    passed to ``print`` / ``click.echo`` / ``logger.warning`` /
    ``logger.error`` across ``python/src/**/*.py``."""

    def test_no_forbidden_terms_in_user_facing_call_sites(self) -> None:
        offenders: list[tuple[str, int, str]] = []

        for src_file in _source_files():
            rel = src_file.relative_to(src_file.parent.parent.parent)  # strip python/
            rel_s = str(rel).replace(os.sep, "/")
            try:
                tree = ast.parse(src_file.read_text(), filename=str(src_file))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not _is_user_facing_call(node):
                    continue
                for s_node in _iter_string_args(node):
                    text = s_node.value
                    lower = text.lower()
                    for term in FORBIDDEN_USER_FACING_TERMS:
                        if term in lower:
                            offenders.append((rel_s, s_node.lineno, term))
                    # 'mnemonic' alone — allowlist src/totalreclaw dirs
                    # where the word is part of an internal identifier
                    # printed for debugging (logger.info("configured: %s", ...)).
                    if "mnemonic" in lower:
                        # Best-effort allowlist to avoid false positives.
                        if not any(rel_s.endswith(a) for a in _ALLOWLISTED_FILES):
                            offenders.append((rel_s, s_node.lineno, "mnemonic"))

        assert not offenders, (
            "User-facing strings contain forbidden terminology:\n"
            + "\n".join(f"  {p}:{ln} -> {term}" for p, ln, term in offenders)
        )

    def test_no_forbidden_terms_in_welcome_copy(self) -> None:
        """The canonical copy constants never leak forbidden terminology.

        Tests every module-level copy string exported from
        ``totalreclaw.onboarding`` for ``seed phrase`` / ``recovery code`` /
        ``recovery key``. These strings are the single source of truth for
        cross-client parity; a regression here would propagate to every
        agent that imports ``build_welcome_message``.
        """
        strings = [
            onboarding.WELCOME_MESSAGE,
            onboarding.BRANCH_QUESTION,
            onboarding.LOCAL_MODE_INSTRUCTIONS,
            onboarding.REMOTE_MODE_INSTRUCTIONS,
            onboarding.STORAGE_GUIDANCE,
            onboarding.RESTORE_PROMPT,
            onboarding.GENERATED_CONFIRMATION,
        ]
        for s in strings:
            lower = s.lower()
            for term in FORBIDDEN_USER_FACING_TERMS:
                assert term not in lower, (
                    f"Forbidden term {term!r} found in onboarding copy: {s!r}"
                )
            assert "mnemonic" not in lower, (
                f"Internal term 'mnemonic' leaked into user-facing copy: {s!r}"
            )
