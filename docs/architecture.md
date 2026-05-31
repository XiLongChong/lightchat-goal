# Architecture — 系统架构图

> 生成日期: 2026-05-31
> 基于实际代码 (v1.5 strict CC causal chain)

---

## 1. 系统模块图

```
┌─────────────────────────────────────────────────────────────────────┐
│                         WeChat 用户                                  │
└────────────────────────────┬────────────────────────────────────────┘
                             │ iLink API
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  bridge/                                                              │
│  ┌──────────────────┐    ┌──────────────────┐                       │
│  │  ilink_client.py  │◄──│  wechat_poller.py │                       │
│  │  (7 HTTP 端点)    │    │  WeChatBridge     │                       │
│  │  · get_updates    │    │  · 长轮询          │                       │
│  │  · send_message   │    │  · 缓冲去抖        │                       │
│  │  · get_qr_code    │    │  · seq 管理        │                       │
│  └──────────────────┘    └────────┬─────────┘                       │
└───────────────────────────────────┼─────────────────────────────────┘
                                    │ POST /chat
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  main.py  (FastAPI — 30+ 路由)                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │ /chat 端点    │  │ /admin/*     │  │ /wechat/*    │              │
│  │ · 认证        │  │ · 面板       │  │ · QR 登录    │              │
│  │ · 锁管理      │  │ · 设置       │  │ · 桥接控制    │              │
│  │ · 中断检查    │  │ · 记忆       │  └──────────────┘              │
│  │ · 历史管理    │  │ · 诊断       │                                │
│  └──────┬───────┘  └──────────────┘                                │
│         │                                                           │
│  ┌──────▼───────────────────────────────────────────────────────┐  │
│  │  编排层                                                        │  │
│  │  · wechat_sender 回调        · 中断检查器工厂                  │  │
│  │  · post-terminal 回调构造    · 对话历史管理                    │  │
│  │  · 记忆导入/导出             · 管理面板数据聚合                │  │
│  └──────────────────────────────────────────────────────────────┘  │
└──────┬──────────┬──────────┬──────────┬───────────────────────────┘
       │          │          │          │
       ▼          ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│ agent/   │ │ memory/  │ │ prompts/ │ │ config   │
│          │ │          │ │          │ │ .yaml    │
└──────────┘ └──────────┘ └──────────┘ └──────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  agent/  (核心 AI 逻辑)                                              │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  chat_agent.py  (状态机主循环)                               │    │
│  │  · 23+ state handler      · 模型路由 (v1.3)                 │    │
│  │  · 中断检查               · post-terminal pipeline          │    │
│  └──────────┬──────────────────────────┬──────────────────────┘    │
│             │                          │                            │
│  ┌──────────▼──────────┐  ┌───────────▼──────────┐                │
│  │  state_machine.py   │  │  tool_classifier.py  │                │
│  │  · TurnState 枚举   │  │  · FACT_TOOL         │                │
│  │  · TurnScratchpad   │  │  · ACTION_TOOL       │                │
│  │  · TurnResult       │  │  · TERMINAL_TOOL     │                │
│  │  · VisibleMessage   │  │  · STATE_TOOL        │                │
│  │  · DeliveryStatus   │  └──────────┬───────────┘                │
│  └─────────────────────┘             │                            │
│                                      ▼                            │
│  ┌──────────────────┐  ┌──────────────────────┐                  │
│  │  tool_registry.py │  │  tools.py            │                  │
│  │  · ToolDef        │  │  · get_time          │                  │
│  │  · ActionDef      │  │  · get_weather       │                  │
│  │  · TOOL_REGISTRY  │  │  · execute_tool      │                  │
│  └──────────────────┘  └──────────────────────┘                  │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────────┐                  │
│  │  output_guard.py  │  │  context_builder.py  │                  │
│  │  · 协议泄露检测   │  │  · compact_history   │                  │
│  │  · 敏感信息过滤   │  │  · 系统提示词构建    │                  │
│  │  · batch guard    │  │  · token 估算        │                  │
│  └──────────────────┘  └──────────────────────┘                  │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  llm_gateway.py  (Provider-neutral LLM 网关)                  │  │
│  │  ┌─────────────────┐    ┌─────────────────────────────────┐  │  │
│  │  │  providers/base  │◄──│  providers/deepseek.py           │  │  │
│  │  │  · ProviderConfig│    │  · DeepSeekAdapter               │  │  │
│  │  │  · Protocol      │    │  · AsyncOpenAI → DeepSeek API   │  │  │
│  │  └─────────────────┘    └─────────────────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────────┐                  │
│  │  memory_worker.py │  │  memory_hook.py      │                  │
│  │  · 异步记忆提取   │  │  · load_context      │                  │
│  │  · LLM 提取      │  │  · update            │                  │
│  │  · fire-and-forget│  └──────────┬───────────┘                  │
│  └────────┬─────────┘             │                            │
│           │                       │                            │
└───────────┼───────────────────────┼────────────────────────────────┘
            │                       │
            ▼                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│  memory/  (持久化层)                                                  │
│  ┌──────────────────┐  ┌──────────────────┐                        │
│  │  store.py         │  │  dedup.py         │                        │
│  │  MemoryStore      │  │  · Jaccard 相似度 │                        │
│  │  · SQLite WAL     │  │  · CJK 二元组     │                        │
│  │  · 15+ 表         │  │  · 去重/重要度    │                        │
│  └──────────────────┘  └──────────────────┘                        │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  data/  (运行时数据, gitignore)                                       │
│  · memory.db  · agent.log  · runtime_settings.json  · backups/      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 主链路流程图

一次微信消息从接收到回复的完整链路:

```
┌──────────────┐
│ 微信用户发送  │
└──────┬───────┘
       │
       ▼
