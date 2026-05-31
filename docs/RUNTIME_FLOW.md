# Runtime Flow — 运行入口和主链路分析

> 最后更新: 2026-05-31
> 基于实际代码追踪 (v3.1 proactive pipeline)

---

## 1. 程序启动入口

**文件**: `main.py` (顶层模块级代码)

启动链路:

```mermaid
flowchart TD
    A["main.py 模块加载"] --> B["load_dotenv()"]
    B --> C["load_config('config.yaml')<br/>v3.1: +proactive section"]
    C --> D["load_runtime_settings()"]
    D --> E["_deep_update(cfg, runtime_settings)"]
    E --> F["logging.basicConfig(stdout + data/agent.log)"]
    F --> G["configure_tools(cfg)"]
    G --> H["LLMGateway.from_model_config(cfg)"]
    H --> I["MemoryStore(db_path=...)"]
    I --> J["configure_runtime(memory_store=...)"]
    J --> K["ChatAgent(...)"]
    K --> L["ProactiveScheduler(chat_agent, config, store, bridge_check)<br/>v3.1"]
    L --> M["MemoryWorker/MemoryHook (条件)"]
    M --> N["WeChatBridge (条件)"]
    N --> O["FastAPI(lifespan=lifespan)"]
```

**lifespan 启动**:

```mermaid
flowchart TD
    A["lifespan startup"] --> B["_startup_validate()<br/>API key / persona / data dir"]
    B --> C{"bridge configured?"}
    C -->|yes| D["await _bridge.start()<br/>启动微信轮询"]
    C -->|no| E{"features.proactive?"}
    D --> E
    E -->|yes| F["asyncio.create_task(_proactive_heartbeat_loop())<br/>v3.1: 后台主动消息心跳"]
    E -->|no| G["yield (service running)"]
    F --> G
    G --> H["shutdown: cancel heartbeat + stop bridge + close store"]
```

---

## 2. 微信消息接收入口

### 内部桥接模式 (WeChatBridge 长轮询)

```mermaid
flowchart TD
    A["WeChatBridge.start()"] --> B["asyncio.create_task(_poll_loop())"]
    B --> C["while running: get_updates(token, timeout_ms)"]
    C --> D["for update in updates: buffer[user_id].append(update)"]
    D --> E["_schedule_flush(user_id)<br/>延迟 buffer_debounce_seconds"]
    E --> F["_flush_buffer(user_id)<br/>合并多条消息"]
    F --> G["POST /chat {messages, user_id, input_latest_seq, ...}"]
```

### 外部调用模式

外部服务直接 POST `/chat` 端点, 格式同上。

---

## 3. 消息进入处理链路

**文件**: `main.py` → `async def chat(request: Request)`

```mermaid
flowchart TD
    A["POST /chat"] --> B["_require_chat_auth(request)"]
    B --> C["body = await request.json()"]
    C --> D["_normalize_chat_messages(body)"]
    D --> E["_validate_chat_user_id()"]
    E --> F["_record_user_message_arrival(user_id, seq)<br/>中断检查基础"]
    F --> G["lock = _get_user_lock(user_id)"]

    G --> H["async with lock:"]
    H --> I{"handle_pending_persona_confirmation()?"}
    I -->|yes| J["快速路径返回"]
    I -->|no| K["_select_route(user_message)"]
    K --> L["history = get_history_async()"]
    L --> M["system_prompt = build_enriched_system_prompt()"]
    M --> N["interrupt_checker = _make_interrupt_checker()"]
    N --> O["构造 _wmh_cb + _hook_cbs"]
    O --> P["result = await chat_agent.chat(...)"]

    P --> Q["记录 L2 turn → create_turn()"]
    Q --> R["记录 outbound → record_outbound_messages()"]
    R --> S["记录 contact state"]
    S --> T["返回 JSONResponse"]
```

---

## 4. 状态机主循环

**文件**: `agent/chat_agent.py` → `ChatAgent.chat()`

