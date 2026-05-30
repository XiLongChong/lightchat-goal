# Goal Chat Agent — Agent 入口

V1.2 (2026-05-30): Level-1 状态机改造完成 — native tool calls 主链路、六态送达模型、中断检查、统一错误分类、post-terminal pipeline。V2.1 主动消息管道 + 多 bot 架构保持兼容。

你是 AI agent（或者是代替人类开发者读这份代码的人）。这里告诉你这个项目的骨架在哪、怎么跑起来、改东西从哪里下手。

## 一句话概括

单进程 Python + FastAPI 服务。DeepSeek 驱动一个可配置 bot，通过微信 iLink 跟用户聊天。有长期记忆、主动消息、定时提醒。所有数据存一个 SQLite 文件，零云依赖。

## 跑起来

```
# 最简启动
pip install -r requirements.txt
python main.py

# 或者 Docker
docker-compose up -d
```

服务默认在 `http://0.0.0.0:8899`。

环境变量在 `.env`，必填项：`DEEPSEEK_API_KEY`。参考 `.env.example`。

## 目录骨架

```
goal/
├── main.py                  ← 主入口。FastAPI 所有路由 + interrupt_checker 注入 + post-terminal callback 构造
├── config.yaml              ← 所有配置（支持 ${ENV_VAR} 替换）
├── prompts/                 ← 提示词文件
│   ├── persona.md           ← V2 旧人设（仍可热生效，default_bot 用 identity 文件）
│   ├── base_protocol.md     ← V2.1 基础协议：输出格式、工具调用规则、安全规则
│   ├── bot_identity_default.md ← V2.1 default_bot 人设（身份、语气、风格）
│   ├── relationship_context.md ← V2.1 关系上下文模板（运行时注入参数）
│   ├── memory_policy.md     ← V2.1 记忆查询策略
│   ├── tool_policy.md       ← V2.1 工具约束（可用工具清单运行时注入）
│   └── trigger_context.md   ← V2.1 触发源模板（user/proactive/morning/night/...）
│
├── agent/                   ← 核心 AI 逻辑
│   ├── state_machine.py     ← V1.2 状态机类型：TurnState 枚举、TurnScratchpad、TurnResult、DeliveryStatus 六态模型、VisibleMessage、InterruptChecker、PostTerminalCallback
│   ├── chat_agent.py        ← V1.2 Level-1 状态机主循环（23 个 state handler + post-terminal pipeline）
│   ├── tool_registry.py     ← V1.2 ToolDef / ToolRegistry / tool_category 注册（fact_tool / terminal_tool / state_tool）
│   ├── tool_classifier.py   ← V1.2 工具调用分类：FACT_TOOL / TERMINAL_TOOL / STATE_TOOL / 组合合法性检查
│   ├── tools.py             ← 工具实现（get_time / get_weather / compute_message_delay / execute_tool）
│   ├── context_builder.py   ← 拼系统提示词 + 历史压缩
│   ├── output_guard.py      ← 统一输出守卫（协议泄露检测 / 敏感信息过滤 / markdown 清理 / profanity guard / batch guard）
│   ├── memory_worker.py     ← 后台异步提取记忆（V2.1 bot-scoped）
│   ├── bot_profile.py       ← V2.1 Bot 画像管理（BotProfile / BotProfileManager / prompt pack 组装）
│   └── scheduler.py         ← 主动消息调度（V2.1.1 due-time + intent 管道）
│
├── bridge/                  ← 微信桥接
│   ├── ilink_client.py      ← iLink API 客户端（从 TypeScript 移植）
│   └── wechat_poller.py     ← 微信消息轮询 + 缓冲去抖
│
├── memory/                  ← 记忆系统
│   ├── store.py             ← SQLite 记忆存储（WAL 模式，V2.1 四层记忆 + bot 隔离）
│   └── dedup.py             ← CJK 二元组 Jaccard 去重
│
├── utils/
│   └── json_parser.py       ← 安全 JSON 解析（多层回退）
│
├── static/                  ← 管理界面（纯 HTML/CSS/JS，无框架）
│   ├── admin.html
│   ├── admin.css
│   └── admin.js
│
├── tests/                   ← pytest (337 passed, 5 skipped)
│   ├── test_chat_agent.py   ← 91 测试（状态机生命周期、中断、协议、工具、发送链路、batch guard）
│   ├── test_output_guard.py
│   ├── test_tool_registry.py
│   ├── test_message_actions.py
│   └── ...
│
└── data/                    ← 运行时数据（gitignore 忽略）
    ├── memory.db            ← SQLite 数据库
    └── agent.log            ← 日志
```

