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

Three plugin-side mechanisms (no host changes required):

1. **Per-conversation session slots (the core fix).** Hermes hands the plugin a
   per-conversation `session_id` on **every turn** — through its native
   `MemoryProvider.sync_turn` and the per-turn hooks (its own
   `self.agent.session_id`, which is distinct per conversation). The plugin now
   routes each turn to a **slot** keyed by that id: every conversation keeps its
   **own** message buffer, turn counter, and session id, and finalizes to its
   **own** Crystal. Even conversations **interleaved in real time** in the same
   chat no longer mix — each turn lands in the right slot. (Previously the
   plugin ignored that id and piled every turn into one buffer.)

2. **Honor the host's `session_id` at session start.** `on_session_start` also
   derives its session key from the id Hermes provides, so a host that
   distinguishes conversations up front inherits that boundary too.

3. **Idle-timeout rollover (fallback).** When TotalReclaw has to mint its own id
   (a host that supplies no per-conversation id at all), a turn that arrives
   after a long silence **rolls into a fresh session** first: the idle session
   is flushed + debriefed, then a new `session_id` starts for the incoming turn.
   A long gap almost always means a new topic.

   - Env: **`TOTALRECLAW_SESSION_IDLE_MINUTES`** (default **60**). Set `0` to
     disable. Host-derived / per-conversation sessions are never idle-rolled —
     the host owns those boundaries.

## Residual (rare)

The per-conversation split relies on the host giving each conversation a
distinct id per turn — Hermes' native gateway does (it passes its
per-conversation `self.agent.session_id`). A host that funnels **every**
conversation through a single **coarse** id (one id reused across unrelated
conversations in a chat) *and* interleaves them in real time can't be told apart
from that id alone; those fall back to the idle rollover (time-separated
conversations still get clean sessions). This is platform-agnostic — nothing
here is specific to any one messenger.

## Tips

- Chatting about a genuinely new topic after a break? The idle rollover already
  gives you a clean new session at the default 60 min. Lower
  `TOTALRECLAW_SESSION_IDLE_MINUTES` if your conversations turn over faster.