```mermaid
flowchart TD
    A["chat(user_message, history, proactive_meta, ...)"] --> B{"source == PROACTIVE_IMPULSE?"}
    B -->|yes| C["验证 user_message is None"]
    B -->|no| D["turn = TurnScratchpad(...)"]
    C --> D
    D --> E["state = RECEIVE_EVENT"]

    E --> F["while state not in TERMINAL_STATES:"]
    F --> G{"llm_call_count ≥ max?"}
    G -->|yes| H{"source == PROACTIVE?"}
    H -->|yes| I["END_PROACTIVE_MAX_LLM_STEPS"]
    H -->|no| J["HARD_FALLBACK"]
    G -->|no| K{"turn timeout?"}
    K -->|yes| L["END_MAX_LIMIT"]
    K -->|no| M["state = _dispatch(state, turn, ...)"]
    M --> N["turn.iteration += 1"]
    N --> F

    M --> O["Post-terminal Pipeline"]
    O --> P["WRITE_MODEL_HISTORY callback<br/>v3.1: proactive → assistant:proactive (delivered-only)"]
    P --> Q["POST_TURN_HOOKS callbacks<br/>v3.1: proactive dev logs + turn record"]
    Q --> R["TurnResult 组装返回"]
```

### 完整状态流转 (v3.1)

```mermaid
flowchart TD
    RECEIVE_EVENT --> CREATE_TURN
    CREATE_TURN --> INTERRUPT_CHECK
    INTERRUPT_CHECK -->|被中断| END_CANCELLED
    INTERRUPT_CHECK -->|通过| BUILD_CONTEXT

    BUILD_CONTEXT -->|v3.1: proactive → inject proactive context| CLASSIFY_CONTEXT_ERROR
    CLASSIFY_CONTEXT_ERROR -->|FATAL| HARD_FALLBACK
    CLASSIFY_CONTEXT_ERROR -->|OK| OPTIONAL_PREPROCESS

    OPTIONAL_PREPROCESS -->|预取时间| CALL_LLM

    CALL_LLM -->|v3.1: proactive → dynamic allowed_tools| HANDLE_MODEL_OUTPUT
    HANDLE_MODEL_OUTPUT -->|无 tool_calls| PROTOCOL_RETRY
    HANDLE_MODEL_OUTPUT -->|有 tool_calls| CLASSIFY_ONE_TOOL_CALL

    CLASSIFY_ONE_TOOL_CALL -->|"FACT_TOOL (get_time/get_weather)"| EXEC_FACT_TOOLS
    CLASSIFY_ONE_TOOL_CALL -->|"wechat_reply"| VALIDATE_REPLY_ARGS
    CLASSIFY_ONE_TOOL_CALL -->|"done"| VALIDATE_DONE_ARGS
    CLASSIFY_ONE_TOOL_CALL -->|"skip_proactive (v3.1)"| VALIDATE_SKIP_PROACTIVE_ARGS

    EXEC_FACT_TOOLS -->|结果回灌| CALL_LLM

    VALIDATE_REPLY_ARGS --> GUARD_OUTPUT
    GUARD_OUTPUT -->|v3.1: proactive → END_PROACTIVE_GUARD_BLOCKED| END_PROACTIVE_GUARD_BLOCKED
    GUARD_OUTPUT -->|通过| PRE_SEND_INTERRUPT_CHECK
    PRE_SEND_INTERRUPT_CHECK -->|被中断| END_SUPERSEDED_BEFORE_SEND
    PRE_SEND_INTERRUPT_CHECK -->|通过| EXEC_SEND_ONE
    EXEC_SEND_ONE -->|v3.1: proactive counter 更新| APPEND_SEND_RESULT
    APPEND_SEND_RESULT --> POST_SEND_THINKING_DELAY
    POST_SEND_THINKING_DELAY --> POST_SEND_INTERRUPT_CHECK
    POST_SEND_INTERRUPT_CHECK -->|被中断| END_INTERRUPTED_DURING_REPLY_SEQUENCE
    POST_SEND_INTERRUPT_CHECK -->|通过| LIMIT_CHECK
    LIMIT_CHECK -->|"v3.1: proactive hard_max → END_PROACTIVE_MAX_REPLY_LIMIT"| END_PROACTIVE_MAX_REPLY_LIMIT
    LIMIT_CHECK -->|通过| CALL_LLM

    VALIDATE_DONE_ARGS --> END_REPLIED
    VALIDATE_DONE_ARGS --> END_NO_REPLY

    VALIDATE_SKIP_PROACTIVE_ARGS --> EXEC_SKIP_PROACTIVE
    EXEC_SKIP_PROACTIVE -->|"v3.1: send_attempt>0→SKIPPED_AFTER_SEND_FAILURE"| END_SKIPPED_PROACTIVE

    HARD_FALLBACK -->|v3.1: proactive → END_FAILED_NO_FALLBACK| END_FAILED_NO_FALLBACK
    HARD_FALLBACK -->|非 proactive| FALLBACK_SEND
    FALLBACK_SEND --> END_FALLBACK_SENT

    PROTOCOL_RETRY -->|max 2 次| CALL_LLM
    PROTOCOL_RETRY -->|超限 + proactive| END_FAILED_NO_FALLBACK
    PROTOCOL_RETRY -->|超限| HARD_FALLBACK
```

