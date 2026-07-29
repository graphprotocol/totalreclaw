"""#262 review finding 2 (blocker) pins — wrap-aware backup guidance."""
from __future__ import annotations


class TestWrapAwareBackupGuidance:
    """#262 review finding 2 (blocker): post-wrap, the generate flow must NOT
    tell the user to back up via `jq -r .mnemonic` — the field holds the
    keychain marker, and backing that up while believing it's the phrase is
    silent permanent vault loss."""

    def test_generate_message_points_to_keychain_when_wrapped(self, tmp_path, monkeypatch, capsys):
        import io as _io
        from totalreclaw.hermes import cli as hcli

        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("TOTALRECLAW_NO_KEYCHAIN", raising=False)
        # Mock backend: store succeeds → the file gets the marker. Also pin
        # detect_backend — wrap_credentials short-circuits to plaintext when
        # no backend exists (the CI/Linux case), which made this test pass
        # locally (macOS `security` present) but fail in CI. Deterministic
        # either way now.
        stored = {}
        monkeypatch.setattr(
            "totalreclaw.credentials_wrap.detect_backend", lambda: "mock"
        )
        monkeypatch.setattr(
            "totalreclaw.credentials_wrap.store_secret",
            lambda account, secret: stored.__setitem__(account, secret),
        )
        out = _io.StringIO()
        rc = hcli._run_generate(
            credentials_path=tmp_path / "credentials.json",
            emit_phrase=False,
            io=hcli._IO(out, out),
        )
        assert rc == 0
        text = out.getvalue()
        assert "jq -r .mnemonic" not in text, "post-wrap guidance must not point at the marker"
        assert "keychain" in text.lower()
        # And no phrase material leaked into the message.
        assert "__keychain__" not in text or "totalreclaw" in text

    def test_generate_message_keeps_jq_advice_when_plaintext(self, tmp_path, monkeypatch):
        import io as _io
        from totalreclaw.hermes import cli as hcli

        monkeypatch.setenv("TOTALRECLAW_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("TOTALRECLAW_NO_KEYCHAIN", "1")
        out = _io.StringIO()
        rc = hcli._run_generate(
            credentials_path=tmp_path / "credentials.json",
            emit_phrase=False,
            io=hcli._IO(out, out),
        )
        assert rc == 0
        assert "jq -r .mnemonic" in out.getvalue()
