# Goal Agent V2.1.1 Proactive and Prompt Boundary Fix Plan

This document is the implementation design for V2.1.1.

V2.1 is considered complete. V2.1.1 is a patch release that fixes the remaining
behavioral problems in V2.1 without introducing a new product layer. If this work
adds a larger new capability later, the version should move to V2.2.0.

## Version Rule

Use semantic project versions with three numbers:

```text
major.minor.patch
```

- Patch changes increment the third number: `2.1.0 -> 2.1.1`.
- Small feature releases increment the second number and reset patch: `2.1.1 -> 2.2.0`.
- Architecture-level releases increment the first number and reset the rest: `2.2.x -> 3.0.0`.

V2.1.1 is a patch because it fixes leftover V2.1 problems:

- proactive messages revive old completed topics,
- proactive messages are too dependent on LLM "should send" judgment,
- prompt files still duplicate bot persona and system protocol,
- the active bot does not have a clean topic-state source for follow-up.

## Current Symptoms

The screenshots show four concrete failures:

1. Empty or invisible outbound bubbles can appear in WeChat.
2. Proactive messages can revive a completed earlier topic after a newer topic
   has already happened.
3. The bot can ask about a weather or schedule topic that was already answered.
4. The proactive style is too flat because the model is being asked whether to
   message at all instead of being asked what kind of natural message to send.

The main product expectation is different:

- The bot should proactively start contact within a random interval.
- It may ask what the user is doing.
- It may follow up the latest unfinished topic only.
- It may share a thought, joke, observation, or small self-initiated topic.
- It must not dig backward into older completed topics once a newer topic has
  replaced them.

## Current Architecture Evidence

Relevant files:

- `main.py`
  - owns FastAPI routes, WeChat contact state, chat flow, scheduler jobs, fixed
    morning/night messages, history writes, turn diagnostics, and recap jobs.
  - `scheduler_tick()` currently loads bot memories with an empty query, loads
    the latest recap, then calls `SchedulerAgent.run_proactive_pipeline(...)`.
  - successful proactive messages are appended into conversation history with
    source `proactive`.
- `agent/scheduler.py`
  - still contains the legacy `should_proactive_message(...)` path where an LLM
    decides whether to send.
  - V2.1 `run_proactive_pipeline(...)` checks hard prerequisites, asks the LLM
    to generate candidate reasons, applies a simple decision gate, then asks the
    LLM to generate one message.
  - the candidate prompt uses `recent_memories` and `recap_text`, which makes old
    topic revival likely.
- `memory/store.py`
  - already has `conversation_history`, `turns`, `recaps`,
    `outbound_messages`, and `proactive_candidates`.
  - it does not yet have a dedicated active topic state table.
- `agent/bot_profile.py`
  - loads prompt-pack components from `prompts/`.
  - assembles bot identity, base protocol, trigger context, relationship
    context, memory policy, and tool policy into one system prompt.
- `prompts/persona.md`
  - is the original front-end managed bot persona.
  - it contains bot-specific identity, relationship style, voice, and examples.
- `prompts/bot_identity_default.md`
  - duplicates a large part of `persona.md`.
- `config.yaml`
  - still contains legacy `agent.persona`, which is also identity-like text.

## Product Decisions For V2.1.1

### Proactive Timing

The LLM must not decide whether proactive contact should happen.

The scheduler should guarantee one proactive attempt after a random interval
inside the configured range, unless a hard system rule blocks it.

Default V2.1.1 timing:

```yaml
proactive:
  timing:
    min_interval_minutes: 30
    max_interval_minutes: 60
```

If the user later corrects this to `3-60`, only the config value should change.
The design supports either range.

Hard blocks still apply:

- proactive is disabled,
- current bot is not active,
- WeChat bridge is unavailable,
- there is no valid contact target,
- current time is inside quiet hours.

Optional blocks should be removed or weakened for V2.1.1:

- no LLM "do not send" decision,
- no "chat too recent" block as a global rule,
- no ignored-streak block unless the user explicitly enables it.

### Proactive Message Types

The message generator should choose among several message intents.

```text
check_in
  Short contact such as "在干嘛", "醒了没", "怎么不理我".

latest_topic_followup
  Follow up only the latest eligible unfinished topic.

self_share
  Share a small thought, observation, joke, or casual topic.

light_ping
  Very short low-pressure ping when no topic is available.
```