## V1.2 架构概览：一次对话怎么走的

```
微信消息 → wechat_poller → /chat 端点
  → _record_user_message_arrival()        ← V1.2 中断检查基础
  → ChatAgent.chat()
      │
      ├─ RECEIVE_EVENT                    ← 接收事件
      ├─ CREATE_TURN                      ← 分配 turn_id + interrupt_token
      ├─ INTERRUPT_CHECK                  ← 防止旧回复干扰（6 处检查点）
      ├─ BUILD_CONTEXT                    ← 组装 ContextBuildResult（messages + warnings + fatal_error）
      ├─ CLASSIFY_CONTEXT_ERROR           ← 区分 RECOVERABLE / FATAL
      ├─ OPTIONAL_PREPROCESS              ← 预取时间（不预取天气）
      ├─ CALL_LLM                         ← 调 DeepSeek API（native tool calls 模式）
      ├─ HANDLE_MODEL_OUTPUT              ← assistant.content only → PROTOCOL_RETRY
      ├─ CLASSIFY_TOOL_CALLS              ← FACT_TOOL / TERMINAL_TOOL / STATE_TOOL / INVALID
      │       │
      │       ├─ FACT_TOOL (get_time, get_weather)
      │       │   → asyncio.gather 并行执行
      │       │   → INTERRUPT_CHECK → CLASSIFY_TOOL_ERROR → CALL_LLM
      │       │
      │       ├─ STATE_TOOL (未注册，严格拒绝)
      │       │   → 无注册 → PROTOCOL_RETRY
      │       │   → 未来注册 → INTERRUPT_CHECK → CLASSIFY_TOOL_ERROR → CALL_LLM
      │       │
      │       └─ TERMINAL_TOOL (send_wechat_messages / skip_reply)
      │           ├─ skip_reply → EXEC_SKIP_REPLY → END_SKIPPED
      │           └─ send_wechat_messages
      │               → PARSE_VISIBLE_BATCH   ← 创建 VisibleMessage（逐条 ID）
      │               → INTERRUPT_CHECK
      │               → GUARD_OUTPUT          ← 逐条 guard_single_message + batch 级 max_count/max_total
      │               → INTERRUPT_CHECK
      │               → EXEC_SEND_WECHAT      ← 五态送达映射
      │               → SEND_RETRY_UNDELIVERED_ONLY ← 仅重试 FAILED_BEFORE_SEND / FAILED_CONFIRMED
      │               → DECIDE_SEND_TERMINAL
      │                   ├─ 全部 SENT_CONFIRMED → END_SENT
      │                   ├─ 部分 SENT_CONFIRMED → END_PARTIAL_SENT
      │                   └─ 无 SENT_CONFIRMED   → END_FAILED
      │
      ├─ HARD_FALLBACK（异常路径）
      │   → INTERRUPT_CHECK → guard_single_message → EXEC_FALLBACK_SEND
      │   → 成功 → END_FALLBACK_SENT / 失败 → END_FAILED
      │
      └─ [terminal state 确定]
          → WRITE_MODEL_HISTORY callback   ← 按终态矩阵写模型历史（V1.2 §15.1）
          → POST_TURN_HOOKS callbacks      ← UPDATE_SUMMARY / UPDATE_MEMORY / UPDATE_EMOTION / WRITE_DEV_LOGS / QUALITY_METRICS
          → TurnResult 组装返还
```

## V1.2 关键架构决策

### 模型输出协议

- 模型必须通过 **native tool calls** 调用 `send_wechat_messages` 或 `skip_reply` 结束回合
- `assistant.content`（普通文本）永不直发微信 → PROTOCOL_RETRY
- 旧 JSON actions（`wechat_reply`、`skip`）永不解析 → detected → PROTOCOL_RETRY
- `send_wechat_messages` 结果不回灌模型
- `skip_reply` 必须是显式工具调用，禁止用空消息列表猜测