---

## 5. LLM 调用位置

**文件**: `agent/chat_agent.py` → `_do_call_llm()`

调用链: `chat_agent._do_call_llm()` → `LLMGateway.complete()` → `DeepSeekAdapter.complete()` → `openai.AsyncOpenAI.chat.completions.create()`

v3.1 新增:
- proactive 回合动态 allowed_tools (Phase A/B/C, 基于 delivered_count)
- proactive_llm_step_count 追踪和上限检查
- tool_choice="required" (主动回合强制工具调用)

---

## 6. Tool Call 执行位置

### 工具分类

**文件**: `agent/tool_classifier.py` → `classify_single_tool_call()`

| 类型 | 工具 | 执行后流向 |
|------|------|-----------|
| FACT_TOOL | get_time, get_weather | execute_tool() → 结果回灌 → CALL_LLM |
| ACTION_TOOL | wechat_reply | 发送链路 → 继续 LLM 循环 |
| TERMINAL_TOOL | done, skip_proactive | 结束 turn |

### FACT_TOOL 执行

**文件**: `agent/chat_agent.py` → `_do_exec_fact_tools()`, `agent/tools.py` → `execute_tool()`

### ACTION_TOOL (wechat_reply) 执行

状态链: VALIDATE_REPLY_ARGS → GUARD_OUTPUT → PRE_SEND_INTERRUPT_CHECK → EXEC_SEND_ONE

v3.1: GUARD_OUTPUT 先过 `guard_proactive_output()` (主动回合) 再过 `guard_single_message()` (通用)

### skip_proactive 执行 (v3.1)

状态链: VALIDATE_SKIP_PROACTIVE_ARGS → EXEC_SKIP_PROACTIVE → END_SKIPPED_PROACTIVE

仅主动回合 Phase A (delivered_count == 0) 可用。send_attempt_count > 0 时状态为 SKIPPED_AFTER_SEND_FAILURE。

---

## 7. 微信发送位置

```mermaid
flowchart TD
    A["chat_agent._do_exec_send_one()"] --> B["wechat_sender(messages)<br/>main.py 注入的回调"]
    B --> C["_bridge.send_text(user_id, text, context_token)"]
    C --> D["ilink_client.send_message(token, to_user, content)"]
    D --> E["httpx.post('https://ilinkai.weixin.qq.com/bot/send_message')"]
```

v3.1: EXEC_SEND_ONE 中主动回合递增 `proactive_send_attempt_count` 和 `proactive_delivered_count`。

---

## 8. 主动消息链路 (v3.1)

### 8.1 心跳触发

```mermaid
flowchart TD
    A["lifespan: asyncio.create_task(_proactive_heartbeat_loop())"] --> B["while True: sleep(interval)"]
    B --> C["proactive_scheduler.run_heartbeat(user_id, bot_id, providers...)"]
    C --> D{"_check_eligibility()<br/>8 项前置判定"}
    D -->|不通过| E["skipped (记录原因)"]
    D -->|通过| F{"_select_impulse()<br/>生成→去重→过期→评分→选择"}
    F -->|无候选| E
    F -->|有候选| G["_execute_proactive_turn()<br/>→ chat_agent.chat(source=PROACTIVE_IMPULSE)"]
    G --> H["TurnResult"]
```

### 8.2 管理界面手动触发

```mermaid
flowchart TD
    A["POST /admin/proactive/trigger"] --> B["_build_test_impulse_meta(body)"]
    B --> C["构造 providers (history, system_prompt, wechat_sender, interrupt_checker)"]
    C --> D["_build_proactive_write_model_history()"]
    D --> E["_build_proactive_post_turn_hooks()"]
    E --> F["proactive_scheduler.run_manual_trigger(user_id, impulse_meta, ...)"]
    F --> G{"lock.locked()?"}
    G -->|yes| H["skipped: active_proactive_turn_running"]
    G -->|no| I["_execute_proactive_turn()"]
    I --> J["返回 result"]
```

