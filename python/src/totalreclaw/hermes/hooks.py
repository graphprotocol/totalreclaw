"""Lifecycle hooks for TotalReclaw Hermes plugin.

Thin adapter that wires the generic ``totalreclaw.agent`` lifecycle
functions into Hermes's hook registration system.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PluginState

from totalreclaw.agent.lifecycle import (
    auto_extract as _auto_extract,
    session_debrief as _session_debrief,
    _is_near_duplicate,
    _fetch_recent_memories,
    _owner_address,
    _owner_addresses,
    STORE_DEDUP_THRESHOLD,
)
from totalreclaw.agent.loop_runner import run_sync
from totalreclaw.agent.pending_drain import drain_pending, has_pending
from totalreclaw.agent.recall import auto_recall
from totalreclaw.agent.extraction import extract_facts_llm, extract_facts_heuristic
from totalreclaw.relay import _HARDCODED_DEFAULT_URL

logger = logging.getLogger(__name__)


# 2.3.3-rc.1 (PR #165) — RC builds bake the staging URL as default. When
# a real user installs a stable wheel they should never see this banner;
# when a QA / maintainer installs an RC wheel they MUST see it so they
# don't mistake the staging environment for production. Fires exactly
# once per session via the ``_totalreclaw_rc_banner_shown`` latch.
_RC_STAGING_BANNER = (
    "## TotalReclaw — RC / staging build\n"
    "WARNING: TotalReclaw is running in RC/staging mode against "
    "api-staging.totalreclaw.xyz.\n"
    "Staging has no SLA + may be wiped between QA cycles. Do NOT use "
    "this build for real data.\n"
    "Install the stable release for production: "
    "`pip install totalreclaw` (no --pre)."
)


def _rc_staging_banner_active() -> bool:
    """Return True iff the bundled default URL points at staging AND the
    user hasn't overridden via ``TOTALRECLAW_SERVER_URL``.

    Reads ``_HARDCODED_DEFAULT_URL`` directly (not ``_default_relay_url``)
    so the env-override check is explicit + isolated from the default
    resolution path. The build-time injection in
    ``.github/workflows/publish-python-client.yml`` rewrites the
    constant to the production URL for stable wheels, so this returns
    False on stable installs even with no env var set.
    """
    if "api-staging.totalreclaw.xyz" not in _HARDCODED_DEFAULT_URL:
        return False
    if os.environ.get("TOTALRECLAW_SERVER_URL"):
        return False
    return True

#: Bug #6 + 2.3.7rc3: when the plugin is installed + enabled but the
#: user hasn't completed pairing (no ``credentials.json`` on disk),
#: inject a one-time-per-session context nudge telling the agent to
#: PROACTIVELY ask whether the user wants to create a new account or
#: restore an existing one — before answering whatever else they
#: said. Catches the "abandoned mid-setup" case after a gateway
#: ``/restart`` (or docker restart) wiped chat history: the agent
#: would otherwise forget it was supposed to continue with
#: ``totalreclaw_pair`` and silently treat the user's latest message
#: as a fresh request.
#:
#: rc.26 → rc3 copy update: drop the implicit "user is asking about
#: memory" framing — the nudge now fires on ANY first turn while
#: unconfigured, so the copy must be unconditional + decision-focused.
_SETUP_NUDGE = (
    "## TotalReclaw setup not complete\n"
    "The TotalReclaw plugin is installed and enabled, but the user "
    "hasn't finished pairing (no credentials on disk yet). They may "
    "have started setup in a previous session and lost chat history "
    "from a gateway /restart — they probably don't realise setup is "
    "incomplete.\n\n"
    "Before answering the user's current message, proactively ask "
    "something like: \"I notice you haven't finished setting up "
    "TotalReclaw. Would you like to create a new account, or restore "
    "an existing one with your 12-word recovery phrase?\"\n\n"
    "Once they pick a path: if they want to create a new account, "
    "call `totalreclaw_pair` with `mode=generate` (or no mode arg — "
    "the default `mode=either` lets them choose on the browser page). "
    "If they want to restore, call `totalreclaw_pair` with "
    "`mode=import`. After pairing completes, use `totalreclaw_remember` "
    "/ `totalreclaw_recall` for end-to-end encrypted memory — they "
    "outrank any built-in `memory` tool for durability + encryption."
)

#: F4 (issue #167): for configured users, schema-level "prefer this tool"
#: language in the REMEMBER/RECALL descriptions is not strong enough — the
#: rc.24 chat-flow QA showed every memory-intent turn routing to Hermes's
#: built-in ``memory`` tool. We inject this nudge on every memory-intent
#: turn (not just first) so context-window summarization can't drop it on
#: the turn it matters. Cost is small (~80 tokens) and only paid when
#: ``_looks_like_memory_intent`` matches.
_TOOL_PRIORITY_NUDGE = (
    "## TotalReclaw tool priority\n"
    "For any user fact, preference, decision-with-reasoning, directive "
    "('always X' / 'never Y'), commitment, or notable episode the user "
    "shares — ALWAYS call `totalreclaw_remember` instead of any built-in "
    "`memory` tool. TotalReclaw stores memories end-to-end encrypted, "
    "on-chain, and portable across every AI tool the user "
    "runs; the built-in `memory` tool is plain-text, session-scoped, and "
    "not cross-agent. For lookups, prefer `totalreclaw_recall` over any "
    "built-in equivalent for the same reason."
)

#: Keywords that trigger the setup nudge when the user isn't configured.
#: Kept intentionally broad — a missed nudge on an unrelated question is
#: a no-op, but a missed nudge on a memory-relevant question means the
#: user sees "memory" work only for the session (via Hermes built-in)
#: and never learns about TotalReclaw.
_MEMORY_INTENT_KEYWORDS = (
    "remember", "recall", "forget", "memory", "memories", "note",
    "save", "store", "record", "encrypted", "persistent", "across",
    "vault", "preference", "what do you know",
)


def _looks_like_memory_intent(user_message: str) -> bool:
    """Cheap heuristic: does the message reference memory semantics?"""
    if not user_message:
        return False
    lower = user_message.lower()
    return any(kw in lower for kw in _MEMORY_INTENT_KEYWORDS)


#: 2.4.4rc2 (F6) — phrases that indicate the user is asking for a
#: session-level summary / debrief. Any of these matches triggers the
#: pre_llm_call debrief nudge.
_DEBRIEF_INTENT_KEYWORDS = (
    "summarize what we",
    "summarize this session",
    "summary of what we",
    "summary of this session",
    "session summary",
    "session recap",
    "session debrief",
    "give me a summary",
    "give me a debrief",
    "give me a recap",
    "what did we discuss",
    "what did we cover",
    "what did we talk about",
    "what have we discussed",
    "what have we covered",
    "rolling memory",
    "debrief on this",
    "recap on this",
    "recap of this",
)

#: Negative patterns — message matches a debrief keyword but is asking
#: about something SPECIFIC, not the session itself. Skip the nudge.
_DEBRIEF_NEGATIVE_GATES = (
    "summary of the code",
    "summary of the doc",
    "summary of the file",
    "summary of the function",
    "summary of the article",
    "summary of the paper",
    "summary of the pr",
    "summary of the issue",
    "recap of the doc",
    "recap of the article",
    "debrief on the code",
    "debrief on the doc",
)


def _detect_debrief_intent(user_message: str) -> bool:
    """2.4.4rc2 (F6) — return True if `user_message` is asking for a
    session-level debrief / summary.

    Used by ``pre_llm_call`` to inject a strong "you MUST call
    `totalreclaw_debrief` this turn" nudge into the agent's context.
    QA on 2.4.4rc1 found the agent producing plain-text recaps without
    calling the tool, despite SKILL.md prose telling it to. Markdown
    rules aren't enough for glm-5-turbo; hook-level enforcement is.

    Heuristic: positive keyword match + negative-gate exclusion (so
    "summary of the code" doesn't trigger). Conservative on false
    positives — better to under-nudge than nudge when the user wasn't
    actually asking for a debrief.
    """
    if not user_message or not user_message.strip():
        return False
    lower = user_message.lower()
    if not any(kw in lower for kw in _DEBRIEF_INTENT_KEYWORDS):
        return False
    # If any negative gate matches, suppress.
    if any(neg in lower for neg in _DEBRIEF_NEGATIVE_GATES):
        return False
    return True


#: 2.4.4rc2 (F6) — context block injected into pre_llm_call when the
#: user explicitly asks for a session debrief. The agent MUST call
#: ``totalreclaw_debrief`` (no args) this turn before responding;
#: plain-text recaps without the tool call were the rc1 QA failure.
_DEBRIEF_NUDGE = (
    "## TotalReclaw — user requested session debrief\n"
    "The user's message matches the debrief-intent pattern (rc.5+ rule). "
    "Before responding, you MUST call `totalreclaw_debrief()` with no args "
    "to capture a structured session summary into the user's vault. After "
    "the tool returns its summary, write your natural-language response to "
    "the user using the summary content.\n\n"
    "Do NOT skip the tool call. Plain-text recaps without `totalreclaw_debrief` "
    "lose the summary forever — the on_session_end hook may not fire for this "
    "session (e.g. user keeps chatting). The 2.4.4 SKILL.md ## Usage section "
    "explicitly says: 'Manual call ONLY when the user explicitly asks for a "
    "session recap mid-conversation: summarize what we discussed, give me a "
    "debrief on this session, what's the rolling memory of this chat?'. All "
    "three trigger phrases the user used match this rule. CALL THE TOOL."
)


def _get_hermes_llm_config():
    """Get LLM config from Hermes's own config files.

    Returns an LLMConfig from ~/.hermes/config.yaml + ~/.hermes/.env, or None.
    Imported lazily to avoid circular imports.
    """
    try:
        from .tools import _read_hermes_llm_config
        return _read_hermes_llm_config()
    except Exception:
        return None


def _maybe_reconfigure_from_disk(state: "PluginState") -> bool:
    """Re-read ``~/.totalreclaw/credentials.json`` and reconfigure ``state``
    if creds appeared after the plugin was loaded (issue #191 root cause).

    DAEMON-MODE flow that this fixes
    ---------------------------------
    The Hermes gateway is a single long-lived process. The TotalReclaw
    plugin is loaded ONCE at gateway startup, which constructs a single
    ``PluginState`` instance via ``register()``. That ctor calls
    ``_try_auto_configure()`` which reads ``credentials.json`` if it
    already exists. If the user pairs AFTER the gateway has booted, the
    pair flow's completion path (sidecar subprocess in rc.24+) writes
    the creds file but the gateway's in-memory ``state`` is never
    notified. ``state.is_configured()`` stays False, so every
    ``post_llm_call`` returns at the early check on line ~273 and NO
    auto-extraction ever fires for the rest of the gateway's lifetime
    — the user has to restart the gateway to pick up creds, which is
    what ``totalreclaw_pair``'s instructions tell them to do.

    Stable 2.3.1 user QA on 2026-04-27 hit exactly this: paired via
    the chat-driven setup flow, did not restart the gateway, then had
    a multi-turn natural conversation with NO extractions firing —
    matching the symptom in QA-bug #191.

    The lazy reconfigure here removes that restart requirement. Called
    on every ``on_session_start`` (cheap — file-stat) and as a safety
    net at the head of ``pre_llm_call`` / ``post_llm_call`` if the
    state is still unconfigured. Idempotent: short-circuits when
    ``state.is_configured()`` is already True.

    Returns ``True`` if a reconfigure happened (state was unconfigured
    and is now configured), ``False`` otherwise.
    """
    if state.is_configured():
        return False
    try:
        # ``_try_auto_configure`` re-reads env + creds.json and calls
        # ``state.configure()`` if it finds a usable mnemonic. Idempotent
        # on the disk side — same canonical creds file shape stays put.
        state._try_auto_configure()
    except Exception as exc:  # pragma: no cover — best-effort
        logger.debug("TotalReclaw: lazy reconfigure failed: %s", exc)
        return False
    if state.is_configured():
        logger.info(
            "TotalReclaw: lazy reconfigure picked up credentials.json "
            "written after plugin load (daemon-mode pair flow). "
            "Auto-extraction is now active."
        )
        return True
    return False


def _eager_account_register(state: "PluginState") -> None:
    """Issue a one-shot ``client.status()`` so the relay creates the
    account record eagerly — fix for QA-bug #192.

    Why
    ---
    Pair-completion writes credentials.json + builds a TotalReclaw
    client object, but the relay only LEARNS about the smart-account
    address when it sees an authenticated request keyed on it. Before
    this hook ran, the first such request was whichever tool the LLM
    happened to call next (often ``totalreclaw_status`` AFTER the user
    explicitly asked "what's my quota?", per the QA report). On a
    fresh setup that means the staging relay had no account record
    until the user manually probed.

    Auto-extraction (fix for #191 above) is also a relay-write path,
    so without this eager probe the FIRST extraction batch would race
    the implicit account creation; the relay handles that race fine
    today (account is created on first wallet-keyed request) but the
    user sees no observable account until they probe — and any
    relay-side billing logic that gates on account-existing would
    silently no-op the first batch.

    What this does
    --------------
    Calls ``state.get_client().status()`` once per state-configure
    event via ``run_sync``. ``client.status()`` resolves the SA
    address (``_ensure_address``), registers the auth key
    (``_ensure_registered``), and hits ``GET /v1/billing/status?
    wallet_address=<sa>`` — all of which together are what the relay
    treats as "first contact" for an account. Best-effort: any
    exception is logged at DEBUG and swallowed so the gateway never
    crashes on a relay outage.

    Idempotent across the gateway lifetime: the per-state ``_eager_
    account_registered`` attribute (ad-hoc, mirrors the existing
    ``_totalreclaw_*`` attribute pattern in this module) suppresses
    repeat calls. Cleared whenever ``state.configure()`` re-runs,
    via the explicit reset in :func:`_maybe_reconfigure_from_disk`.
    """
    if not state.is_configured():
        return
    if getattr(state, "_eager_account_registered", False):
        return
    client = state.get_client()
    if client is None:
        return
    try:
        # ``client.status`` runs the full first-contact handshake:
        # SA derivation → auth-key register → /v1/billing/status. A
        # 200 from the billing endpoint with the SA on it is exactly
        # what triggers the relay-side account record.
        run_sync(client.status())
        state._eager_account_registered = True
        logger.info(
            "TotalReclaw: eager account register completed (relay "
            "now has account record for SA before any extraction)."
        )
    except Exception as exc:
        # Don't latch the flag — let the next session_start retry.
        logger.debug(
            "TotalReclaw: eager account register failed (will retry "
            "next session_start): %s",
            exc,
        )


def on_session_start(state: "PluginState", **kwargs) -> None:
    """Initialize client, drain any pending extraction queue, and check billing.

    The drain step recovers messages whose extraction was lost in a
    previous CLI one-shot turn — see issue #148 + ``pending_drain``. The
    interpreter is healthy at session start, so the persistent sync-loop
    runner can drive httpx normally.

    DAEMON-MODE FIXES (2.3.2-rc.1):
      * Lazy reconfigure (#191): if creds.json appeared on disk after
        the gateway loaded the plugin (the user paired mid-conversation
        and the sidecar wrote creds without restart), pick them up here
        instead of forcing a gateway restart.
      * Eager account register (#192): once configured, call
        ``client.status()`` once so the relay creates the account
        record before the user explicitly queries it.
    """
    session_id = kwargs.get("session_id", "")
    logger.debug("TotalReclaw on_session_start: %s", session_id)

    state.reset_turn_counter()
    # 2.4.4rc2 (F7) — reset the pending-auto-extract buffer + the
    # suppressed-writes counter at session boundaries. A previous
    # session's pending entries are stale + would suppress legitimate
    # writes in the new session. Defensive: tolerate legacy state
    # subclasses / test mocks that don't expose the method.
    if hasattr(state, "clear_pending_extract_buffer"):
        state.clear_pending_extract_buffer()
    # 2.4.4rc2 (F6) — reset the debrief-nudge latch so each new
    # session can independently nudge the agent if the user requests
    # a debrief.
    if hasattr(state, "_totalreclaw_debrief_nudge_turn"):
        state._totalreclaw_debrief_nudge_turn = -1
    if hasattr(state, "_totalreclaw_debrief_skip_count"):
        state._totalreclaw_debrief_skip_count = 0
    # memq-3 — assign a fresh UUIDv7 to AgentState so per-session
    # artefacts (extraction → Crystal → debrief) can be keyed by it.
    # Runs before the unconfigured-state early-return so log-only
    # sessions still get an id.
    state.start_session()

    # 2.3.7rc3 — reset the setup-nudge latch so each new session gets
    # one proactive nudge. Without this, an unconfigured user who
    # closed session A (nudge consumed) and opens session B inside the
    # SAME daemon process never re-surfaces the nudge — silently
    # stuck mid-setup. The latch deliberately persists across turns
    # within a single session so the agent doesn't spam the prompt.
    if hasattr(state, "_totalreclaw_setup_nudge_shown"):
        state._totalreclaw_setup_nudge_shown = False

    # 2.3.3-rc.1 (PR #165) — emit the RC/staging banner exactly once per
    # session when the wheel is RC AND the user hasn't overridden the
    # server URL. Surfaced via the existing ``quota_warning`` channel so
    # the next ``pre_llm_call`` injects it as ``context``. Latch lives
    # on the state instance so the banner is one-shot per process. Runs
    # BEFORE the configured-only return below so unconfigured RC users
    # see the banner on their first turn.
    if _rc_staging_banner_active():
        already_shown = getattr(state, "_totalreclaw_rc_banner_shown", False)
        if not already_shown:
            state._totalreclaw_rc_banner_shown = True
            state.set_quota_warning(_RC_STAGING_BANNER)
            logger.info(
                "TotalReclaw: RC/staging banner queued for first turn "
                "(default URL = %s).",
                _HARDCODED_DEFAULT_URL,
            )

    # Fix #191 — pick up creds written after plugin load. Cheap (one
    # file-stat) when the state is already configured.
    _maybe_reconfigure_from_disk(state)

    if not state.is_configured():
        return

    # Fix #192 — register account with relay eagerly so the first
    # extraction (or any future write) doesn't race silent account-
    # creation server-side.
    _eager_account_register(state)

    # Drain any messages that a prior interpreter-shutdown race deferred.
    # This must run before billing-cache logic so a drain-induced
    # quota_warning isn't overwritten.
    #
    # We pass ALL known owner addresses (EOA + SA) so the drain matches
    # entries written under either side. Issue #169: ``_owner_address``
    # at write time picks SA when set / EOA otherwise; lifecycle ordering
    # in ``hermes chat -q`` can put the next-session ``on_session_start``
    # on the OTHER side of that split, silently missing queued batches.
    try:
        owners = _owner_addresses(state)
        if owners and has_pending(owners):
            batches = drain_pending(owners)
            recovered_count = _drain_into_state(state, batches)
            if recovered_count > 0:
                logger.info(
                    "TotalReclaw: drained %d message(s) from prior interpreter-shutdown race.",
                    recovered_count,
                )
                state.set_quota_warning(
                    f"TotalReclaw: caught up on auto-extraction for {recovered_count} "
                    f"message(s) deferred from a prior CLI session."
                )
    except Exception as exc:
        logger.warning("TotalReclaw: pending-drain failed at session start: %s", exc)

    # Check billing cache and update server-driven config
    try:
        billing = state.get_cached_billing()
        if billing:
            # Update extraction config from server
            state.update_from_billing(billing)

            used = billing.get("free_writes_used", 0)
            limit = max(billing.get("free_writes_limit", 250), 1)
            tier = billing.get("tier", "free")
            if used / limit > 0.8:
                pct = int(used / limit * 100)
                if tier == "pro":
                    nudge = (
                        "Pro cap is 1,500 memories/month. The quota resets on "
                        "the 1st of next month."
                    )
                else:
                    nudge = (
                        "Free cap is 250 memories/month. Upgrade to Pro for "
                        "1,500 memories/month — run "
                        "totalreclaw_upgrade or visit "
                        "https://totalreclaw.xyz/pricing."
                    )
                state.set_quota_warning(
                    f"TotalReclaw: {used}/{limit} memories used this month "
                    f"({pct}%). {nudge}"
                )
                logger.info("TotalReclaw: Memory usage >80%% — quota warning set")
    except Exception:
        pass


def _drain_into_state(state: "PluginState", batches: list[list[dict]]) -> int:
    """Replay drained message batches into the agent state and run a
    full-mode auto-extract to land the facts.

    Returns the total number of messages recovered. Errors during the
    extraction call are logged and swallowed — drain is best-effort.
    Whether the drain landed facts or not, the messages are consumed
    from the queue (already done by ``drain_pending``) so we don't
    re-attempt indefinitely on a persistent failure.
    """
    if not batches:
        return 0

    total = 0
    # Snapshot the original buffer so the active session's own messages
    # stay intact after the drain. We process drained batches in a
    # separate state-buffer slice via ``add_message`` + final
    # ``mark_messages_processed`` so the user-visible session message
    # buffer keeps the drained content for context.
    for batch in batches:
        for msg in batch:
            role = msg.get("role") if isinstance(msg, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if role and content:
                state.add_message(role, content)
                total += 1

    if total == 0:
        return 0

    try:
        _auto_extract(state, mode="full", llm_config=_get_hermes_llm_config())
    except Exception as exc:
        logger.warning("TotalReclaw: drain-side auto_extract failed: %s", exc)

    return total


def pre_llm_call(state: "PluginState", **kwargs) -> Optional[dict]:
    """Auto-recall on first turn, inject memories, quota warnings, the
    unconfigured-user setup nudge (Bug #6), and the configured-user
    tool-priority nudge (F4 / issue #167) into context.

    When the plugin is installed but the user hasn't run
    ``totalreclaw_setup`` yet, a one-time nudge is injected on the first
    turn that references any memory intent. The nudge tells the Hermes
    agent to offer setup — preventing silent routing to Hermes's
    built-in ``memory`` tool.

    For configured users, a parallel tool-priority nudge fires on every
    memory-intent turn (cheap and reliably present in active LLM context)
    so the LLM picks ``totalreclaw_remember`` instead of Hermes's
    built-in ``memory`` for fact/preference/directive/commitment/episode
    intents.
    """
    user_message = kwargs.get("user_message", "")
    is_first_turn = kwargs.get("is_first_turn", False)

    # Fix #191 safety net — if the user paired AFTER on_session_start
    # already ran (e.g. they paired mid-session inside the same daemon
    # session id), pick up creds.json before we decide whether to inject
    # the unconfigured-user nudge or the configured-user tool-priority
    # nudge. Cheap when the state is already configured (early return).
    if _maybe_reconfigure_from_disk(state):
        # Reconfigure just landed — also kick off the eager account
        # register so #192 stays fixed for the mid-session pair path.
        _eager_account_register(state)

    # Bug #6 + 2.3.7rc3 — unconfigured: one-time-per-session setup
    # nudge. The original rc.26 implementation gated the nudge on
    # ``_looks_like_memory_intent(user_message)`` — i.e. the nudge only
    # surfaced if the user asked something memory-related on their very
    # first turn. That worked for users who paired uninterrupted from
    # the first prompt, but Pedro's 2.3.7rc2 QA (2026-05-14) hit the
    # "abandoned mid-setup" path: the agent had asked the user to
    # ``/restart``, the restart killed the daemon and wiped chat
    # history, the user reopened chat and typed something unrelated
    # (a greeting / unrelated question) → no memory intent → no nudge,
    # so the agent silently moved on without resuming pairing.
    #
    # rc3 drops the memory-intent gate. If the plugin is loaded and
    # creds.json is absent, fire the nudge on the FIRST TURN of EVERY
    # session. The latch is reset in ``on_session_start`` (see below)
    # so a user who closed session A unconfigured + opens session B
    # gets a fresh nudge.
    if not state.is_configured():
        shown = getattr(state, "_totalreclaw_setup_nudge_shown", False)
        if not shown:
            state._totalreclaw_setup_nudge_shown = True
            return {"context": _SETUP_NUDGE}
        return None

    context_parts: list[str] = []

    # F4 (issue #167) — configured: bias LLM toward totalreclaw_remember
    # over Hermes's built-in ``memory`` for memory-intent turns. Fires
    # every matching turn (not gated to first) so sliding context windows
    # can't drop it on the turn the user actually expresses an intent.
    if user_message and _looks_like_memory_intent(user_message):
        context_parts.append(_TOOL_PRIORITY_NUDGE)

    # 2.4.4rc2 (F6) — debrief-intent nudge. Fires when the user message
    # matches one of the canonical session-debrief phrases. Latched per
    # turn so we don't re-inject on the same turn (would happen if
    # pre_llm_call runs multiple times per LLM call); reset at session
    # start. We DO fire it again on a SUBSEQUENT turn if the agent
    # skipped the previous nudge — the F6 fallback in post_llm_call
    # detects skip + the next turn's nudge tries again.
    if user_message and _detect_debrief_intent(user_message):
        last_nudge_turn = getattr(state, "_totalreclaw_debrief_nudge_turn", -1)
        if last_nudge_turn != state.turn_count:
            state._totalreclaw_debrief_nudge_turn = state.turn_count
            context_parts.append(_DEBRIEF_NUDGE)
            logger.info(
                "TotalReclaw: injected debrief nudge on turn %d (intent matched)",
                state.turn_count,
            )

    # Inject quota warning (once per session)
    quota_warning = state.get_quota_warning()
    if quota_warning:
        context_parts.append(quota_warning)
        state.clear_quota_warning()

    # Proactive import-completion notice (PRD-IMP OQ-5) — one-shot per
    # completed import. The background import task only writes
    # status=completed; this surfaces it on the next turn so the agent tells
    # the user the import finished WITHOUT being asked (pull -> push).
    try:
        from totalreclaw.import_state import (
            read_completed_unannounced_imports,
            mark_import_announced,
        )
        for done in read_completed_unannounced_imports():
            dups = (
                f", {done.dups_skipped} duplicate(s) skipped"
                if done.dups_skipped else ""
            )
            context_parts.append(
                f"[totalreclaw] The background import from {done.source} just "
                f"finished: {done.facts_stored} memories stored{dups}. Proactively "
                "tell the user the import is done and briefly what was imported."
            )
            mark_import_announced(done.import_id)
    except Exception as e:  # never let notification break the turn
        logger.debug("import-completion injection skipped: %s", e)

    if is_first_turn and user_message:
        # Auto-recall relevant memories for the first turn (shared entry point).
        try:
            memories_ctx = recall_for_query(state, user_message, top_k=8)
            if memories_ctx:
                context_parts.append(memories_ctx)
        except Exception as e:
            logger.warning("TotalReclaw pre_llm_call auto-recall failed: %s", e)

    if not context_parts:
        return None

    return {"context": "\n\n".join(context_parts)}


# ---------------------------------------------------------------------------
# Hook-free shared memory entry points (Hermes provider conformance §5.1).
#
# Recall and extract already live in the cross-client agent layer
# (``agent.recall.auto_recall`` / ``agent.lifecycle.auto_extract``). These two
# wrappers expose the Hermes-side *orchestration* (turn bookkeeping, interval
# gating, extraction-LLM resolution) as plain functions that take a
# ``PluginState`` and no Hermes hook kwargs — so the lifecycle hooks below AND
# the Hermes ``MemoryProvider`` (§5.2: ``prefetch`` → ``recall_for_query``,
# ``sync_turn`` → ``ingest_turn``) can share one code path. No behavior change
# vs the previous inline ``pre_llm_call`` / ``post_llm_call`` logic.
# ---------------------------------------------------------------------------


def recall_for_query(
    state: "PluginState", query: str, *, top_k: int = 8
) -> Optional[str]:
    """Recall a context block for *query* (hook-free).

    Thin wrapper over the shared ``agent.recall.auto_recall``; the single
    entry point the Hermes hook (``pre_llm_call``) and the provider
    (``prefetch``) both call.
    """
    return auto_recall(query, state, top_k=top_k)


def _resolve_extraction_llm_config(state: "PluginState"):
    """Resolve the LLM config for auto-extraction.

    Hermes config first, then env-var detection (so non-Hermes-hosted agents
    work). If neither resolves, surface a one-time quota-channel warning so the
    user sees *why* memories stopped appearing (Bug #5). Returns the config or
    ``None`` — ``auto_extract`` treats ``None`` as a safe silent skip.
    """
    llm_config = _get_hermes_llm_config()
    if llm_config is None:
        try:
            from totalreclaw.agent.llm_client import detect_llm_config
            llm_config = detect_llm_config()
        except Exception:
            llm_config = None

    if llm_config is None:
        _warned_attr = "_totalreclaw_llm_missing_warned"
        if not getattr(state, _warned_attr, False):
            setattr(state, _warned_attr, True)
            state.set_quota_warning(
                "TotalReclaw: automatic memory extraction is DISABLED — no "
                "LLM config was resolved. To enable: ensure ~/.hermes/"
                "config.yaml has a model + provider set AND ~/.hermes/.env "
                "contains the matching API key. (Fallback: export "
                "OPENAI_MODEL + OPENAI_API_KEY.) Until this is fixed, "
                "explicit `totalreclaw_remember` and `totalreclaw_recall` "
                "still work."
            )
            logger.warning(
                "TotalReclaw: no LLM config for auto-extraction; surfacing "
                "one-time warning via quota channel"
            )
    return llm_config


def ingest_turn(
    state: "PluginState", user_message: str, assistant_response: str
) -> None:
    """Record a completed turn and run interval-gated auto-extraction (hook-free).

    The shared core of ``post_llm_call``: bookkeeping always runs (the client
    may be configured mid-session); extraction runs only when configured and on
    the Nth turn. Reused by the Hermes ``MemoryProvider.sync_turn`` (§5.2).
    Mirrors the previous inline ``post_llm_call`` behavior exactly.
    """
    # Always track turns and messages (client may be configured mid-session).
    state.increment_turn()
    state.add_message("user", user_message)
    state.add_message("assistant", assistant_response)
    # 2.4.4rc2 (F7) — pending-extract buffer so later manual
    # `totalreclaw_remember` calls can suppress duplicates. Idempotent on "".
    state.track_pending_extract(user_message)

    if not state.is_configured():
        return

    if state.turn_count % state.get_extraction_interval() != 0:
        return

    llm_config = _resolve_extraction_llm_config(state)
    # Fall through with llm_config possibly None — ``_auto_extract`` logs the
    # silent-skip path itself; callers that mock ``extract_facts_llm`` directly
    # still exercise the wiring.
    try:
        _auto_extract(state, mode="turn", llm_config=llm_config)
    except Exception as e:
        logger.warning("TotalReclaw post_llm_call extraction failed: %s", e)


def post_llm_call(state: "PluginState", **kwargs) -> None:
    """Auto-extract facts every N turns (Hermes ``post_llm_call`` hook).

    Hermes-hook-specific wrapper around :func:`ingest_turn`: the only extra is
    the Fix #191 mid-session reconfigure (pick up creds if the user paired
    between ``on_session_start`` and now, without a gateway restart). Reordering
    the reconfigure ahead of the turn bookkeeping is behavior-equivalent — the
    two touch disjoint state (creds vs turn-count/messages).
    """
    # Fix #191 safety net — pick up a mid-session pair before ingest_turn
    # decides whether the user is configured. Idempotent + cheap.
    if _maybe_reconfigure_from_disk(state):
        _eager_account_register(state)

    ingest_turn(
        state,
        kwargs.get("user_message", ""),
        kwargs.get("assistant_response", ""),
    )


def on_session_end(state: "PluginState", **kwargs) -> None:
    """No-op. ``on_session_end`` is dispatched by hermes_cli at the end of
    every ``run_conversation()`` call — i.e. once per user turn, NOT at
    true session end. Session-end flush + debrief + message-buffer clear
    have moved to ``on_session_finalize``.

    Before 2.3.1rc16 this handler ran the flush + debrief and wiped
    ``state._messages`` in its ``finally`` block. Because the hook fires
    per-turn, the clear ran after every turn and ``totalreclaw_debrief``
    always saw <8 messages even in 10+ turn sessions (issue #101, parent
    #85 bug 5).
    """
    return None


def on_session_finalize(state: "PluginState", **kwargs) -> None:
    """Comprehensive flush of unprocessed messages + session debrief.

    Fires at true session boundaries (hermes_cli atexit, gateway session
    finalize). Per-turn auto-extraction runs from ``post_llm_call``; this
    handler catches residual unprocessed messages and runs the session
    debrief while the full conversation buffer is still intact.
    """
    if not state.is_configured():
        # memq-3 — still clear the session id we set in on_session_start
        # for unconfigured sessions so the invariant "session_id is None
        # outside an active session" holds in every code path.
        state.end_session()
        return

    try:
        stored_fact_texts: list[str] = []
        if state.has_unprocessed_messages():
            try:
                stored_fact_texts = _auto_extract(state, mode="full", llm_config=_get_hermes_llm_config())
            except Exception as e:
                logger.warning("TotalReclaw on_session_finalize flush failed: %s", e)

        try:
            _session_debrief(state, stored_fact_texts=stored_fact_texts)
        except Exception as e:
            logger.warning("TotalReclaw on_session_finalize debrief failed: %s", e)
    finally:
        state.clear_messages()
        # memq-3 — clear session id alongside the message buffer so the
        # next on_session_start gets a fresh id.
        state.end_session()


def on_session_reset(state: "PluginState", **kwargs) -> None:
    """User-initiated reset (``/reset``). Clean slate without the expensive
    debrief — a finalize would have fired first if the conversation was
    meant to be persisted.
    """
    state.clear_messages()
    state.reset_turn_counter()


# Backward-compatible alias used by tests
def _extract_and_store(state: "PluginState", mode: str = "turn") -> list[str]:
    """Backward-compatible wrapper for auto_extract."""
    return _auto_extract(state, mode=mode)
