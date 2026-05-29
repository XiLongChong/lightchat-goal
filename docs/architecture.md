# Goal Chat Agent — Architecture Reference (AI-facing)

Generated: 2026-05-29 | Branch: v3

## Purpose
Single-process Python/FastAPI service. DeepSeek-driven configurable bot chats with users via WeChat iLink. All data in one SQLite file. Zero cloud dependencies.

## Project Tree
```
goal/
├── main.py                  # FastAPI app: all routes, startup, config, auth, history
├── config.yaml              # All config with ${ENV_VAR} substitution
├── agent/
│   ├── chat_agent.py        # Core chat loop: prompt→LLM→parse actions→loop until done
│   ├── context_builder.py   # Assemble system prompt (legacy) + V2.1 glue→BotProfileManager
│   ├── bot_profile.py       # BotProfile + BotProfileManager + prompt pack assembly
│   ├── tools.py             # Tool executors (weather/time/email/memory/persona)
│   ├── tool_registry.py     # ActionRegistry + ToolRegistry + native-tool-eligible flags
│   ├── llm_gateway.py       # Provider-neutral gateway (owns adapter + retry + trace)
│   ├── llm_types.py         # LLMRequest / LLMResponse / LLMUsage / LLMError dataclasses
│   ├── output_guard.py      # Protocol leak detection, nested JSON unwrap, profanity filter
│   ├── memory_worker.py     # Fire-and-forget async memory extraction (LLM-driven)
│   └── providers/
│       ├── base.py          # ProviderConfig + ProviderAdapter abstract
│       └── deepseek.py      # DeepSeek-specific adapter (json_object + tools + thinking)
├── memory/
│   ├── store.py             # MemoryStore: SQLite WAL, 4-layer memory, bot-scoped
│   └── dedup.py             # CJK bigram Jaccard similarity + importance scoring
├── bridge/
│   ├── ilink_client.py      # WeChat iLink API client
│   └── wechat_poller.py     # Polling loop + message buffering + debounce
├── utils/
│   └── json_parser.py       # JSON extract/parse/validate with multi-layer fallback
├── prompts/                 # 8 .md prompt files (see Prompt Pack section)
├── static/                  # Admin UI (HTML/CSS/JS, no framework)
└── tests/                   # 15 test files (pytest)
```

## Data Flow: Inbound Message

```
WeChat iLink poll → wechat_poller → POST /chat {"messages":[...], "user_id":"wxid_xxx"}
  │
  ├── Auth gate: Bearer token or X-API-Key (secrets.compare_digest)
  ├── Validate: user_id format, message length, total chars
  ├── Per-user asyncio.Lock (turn ordering, max 512 held)
  │
  ├── Resolve current_bot_id → BotProfile + prompt pack
  ├── Route selection: SystemFast | ChatLite | ChatFull
  ├── Context policy: hot_only | hot+cheap_memory | hot+full_memory
  ├── Load: memories (bot-scoped CJK search), relationship, history, recap
  │
  └── ChatAgent.chat()
        │
        ├── Build system prompt (BotProfileManager from prompt pack)
        ├── Assemble messages: [system, history_summary?, history(-16), current_turn]
        ├── Loop (max 5 iterations):
        │     ├── LLMGateway.complete() → DeepSeek API
        │     │     ├── response_mode: "json_object" (strict) or "text" (legacy)
        │     │     ├── thinking: "disabled" always
        │     │     └── tools: [wechat_reply, get_time, get_weather, search_memory] if native enabled
        │     ├── Parse response:
        │     │     ├── If native tool_calls present: execute tools, continue loop
        │     │     └── Else: parse JSON actions (strict or legacy parser)
        │     ├── Schema validation → filter invalid actions
        │     ├── Process actions:
        │     │     ├── type="message" → guard_single_message → all_messages
        │     │     ├── type="tool" name="wechat_reply" → guard → all_messages
        │     │     ├── type="tool" name=<other> → execute_tool() → tool_results
        │     │     └── type="skip"/"no_reply" → skip_reply=True
        │     └── if done=true or no tool_results: break
        │
        └── Return: {messages, tool_calls, skip_reply, token_usage, diagnostics, ...}
  │
  ├── Send via bridge (under lock) or return to poller
  ├── Write: turn (L2), outbound_messages, turn_diagnostics, turn_draft
  ├── Fire-and-forget: memory extraction (memory_worker)
  └── Background: recap check (every N turns)
```

## Current Features

