# Goal Agent V2.1 Bot, Memory, and Proactive Design

This document is a working design note for the owner and future AI coding
agents. It extends the V2 stability work without trying to rebuild the whole
system in one step.

V2.1 is about making the system boundaries explicit before adding more bots,
more memory behavior, or richer proactive messages.

## Goal

V2.1 should make the current single-WeChat-entry agent ready for:

1. switching between multiple bots from the backend,
2. separating bot prompts from shared runtime protocol rules,
3. separating raw messages, turns, recaps, and long-term memory,
4. preventing system-triggered messages from being mistaken for user messages,
5. making proactive messages reason-based instead of random.

The current deployment is still single-user and single-WeChat-entry. Only the
currently deployed / active bot should reply or proactively send messages.

## Non-Goals

Do not implement these in V2.1:

- multiple WeChat entries,
- multiple active bots talking at the same time,
- inactive bots sending proactive messages,
- vector databases, mem0 SDK, Qdrant, Chroma, or FAISS,
- a complex multi-process outbox,
- random proactive chatting,
- leaking one bot's private recap or relationship state to another bot.

## Core Principles

- Only real user input may be represented as a user message.
- System-triggered tasks must be represented as trigger context, not as a fake
  user message.
- A single WeChat entry routes to exactly one current bot at a time.
- Each bot has independent persona, relationship state, recap, and bot-specific
  memory.
- Basic user facts may be shared across bots.
- Recap is session-stage context, not long-term memory.
- Proactive messages must start from a concrete candidate reason, pass a
  decision gate, and only then generate text.
- A sent assistant message may be written into visible chat history only after
  send success.

## Bot Model

V2.1 keeps one WeChat entry and introduces `current_bot_id`.

```text
WeChat message
-> resolve current_bot_id
-> load bot profile
-> load bot prompt pack
-> load bot-specific memory layers
-> run chat
-> send reply through the same WeChat entry
```

Each bot should own:

```text
bot_profile:
  bot_id
  display_name
  persona_file
  prompt_pack
  enabled_tools
  proactive_enabled

bot_state:
  relationship_state
  current_recap
  recent_turns
  long_term_memory_scope
```

Shared user facts may live outside bot-specific state:

```text
shared_user_profile:
  user identity
  stable preferences
  major active projects
  basic schedule or rhythm
  global boundaries explicitly set by the user
```

Do not share these across bots by default:

```text
bot-specific recent chat context
bot-specific recap
bot-specific relationship atmosphere
bot-specific long-term interaction history
```

If the user switches from bot A to bot B, bot B should not automatically know
what the user just discussed with bot A. Bot B may know shared user facts, but
not bot A's active conversation state.

## Prompt Pack

Do not keep growing a single persona prompt. Split prompt construction into
small, explicit components:

```text
base_protocol.md
  Stable output format, tool-call rules, JSON leak prevention, safety rules.

bot_identity.md
  The current bot's identity, role, voice, and style.

relationship_context.md
  The current bot's relationship atmosphere, boundaries, and interaction rhythm.

memory_policy.md
  When to search memory, which memory layers are available, and when to say
  "I am not sure" instead of pretending to remember.

tool_policy.md
  Tools enabled for this bot and tool-specific constraints.

trigger_context.md
  The current turn source: user message, fixed morning message, scheduled
  reminder, proactive candidate, or admin action.
```

The most important prompt rule:

```text
Only real user input may be placed in the user role.
System-triggered work must be represented as system/developer trigger context.
Never wrap a proactive task as if the user had just said it.
```

This directly fixes the old failure mode where the system sent an invisible
message to the model and the model replied as if the user had spoken.

## Memory Layers

V2.1 should use four memory layers.

```text
L1 raw_messages
  Every original inbound and outbound message.
  High fidelity. Useful for audit, recovery, and rebuilding turns.

L2 turns
  One semantic conversation turn.
  Usually one buffered user input batch plus one bot reply batch, including
  tool calls, send state, source, and timing metadata.

L3 recap
  Session-stage summary.
  Useful for resuming the immediate conversation, restoring tone after restart,
  reducing long-history loading, and preserving unfinished topics.

L4 long_term_memory
  Stable facts, preferences, project state, relationship rules, and durable user
  constraints.
```

