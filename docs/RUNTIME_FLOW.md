# Runtime Flow — 运行入口和主链路分析

> 生成日期: 2026-05-31
> 基于实际代码追踪

---

## 1. 程序启动入口

**文件**: `main.py` (顶层模块级代码)

启动链路:

```
main.py 模块加载
  ├─ load_dotenv()                           # 加载 .env
  ├─ load_config("config.yaml")              # 加载配置, 支持 ${ENV_VAR} 替换
  ├─ load_runtime_settings()                 # 加载 data/runtime_settings.json
  ├─ _deep_update(cfg, runtime_settings)     # 合并运行时设置
  ├─ logging.basicConfig(...)                # 初始化日志 (stdout + data/agent.log)
  ├─ configure_tools(cfg)                    # 注入工具运行时配置
  ├─ LLMGateway.from_model_config(cfg)       # 创建 LLM 网关 (DeepSeek adapter)
  ├─ MemoryStore(db_path=...)                # 创建记忆存储 (SQLite WAL)
  ├─ configure_runtime(memory_store=...)     # 注入 memory_store 到 tools
  ├─ ChatAgent(...)                          # 创建聊天 agent 实例
  ├─ MemoryWorker(...) / MemoryHook(...)     # 条件创建 (features.memory=true)
  ├─ WeChatBridge(...)                       # 条件创建 (wechat.enabled=true)
  └─ FastAPI(lifespan=lifespan)              # 创建 FastAPI app
```

**lifespan 启动** (`main.py:1314-1333`):

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup_validate()           # 启动自检 (API key, persona, data dir)
    if _bridge is not None:
        await _bridge.start()     # 启动微信轮询后台任务
    yield
    # shutdown:
    if _bridge is not None:
        await _bridge.stop()
    await memory_store.close()
```

**uvicorn 启动** (由 `run.bat` 或命令行):

```
python main.py  →  uvicorn main:app --host 0.0.0.0 --port 8899
```

---

## 2. 微信消息接收入口

### 2.1 内部桥接模式 (WeChatBridge 长轮询)

**文件**: `bridge/wechat_poller.py` → `WeChatBridge._poll_loop()`

```
WeChatBridge.start()
  └─ asyncio.create_task(self._poll_loop())   # 后台轮询

_poll_loop():
  while self.running:
    updates = await get_updates(token, timeout_ms)  # 调用 iLink API
    for update in updates:
      user_id = extract_user_id(update)
      self._buffers[user_id].append(update)         # 缓冲去抖
      self._schedule_flush(user_id)                  # 延迟后 flush

_schedule_flush(user_id):
  await asyncio.sleep(buffer_debounce_seconds)
  await _flush_buffer(user_id)

_flush_buffer(user_id):
  messages = [extract_text(u) for u in self._buffers[user_id]]
  await httpx.post(f"{goal_url}/chat", json={
      "messages": messages,
      "user_id": user_id,
      "input_message_ids": [...],
      "input_latest_seq": seq,
      ...
  })
```

### 2.2 外部调用模式

外部服务直接 POST `/chat` 端点, 格式同上。

---

## 3. 消息进入处理链路

**文件**: `main.py:2030` → `async def chat(request: Request)`

```
POST /chat
  ├─ _require_chat_auth(request)              # API key 验证
  ├─ body = await request.json()              # 解析 JSON
  ├─ _normalize_chat_messages(body)           # 标准化消息列表
  ├─ user_id = _validate_chat_user_id(...)    # 验证 user_id
  ├─ _record_user_message_arrival(user_id, seq)  # 记录 inbound seq (中断检查基础)
  ├─ lock = _get_user_lock(user_id)           # per-user 异步锁
  │
  └─ async with lock:
      ├─ handle_pending_persona_confirmation()  # 人设变更确认 (快速路径)
      ├─ _select_route(user_message)            # 路由选择 (SystemFast/ChatLite/ChatFull)
      ├─ history = get_history_async(user_id)   # 获取对话历史
      ├─ system_prompt = build_enriched_system_prompt(user_id, msg)
      ├─ interrupt_checker = _make_interrupt_checker(user_id, seq)
      ├─ _wmh_cb = _build_chatagent_write_model_history(user_id, bot_id)
      ├─ _hook_cbs = _build_chatagent_post_turn_hooks(...)
      │
      ├─ result = await chat_agent.chat(...)    # ← 进入状态机
      │
      ├─ 记录 turn (L2) → memory_store.create_turn(...)
      ├─ 记录 outbound → memory_store.record_outbound_messages(...)
      ├─ 记录诊断 → memory_store.record_turn_diagnostics(...)
      └─ 返回 JSONResponse