### 工具分类

| 类型 | 注册表 | 执行后去向 | 结果回灌 | 并行 |
|---|---|---|---|---|
| FACT_TOOL | get_time, get_weather | INTERRUPT_CHECK → CLASSIFY_TOOL_ERROR → CALL_LLM | 是 | 是 (asyncio.gather) |
| STATE_TOOL | 未注册（严格拒绝） | INTERRUPT_CHECK → CLASSIFY_TOOL_ERROR → CALL_LLM | 否 | 串行 |
| TERMINAL_TOOL | send_wechat_messages, skip_reply | 进入终态分支 | **否** | 禁止多个 |

组合禁止：TERMINAL + 任何其他工具 → PROTOCOL_RETRY；多个 TERMINAL → PROTOCOL_RETRY。

### 送达模型（六态）

| 状态 | 可重试 | 写成功历史 |
|---|---|---|
| `PENDING` | — | 否 |
| `FAILED_BEFORE_SEND` | 是 | 否 |
| `UNKNOWN_AFTER_ATTEMPT` | **否** | 否 |
| `SENT_CONFIRMED` | 否 | 是 |
| `FAILED_CONFIRMED` | 是 | 否 |
| `BLOCKED_BY_GUARD` | 否 | 否 |

### 中断检查（6 处）

1. CREATE_TURN 后  2. CALL_LLM 返回后  3. EXEC_FACT_TOOLS 返回后
4. GUARD_OUTPUT 前  5. EXEC_SEND_WECHAT 前  6. HARD_FALLBACK 发送前

interrupt_token 格式：`it_{turn_id}_{timestamp_ms}`。Checker 比较 token 时间戳与 `_user_latest_message_ts`。生产环境必须注入 checker（test_mode 可用 always_false）。

### Post-terminal pipeline（ChatAgent 内部，TurnResult 组装前执行）

```
terminal_state 确定
→ WRITE_MODEL_HISTORY（callbacks 注入，按终态矩阵写用户 + assistant 历史）
→ POST_TURN_HOOKS（callbacks 注入，5 钩子串行，单失败不阻塞）
→ TurnResult 组装
```

main.py 不再拥有终态历史写入判断。传输层操作（L2 turn 记录、contact state 更新、turn draft）仍在 main.py。

### 硬兜底规则

- 使用固定系统文案 `"我刚刚卡了一下 重新说"`（永不使用模型原文）
- 必须经过 GUARD_OUTPUT
- 成功 → `END_FALLBACK_SENT`（不算 END_SENT）
- 失败 / 无 sender → `END_FAILED`
- 如写历史，必须标记 `source="assistant:fallback"`，不能写成普通 assistant

## V2.1 保持兼容的功能

### Bot 模型

```
WeChat 消息 → resolve current_bot_id → 加载 bot 画像
→ 加载 bot prompt pack → 加载 bot-scoped 记忆层
→ 对话 → 通过同一微信入口回复
```

### 记忆四层 (L1-L4)

```
L1 raw_messages  → 每条原始消息（审计、恢复）
L2 turns         → 语义化对话回合（user batch + bot reply batch + 工具调用 + 元数据）
L3 recap         → 会话阶段摘要（每 N turns 后台 LLM 生成，用于恢复上下文）
L4 long_term_memory → 稳定事实、偏好、项目状态（现有 memories 表，bot-scoped）
```

### 主动消息 V2.1.1 due-time + intent 管道

```
Stage 1: hard_precheck      → sleep hours / bridge / contact / daily cap
Stage 2: due_time_check     → next_due_ts 已到？
Stage 3: load_latest_topic  → 仅加载活跃话题
Stage 4: select_intent      → check_in / latest_topic_followup / self_share / light_ping
Stage 5: generate_message   → LLM 生成消息文本
Stage 6: send_and_record    → 发送 + 记录候选
Stage 7: schedule_next_due  → 随机下一次到期时间
```

### 出站消息语义

每条出站消息携带：`source`（user_reply/fixed_morning/scheduled_reminder/proactive/admin）、`author`（assistant/system）、`trigger`、`visible_to_user`、`delivery_status`（V1.2 六态模型）。

## 从哪里下手改

