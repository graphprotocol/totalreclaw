"""Tests for the 5-tier ``/restart`` auth fallback (issue #215, 2.3.6rc1).

Hermes parity with the OpenClaw plugin's ``restart-auth.test.ts``.

Asserted properties:
  - Each tier (1..5) accepts the invoker when the tier's preconditions hold.
  - Explicit-deny only fires when an explicit allow-from is configured
    and excludes the invoker (no later tier matches).
  - Default-config + lone user → tier 5 allow.
  - Default-config + multi user → no-tier-matched reject.
  - Wildcard ``*`` in ownerAllowFrom → tier 1 allow.
  - Channel-prefixed entry (``telegram:12345``) matches bare senderId.
  - Empty senderId + default-config + 1 inbound user → still allow
    (tier 5 is sender-agnostic on lone-user installs).
  - Hermes snake_case (``commands.owner_allow_from``,
    ``channels.<p>.allow_from``) AND OpenClaw camelCase
    (``commands.ownerAllowFrom``, ``channels.<p>.allowFrom``) both work.

Run with: ``python -m pytest tests/test_restart_auth_5_tier_2_3_6.py``
"""
from __future__ import annotations

from totalreclaw.hermes.restart_auth import (
    RestartAuthDeps,
    RestartAuthVerdict,
    reject_message_for,
    resolve_restart_auth,
)


def make_deps(
    *,
    credentials: bool = False,
    paired_channels: tuple[str, ...] = (),
    inbound_counts: dict[str, int] | None = None,
) -> RestartAuthDeps:
    counts = inbound_counts or {}
    return RestartAuthDeps(
        load_credentials_exists=lambda: credentials,
        was_paired_via_channel=lambda ch: ch in paired_channels,
        get_distinct_inbound_user_count=lambda ch: counts.get(ch, 0),
    )


# ---------------------------------------------------------------------------
# Tier 1: ownerAllowFrom (camelCase)
# ---------------------------------------------------------------------------
def test_tier1_owner_allow_from_camelcase():
    cfg = {"commands": {"ownerAllowFrom": ["12345"]}}
    v = resolve_restart_auth("12345", "telegram", cfg, make_deps())
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER1_OWNER_ALLOW_FROM


def test_tier1_owner_allow_from_snake_case():
    cfg = {"commands": {"owner_allow_from": ["12345"]}}
    v = resolve_restart_auth("12345", "telegram", cfg, make_deps())
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER1_OWNER_ALLOW_FROM


def test_tier1_owner_allow_from_wildcard():
    cfg = {"commands": {"ownerAllowFrom": ["*"]}}
    v = resolve_restart_auth("anyone", "telegram", cfg, make_deps())
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER1_OWNER_ALLOW_FROM


def test_tier1_prefixed_entry_matches_bare_senderid():
    cfg = {"commands": {"ownerAllowFrom": ["telegram:12345"]}}
    v = resolve_restart_auth("12345", "telegram", cfg, make_deps())
    assert v.allow is True


def test_tier1_commands_allow_from_global_wildcard_key():
    cfg = {"commands": {"allowFrom": {"*": ["12345"]}}}
    v = resolve_restart_auth("12345", "telegram", cfg, make_deps())
    assert v.allow is True


def test_tier1_commands_allow_from_per_provider_key():
    cfg = {"commands": {"allow_from": {"telegram": ["99999"]}}}
    v = resolve_restart_auth("99999", "telegram", cfg, make_deps())
    assert v.allow is True


# ---------------------------------------------------------------------------
# Tier 2: channels.<provider>.allow_from
# ---------------------------------------------------------------------------
def test_tier2_channel_allow_from_camelcase():
    cfg = {"channels": {"telegram": {"allowFrom": ["67890"]}}}
    v = resolve_restart_auth("67890", "telegram", cfg, make_deps())
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER2_CHANNEL_ALLOW_FROM


def test_tier2_channel_allow_from_snake_case():
    cfg = {"channels": {"telegram": {"allow_from": ["67890"]}}}
    v = resolve_restart_auth("67890", "telegram", cfg, make_deps())
    assert v.allow is True