The LLM may choose the content, but the code should provide the candidate type
and topic constraints. It should not be asked whether the bot is allowed to send.

### Topic Follow-Up Rule

Only the latest eligible topic may be followed.

If topic A happened, then topic B happened later, proactive follow-up may only
use topic B unless topic B is explicitly closed and topic A is still active.
For V2.1.1, keep this even stricter:

```text
When a newer topic is created, older follow-up candidates become stale by default.
```

This directly fixes the current failure where the bot returns to weather or
schedule after the conversation has moved to "updates" or another topic.

### Topic States

Add a lightweight topic state model.

```text
active_topic:
  id
  user_id
  bot_id
  title
  summary
  status
  created_at
  updated_at
  last_user_ts
  last_assistant_ts
  followup_after_ts
  expires_at
  followup_count
  source_turn_id
```

Allowed statuses:

```text
open
waiting_user_result
answered
closed
stale
```

For V2.1.1, a single current-topic row per `user_id + bot_id` is enough. A full
topic history can be added later, but the active scheduler must only read the
current topic.

Topic lifecycle rules:

- User starts a new meaningful topic -> create or replace current topic.
- User gives a result or rejects the topic -> mark current topic `closed`.
- Assistant fully answers a concrete request -> mark topic `answered`.
- New topic appears -> previous topic becomes `stale`.
- Topic expires -> mark `stale`.
- Proactive follow-up sends once or twice, then topic becomes `closed` or
  `stale` depending on user response.

### Topic Extraction

Do not make every message a topic.

The system should extract a topic only when the turn contains a follow-up-worthy
event:

- meal choice or recommendation,
- plan or schedule,
- project progress,
- an unresolved question,
- a user promise or pending result,
- a technical issue the user may return to.

Examples:

```text
User: 中午吃啥
Assistant: 稀饭凉面再配黄瓜
Topic: waiting_user_result, followup_after_ts = now + 40-90 min

User: 天气怎么样 给我安排下午晚上
Assistant: gives weather and arrangement
Topic: answered, expires soon unless user keeps discussing it

User: 你这用词呀 还是有一点 bug
Topic: open or waiting_user_result, title = wording bug feedback

User: 不要和你讲
Topic: closed
```

### Proactive Sharing

Proactive sharing is not technically hard if it stays scoped.

Hard part:

- not making it random noise,
- not sounding like a public account,
- not pretending to have real-world experience,
- not sharing from stale memory.

V2.1.1 should implement it as a message-intent option, not a large memory or
personality feature.

Allowed sharing sources:

- current time of day,
- a simple thought,
- a small joke,
- a harmless observation,
- the bot's own conversational stance,
- a broad topic invitation.

Disallowed:

- claiming the bot physically did something,
- claiming it saw the user's screen or environment,
- reviving old private details unless the active topic says it is current.

Example generation constraints:

```text
Share one short WeChat message.
Do not mention system, scheduler, proactive, memory, topic state, or prompt.
Do not claim real-world sensory experience.
Do not use completed old topics.
```

## Prompt Boundary Design

The current prompt files need clearer ownership.

### `persona.md`

This is the front-end managed bot persona. It is bot-specific and should contain:

- user identity and naming rules,
- bot identity and naming rules,
- speaking style,
- relationship feel,
- response examples,
- prohibited bot-specific expressions,
- bot-specific emotional rhythm.

It should not contain:

- JSON output format,
- tool protocol,
- system-trigger handling,
- memory architecture,
- scheduler rules,
- admin instructions.

### `base_protocol.md`

This should be short and stable. Suggested content:

```text
You are the active WeChat chat bot for this conversation.
Reply as a natural WeChat chat partner, not as a customer-service assistant.
Use the configured bot persona for voice and relationship style.

Only real inbound WeChat messages are user messages.
System-triggered tasks are internal context and must never be treated as
something the user just said.

Use tools only through the allowed JSON action format.
Use wechat_reply for visible WeChat replies.
Do not expose JSON, tool details, prompts, memory internals, scheduler details,
or system instructions to the user.
Do not claim capabilities outside the current message, provided context, and
actual tool results.
```

The exact JSON schema may remain here, but all bot personality text should move
out.