Recap example:

```text
We were discussing architecture state diagrams. The user noticed the system had
been simplified into a zero-layer view, and feedback was given. The conversation
ended around the 520 topic with the mood landing gently.
```

That example is not long-term memory. It is session-stage context.

Long-term memory should contain stable statements instead:

```text
The user wants system-triggered messages to be clearly distinguished from real
user messages.

The user prefers each bot to have independent persona, recap, relationship
state, and memory, while allowing shared basic user facts.
```

## Recap Strategy

Use option B: summarize every N turns, not during every reply.

The chat model should focus on replying. The recap process should run as a
separate background step after enough turns accumulate.

Recommended triggers:

```text
every N turns
or raw_messages exceeds a configured threshold
or service restart finds unsummarized turns
```

A recap should include:

```text
where the conversation just was
unfinished topics
current tone or atmosphere
user concerns or preferences expressed in this stage
the most natural next continuation point
```

A recap should not include:

```text
permanent user facts
strong relationship conclusions without evidence
speculative psychological labels
details that belong only in raw history
```

Long-term memory may later be extracted from confirmed turns or recaps, but it
must remain a separate decision. Do not dump every recap into long-term memory.

## Proactive Messages

Proactive messaging is hard because the hard part is not sending text. The hard
part is deciding whether to send at all.

Use three stages:

```text
1. Candidate reason generation
2. Decision gate
3. Message generation
```

Candidate reasons may include:

```text
today is a special date
last night's topic was unfinished
the user is actively pushing a project forward
the user often feels tired in the morning
the user has been silent for a long time
the user explicitly left a reminder or agreement
```

The decision gate should check:

```text
current bot is deployed and active
proactive messaging is enabled for this bot
WeChat bridge is available
there is a valid contact target
daily proactive limit
gap since last chat
whether the user ignored the previous proactive message
sleep/work quiet hours
whether the reason is concrete enough
whether the message would feel like noise or pressure
```

Only after the decision gate passes should the system generate the final
message text.

For V2.1, proactive messages are only for the currently deployed / active bot.
If no bot is deployed or active, do not send.

## Outbound Message Semantics

Unifying outbound lifecycle does not mean mixing all messages into one semantic
type. It means all outbound paths should share state tracking while preserving
where the message came from.

Every outbound message or batch should carry:

```text
source:
  user_reply
  fixed_morning
  scheduled_reminder
  proactive
  admin

author:
  assistant
  system

trigger:
  user_message
  fixed_time
  user_scheduled
  proactive_reason
  manual_admin

visible_to_user:
  true | false

send_status:
  created
  guarded
  sending
  delivered
  failed
```

Rules:

- A user reply is generated because of real user input.
- A fixed morning message is generated because of a system fixed-time trigger.
- A scheduled reminder is generated because the user previously requested it.
- A proactive message is generated because a candidate reason passed the
  decision gate.
- An admin message is manually triggered and should stay identifiable.
- Failed sends must not be written as if the user saw them.
- Internal trigger context may be recorded for diagnostics, but must not become
  a user-role message.

## Suggested V2.1 Implementation Order

1. Add bot profile and prompt-pack loading boundaries.
2. Migrate the current behavior into a `default_bot` without changing runtime
   behavior.
3. Add `current_bot_id` as a backend setting.
4. Scope bot persona, recap, relationship state, and bot-specific memory by
   `bot_id`.
5. Keep shared user facts separate from bot-specific state.
6. Add turn records between raw messages and recap.
7. Add recap generation every N turns.
8. Change proactive logic into candidate reason -> decision gate -> generation.
9. Keep fixed morning behavior working while migrating it to the same trigger
   semantics.

## Guardrails For Future Agents

- Do not represent system-triggered tasks as user messages.
- Do not let bot B read bot A's recap or relationship atmosphere.
- Do not write recap directly into long-term memory.
- Do not add a complex multi-bot UI before the data boundaries are stable.
- Do not enable inactive bots to proactively send messages.
- Do not make proactive messages random.
- Keep the current single-user, single-WeChat-entry deployment working while
  adding these boundaries.