| 你想改什么 | 看哪个文件 |
|-----------|-----------|
| 状态机枚举 / TurnScratchpad / VisibleMessage | `agent/state_machine.py` |
| 对话逻辑 / state handler / 发送链路 / 中断检查 | `agent/chat_agent.py` |
| 工具注册 / tool_category / native_tool_call_eligible | `agent/tool_registry.py` |
| 工具分类 / 组合合法性 | `agent/tool_classifier.py` |
| 工具实现（get_time / get_weather） | `agent/tools.py` |
| 输出保护 / batch guard / profanity | `agent/output_guard.py` |
| 怎么拼提示词 / 历史压缩 | `agent/context_builder.py` |
| AI 的性格/说话方式 | `prompts/persona.md` |
| Bot 画像 / 提示词分包 | `agent/bot_profile.py` |
| 记忆怎么存 / 怎么搜 / 怎么衰减 | `memory/store.py` + `memory/dedup.py` |
| 主动消息 V2.1.1 due-time + intent 管道 | `agent/scheduler.py`（`run_proactive_pipeline` 是入口） |
| interrupt_checker / post-terminal callbacks | `main.py` |
| API 路由 / 管理面板 | `main.py`（所有 `@app.get/post/delete`） |
| 微信接入细节 | `bridge/ilink_client.py` + `bridge/wechat_poller.py` |
| 管理面板 UI | `static/admin.html` + `admin.css` + `admin.js` |
| 配置项含义 | `config.yaml`（中文注释很全） |

## API 路由速查

```
GET  /health               # 健康检查
POST /chat                 # 主聊天端点（微信消息最终走到这里）
GET  /admin                # 管理面板页面
GET  /admin/config         # 查看配置
POST /admin/persona        # 保存人设
POST /admin/settings       # 运行时设置
POST /admin/send           # 手动发消息
GET  /admin/bots           # V2.1 列出所有 bot
POST /admin/bots/switch    # V2.1 切换 active bot
GET  /admin/recaps         # V2.1 查看 recap 列表
POST /admin/recaps/generate # V2.1 手动触发 recap 生成
GET  /admin/turns          # V2.1 查看 L2 turns
GET  /admin/outbound       # V2.1 查看出站消息追踪
GET  /admin/proactive/candidates # V2.1 查看主动消息候选记录
GET  /admin/proactive/state    # V2.1.1 查看主动调度状态
GET  /admin/proactive/topic    # V2.1.1 查看当前活跃话题
POST /admin/proactive/trigger  # V2.1.1 手动触发主动消息
POST /admin/proactive/topic/close # V2.1.1 手动关闭活跃话题
GET  /admin/shared-facts   # V2.1 查看共享用户事实
GET  /admin/diagnostics    # V2 查看 turn 诊断
GET  /admin/drafts         # V2 查看 turn drafts
GET  /memories             # 列出记忆
POST /memories/import      # 导入记忆
DELETE /memories/{id}      # 删记忆
GET  /history/{user_id}    # 查看聊天记录
DELETE /history/{user_id}  # 清除当前 bot 历史
GET  /wechat/qr            # 获取微信登录二维码
POST /wechat/deploy/start  # 开始部署微信
POST /wechat/start         # 启动微信桥接
POST /wechat/stop          # 停止微信桥接
POST /scheduler/trigger    # 手动触发一轮调度
GET  /scheduler/state      # 调度器状态
GET  /relationship         # 查看关系数据
```

## 关键技术选型

- **Native tool calls（原生工具调用）**：模型通过 API `tool_calls` 字段返回结构化动作，后端不解析 assistant.content 中的 JSON
- **VisibleMessage + 六态送达模型**：每条微信消息独立 ID，逐条追踪 PENDING/FAILED_BEFORE_SEND/UNKNOWN_AFTER_ATTEMPT/SENT_CONFIRMED/FAILED_CONFIRMED/BLOCKED_BY_GUARD
- **InterruptChecker token 机制**：每 turn 生成 `it_{turn_id}_{timestamp_ms}` token，6 处关键检查点比较时间戳
- **Post-terminal pipeline**：WRITE_MODEL_HISTORY + POST_TURN_HOOKS 在 ChatAgent 内部执行，main.py 通过 PostTerminalCallback 注入
- **CJK 二元组 Jaccard 相似度**：中文记忆去重和检索，不需要嵌入模型
- **SQLite + WAL**：单文件，写入快，不需要外部数据库
- **即发即弃记忆提取**：回复用户后才异步提取记忆，不阻塞消息返回
- **V2.1 四层记忆**：raw → turn → recap → long-term，bot 隔离
- **V2.1 提示词分包**：6 个独立 .md 文件，bot 画像可替换 identity 文件