### Core Chat
| Feature | Status | Route |
|---------|--------|-------|
| User message → LLM → reply | Active | `/chat` POST |
| JSON output mode (json_object) | Active | Default in ChatAgent |
| Native tool calls (DeepSeek function calling) | Active | `native_tool_calls: true` in config |
| Legacy text-mode JSON parsing | Active | `json_output_enabled: false` fallback |
| Multi-turn tool loop (done:false) | Active | Max 5 iterations |
| finish_reason=length retry | Active | 1 retry in strict json mode |
| Empty content / LLM error fallback | Active | "信号不太好，稍等我一下" |

### Tools (ToolRegistry)
| Tool | Type | Native Eligible | Side Effect | Notes |
|------|------|----------------|-------------|-------|
| `wechat_reply` | Action (internal) | N/A (inline) | internal_message | Intercepted as message, never reaches executor |
| `skip` | Action (internal) | N/A | none | Model-requested silence |
| `get_time` | Tool (external) | **Yes** | none | pytz timezone lookup, 12 whitelisted zones |
| `get_weather` | Tool (external) | **Yes** | none | wttr.in, in-memory cache (TTL 600s) |
| `search_memory` | Tool (external) | **Yes** | none | CJK bigram Jaccard, bot-scoped, top-k=5 |
| `read_persona_file` | Tool (external) | No | low | Reads persona.md, path-bound to project root |
| `propose_persona_change` | Tool (external) | No | low | Drafts change, requires user confirmation |
| `send_email` | Tool (external) | No | **high** | SMTP, hard-denied from native tool calls |

Backward-compat TOOL_WHITELIST = union of ActionRegistry + ToolRegistry names (8 names total).

### Native Tool Call Hard Deny List
`_NATIVE_TOOL_HARD_DENY = {"send_email"}` — regardless of config, these are never exposed as function-calling tools.

### Memory System
4-layer architecture, all SQLite WAL:
- **L1** `conversation_history` — raw messages (recovery after restart)
- **L2** `turns` — semantic turns (user_messages + assistant_actions + tool_calls)
- **L3** `recaps` — LLM-generated session-stage summaries (every N turns)
- **L4** `memories` — stable long-term facts, preferences, bot-scoped
- **Shared**: `shared_user_facts` — cross-bot user facts
- **Dedup**: CJK bigram Jaccard similarity, threshold 0.75
- **Decay**: Time-based importance decay (week_factor 0.95, month_factor 0.90)

### Bot Profiles
- `BotProfileManager` manages multiple bots from `config.yaml` → `bots:` section
- Each bot: independent identity file, enabled_tools, prompt pack, relationship state, recap
- `current_bot_id` routes single WeChat entry to active bot
- Memory/history/relationship scoped by `bot_id`

### Prompt Pack
6 components loaded from `prompts/*.md`:
1. `base_protocol.md` — Output format, JSON contract, grounding rules, removed capabilities
2. `<bot identity file>` (default: `persona.md`) — Bot identity, voice, style
3. `relationship_context.md` — Relationship atmosphere, boundaries (template with {vars})
4. `memory_policy.md` — When to search memory, uncertainty rules
5. `tool_policy.md` — Available tools, usage rules, disabled features
6. `trigger_context.md` — Turn source templates (user / admin)

### LLM Gateway
- `LLMGateway` wraps `ProviderAdapter` (DeepSeekAdater)
- Automatic retry (configurable, default 1)
- Trace callback system (token counts, latency, tool observability → `llm_traces` table)
- `LLMRequest` supports: tools, tool_choice, response_mode, thinking, reasoning_effort
- `LLMResponse` includes: content, tool_calls, usage, finish_reason, latency_ms
- **Never persisted**: raw_response, reasoning_content, provider error strings

### Route & Context Selection (V2 Conservative Routing)
| Route | Trigger | Context Policy | Cost |
|-------|---------|----------------|------|
| `SystemFastPath` | Empty input or system-level | `hot_only` | Minimal |
| `ChatLite` | ≤30 chars, no tool signal | `hot+cheap_memory` if memories exist | Low |
| `ChatFull` | Everything else | `hot+full_memory` | Full |

### WeChat Bridge
- iLink API (emoRebot protocol): QR login, long-polling, text send
- Buffer/debounce: groups rapid messages within N seconds
- `context_token` tracked per contact for reply routing
- Outbound message lifecycle: created → generated → sending → delivered/failed

### Admin UI (`/admin`)
- Auth: password-based, cookie session (7 day)
- Dashboard: events timeline, usage stats, bridge status
- Pages: Persona editor, runtime settings, memory import/export, recaps, turns, outbound tracking, logs, diagnostics, drafts
- Mobile-first WeChat-style design