```

---

## 4. 状态机主循环

**文件**: `agent/chat_agent.py` → `ChatAgent.chat()`

```
chat(user_message, history, ...)
  ├─ turn = TurnScratchpad(...)               # 创建 turn 本地状态
  ├─ state = TurnState.RECEIVE_EVENT
  │
  └─ while state not in TERMINAL_STATES:
      ├─ 检查 max_llm_steps 超限 → HARD_FALLBACK
      ├─ 检查 turn timeout → END_MAX_LIMIT
      ├─ state = await self._dispatch(state, turn, ...)
      └─ turn.iteration += 1
```

### 状态流转图

```
RECEIVE_EVENT
  → CREATE_TURN
    → INTERRUPT_CHECK ──(被中断)──→ END_CANCELLED
      → BUILD_CONTEXT
        → CLASSIFY_CONTEXT_ERROR ──(FATAL)──→ HARD_FALLBACK
          → OPTIONAL_PREPROCESS (预取时间)
            → CALL_LLM ──────────────────────────────────────┐
              ← HANDLE_MODEL_OUTPUT                          │
                ├─ 有 content 无 tool_calls → PROTOCOL_RETRY ─┤
                └─ 有 tool_calls                             │
                  → CLASSIFY_ONE_TOOL_CALL                   │
                    ├─ 未知/废弃工具 → PROTOCOL_RETRY ────────┤
                    ├─ FACT_TOOL (get_time/get_weather)      │
                    │   → EXEC_FACT_TOOLS                    │
                    │     → INTERRUPT_CHECK                  │
                    │       → CALL_LLM ──────────────────────┘
                    │
                    ├─ ACTION_TOOL (wechat_reply)
                    │   → VALIDATE_REPLY_ARGS
                    │     → GUARD_OUTPUT
                    │       → PRE_SEND_INTERRUPT_CHECK
                    │         → EXEC_SEND_ONE (发送单条)
                    │           → APPEND_SEND_RESULT
                    │             → POST_SEND_THINKING_DELAY
                    │               → POST_SEND_INTERRUPT_CHECK
                    │                 → LIMIT_CHECK
                    │                   → CALL_LLM ──────────┘
                    │
                    └─ TERMINAL_TOOL (done)
                        → VALIDATE_DONE_ARGS
                          → END_REPLIED / END_NO_REPLY

HARD_FALLBACK
  → INTERRUPT_CHECK
    → guard_single_message (固定文案 "我刚刚卡了一下 重新说")
      → EXEC_SEND_ONE
        → END_FALLBACK_SENT / END_FAILED

[terminal state]
  → WRITE_MODEL_HISTORY (callback)
    → POST_TURN_HOOKS (callbacks)
      → TurnResult 组装
```

---

## 5. LLM 调用位置

**文件**: `agent/chat_agent.py:666` → `_do_call_llm()`

```python
async def _do_call_llm(self, turn: TurnScratchpad) -> TurnState:
    # 1. 模型路由 (v1.3)
    model_label, routing_reason, profile = self._select_model(turn)

    # 2. 构建 LLMRequest
    request = LLMRequest(
        purpose="chat",
        messages=turn.messages,
        model=profile.model_name,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        tools=tool_specs,              # native tool calls
        tool_choice="auto",
        timeout_s=profile.timeout_seconds,
    )

    # 3. 调用 LLM Gateway
    response: LLMResponse = await self.llm_gateway.complete(request)
```

**文件**: `agent/llm_gateway.py` → `LLMGateway.complete()`

```python
async def complete(self, request: LLMRequest) -> LLMResponse:
    return await self.adapter.complete(request)
```

**文件**: `agent/providers/deepseek.py` → `DeepSeekAdapter.complete()`

```python
async def complete(self, request: LLMRequest) -> LLMResponse:
    payload = self._build_payload(request)
    response = await self.client.chat.completions.create(**payload)
    return self._parse_response(response, request)
```

调用链: `chat_agent._do_call_llm()` → `LLMGateway.complete()` → `DeepSeekAdapter.complete()` → `openai.AsyncOpenAI.chat.completions.create()`

---

## 6. Tool Call 执行位置

### 6.1 工具分类

**文件**: `agent/tool_classifier.py` → `classify_single_tool_call()`

分类结果:
- `FACT_TOOL`: get_time, get_weather → 执行后回灌 LLM
- `ACTION_TOOL`: wechat_reply → 发送单条消息, 继续 LLM 循环
- `TERMINAL_TOOL`: done → 结束 turn
- `STATE_TOOL`: 未注册, 严格拒绝

### 6.2 FACT_TOOL 执行

**文件**: `agent/chat_agent.py` → `_do_exec_fact_tools()`

```python
async def _do_exec_fact_tools(self, turn: TurnScratchpad) -> TurnState:
    results = await asyncio.gather(*[
        execute_tool(name, args) for name, args in fact_calls
    ])
    # 结果写入 turn.tool_results, 然后 → CALL_LLM (回灌模型)
