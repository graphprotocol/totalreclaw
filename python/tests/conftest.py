"""Shared fixtures for TotalReclaw Python tests.

IMPORTANT — test-safety invariant: no pytest session in this repo may hit
the production relay. CLAUDE.md mandates that all tests use staging, but
``DEFAULT_RELAY_URL`` resolves to production when unset, and some unit
tests create a real ``TotalReclaw`` client without explicitly overriding
the URL. If such a test makes any method call that flows through
``_ensure_registered``, it will POST to whichever URL the client picked —
which would be production.

To prevent that, this module forces the relay URL to staging at import
time, BEFORE any test module imports ``totalreclaw.relay`` or
``totalreclaw.client``. The env var is also re-asserted per-test so a
test that clears it accidentally gets it back.
"""
from __future__ import annotations

import os

import pytest

# Force staging for every test, always. Individual tests that need a
# different URL can override locally, but the default is never production.
_STAGING_URL = "https://api-staging.totalreclaw.xyz"
os.environ.setdefault("TOTALRECLAW_SERVER_URL", _STAGING_URL)
# Session finalize defers LLM stages it cannot prove fit the 10 s hook budget
# (#657). Tests exercising the inline path want the "warm" behavior; the
# budget tests set their own estimates.
os.environ.setdefault("TOTALRECLAW_FINALIZE_STAGE_ESTIMATE_SECONDS", "0")


