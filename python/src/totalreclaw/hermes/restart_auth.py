"""5-tier auth fallback for /restart (issue #215, 2.3.6rc1).

Hermes plugin parity with the OpenClaw plugin's
``skill/plugin/restart-auth.ts``. The matrix is identical so a future
Hermes upstream change can plug this in directly.

NOTE on Hermes-side wiring: as of Hermes ``hermes-agent`` 2026.4.x the
plugin context API does NOT expose ``register_command()`` (the Hermes
team has it on the roadmap — see ``website/docs/guides/build-a-hermes-
plugin.md`` line 240). Until that lands, this module is **shipped as
an exported util** so:

  * The matrix is unit-testable + version-pinned.
  * When Hermes adds ``register_command``, the plugin's
    ``register()`` can wire ``/restart`` in two lines.
  * Documentation surfaces (SKILL.md, hermes-setup.md) can link the
    user to the per-tier reasoning if their config rejects them.

Tier order (highest priority first):

1. ``commands.ownerAllowFrom`` explicitly lists invoker → allow
2. ``channels.<provider>.allow_from`` explicitly lists invoker → allow
3. Invoker is the same identity the channel session is bound to → allow
4. ``credentials.json`` exists AND was paired via this same channel → allow
5. BOTH allow-from configs are unset (default) AND only one user has
   ever messaged this gateway → allow (lone-user heuristic)

Rejection ONLY when explicit config exists and excludes the invoker.

Pure (no fs / network side effects). Filesystem lookups are passed in
via ``RestartAuthDeps`` — the test suite injects mocks; production
plumbing (when Hermes ``register_command`` lands) reads from the
gateway config + the inbound-user tracker.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence


class RestartAuthVerdict(str, Enum):
    """Reason returned alongside the allow/reject decision.

    Tier 1..5 → allow. ``EXPLICIT_DENY_*`` and ``NO_TIER_MATCHED`` →
    reject. The string values match the OpenClaw plugin's TypeScript
    enum so logs are cross-grep-able.
    """

    TIER1_OWNER_ALLOW_FROM = "tier1-owner-allow-from"
    TIER2_CHANNEL_ALLOW_FROM = "tier2-channel-allow-from"
    TIER3_SESSION_BOUND = "tier3-session-bound"
    TIER4_CREDENTIALS_PAIRED = "tier4-credentials-paired"
    TIER5_LONE_USER = "tier5-lone-user"
    EXPLICIT_DENY_OWNER = "explicit-deny-owner"
    EXPLICIT_DENY_CHANNEL = "explicit-deny-channel"
    NO_TIER_MATCHED = "no-tier-matched"


@dataclass(frozen=True)
class RestartAuthResult:
    allow: bool
    reason: RestartAuthVerdict


@dataclass
class RestartAuthDeps:
    """Filesystem / runtime dependencies. Tests inject mocks; production
    will pass real fs reads (Hermes credentials.json existence) and a
    counter tracker (``InboundUserTracker`` parity TBD on Hermes).
    """

    load_credentials_exists: Callable[[], bool]
    was_paired_via_channel: Callable[[str], bool]
    get_distinct_inbound_user_count: Callable[[str], int]


def _normalize_entry(value: Any) -> str:
    return str(value).strip().lower()


def _entry_matches(allow_from: Sequence[Any], sender_id: str) -> bool:
    if not sender_id:
        return False
    needle = sender_id.strip().lower()
    if not needle:
        return False
    for raw in allow_from:
        normalized = _normalize_entry(raw)
        if not normalized:
            continue
        if normalized == "*":
            return True
        if normalized == needle:
            return True
        # Channel-prefixed entries: `telegram:12345`, `discord:user:12345`, etc.
        # Compare suffix after the LAST colon to support nested prefixes.
        last_colon = normalized.rfind(":")
        if last_colon >= 0:
            tail = normalized[last_colon + 1 :]
            if tail == needle:
                return True
    return False


def resolve_restart_auth(
    sender_id: Optional[str],
    channel: Optional[str],
    config: Optional[Mapping[str, Any]],
    deps: RestartAuthDeps,
) -> RestartAuthResult:
    """Apply the 5-tier fallback. See module docstring for tier order.

    ``config`` is the gateway config dict (Hermes loads this from
    ``~/.hermes/config.yaml``). The relevant sections are
    ``commands.ownerAllowFrom`` / ``commands.allowFrom`` and
    ``channels.<provider>.allow_from`` (Hermes uses snake_case in
    YAML; OpenClaw uses camelCase in JSON5 — the resolver accepts
    both shapes).
    """
    sid = (sender_id or "").strip()
    ch = (channel or "").strip().lower()
    cfg = config or {}

    commands = cfg.get("commands") or {}
    channels = cfg.get("channels") or {}

    # ---------------------------------------------------------------
    # Tier 1: commands.ownerAllowFrom or commands.allowFrom
    # ---------------------------------------------------------------
    owner_allow_from = commands.get("ownerAllowFrom") or commands.get("owner_allow_from")
    owner_list = list(owner_allow_from) if isinstance(owner_allow_from, (list, tuple)) else []
    owner_list_configured = len(owner_list) > 0
    if owner_list_configured and sid and _entry_matches(owner_list, sid):
        return RestartAuthResult(True, RestartAuthVerdict.TIER1_OWNER_ALLOW_FROM)

    cmd_allow_from = commands.get("allowFrom") or commands.get("allow_from") or {}
    cmd_global = cmd_allow_from.get("*") if isinstance(cmd_allow_from, Mapping) else None
    cmd_channel = cmd_allow_from.get(ch) if (isinstance(cmd_allow_from, Mapping) and ch) else None
    cmd_global_list = list(cmd_global) if isinstance(cmd_global, (list, tuple)) else []
    cmd_channel_list = list(cmd_channel) if isinstance(cmd_channel, (list, tuple)) else []
    cmd_allow_configured = len(cmd_global_list) > 0 or len(cmd_channel_list) > 0
    if sid and (
        _entry_matches(cmd_global_list, sid) or _entry_matches(cmd_channel_list, sid)
    ):
        return RestartAuthResult(True, RestartAuthVerdict.TIER1_OWNER_ALLOW_FROM)

    # ---------------------------------------------------------------
    # Tier 2: channels.<provider>.allow_from
    # ---------------------------------------------------------------
    channel_section = channels.get(ch) if ch else None
    channel_allow_from = (
        (channel_section.get("allow_from") if isinstance(channel_section, Mapping) else None)
        or (channel_section.get("allowFrom") if isinstance(channel_section, Mapping) else None)
    )
    channel_list = list(channel_allow_from) if isinstance(channel_allow_from, (list, tuple)) else []
    channel_list_configured = len(channel_list) > 0
    if channel_list_configured and sid and _entry_matches(channel_list, sid):
        return RestartAuthResult(True, RestartAuthVerdict.TIER2_CHANNEL_ALLOW_FROM)

    # ---------------------------------------------------------------
    # Tier 3: session-bound (paired channel + lone inbound user)
    # ---------------------------------------------------------------
    if ch and sid and deps.was_paired_via_channel(ch):
        if deps.get_distinct_inbound_user_count(ch) == 1:
            return RestartAuthResult(True, RestartAuthVerdict.TIER3_SESSION_BOUND)

    # ---------------------------------------------------------------
    # Tier 4: credentials present + paired-via-this-channel
    # ---------------------------------------------------------------
    if ch and deps.load_credentials_exists() and deps.was_paired_via_channel(ch):
        return RestartAuthResult(True, RestartAuthVerdict.TIER4_CREDENTIALS_PAIRED)

    # ---------------------------------------------------------------
    # Tier 5: lone-user heuristic (default config + 1 inbound user)
    # ---------------------------------------------------------------
    any_explicit = owner_list_configured or cmd_allow_configured or channel_list_configured
    if not any_explicit and ch and deps.get_distinct_inbound_user_count(ch) == 1:
        return RestartAuthResult(True, RestartAuthVerdict.TIER5_LONE_USER)

    # ---------------------------------------------------------------
    # No tier matched — pick the most specific rejection reason.
    # ---------------------------------------------------------------
    if owner_list_configured or cmd_allow_configured:
        return RestartAuthResult(False, RestartAuthVerdict.EXPLICIT_DENY_OWNER)
    if channel_list_configured:
        return RestartAuthResult(False, RestartAuthVerdict.EXPLICIT_DENY_CHANNEL)
    return RestartAuthResult(False, RestartAuthVerdict.NO_TIER_MATCHED)


def reject_message_for(reason: RestartAuthVerdict) -> str:
    """Human-readable rejection text — kept here so callers don't have
    to duplicate the strings."""
    if reason == RestartAuthVerdict.EXPLICIT_DENY_OWNER:
        return (
            "You are not authorized to use this command. Add your channel id "
            "to `commands.ownerAllowFrom` in your Hermes config to grant access."
        )
    if reason == RestartAuthVerdict.EXPLICIT_DENY_CHANNEL:
        return (
            "You are not authorized to use this command. Add your channel id "
            "to `channels.<provider>.allow_from` in your Hermes config to grant "
            "access."
        )
    return (
        "You are not authorized to use this command. Multiple users have "
        "messaged this gateway; configure `commands.ownerAllowFrom` to "
        "identify the owner."
    )