┌──────────────────────────────────────────────────┐
│ WeChatBridge._poll_loop()                         │
│ bridge/wechat_poller.py                           │
│                                                   │
│  iLink get_updates() ──→ 缓冲去抖 ──→ flush      │
└──────────────────────────┬───────────────────────┘
                           │ POST /chat
                           │ {messages, user_id, input_latest_seq, ...}
                           ▼
┌──────────────────────────────────────────────────┐
│ POST /chat                                        │
│ main.py:2030                                      │
│                                                   │
│  1. _require_chat_auth()        API key 验证      │
│  2. _normalize_chat_messages()  标准化消息         │
│  3. _validate_chat_user_id()    验证 user_id      │
│  4. _record_user_message_arrival(seq)  中断基础    │
│  5. _get_user_lock(user_id)     per-user 锁       │
└──────────────────────────┬───────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────┐
│ 预处理 (main.py, 锁内)                            │
│                                                   │
│  handle_pending_persona_confirmation()            │
│  ├─ 有 pending → 快速路径返回                      │
│  └─ 无 pending → 继续                             │
│                                                   │
│  _select_route()                                  │
│  ├─ 空消息 → SystemFastPath                       │
│  ├─ ≤30字 → ChatLite                              │
│  └─ 其他  → ChatFull                              │
│                                                   │
│  history = get_history_async(user_id)             │
│  system_prompt = build_enriched_system_prompt()   │
│  interrupt_checker = _make_interrupt_checker()    │
│  _wmh_cb = _build_chatagent_write_model_history() │
│  _hook_cbs = _build_chatagent_post_turn_hooks()   │
└──────────────────────────┬───────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────┐
│ chat_agent.chat()                                 │
│ agent/chat_agent.py:146                           │
│                                                   │
│  turn = TurnScratchpad(...)                       │
│  state = RECEIVE_EVENT                            │
└──────────────────────────┬───────────────────────┘
                           │
     ┌─────────────────────┘
     ▼