```

**文件**: `agent/tools.py` → `execute_tool()`

```python
async def execute_tool(name: str, args: dict) -> str:
    if name == "get_time":
        return await _get_time(**args)
    elif name == "get_weather":
        return await _get_weather(**args)
    ...
```

### 6.3 ACTION_TOOL (wechat_reply) 执行

**文件**: `agent/chat_agent.py` → 状态链:

```
VALIDATE_REPLY_ARGS → GUARD_OUTPUT → PRE_SEND_INTERRUPT_CHECK → EXEC_SEND_ONE
```

`EXEC_SEND_ONE` 调用注入的 `wechat_sender` 回调。

### 6.4 TERMINAL_TOOL (done) 执行

**文件**: `agent/chat_agent.py` → `_do_validate_done_args()`

done 工具不执行实际操作, 仅验证参数并设置终态:
- `status="replied"` → `END_REPLIED`
- `status="no_reply"` → `END_NO_REPLY`

---

## 7. 微信发送位置

### 7.1 wechat_sender 回调

**文件**: `main.py:417` → `wechat_sender()`

```python
async def wechat_sender(user_id, context_token, messages):
    for i, m in enumerate(messages):
        await asyncio.sleep(delay)
        await _bridge.send_text(user_id, text, context_token)
        m["send_status"] = "delivered"
```

### 7.2 iLink API 调用

**文件**: `bridge/ilink_client.py` → `send_message()`

```python
async def send_message(token, to_user, content, base_url=""):
    body = json.dumps({"to_user": to_user, "content": content})
    return await _api_fetch("bot/send_message", token=token, body=body, ...)
```

### 7.3 发送链路总览

```
chat_agent._do_exec_send_one()
  └─ wechat_sender(messages)           # main.py 注入的回调
      └─ _bridge.send_text(user_id, text, context_token)
          └─ ilink_client.send_message(token, to_user, content)
              └─ httpx.post("https://ilinkai.weixin.qq.com/bot/send_message")
```

---

## 8. 日志/历史记录位置

### 8.1 应用日志

**文件**: `main.py:210-220`

```python
logging.basicConfig(
    handlers=[
        logging.StreamHandler(),                    # stdout
        logging.FileHandler("./data/agent.log"),    # 文件
    ]
)
```

输出: `data/agent.log`

### 8.2 对话历史 (L1)

**内存**: `main.py` → `conversation_histories: dict[str, list[dict]]`

**持久化**: `memory/store.py` → `MemoryStore.append_conversation_history()`

表: `conversation_history`

### 8.3 Turn 记录 (L2)

**文件**: `main.py:2208` (chat endpoint 内)

```python
asyncio.create_task(
    memory_store.create_turn(
        turn_id=turn_id,
        user_id=user_id,
        bot_id=bot_id,
        user_messages=user_message,
        assistant_actions=...,
        tool_calls=...,
        send_status=send_status,
    )
)
```

表: `turns`

### 8.4 出站消息追踪

**文件**: `main.py:2222`

```python
await memory_store.record_outbound_messages(
    turn_id=turn_id,
    messages=result.visible_messages,
    ...
)
```

表: `outbound_messages`

### 8.5 Turn 诊断

**文件**: `main.py` → `_build_chatagent_post_turn_hooks()` → `_hook_dev_logs()`

```python
await memory_store.record_turn_diagnostics(
    turn_id=turn_id,
    route=route,
    llm_status=turn.llm_status,
    token_count=token_count,
    elapsed_ms=turn.elapsed_ms,
    ...
)
```

表: `turn_diagnostics`

### 8.6 LLM Trace

**文件**: `main.py:293-297`

```python
async def _on_llm_trace(trace: dict):
    await memory_store.save_llm_trace(trace)
```

表: `llm_traces`

### 8.7 记忆提取 (异步)

**文件**: `agent/memory_worker.py` → `MemoryWorker.extract()`

```python
async def extract(self, user_msg, assistant_reply):
    # LLM 提取 → dedup → memory_store.add_memory()
```

表: `memories`

触发时机: POST_TURN_HOOKS 中的 `_hook_memory` (features.memory=true 时)

---

## 9. 完整请求生命周期 (单条微信消息)

```
[微信用户发送消息]
  │
  ▼
[WeChatBridge._poll_loop() — bridge/wechat_poller.py]
  │ get_updates() → iLink API 长轮询
  │ 缓冲去抖 → flush_buffer
  ▼
[POST /chat — main.py:2030]
  │ 认证 → 解析 → 记录 seq → 获取锁
  ▼