def _stub_wallet_privates(client) -> None:
    """Stub the wallet-assembly privates the import engine's Gnosis store path
    reads (internal#448 single-pass). Only call this on lightweight test fakes
    that lack a real ``_wallet_context`` — never on a real ``TotalReclaw``."""
    from unittest.mock import AsyncMock, MagicMock

    client._ensure_address = AsyncMock()
    client._ensure_registered = AsyncMock()
    client._wallet_context = MagicMock(return_value=MagicMock(name="wallet"))
    client._get_lsh_hasher = MagicMock(return_value=None)
    client._relay = MagicMock(name="relay")
    client._data_edge_address = None
    # NOTE: find_duplicate_texts is intentionally NOT stubbed — some fakes
    # (e.g. _DedupBatchClient) define real dedup logic the test asserts on.
    # The engine's pre-write dedup is fail-open, so a MagicMock auto-attr that
    # can't be awaited is harmlessly skipped.


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch, tmp_path_factory):
    """#704: no test may auto-configure from a developer's real vault.

    ``AgentState()`` / ``get_credential_provider()`` read
    ``~/.totalreclaw/credentials.json`` and, with the staging relay default,
    a test that then writes (e.g. ``remember``) would put data on a real
    staging account. Point ``HOME`` at an empty per-session temp dir (the default
    credentials path derives from it) and drop any inherited override,
    the external credential-provider env vars, and XDG_CONFIG_HOME / HERMES_HOME.

    Known gap: constants bound at import time via ``Path.home()``
    (``onboarding.CANONICAL_CREDENTIALS_PATH``, ``update_notice._STATE_DIR``,
    ...) are NOT retargeted by the HOME override; tests that exercise them
    monkeypatch the attribute themselves. Tests that need credentials
    set their own paths locally (monkeypatch overrides this per test).
    """
    home = tmp_path_factory.getbasetemp() / "tr-fake-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Do NOT set TOTALRECLAW_CREDENTIALS_PATH here: many tests re-point HOME
    # at their own tmp dir and rely on the default ``~/.totalreclaw`` path.
    monkeypatch.delenv("TOTALRECLAW_CREDENTIALS_PATH", raising=False)
    for var in (
        "TOTALRECLAW_CREDENTIALS_PROVIDER",
        "TOTALRECLAW_EXTERNAL_CREDENTIALS_JSON",
        "TOTALRECLAW_EXTERNAL_CREDENTIALS_PATH",
        "XDG_CONFIG_HOME",
        "HERMES_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("TOTALRECLAW_RECOVERY_PHRASE", raising=False)


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """cred-2 (#262) phrase-safety invariant: no test may touch the real OS
    keychain. ``credentials_wrap`` honours ``TOTALRECLAW_NO_KEYCHAIN=1`` as a
    plaintext-only kill-switch, so arming it for the whole suite makes every
    wrap/resolve take the plaintext path by default — the real macOS
    ``security`` / Linux Secret Service backend is never invoked.

    Tests that exercise the keychain path delete this env var and patch
    ``credentials_wrap.detect_backend`` / ``store_secret`` / ``load_secret``
    onto an in-memory fake (see ``fake_keychain`` in test_credentials_wrap).
    """
    monkeypatch.setenv("TOTALRECLAW_NO_KEYCHAIN", "1")


@pytest.fixture(autouse=True)
def _isolate_extraction_model_state(monkeypatch, tmp_path_factory):
    """2.5.3rc4: the extraction model depends on ``~/.totalreclaw/settings.json``
    and on in-process fallback memory. Point the settings file at an empty
    per-session temp path (a developer's real setting must not change test
    outcomes), forget fallback decisions between tests, and drop an exported
    ``TOTALRECLAW_EXTRACTION_MODEL``. Tests that set either do so locally."""
    from totalreclaw import user_settings
    from totalreclaw.agent import llm_client

    path = tmp_path_factory.getbasetemp() / "tr-user-settings" / "settings.json"
    if path.exists():
        path.unlink()
    monkeypatch.setattr(user_settings, "settings_path", lambda: path)
    monkeypatch.delenv("TOTALRECLAW_EXTRACTION_MODEL", raising=False)
    llm_client.reset_extraction_fallback_state()


@pytest.fixture(autouse=True)
def _no_auto_memory_reconcile(monkeypatch):
    """The Hermes provider queues a built-in memory reconcile pass at
    ``initialize`` / ``on_session_end``. A pass reads ``$HERMES_HOME/memories``
    and writes a snapshot under ``~/.totalreclaw``, so left on, provider tests
    would touch the developer's real files. Tests of the reconcile itself
    call ``memory_reconcile.reconcile`` directly, or delete this env var
    after pointing ``HERMES_HOME`` / ``TOTALRECLAW_STATE_DIR`` at tmp dirs.
    """
    monkeypatch.setenv("TOTALRECLAW_HERMES_MEMORY_RECONCILE", "0")


@pytest.fixture(autouse=True)
def _route_import_engine_store_to_fake_recorder(monkeypatch):
    """internal#448 single-pass test shim — keeps existing import-engine unit
    tests (and their assertions) UNCHANGED.

    Background: the import engine's Gnosis store path now calls
    ``store_fact_batch`` directly through ONE ``group_and_store_adaptive`` pass,
    instead of wrapping ``client.remember_batch`` (which would nest two halving
    cascades and re-store already-stored facts). Import-engine unit tests
    record stores via a lightweight fake client whose ``remember_batch``
    appends to ``self.batches`` and assert on that recorder.

    This fixture preserves those fakes byte-for-byte by:

      * stubbing the wallet-assembly privates (``_ensure_address`` /
        ``_wallet_context`` / ``_relay`` / …) on any client that lacks them, so
        the engine's wallet assembly reaches the store; and
      * routing the engine's ``store_fact_batch`` back through the captured
        client's ``remember_batch`` recorder.

    It only acts on lightweight fakes (any client that is NOT a real
    ``TotalReclaw`` instance). Real ``TotalReclaw`` clients keep the real store
    path, and tests that patch ``store_fact_batch`` themselves (the #448
    single-pass regression, the rc4 cascade / shared-sizing / engine-batching
    tests) override this routing — so it never re-introduces the double-pass.
    """
    from totalreclaw.client import TotalReclaw
    from totalreclaw.imports import engine as _engine
    from totalreclaw.imports.engine import ImportEngine

    captured: dict = {}
    orig_init = ImportEngine.__init__

    def _init(self, client=None, *args, **kwargs):
        if client is not None and not isinstance(client, TotalReclaw):
            # Lightweight test fake (plain class or MagicMock): stub the wallet
            # privates the engine now reads, and mark it so the store router
            # routes to its recorder. (MagicMock auto-creates every attribute,
            # so a hasattr probe can't tell a fake from a real client — hence
            # the isinstance check against the real TotalReclaw class.)
            _stub_wallet_privates(client)
            client._tr_import_test_fake = True
        captured["client"] = client
        return orig_init(self, client, *args, **kwargs)

    monkeypatch.setattr(ImportEngine, "__init__", _init)

    real_store = _engine.store_fact_batch

    async def _store(*a, **kw):
        facts = kw.get("facts") or (a[0] if a else [])
        client = captured.get("client")
        if (
            client is not None
            and getattr(client, "_tr_import_test_fake", False)
            and hasattr(client, "remember_batch")
        ):
            return await client.remember_batch(facts, source=kw.get("source"))
        return await real_store(*a, **kw)

    monkeypatch.setattr(_engine, "store_fact_batch", _store)
    yield


@pytest.fixture(autouse=True)
def _no_real_pin_check_llm_call(monkeypatch):
    """S41: ``auto_extract`` resolves an LLM config with ``detect_llm_config()``
    for the pinned-conflict check, which on a developer machine can find a real
    key. No test may reach a real LLM through it: the default stub answers
    nothing, so a pinned hit keeps its 2.6.0rc2 behaviour. Tests of the check
    patch ``pin_check.chat_completion`` themselves."""
    from totalreclaw.agent import pin_check

    async def _no_answer(*args, **kwargs):
        return None

    monkeypatch.setattr(pin_check, "chat_completion", _no_answer)


@pytest.fixture(autouse=True)
def _pin_embedder_unavailable(monkeypatch):
    """No unit test may load the real ONNX model by accident (auto-recall
    gates on these). Tests that want an embedder patch them back on."""
    monkeypatch.setattr("totalreclaw.embedding.is_model_loaded", lambda: False)
    monkeypatch.setattr("totalreclaw.embedding.is_model_available_locally", lambda: False)
    from totalreclaw.agent import recall as _recall

    monkeypatch.setattr(_recall, "_embed_inflight", None)
    monkeypatch.setattr(_recall, "_warmup_started", False)


@pytest.fixture(autouse=True)
def _reset_hermes_turn_dedup():
    """The per-turn dedup guard (internal#720) is process-global; tests reuse turn text."""
    from totalreclaw.hermes import hooks

    hooks._recent_turns.clear()
    yield