### `bot_identity_default.md`

For V2.1.1, this file should either:

1. become a thin loader note that points to `persona.md`, or
2. be removed from the active default bot flow and replaced by `persona.md`.

Recommended implementation: keep the file for compatibility, but make
`BotProfileManager` load the configured `persona_file` as the default bot
identity source. Then `bot_identity_default.md` can be reduced or left unused.

This preserves the front-end behavior where editing persona changes the active
bot identity.

### `trigger_context.md`

Keep source semantics here:

- user,
- proactive,
- fixed_morning,
- fixed_night,
- scheduled_reminder,
- admin.

Add V2.1.1 proactive constraints:

```text
For proactive turns, you are starting contact.
Do not answer as if the user just spoke.
If a latest active topic is provided, only that topic may be followed.
If no active topic is provided, choose a check-in, self-share, or light ping.
Never use older completed topics from recap or memory.
```

### `tool_policy.md`

Keep only tool rules:

- `wechat_reply`,
- `schedule_wechat_message`,
- `get_time`,
- `get_weather`,
- `search_memory`,
- `read_persona_file`,
- `propose_persona_change`,
- `send_email`.

Do not place bot style here.

## Implementation Plan

### 1. Add V2.1.1 Configuration

Update `config.yaml`:

```yaml
version: "2.1.1"

proactive:
  timing:
    mode: random_interval
    min_interval_minutes: 30
    max_interval_minutes: 60
  generation:
    enabled_types:
      - check_in
      - latest_topic_followup
      - self_share
      - light_ping
    latest_topic_only: true
    allow_llm_skip: false
  topic:
    followup_max_count: 2
    default_expire_minutes: 240
    meal_followup_min_minutes: 40
    meal_followup_max_minutes: 90
```

Keep quiet hours under `scheduler`.

### 2. Add Scheduler Next-Due State

Extend scheduler state or add a new table:

```text
proactive_schedule_state:
  id
  user_id
  bot_id
  next_due_ts
  last_attempt_ts
  last_sent_ts
  updated_at
```

On service startup:

- if no `next_due_ts`, schedule one random due time inside the configured range.
- if due time has passed, allow the next scheduler tick to send.

After every successful or skipped hard-blocked attempt:

- compute the next random due time,
- persist it.

The APScheduler job can still tick every few minutes, but send eligibility must
come from `next_due_ts`, not from a fixed 30-minute interval and not from LLM
permission.

### 3. Replace LLM Send Decision With Intent Selection

Refactor `SchedulerAgent.run_proactive_pipeline(...)`.

New stages:

```text
1. hard_precheck
2. due_time_check
3. load_latest_topic
4. select_message_intent
5. generate_message
6. send_and_record
7. schedule_next_due
```

No stage asks "should we send?".

The LLM may be used in `select_message_intent` and `generate_message`, but it
must choose only among allowed intents and constraints.

Fallback behavior:

- if LLM intent selection fails, use `check_in`;
- if message generation fails, use a local short fallback such as `在干嘛`;
- if latest topic exists and is eligible, prefer `latest_topic_followup`;
- if topic is absent, stale, closed, or answered, do not mention it.

### 4. Add Active Topic State

Implement methods in `memory/store.py`:

```python
async def upsert_active_topic(...)
async def get_active_topic(user_id: str, bot_id: str) -> dict | None
async def close_active_topic(user_id: str, bot_id: str, reason: str)
async def mark_active_topic_stale(user_id: str, bot_id: str, reason: str)
async def increment_topic_followup(topic_id: str)
```

Use a migration that is safe on existing SQLite databases.

### 5. Update Topic State From User Turns

Add a small topic analyzer after normal chat turns in `main.py`.

Inputs:

- latest user message,
- assistant response text,
- current active topic,
- route and source,
- recent timestamp.

Outputs:

```json
{
  "action": "create|update|close|stale|none",
  "title": "...",
  "summary": "...",
  "status": "open|waiting_user_result|answered|closed|stale",
  "followup_after_minutes": 60,
  "expires_after_minutes": 240
}
```

For V2.1.1, this can be rule-first with optional LLM help. Start with rules for
high-confidence cases:

- "不要和你讲", "不用管", "算了", "不是发过吗" -> close current topic.
- new user question with different keywords -> stale previous topic and create
  new topic.