## 生产安全保障

### V1.2 核心边界

1. 所有微信可见输出必须走：TERMINAL_TOOL → GUARD_OUTPUT → EXEC_SEND_WECHAT → 发送终态
2. assistant.content 永不直发微信（→ PROTOCOL_RETRY）
3. 旧 JSON actions 永不解析（→ PROTOCOL_RETRY）
4. send_wechat_messages 结果不回灌模型
5. 硬兜底使用固定文案，不绕过 GUARD_OUTPUT
6. 中断检查 6 处覆盖，生产环境强制注入 checker
7. 上下文错误分类（RECOVERABLE → 降级继续，FATAL → HARD_FALLBACK）
8. 工具错误分类（success/recoverable → CALL_LLM，invalid_args → PROTOCOL_RETRY，fatal → HARD_FALLBACK）
9. batch guard 直接作用于 VisibleMessage 列表（max_count 截断 → BLOCKED_BY_GUARD）
10. UNKNOWN_AFTER_ATTEMPT 不重试不写成功历史
11. END_PARTIAL_SENT 只写 SENT_CONFIRMED 消息到模型历史
12. END_FALLBACK_SENT 历史标记为 assistant:fallback，不写普通 assistant
13. WRITE_DEV_LOGS 在 POST_TURN_HOOKS 中，不污染模型历史
14. POST_TURN_HOOKS 单失败不阻塞，不改本轮终态，不补发微信

### V2.1 安全边界

- 系统触发任务不进 user role（trigger != "user" → system role）
- bot 间记忆/关系/历史/摘要完全隔离（bot_id 做 key）
- 决策门纯逻辑拦截（不浪费 LLM 调用）
- enabled_tools 在 prompt 和执行层双重拦截
- 出站消息生命周期追踪

### 常量限制

- MAX_TOOL_LOOP = 5（最多 5 轮 LLM 调用）
- MAX_MESSAGE_COUNT = 8（batch guard 最大消息数）
- MAX_MESSAGE_LEN = 500（单消息最大字符）
- MAX_TOTAL_CHARS = 2000（batch 总字符上限）
- max_protocol_retries = 1（协议违规最多重试 1 次）
- max_send_retries = 2（发送最多重试 2 次）

## 跑测试

```
pytest tests/ -v
```

测试覆盖：337 passed, 5 skipped（Windows tmp_path 权限，非代码缺陷）。覆盖状态机生命周期、中断检查、上下文错误分类、工具分类与组合、协议硬化、FACT_TOOL 并行、五态送达模型、batch guard 集成、硬兜底、skip_reply 路径、输出守卫。

## Admin UI Design Direction

The `/admin` UI follows a WeChat-style personal management console. Keep this
style for future admin work unless the product direction changes.

- Language: Chinese UI by default. Labels should be short and direct.
- Layout: desktop uses a quiet left sidebar plus content panels; mobile uses a
  fixed bottom navigation.
- Visual style: soft WeChat-like green background, white panels, 8-10px radius,
  restrained shadows, no decorative gradients or oversized marketing sections.
- Information density: avoid explanatory subtitles and dense small text. Prefer
  short section titles, status pills, metric cards, chat bubbles, and compact
  rows.
- Conversation diagnostics: represent user/model/system events as chat bubbles
  when possible. Use structured Chinese summaries before raw logs.
- Settings: persona editing should be one large textarea. Runtime settings stay
  grouped by actual feature: proactive, WeChat/tools, email, manual send/tasks.
- Memory/proactive/logs: keep these as first-class admin pages. Do not hide
  proactive candidates, active topic state, memory import/export, recap
  generation, email settings, or raw logs when changing the UI.
- Mobile: every core page must remain reachable and must not require horizontal
  scrolling at 390px width.
