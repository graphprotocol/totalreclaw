# Hermes session hygiene (per-conversation grouping)

TotalReclaw ties a session's **Crystal** (summary) and its atomic memories
together with a `session_id`. The web vault groups memories by that id. So the
quality of the grouping depends entirely on when Hermes starts a new session.

## The problem this fixes

Hermes runs as **one process**. Before this change, the TotalReclaw plugin
minted **one `session_id` per process lifecycle** and ignored the `session_id`
Hermes passes to `on_session_start`. If you talk to your agent through **several
parallel conversations at once** — e.g. multiple Telegram chats / group chats /
forum topics on the same bot — their turns all landed under the **same
`session_id`**, so unrelated memories interleaved into **one Crystal**.

## What changed

Two plugin-side mitigations (no host changes required):

1. **Honor the host's `session_id`.** `on_session_start` now derives the
   TotalReclaw session key deterministically from the `session_id` Hermes
   provides. If your host distinguishes conversations (passes a distinct id per
   chat/topic), TotalReclaw **inherits that boundary** automatically — parallel
   conversations get separate sessions and separate Crystals.

2. **Idle-timeout rollover.** When TotalReclaw mints its own id (the host does
   *not* distinguish conversations), a turn that arrives after a long silence
   **rolls into a fresh session** first: the idle session is flushed +
   debriefed, then a new `session_id` starts for the incoming turn. A long gap
   almost always means a new topic, so bursty parallel conversations separated
   in time stop merging.

   - Env: **`TOTALRECLAW_SESSION_IDLE_MINUTES`** (default **60**). Set `0` to
     disable. Host-derived sessions (case 1) are never idle-rolled — the host
     owns that boundary.

## What is *not* fixed (host-side)

If two conversations are **truly interleaved in real time** and Hermes does
**not** pass a per-conversation id on every turn, the plugin cannot tell the
turns apart — Hermes' `pre_llm_call` / `post_llm_call` hooks currently carry only
the message text, no `chat_id` / `message_thread_id`. The complete fix needs the
**host** to pass a conversation id (e.g. Telegram `chat_id` + `message_thread_id`)
into the per-turn hooks; the plugin already knows how to scope by it (mechanism
1). Until then, use idle rollover, or the explicit "new session" fast-follow.

## Tips

- Chatting about a genuinely new topic after a break? The idle rollover already
  gives you a clean new session at the default 60 min. Lower
  `TOTALRECLAW_SESSION_IDLE_MINUTES` if your conversations turn over faster.