[chat_agent.chat() — agent/chat_agent.py:146]
  │
  ├─ RECEIVE_EVENT → CREATE_TURN → INTERRUPT_CHECK
  │
  ├─ BUILD_CONTEXT — 组装 system_prompt + history + user_message
  │   └─ system_prompt 来自 build_enriched_system_prompt()
  │       └─ persona.md + 可选 memory context
  │
  ├─ CLASSIFY_CONTEXT_ERROR — 检查 fatal/recoverable
  │
  ├─ OPTIONAL_PREPROCESS — 预取当前时间, 合入 system message
  │
  ├─ CALL_LLM — agent/llm_gateway.py → agent/providers/deepseek.py
  │   └─ DeepSeek API (native tool calls)
  │
  ├─ HANDLE_MODEL_OUTPUT — 检查 assistant.content / tool_calls
  │
  ├─ CLASSIFY_ONE_TOOL_CALL — agent/tool_classifier.py
  │   │
  │   ├─ [FACT_TOOL: get_time/get_weather]
  │   │   └─ EXEC_FACT_TOOLS → agent/tools.py → execute_tool()
  │   │       └─ 结果回灌 → CALL_LLM (循环)
  │   │
  │   ├─ [ACTION_TOOL: wechat_reply]
  │   │   └─ VALIDATE_REPLY_ARGS → GUARD_OUTPUT (agent/output_guard.py)
  │   │       → EXEC_SEND_ONE → wechat_sender() (main.py)
  │   │           → _bridge.send_text() → ilink_client.send_message()
  │   │               → POST_SEND_THINKING_DELAY → CALL_LLM (循环)
  │   │
  │   └─ [TERMINAL_TOOL: done]
  │       └─ VALIDATE_DONE_ARGS → END_REPLIED / END_NO_REPLY
  │
  ├─ [异常路径] HARD_FALLBACK
  │   └─ 固定文案 "我刚刚卡了一下 重新说" → GUARD_OUTPUT → EXEC_SEND_ONE
  │
  └─ [终态确定]
      ├─ WRITE_MODEL_HISTORY — 写入 conversation_history
      ├─ POST_TURN_HOOKS
      │   ├─ _hook_memory — 异步记忆提取 (features.memory=true)
      │   └─ _hook_dev_logs — 写入 turn_diagnostics + usage_events
      └─ TurnResult 组装返还
  │
  ▼
[POST /chat 续]
  │ 记录 L2 turn → memory_store.create_turn()
  │ 记录 outbound → memory_store.record_outbound_messages()
  │ 返回 JSONResponse 给调用方
  ▼
[WeChatBridge._flush_buffer() 收到响应]
  │ (响应被丢弃, bridge 不处理 /chat 返回值)
  ▼
[用户收到微信消息]
```

---

## 10. 关键函数索引

| 步骤 | 文件 | 函数 |
|------|------|------|
| 启动入口 | `main.py` | 模块级代码 + `lifespan()` |
| 微信轮询 | `bridge/wechat_poller.py` | `WeChatBridge._poll_loop()` |
| 消息接收 | `main.py` | `chat()` (line 2030) |
| 中断检查 | `main.py` | `_make_interrupt_checker()` |
| 路由选择 | `main.py` | `_select_route()` |
| 系统提示词 | `main.py` | `build_enriched_system_prompt()` |
| 状态机入口 | `agent/chat_agent.py` | `ChatAgent.chat()` |
| 状态分发 | `agent/chat_agent.py` | `ChatAgent._dispatch()` |
| 上下文构建 | `agent/chat_agent.py` | `_do_build_context()` |
| LLM 调用 | `agent/chat_agent.py` | `_do_call_llm()` |
| LLM 网关 | `agent/llm_gateway.py` | `LLMGateway.complete()` |
| DeepSeek 适配 | `agent/providers/deepseek.py` | `DeepSeekAdapter.complete()` |
| 工具分类 | `agent/tool_classifier.py` | `classify_single_tool_call()` |
| 工具执行 | `agent/tools.py` | `execute_tool()` |
| 输出守卫 | `agent/output_guard.py` | `guard_messages()`, `guard_single_message()` |
| 微信发送 | `main.py` | `wechat_sender()` |
| iLink API | `bridge/ilink_client.py` | `send_message()` |
| 历史写入 | `main.py` | `append_history_async()` |
| Turn 记录 | `main.py` | `_build_chatagent_write_model_history()` |
| 诊断记录 | `main.py` | `_build_chatagent_post_turn_hooks()` → `_hook_dev_logs()` |
| 记忆提取 | `agent/memory_worker.py` | `MemoryWorker.extract()` |
| 记忆存储 | `memory/store.py` | `MemoryStore` |