┌─────────────────────────────────────────────────────┐
│                 状态机主循环                           │
│  while state not in TERMINAL_STATES:                 │
│    state = _dispatch(state, turn)                    │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │ RECEIVE_EVENT                                 │   │
│  │   → CREATE_TURN                               │   │
│  │     → INTERRUPT_CHECK ──被中断──→ END_CANCELLED │   │
│  │       → BUILD_CONTEXT                         │   │
│  │         → CLASSIFY_CONTEXT_ERROR              │   │
│  │           ├─ FATAL → HARD_FALLBACK            │   │
│  │           └─ OK → OPTIONAL_PREPROCESS         │   │
│  │               → CALL_LLM ◄────────────────┐   │   │
│  │                 → HANDLE_MODEL_OUTPUT      │   │   │
│  │                   → CLASSIFY_ONE_TOOL_CALL │   │   │
│  │                     │                     │   │   │
│  │    ┌────────────────┼────────────────┐    │   │   │
│  │    ▼                ▼                ▼    │   │   │
│  │ FACT_TOOL     ACTION_TOOL      TERM_TOOL  │   │   │
│  │ get_time      wechat_reply      done      │   │   │
│  │ get_weather                     │         │   │   │
│  │    │                │            ▼        │   │   │
│  │    ▼                ▼       VALIDATE_DONE │   │   │
│  │ EXEC_FACT     VALIDATE_REPLY    │         │   │   │
│  │    │               │            ▼         │   │   │
│  │    ▼               ▼      END_REPLIED /   │   │   │
│  │ 回灌 LLM ──→ CALL_LLM  END_NO_REPLY      │   │   │
│  │              ▲    │                       │   │   │
│  │              │    ▼                       │   │   │
│  │              │  GUARD_OUTPUT              │   │   │
│  │              │    │                       │   │   │
│  │              │    ▼                       │   │   │
│  │              │  EXEC_SEND_ONE             │   │   │
│  │              │    │                       │   │   │
│  │              │    ▼                       │   │   │
│  │              │  POST_SEND_DELAY           │   │   │
│  │              │    │                       │   │   │
│  │              │    ▼                       │   │   │
│  │              │  LIMIT_CHECK               │   │   │
│  │              │    │                       │   │   │
│  │              └────┘ (未达上限, 继续 LLM)   │   │   │
│  └──────────────────────────────────────────┘   │   │
│                                                  │   │
│  ┌──────────────────────────────────────────┐   │   │
│  │ HARD_FALLBACK                             │   │   │
│  │   → INTERRUPT_CHECK                      │   │   │
│  │     → guard("我刚刚卡了一下 重新说")       │   │   │
│  │       → EXEC_SEND_ONE                    │   │   │
│  │         → END_FALLBACK_SENT / END_FAILED  │   │   │
│  └──────────────────────────────────────────┘   │   │
└─────────────────────────────────────────────────────┘
                           │
                           │ terminal state 确定
                           ▼
┌──────────────────────────────────────────────────┐
│ Post-terminal Pipeline (chat_agent.py 内部)       │
│                                                   │
│  1. WRITE_MODEL_HISTORY                           │
│     · 写 user 消息到 conversation_history         │
│     · 按终态矩阵写 assistant 消息                  │
│     · source 区分: assistant / assistant:fallback  │
│                                                   │
│  2. POST_TURN_HOOKS (串行, 单失败不阻塞)          │
│     · _hook_memory    → 异步记忆提取              │
│     · _hook_dev_logs  → turn_diagnostics + usage  │
└──────────────────────────┬───────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────┐
│ TurnResult 组装返回 main.py                        │
│                                                   │
│  · 记录 L2 turn → memory_store.create_turn()      │
│  · 记录 outbound → record_outbound_messages()     │
│  · 返回 JSONResponse 给调用方                      │
└──────────────────────────┬───────────────────────┘
                           │
                           ▼