### 8.3 可见上下文一致性

```
发送层: EXEC_SEND_ONE → delivery_status
  ↓
历史写入层: WRITE_MODEL_HISTORY → 只写 proactive_delivered_texts
  ↓
上下文构造器: BUILD_CONTEXT → 只从 confirmed visible history 构造
  ↓
日志层: dev log → 记录全量 attempts (与对话历史隔离)
```

---

## 9. 日志/历史记录位置

| 数据类型 | 写入位置 | 触发时机 |
|----------|----------|----------|
| 应用日志 | `data/agent.log` | 全局 logging |
| 对话历史 (L1) | `conversation_history` 表 | WRITE_MODEL_HISTORY callback |
| Turn 记录 (L2) | `turns` 表 | POST_TURN_HOOKS → create_turn() |
| 出站消息 | `outbound_messages` 表 | POST_TURN_HOOKS → record_outbound_messages() |
| Turn 诊断 | `turn_diagnostics` 表 | POST_TURN_HOOKS → _hook_dev_logs() |
| LLM Trace | `llm_traces` 表 | llm_gateway.on_trace callback |
| 主动消息事件 | `usage_events` 表 | proactive post_turn_hooks |
| 记忆提取 | `memories` 表 | POST_TURN_HOOKS → _hook_memory (条件) |

---

## 10. 关键函数索引

| 步骤 | 文件 | 函数 |
|------|------|------|
| 启动入口 | `main.py` | 模块级代码 + `lifespan()` |
| 主动心跳 (v3.1) | `main.py` | `_proactive_heartbeat_loop()` |
| 微信轮询 | `bridge/wechat_poller.py` | `WeChatBridge._poll_loop()` |
| 消息接收 | `main.py` | `chat()` |
| 主动触发 (v3.1) | `main.py` | `admin_trigger_proactive()` |
| 中断检查 | `main.py` | `_make_interrupt_checker()` |
| 路由选择 | `main.py` | `_select_route()` |
| 状态机入口 | `agent/chat_agent.py` | `ChatAgent.chat()` |
| 状态分发 | `agent/chat_agent.py` | `ChatAgent._dispatch()` |
| 上下文构建 | `agent/chat_agent.py` | `_do_build_context()` |
| 主动上下文 (v3.1) | `agent/chat_agent.py` | `_build_proactive_context_system_message()` |
| LLM 调用 | `agent/chat_agent.py` | `_do_call_llm()` |
| 主动 allowed_tools (v3.1) | `agent/chat_agent.py` | `_get_proactive_allowed_tools()` |
| 工具分类 | `agent/tool_classifier.py` | `classify_single_tool_call()` |
| skip_proactive 验证 (v3.1) | `agent/chat_agent.py` | `_do_validate_skip_proactive_args()` |
| skip_proactive 执行 (v3.1) | `agent/chat_agent.py` | `_do_exec_skip_proactive()` |
| 输出守卫 | `agent/output_guard.py` | `guard_single_message()`, `guard_proactive_output()` |
| 工具执行 | `agent/tools.py` | `execute_tool()` |
| 微信发送 | `main.py` | `wechat_sender()` |
| iLink API | `bridge/ilink_client.py` | `send_message()` |
| 调度编排 (v3.1) | `agent/scheduler.py` | `ProactiveScheduler.run_heartbeat()`, `run_manual_trigger()` |
| 资格检查 (v3.1) | `agent/scheduler.py` | `ProactiveScheduler._check_eligibility()` |
| 冲动生成 (v3.1) | `agent/scheduler.py` | `ProactiveScheduler._select_impulse()` |
| 关系热度 (v3.1) | `agent/scheduler.py` | `ProactiveScheduler.update_relationship_temperature()` |
| 历史写入 | `main.py` | `_build_chatagent_write_model_history()`, `_build_proactive_write_model_history()` |
| 诊断记录 | `main.py` | `_build_chatagent_post_turn_hooks()`, `_build_proactive_post_turn_hooks()` |
| 记忆提取 | `agent/memory_worker.py` | `MemoryWorker.extract()` |