### Output Guard Pipeline
1. Protocol JSON leak detection → unwrap or block
2. Nested protocol unwrap (model double-wraps JSON)
3. Empty reply → hard fallback text
4. Length clamp (500 chars/message)
5. Sensitive info filter (API keys, tokens)
6. WeChat format cleanup (strip markdown, normalize whitespace)
7. Profanity guard (forbidden expressions always blocked)

### Recap Generation
- Background LLM call every N turns (default 8)
- Generates Chinese session-stage summary: what happened, unfinished topics, mood, next direction
- Excludes: permanent facts, speculative psychology, raw history

## Database Schema (Key Tables)
| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `memories` | L4 long-term memory | id, content, category, importance, bot_id |
| `turns` | L2 semantic turns | id, user_id, bot_id, turn_number, user_messages, assistant_actions |
| `recaps` | L3 session summaries | id, user_id, bot_id, recap_text, start_turn_id, end_turn_id |
| `conversation_history` | L1 raw messages | id, user_id, bot_id, role, content, source, ts |
| `history_summaries` | Compacted old history | user_id, bot_id, summary, updated_at |
| `relationship_state` | Bot-scoped relationship | bot_id, familiarity, current_atmosphere, chat_boundary |
| `turn_diagnostics` | Per-turn observability | turn_id, route, context_policy, llm_status, token_count, fallback_type |
| `turn_drafts` | Pre-send delivery state | id, user_id, status, messages_json |
| `outbound_messages` | Message delivery tracking | id, turn_id, user_id, source, author, send_status |
| `llm_traces` | LLM call observability | provider, model, latency_ms, tokens, cache_hits, error_type |
| `bot_contact_state` | WeChat contact binding | user_id, context_token, bot_id |
| `shared_user_facts` | Cross-bot user facts | id, content, category, confidence |
| `usage_events` | Local telemetry | event_type, message_count, token_count, elapsed_ms |

### Legacy Tables (schema exists, feature removed)
`scheduler_state`, `scheduled_messages`, `proactive_candidates`, `proactive_schedule_state`, `clingy_state`, `active_topics`, `bot_emotion_state`, `bot_emotion_events`, `dialogue_state`, `dialogue_state_events` — kept for backward DB compat; not used by any active code path.

## Safety Safeguards (10 items, built-in)
1. JSON parse multi-layer fallback → plain text (never crash)
2. Hard action count / message length / total char limits
3. done=false max 5 loop iterations
4. Tool whitelist + bot-level enabled_tools + hard deny list
5. Low importance memory rejection
6. Auth gate before any state mutation
7. Fire-and-forget memory extraction (non-blocking)
8. SQLite WAL + per-user asyncio.Lock
9. History compaction on overflow
10. Test suite covers core paths

## Deleted/Removed Features
| What | When | Residue |
|------|------|---------|
| Proactive messaging scheduler | `agent/scheduler.py` deleted | DB tables kept for migration compat |
| Emotion module | `agent/emotion/` deleted | `bot_emotion_state` table kept (unused) |
| Dialogue state machine | `agent/dialogue_state/` deleted | `dialogue_state` table kept (unused) |
| `schedule_wechat_message` tool | Never in registry | Rejected as "未知操作" if model tries |
| Scheduler prompt files | All deleted from `prompts/` | Verified absent by test_prompt_externalization |
| Clingy followup state machine | Code deleted | `clingy_state` table kept (unused) |

## Config Toggles (config.yaml)
- `model.json_output.chat_agent`: true → strict json_object mode (default: true)
- `model.native_tool_calls.chat_agent`: true → expose tools as native function calls (default: true)
- `model.native_tool_calls.allowed_tools`: [wechat_reply, get_time, get_weather, search_memory]
- `model.thinking.default`: "disabled" (always disabled for chat turns)
- `recap.enabled`: true → background recap generation
- `api.chat_auth_enabled`: true → require Bearer/X-API-Key on /chat
- Env override: `GOAL_NATIVE_TOOL_CALLS=true` overrides config without file edit

## Key Design Decisions
- **JSON actions over OpenAI function calling** (legacy path, still active)
- **Native tool calls** now primary for eligible tools (DeepSeek supports function calling)
- **CJK bigram Jaccard** for memory retrieval (no embedding model needed)
- **SQLite WAL** for concurrent reads during writes
- **Bot-scoped everything** (memories, history, relationship, recap) keyed by bot_id
- **Prompt pack** splits system prompt into 6 independent .md files, hot-reloadable
- **No thinking mode** for chat turns (thinking=disabled, no reasoning_effort)
- **wechat_reply is an internal action** not a real tool — intercepted inline, never reaches executor