┌──────────────────────────┐
│ 微信用户收到回复          │
│ (多条消息逐条发送)        │
└──────────────────────────┘
```

---

## 3. Tool Call 循环图

v1.5 严格 CC 因果链: 每次 LLM 响应只允许一个 tool call, wechat_reply 是 action (非终态), done 是唯一终态工具。

```
                         ┌──────────────────────┐
                         │      CALL_LLM        │
                         │  agent/chat_agent.py  │
                         │  _do_call_llm()       │
                         └──────────┬───────────┘
                                    │
                                    │ LLM 响应
                                    ▼
                         ┌──────────────────────┐
                         │  HANDLE_MODEL_OUTPUT  │
                         │  _do_handle_model_    │
                         │  output()             │
                         └──────────┬───────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               ▼               ▼
              ┌──────────┐   ┌──────────┐   ┌──────────┐
              │ 有 content │   │ 有 tool  │   │  空响应   │
              │ 无 tool    │   │ _calls   │   │          │
              │ → PROTOCOL │   │          │   │ → PROTO  │
              │   _RETRY   │   └────┬─────┘   │   COL    │
              └──────────┘        │          │  _RETRY   │
                                  ▼          └──────────┘
                      ┌──────────────────────┐
                      │ CLASSIFY_ONE_TOOL_CALL│
                      │ classify_single_      │
                      │ tool_call()           │
                      └──────────┬───────────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
          ▼                      ▼                      ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│   FACT_TOOL      │ │   ACTION_TOOL    │ │   TERMINAL_TOOL  │
│                  │ │                  │ │                  │
│  get_time        │ │  wechat_reply    │ │  done            │
│  get_weather     │ │                  │ │                  │
│                  │ │                  │ │                  │
│  信息查询,       │ │  发送单条消息,   │ │  结束 turn       │
│  结果回灌 LLM    │ │  继续 LLM 循环   │ │                  │
└────────┬─────────┘ └────────┬─────────┘ └────────┬─────────┘
         │                    │                    │
         ▼                    ▼                    ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│ EXEC_FACT_TOOLS  │ │ VALIDATE_REPLY   │ │ VALIDATE_DONE    │
│                  │ │ _ARGS            │ │ _ARGS            │
│ asyncio.gather   │ │                  │ │                  │
│ 并行执行         │ │ 检查 text 非空   │ │ 检查 status      │
│                  │ │                  │ │                  │
│ execute_tool()   │ │                  │ │                  │
│ · _get_time()    │ └────────┬─────────┘ └────────┬─────────┘
│ · _get_weather() │          │                    │
└────────┬─────────┘          ▼                    ▼
         │           ┌──────────────────┐ ┌──────────────────┐
         │           │   GUARD_OUTPUT   │ │  END_REPLIED     │
         │           │                  │ │  END_NO_REPLY    │
         │           │ guard_single_    │ │                  │
         │           │ message()        │ │  → terminal      │
         │           │ · 协议泄露检测   │ └──────────────────┘
         │           │ · 敏感信息过滤   │
         │           │ · 长度截断       │
         │           └────────┬─────────┘
         │                    │
         │                    ▼
         │           ┌──────────────────┐
         │           │ PRE_SEND_        │
         │           │ INTERRUPT_CHECK  │
         │           │                  │
         │           │ 被中断?          │
         │           │ ├─ 是 → END_     │
         │           │ │  SUPERSEDED    │
         │           │ └─ 否 → 继续     │
         │           └────────┬─────────┘
         │                    │
         │                    ▼
         │           ┌──────────────────┐
         │           │  EXEC_SEND_ONE   │
         │           │                  │
         │           │  wechat_sender() │
         │           │  → bridge.send_  │
         │           │    text()        │
         │           │  → ilink API     │
         │           └────────┬─────────┘
         │                    │
         │                    ▼
         │           ┌──────────────────┐
         │           │ APPEND_SEND_     │
         │           │ RESULT           │
         │           │                  │
         │           │ 记录送达状态:     │
         │           │ · SENT_CONFIRMED │
         │           │ · FAILED_        │
         │           │   CONFIRMED      │
         │           │ · RATE_LIMITED   │
         │           └────────┬─────────┘
         │                    │
         │                    ▼
         │           ┌──────────────────┐
         │           │ POST_SEND_       │
         │           │ THINKING_DELAY   │
         │           │                  │
         │           │ 可中断延迟       │
         │           │ (模拟打字时间)   │
         │           └────────┬─────────┘
         │                    │
         │                    ▼
         │           ┌──────────────────┐
         │           │ POST_SEND_       │
         │           │ INTERRUPT_CHECK  │
         │           │                  │
         │           │ 被中断?          │
         │           │ ├─ 是 → END_     │
         │           │ │  INTERRUPTED    │
         │           │ └─ 否 → 继续     │
         │           └────────┬─────────┘
         │                    │
         │                    ▼
         │           ┌──────────────────┐
         │           │  LIMIT_CHECK     │
         │           │                  │
         │           │  已发消息数      │
         │           │  ≥ MAX_REPLY?    │
         │           │  ├─ 是 → END_    │
         │           │  │  MAX_LIMIT    │
         │           │  └─ 否 → 继续    │
         │           └────────┬─────────┘
         │                    │
         └────────────────────┤
                              │
                              ▼
                    ┌──────────────────┐
                    │    CALL_LLM      │  ◄── 下一轮循环
                    │  (带工具结果     │
                    │   回灌到消息)    │
                    └──────────────────┘


