# Hermes session hygiene (per-conversation grouping)

TotalReclaw ties a session's **Crystal** (summary) and its atomic memories
together with a `session_id`. The web vault groups memories by that id. So the
quality of the grouping depends entirely on when Hermes starts a new session.

## The problem this fixes

Hermes runs as **one process**. Before this change, the TotalReclaw plugin
minted **one `session_id` per process lifecycle** and ignored the `session_id`
Hermes passes to `on_session_start`. If you talk to your agent through **several
parallel conversations at once** — e.g. several chats, group chats, or forum
topics on the same messaging platform (Telegram, WhatsApp, Slack, Matrix, …) —
their turns all landed under the **same `session_id`**, so unrelated memories
interleaved into **one Crystal**. (Nothing here is platform-specific; the fix
works for any messenger you configure Hermes with.)

## What changed

Four plugin-side mechanisms (no host changes required):

1. **Per-conversation session slots (the core fix).** Hermes hands the plugin a
   per-conversation `session_id` on **every turn** — through its native
   `MemoryProvider.sync_turn` and the per-turn hooks (its own
   `self.agent.session_id`, which is distinct per conversation / Telegram topic).
   The plugin routes each turn to a **slot** keyed by that id: every conversation
   keeps its **own** message buffer, turn counter, and session id, and finalizes
   to its **own** Crystal. Even conversations **interleaved in real time** in the
   same chat no longer mix — each turn lands in the right slot. (Previously the
   plugin ignored that id and piled every turn into one buffer.)

2. **Per-conversation idle Crystal sweep (so topics actually mint Crystals).** A
   Crystal is born at `on_session_finalize` — but in Hermes **gateway** mode that
   hook can fire rarely or never: if the host's `session_reset` is `none` (a
   common config), Hermes' own idle-finalize watcher is disarmed, so a topic you
   simply stop replying to would never crystallize until a restart. The plugin
   now closes this itself: on each turn it **crystallizes + retires any *other*
   slot that has gone quiet** past `TOTALRECLAW_SESSION_IDLE_MINUTES`. A busy chat
   B drains the pending Crystal for quiet chat A on one of B's turns — so each
   topic mints its own Crystal minutes after it goes silent, independent of the
   host's `session_reset` config, and without ever touching the live conversation.

3. **Honor the host's `session_id` at session start.** `on_session_start` also
   derives its session key from the id Hermes provides, so a host that
   distinguishes conversations up front inherits that boundary too.

4. **Idle-timeout rollover (fallback for single-session hosts).** When TotalReclaw
   has to mint its own id (a host that supplies no per-conversation id at all), a
   turn after a long silence **rolls into a fresh session** first: the idle
   session is flushed + debriefed, then a new `session_id` starts.

   - Env: **`TOTALRECLAW_SESSION_IDLE_MINUTES`** (default **60**; `0` disables)
     controls both the idle Crystal sweep (mechanism 2) and this rollover. Lower
     it if your topics turn over faster than an hour.

## Residual (rare)

The per-conversation split relies on the host giving each conversation a
distinct id per turn — Hermes' native gateway does (it passes its
per-conversation `self.agent.session_id`). A host that funnels **every**
conversation through a single **coarse** id (one id reused across unrelated
conversations in a chat) *and* interleaves them in real time can't be told apart
from that id alone; those fall back to the idle rollover (time-separated
conversations still get clean sessions). This is platform-agnostic — nothing
here is specific to any one messenger.

## Known limitations (architectural)

Two consequences of the current design worth being aware of:

1. **Per-conversation slots are in-memory.** The plugin's per-conversation state
   lives in the gateway process (`AgentState._session_slots`), not on disk. On a
   gateway restart (updates, systemd, a crash) the slots are cleared. Hermes
   *persists the conversation* (so resuming a topic keeps the same `session_id`),
   but a **short conversation that hasn't hit the per-turn extraction interval
   (default 3 turns) and is idle across a restart loses its buffered turn** — no
   facts, no Crystal for it. For a longer conversation this barely matters:
   per-turn extraction has already written most facts on-chain; only the tail +
   the Crystal are at risk. (A future hardening would persist slots to disk.)

2. **The idle Crystal sweep is turn-driven.** It piggybacks on *some* turn to
   fire, so a topic only crystallizes when the bot processes a subsequent turn
   (another chat's message, a resume, or a cron tick). If the whole bot goes
   fully silent, idle topics wait until the next message anywhere. In practice
   crons + resumed conversations keep this ticking; a `threading.Timer`-based
   sweeper is the follow-up if lull-latency ever matters.

## Tips

- Chatting about a genuinely new topic after a break? The idle rollover already
  gives you a clean new session at the default 60 min. Lower
  `TOTALRECLAW_SESSION_IDLE_MINUTES` if your conversations turn over faster.