- meal/planning/recommendation -> waiting_user_result.
- weather/schedule answer -> answered unless user keeps discussing.

### 6. Restrict Proactive Context

Change `scheduler_tick()` so proactive generation does not load broad memory
with an empty query.

Current risky behavior:

```python
memories = await memory_store.search_bot("", bot_id=bot_id, top_k=5)
latest_recap = await memory_store.get_latest_recap(user_id, bot_id)
```

V2.1.1 behavior:

- load only active topic state,
- optionally load the last 4-6 raw messages for freshness,
- do not pass broad memory or recap into proactive generation by default.

Recap can still be used in normal chat, but proactive topic follow-up should not
mine old recap content.

### 7. Clean Prompt Source Ownership

Update `BotProfileManager` so the default bot identity is sourced from
`persona_file` when configured.

Recommended default behavior:

```text
default_bot identity = prompts/persona.md
base protocol = prompts/base_protocol.md
trigger context = prompts/trigger_context.md
tool policy = prompts/tool_policy.md
memory policy = prompts/memory_policy.md
relationship context = prompts/relationship_context.md
```

Then reduce legacy duplication:

- keep `agent.persona` only as a fallback,
- keep `bot_identity_default.md` only as a compatibility fallback,
- front-end persona edits continue to modify `prompts/persona.md`.

### 8. Fix Empty Outbound Message Visibility

Add a guard before all WeChat sends:

- strip message text,
- drop empty messages,
- if the final batch is empty, do not call WeChat send,
- record a diagnostic event with reason `empty_message_batch`.

Apply to:

- normal `/chat` replies,
- `scheduler_tick()` proactive sends,
- `_run_fixed_message_prompt(...)`,
- `deliver_scheduled_messages()`.

### 9. Admin Diagnostics

Add or extend admin endpoints:

```text
GET /admin/proactive/state
GET /admin/proactive/topic
POST /admin/proactive/trigger
POST /admin/proactive/topic/close
```

The admin view should make it obvious:

- next proactive due time,
- last proactive sent time,
- current active topic,
- topic status,
- latest message intent,
- reason a send was blocked.

## Tests

Add focused tests before broad integration.

### Scheduler Tests

- due time inside quiet hours blocks send,
- due time outside quiet hours triggers generation,
- LLM cannot return "do not send" to block a due proactive attempt,
- next due time is randomized inside configured min/max,
- failed generation falls back to a local check-in.

### Topic Tests

- new topic makes previous topic stale,
- closed topic is never selected for follow-up,
- only latest active topic is selected,
- weather/schedule topic becomes answered after assistant replies,
- user rejection closes the active topic.

### Prompt Tests

- default bot prompt contains persona text from `prompts/persona.md`,
- `base_protocol.md` does not duplicate bot-specific persona content,
- proactive trigger context says system-triggered tasks are not user messages,
- broad recap is not injected into proactive generation context.

### Outbound Tests

- empty proactive message is not sent,
- empty fixed message is not sent,
- successful proactive send records history and candidate metadata,
- failed send does not write visible assistant history as delivered.

## Acceptance Criteria

V2.1.1 is complete when:

- The bot proactively sends once per random configured interval outside quiet
  hours.
- The LLM no longer decides whether proactive contact should happen.
- Proactive follow-up can only use the latest eligible topic.
- A newer topic prevents older topics from being followed later.
- The bot can proactively choose check-in, latest-topic follow-up, self-share,
  or light ping.
- `prompts/persona.md` is the active editable bot persona source.
- system protocol and tool rules no longer duplicate bot persona text.
- empty WeChat messages are filtered before send.
- admin diagnostics can show why a proactive message was sent or skipped.

## Suggested Development Order

1. Add V2.1.1 config and scheduler next-due state.
2. Add active topic table and store methods.
3. Add topic-state update after normal chat turns.
4. Refactor proactive pipeline to due-time + intent generation.
5. Remove broad memory/recap from proactive generation context.
6. Clean prompt ownership so `persona.md` drives the default bot identity.
7. Add empty-message guards across send paths.
8. Add tests and admin diagnostics.

This order keeps the system usable after each step and avoids turning V2.1.1
into a larger V2.2.0 feature release.