═══════════════════════════════════════════════════════
  协议违规 / 工具错误 / LLM 错误 的回退路径
═══════════════════════════════════════════════════════

  ┌──────────────────┐
  │ PROTOCOL_RETRY   │  ← 未知工具、废弃工具、
  │                  │    content 无 tool_calls、
  │ max 1 次重试     │    多个 tool_calls、
  │                  │    组合违规
  │ → CALL_LLM      │
  │   (带错误提示)   │
  └──────────────────┘

  ┌──────────────────┐
  │ HARD_FALLBACK    │  ← LLM 超时、API 错误、
  │                  │    上下文 FATAL、
  │ 固定文案:        │    连续协议违规
  │ "我刚刚卡了一下  │
  │  重新说"         │
  │                  │
  │ → GUARD_OUTPUT   │
  │ → EXEC_SEND_ONE  │
  │ → END_FALLBACK_  │
  │   SENT           │
  └──────────────────┘

  ┌──────────────────┐
  │ FLASH_TO_PRO_    │  ← flash 模型失败时
  │ UPGRADE          │    升级到 pro 模型重试
  │                  │
  │ → CALL_LLM      │
  │   (用 pro 模型)  │
  └──────────────────┘
```

### Tool Call 循环的关键约束

```
┌─────────────────────────────────────────────────────┐
│  约束                           │ 值                  │
├─────────────────────────────────────────────────────┤
│  每次 LLM 响应最多 tool calls   │ 1 (严格单工具)      │
│  MAX_LLM_STEPS_PER_TURN        │ 5                   │
│  MAX_PROTOCOL_RETRIES          │ 1                   │
│  MAX_REPLY_MESSAGES_PER_TURN   │ 8                   │
│  MAX_SEND_FAILURES_PER_TURN    │ 3                   │
│  turn timeout                  │ 可配置               │
├─────────────────────────────────────────────────────┤
│  FACT_TOOL 结果                │ 回灌 LLM            │
│  ACTION_TOOL 结果              │ 继续 LLM 循环       │
│  TERMINAL_TOOL (done)          │ 结束 turn           │
│  PROTOCOL_RETRY 最多           │ 1 次                │
│  HARD_FALLBACK                 │ 使用固定文案         │
│  send_wechat_messages          │ 已废弃 (v1.5)       │
│  skip_reply                    │ 已废弃 (v1.5)       │
└─────────────────────────────────────────────────────┘
```
