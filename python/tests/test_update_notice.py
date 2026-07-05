"""Tests for the update-notice bookkeeping (version compare + rate-limit +
kill-switch). See ``totalreclaw.update_notice``."""
import time

import pytest

from totalreclaw import update_notice as un


# ── version comparison matrix ────────────────────────────────────────────
class TestIsNewerStable:
    @pytest.mark.parametrize(
        "latest,installed,expected",
        [
            # basic ordering
            ("2.4.5", "2.4.4", True),
            ("2.4.5", "2.4.5", False),
            ("2.4.5", "2.4.6", False),
            ("2.4.5", "2.5.0", False),
            ("2.5.0", "2.4.9", True),
            ("3.0.0", "2.9.9", True),
            ("1.9.9", "2.0.0", False),
            # patch-less installed defaults patch=0
            ("2.4.1", "2.4", True),
            ("2.4.0", "2.4", False),
            # rc-vs-final: a final beats its OWN rc line
            ("2.4.5", "2.4.5rc11", True),
            ("2.4.5", "2.4.5rc1", True),
            # user already ahead on a newer rc line — NOT nudged by older final
            ("2.4.5", "2.4.6rc1", False),
            ("2.4.5", "2.5.0rc1", False),
            # final vs a LOWER final where installed is an rc of that lower one
            ("2.4.5", "2.4.4rc9", True),
            # rc latest vs final installed of same base — final installed wins
            ("2.4.5rc2", "2.4.5", False),
            # rc latest newer than installed rc on same base
            ("2.4.5rc5", "2.4.5rc2", True),
            ("2.4.5rc2", "2.4.5rc5", False),
            # phase ordering a < b < rc < final
            ("2.4.5b1", "2.4.5a9", True),
            ("2.4.5rc1", "2.4.5b9", True),
            # 'v' prefix tolerated
            ("v2.4.5", "2.4.4", True),
            # absent / malformed ⇒ never nudge
            (None, "2.4.4", False),
            ("2.4.5", None, False),
            ("", "2.4.4", False),
            ("garbage", "2.4.4", False),
            ("2.4.5", "not-a-version", False),
        ],
    )
    def test_matrix(self, latest, installed, expected):
        assert un.is_newer_stable(latest, installed) is expected


# ── rate-limit persistence ───────────────────────────────────────────────
class TestRateLimit:
    @pytest.fixture(autouse=True)
    def _isolate_state_dir(self, tmp_path, monkeypatch):
        # Redirect the sentinel dir into a temp path so tests never touch the
        # real ~/.totalreclaw and don't interfere with each other.
        monkeypatch.setattr(un, "_STATE_DIR", tmp_path / ".totalreclaw")

    def test_no_sentinel_is_not_rate_limited(self):
        assert un.last_notified_at() is None
        assert un.within_rate_limit() is False

    def test_mark_then_within_window(self):
        un.mark_notified()
        assert un.last_notified_at() is not None
        assert un.within_rate_limit() is True

    def test_outside_window_after_24h(self):
        now = time.time()
        un.mark_notified(now=now - (un.NOTICE_INTERVAL_SECONDS + 10))
        # A "now" past the window ⇒ no longer rate-limited.
        assert un.within_rate_limit(now=now) is False

    def test_just_inside_window(self):
        now = time.time()
        un.mark_notified(now=now - (un.NOTICE_INTERVAL_SECONDS - 10))
        assert un.within_rate_limit(now=now) is True

    def test_unreadable_sentinel_degrades_to_not_limited(self, tmp_path):
        # Write garbage into the sentinel — parsing fails ⇒ not rate-limited.
        un._STATE_DIR.mkdir(parents=True, exist_ok=True)
        un._sentinel_path().write_text("not-a-float", encoding="utf-8")
        assert un.last_notified_at() is None
        assert un.within_rate_limit() is False


# ── kill-switch ──────────────────────────────────────────────────────────
class TestKillSwitch:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_disabled_truthy(self, monkeypatch, val):
        monkeypatch.setenv("TOTALRECLAW_DISABLE_UPDATE_NOTICE", val)
        assert un.disabled_by_env() is True

    @pytest.mark.parametrize("val", ["0", "false", "", "no", "off"])
    def test_not_disabled(self, monkeypatch, val):
        monkeypatch.setenv("TOTALRECLAW_DISABLE_UPDATE_NOTICE", val)
        assert un.disabled_by_env() is False

    def test_unset_is_not_disabled(self, monkeypatch):
        monkeypatch.delenv("TOTALRECLAW_DISABLE_UPDATE_NOTICE", raising=False)
        assert un.disabled_by_env() is False


# ── combined gate: maybe_build_update_notice ─────────────────────────────
class TestMaybeBuildUpdateNotice:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(un, "_STATE_DIR", tmp_path / ".totalreclaw")
        monkeypatch.delenv("TOTALRECLAW_DISABLE_UPDATE_NOTICE", raising=False)

    def test_fires_when_newer_and_not_rate_limited(self):
        notice = un.maybe_build_update_notice("2.4.5", "2.4.4")
        assert notice is not None
        assert "2.4.5 is available" in notice
        assert "2.4.4" in notice
        assert "update TotalReclaw" in notice

    def test_none_when_not_newer(self):
        assert un.maybe_build_update_notice("2.4.5", "2.4.5") is None
        assert un.maybe_build_update_notice("2.4.5", "2.5.0") is None

    def test_none_when_absent(self):
        assert un.maybe_build_update_notice(None, "2.4.4") is None

    def test_none_when_kill_switch(self, monkeypatch):
        monkeypatch.setenv("TOTALRECLAW_DISABLE_UPDATE_NOTICE", "1")
        assert un.maybe_build_update_notice("2.4.5", "2.4.4") is None

    def test_none_when_rate_limited(self):
        un.mark_notified()  # burn the window
        assert un.maybe_build_update_notice("2.4.5", "2.4.4") is None

    def test_rc_user_notified_when_final_ships(self):
        notice = un.maybe_build_update_notice("2.4.5", "2.4.5rc11")
        assert notice is not None
        assert "2.4.5 is available" in notice

    def test_newer_rc_user_not_notified_by_older_final(self):
        assert un.maybe_build_update_notice("2.4.5", "2.4.6rc1") is None

    def test_second_call_within_window_suppressed_after_mark(self):
        # Simulate the hook: build once, then mark, then the next build in-window
        # returns None.
        first = un.maybe_build_update_notice("2.4.5", "2.4.4")
        assert first is not None
        un.mark_notified()
        second = un.maybe_build_update_notice("2.4.5", "2.4.4")
        assert second is None