# ---------------------------------------------------------------------------
# Tier 3: session-bound
# ---------------------------------------------------------------------------
def test_tier3_paired_channel_lone_user():
    deps = make_deps(paired_channels=("telegram",), inbound_counts={"telegram": 1})
    v = resolve_restart_auth("11111", "telegram", {}, deps)
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER3_SESSION_BOUND


def test_tier3_skipped_when_multi_user_then_tier4_fires():
    deps = make_deps(
        credentials=True,
        paired_channels=("telegram",),
        inbound_counts={"telegram": 3},
    )
    v = resolve_restart_auth("11111", "telegram", {}, deps)
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER4_CREDENTIALS_PAIRED


# ---------------------------------------------------------------------------
# Tier 4: credentials + paired-via-channel
# ---------------------------------------------------------------------------
def test_tier4_credentials_plus_pairing():
    deps = make_deps(
        credentials=True,
        paired_channels=("telegram",),
        inbound_counts={"telegram": 5},
    )
    v = resolve_restart_auth("22222", "telegram", {}, deps)
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER4_CREDENTIALS_PAIRED


def test_tier4_no_pairing_rejects():
    deps = make_deps(credentials=True, paired_channels=(), inbound_counts={"telegram": 5})
    v = resolve_restart_auth("22222", "telegram", {}, deps)
    assert v.allow is False
    assert v.reason == RestartAuthVerdict.NO_TIER_MATCHED


# ---------------------------------------------------------------------------
# Tier 5: lone-user heuristic
# ---------------------------------------------------------------------------
def test_tier5_default_config_lone_user():
    deps = make_deps(inbound_counts={"telegram": 1})
    v = resolve_restart_auth("33333", "telegram", {}, deps)
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER5_LONE_USER


def test_tier5_empty_sender_id_still_allowed_on_lone_install():
    """Fresh install: bot may receive /restart before sender id stable; tier 5 carries on."""
    deps = make_deps(inbound_counts={"telegram": 1})
    v = resolve_restart_auth("", "telegram", {}, deps)
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER5_LONE_USER


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------
def test_reject_owner_allow_from_set_sender_not_in_list():
    cfg = {"commands": {"ownerAllowFrom": ["12345"]}}
    deps = make_deps(inbound_counts={"telegram": 1})
    v = resolve_restart_auth("99999", "telegram", cfg, deps)
    assert v.allow is False
    assert v.reason == RestartAuthVerdict.EXPLICIT_DENY_OWNER


def test_reject_channel_allow_from_set_sender_not_in_list():
    cfg = {"channels": {"telegram": {"allow_from": ["67890"]}}}
    deps = make_deps(inbound_counts={"telegram": 1})
    v = resolve_restart_auth("99999", "telegram", cfg, deps)
    assert v.allow is False
    assert v.reason == RestartAuthVerdict.EXPLICIT_DENY_CHANNEL


def test_reject_default_config_multi_user_no_tier_matched():
    deps = make_deps(inbound_counts={"telegram": 5})
    v = resolve_restart_auth("11111", "telegram", {}, deps)
    assert v.allow is False
    assert v.reason == RestartAuthVerdict.NO_TIER_MATCHED


# ---------------------------------------------------------------------------
# Reject messages
# ---------------------------------------------------------------------------
def test_reject_message_for_owner_deny():
    msg = reject_message_for(RestartAuthVerdict.EXPLICIT_DENY_OWNER)
    assert msg.startswith("You are not authorized")
    assert "ownerAllowFrom" in msg


def test_reject_message_for_channel_deny():
    msg = reject_message_for(RestartAuthVerdict.EXPLICIT_DENY_CHANNEL)
    assert "channels." in msg
    assert "allow_from" in msg


def test_reject_message_for_no_tier_matched():
    msg = reject_message_for(RestartAuthVerdict.NO_TIER_MATCHED)
    assert "Multiple users" in msg


# ---------------------------------------------------------------------------
# Edge case: tier 1 fails but tier 2 saves the day
# ---------------------------------------------------------------------------
def test_tier_1_fails_tier_2_wins():
    cfg = {
        "commands": {"ownerAllowFrom": ["ownerguy"]},
        "channels": {"telegram": {"allow_from": ["12345"]}},
    }
    v = resolve_restart_auth("12345", "telegram", cfg, make_deps())
    assert v.allow is True
    assert v.reason == RestartAuthVerdict.TIER2_CHANNEL_ALLOW_FROM
