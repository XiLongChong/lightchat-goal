# CLAUDE.md

## Current AsPocket Router scope (2026-06-21)

The App adapter is no longer limited to `/api/profile` and `/api/messages`.
Current AsPocket Router capabilities include:

- App contact invitations
- App message send/history with `remoteAgentId`
- image attachment upload/download
- sticker attachment validation
- attached-session callback routing
- Claude Code channel routing
- cached Phone Context snapshots and `aspocket_get_phone_context`

For current review/planning, use:

- `docs/done/20260621/20260621-current-capabilities-and-upgrade-map.md`

Keep protocol types generic. Do not create a bespoke Router protocol per external agent; normalize
toward generic `internal`, `callback`, and `channel` concepts while preserving compatibility.

Phone Context privacy boundary: do not request or store message content, typed text, contact lists,
location, SSID, IP address, clipboard contents, media library contents, or health data as phone
context without a new explicit design review.

Goal Chat Agent — 基于 DeepSeek LLM 的微信聊天机器人，单进程 FastAPI + SQLite。

## 项目规则

- **不要删除 `bot_contact_state`** — 包含微信 iLink 的 context_token，丢失后需重新扫码部署
- **不要新建文档文件（*.md）** 除非明确要求。根目录只保留此文件和 README.md
- **不要引入新的依赖** 除非不可避免。当前依赖见 requirements.txt
- **配置优先级**：命令行/环境变量 > runtime_settings.json > config.yaml 默认值
- **日志**：`./data/agent.log`，可通过 admin UI 查看（`/admin/logs`）
- **Windows 环境注意**：PowerShell 5.1，无 `&&`/`||`，无 tail/head，需用对应 PowerShell 语法

## 常用命令

```bash
python main.py                       # 启动服务
pytest tests/ -v                     # 全部测试
pytest tests/ -v -k "test_name"      # 按名称过滤
python scripts/reset_runtime_data.py # 预览清除
python scripts/reset_runtime_data.py --confirm  # 确认清除
```

## 目录索引

| 目录 | 职责 | 详情 |
|------|------|------|
| `agent/` | LLM 交互层（核心对话循环、工具执行、输出 guard） | [agent/CLAUDE.md](agent/CLAUDE.md) |
| `bridge/` | 微信 iLink 桥接（长轮询、消息收发） | [bridge/CLAUDE.md](bridge/CLAUDE.md) |
| `memory/` | SQLite 持久化（联系人、历史、诊断） | [memory/CLAUDE.md](memory/CLAUDE.md) |
| `runtime/` | Chat runtime 薄封装 | [runtime/CLAUDE.md](runtime/CLAUDE.md) |
| `runtime_contracts/` | 共享数据契约类型 | [runtime_contracts/CLAUDE.md](runtime_contracts/CLAUDE.md) |
| `tests/` | 测试套件 | [tests/CLAUDE.md](tests/CLAUDE.md) |
| `scripts/` | 开发辅助脚本 | [scripts/CLAUDE.md](scripts/CLAUDE.md) |
| `static/` | 管理后台前端 | [static/CLAUDE.md](static/CLAUDE.md) |
| `prompts/` | LLM 提示词模板 | [prompts/CLAUDE.md](prompts/CLAUDE.md) |
| `tools/` | 外部工具/通道（Claude Code mock adapter、AsPocket channel 脚本） | — |
| `docs/` | 项目文档（demo/doing/done/bug） | [docs/CLAUDE.md](docs/CLAUDE.md) |

## 数据流

### 微信桥接路径（一次用户消息的完整路径）

```
微信 → iLink 长轮询 → bridge/wechat_poller
  → HTTP POST /chat → main.py（认证 → 校验 → 获取锁 → 构建上下文）
  → runtime/chat_runtime → agent/chat_agent.chat()
    → agent/runtime_context_builder（构建系统 prompt）
    → agent/llm_gateway → agent/providers/deepseek（LLM 调用）
    → agent/tools（工具执行）
    → agent/output_guard（消息清洗）
    → bridge/sender 回调 → iLink 发送 → 微信
  → memory/store（持久化 protocol transcript + 诊断）
  → 释放锁 → 返回响应
```

### App 适配器路径（AsPocket Android App）

```
App → POST /api/messages?remoteAgentId=... → main.py
  ├─ 认证 + 幂等检查（clientMessageId）
  ├─ 附件验证（image / sticker）
  ├─ resolve_app_remote_agent → 三路路由
  │   ├─ [default] → chat_runtime.handle_chat → 同微信路径
  │   ├─ [attached_session] → HTTP POST callbackUrl/message
  │   └─ [claude_channel] → channel_pending_inbox → 长轮询拉取
  ├─ 55s 超时保护
  └─ 返回 {userMessage, agentReplies}

Claude Code 长轮询：
  GET /api/app-agent-sessions/{id}/pending
  POST /api/app-agent-sessions/{id}/ack
  POST /api/app-agent-sessions/{id}/replies
```

## 关键概念

- **Android App 适配器端点**：`main.py` 中为 AsPocket Android 客户端暴露的 REST 端点群，
  与 `/chat` 的 iLink/微信桥接逻辑解耦。认证同 `/chat`（Bearer / `x-api-key`）。
  完整端点列表：`/api/profile`、`POST/GET /api/messages`、`POST/GET /api/attachments`、
  `/api/contact-invitations`、`POST/GET /api/phone-context/*`、
  `/api/app-agent-sessions/*`（register/heartbeat/offline/replies/pending/ack）。
  `POST /api/messages` 按 `clientMessageId` + `remoteAgentId` 幂等，仅 `send_text_message`
  的已投递输出成为 `agentReplies[]`；Agent 超时 55s。2026-06-17 已与 App 侧
  `docs/api-contract.md` 逐字段核对一致。

- **用户交互轮 (turn)**：从用户发送一条消息开始 → 模型完成回复（发送完毕或异常结束）为止。一次用户消息 = 一个 turn。
- **模型调用 (call/step)**：一次 turn 内可能多次调用 LLM（最大 20 次）。每次收到 `send_text_message` 的 `sent` 结果后，模型判断是否还需要再发一条消息——如需则再调一次 LLM。不能提前预编多条回复。
- **对话历史加载**：`ConversationContext` 内存常驻完整消息数组，`/chat` 不查 DB。仅在重启 / clear / 压缩变更时从 `raw_events` 重建（只加载 `active=1` 的事件）。

## LLM Trace 可观测性

每次 LLM 调用通过 `agent/llm_gateway.py` 的 `_build_trace()` 生成一条扁平 trace dict，
通过回调（`trace_callbacks`）分发到已注册的监听器（如结构化日志、外部可观测系统）。
Trace 不再写入 SQLite（`llm_traces` 表已在 v3 架构中移除）。

| 字段 | 说明 |
|------|------|
| `request_id` | 请求 UUID |
| `turn_id` / `call_id` | 归属的 turn 和本次调用标识 |
| `task_type` | `chat` |
| `provider` / `model` | deepseek / 模型名 |
| `thinking` / `response_mode` | disabled / text |
| `latency_ms` | 延迟（毫秒） |
| `attempt_count` | 重试次数 |
| `success` | 是否成功 |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | token 用量 |
| `cache_hit_tokens` / `cache_miss_tokens` | 缓存命中/未命中 |
| `reasoning_tokens` | 推理 token |
| `tools_exposed_count` / `tools_exposed_names` | 开放的工具 |
| `tool_calls_returned_count` / `tool_calls_returned_names` | 模型调用的工具 |
| `request_json` | **完整请求体 JSON**（含 messages、system、tools） |
| `response_json` | **完整响应体 JSON**（含 content、tool_calls、usage） |

`request_json` 和 `response_json` 包含发给 provider 和从 provider 收到的原始 JSON。
通过 claude-tap 可以看到相同内容（HTTP 层抓包）。

## 关键机制

- **用户级锁**：每 user_id 一个 asyncio.Lock，同用户串行、跨用户并发（`runtime/user_turns.py`）
- **中断检测**：新消息 seq > 当前 turn 的 input_latest_seq 时取消当前 turn
- **配置**：config.yaml（`${VAR:-default}` 替换）+ .env（密钥）+ data/runtime_settings.json（热加载覆盖）
- **每日压缩**：3:56-4:25 窗口内触发。从内存消息数组追加伪造 user 消息调用 LLM 生成摘要。摘要单独写入 DB（active=1），旧事件软删除（active=0）。ContextBuilder 自动将摘要与下一条用户消息合并。
- **长期记忆**：`data/long_term_memory/<sha256(user_id)[:32]>/` 下保存 `MEMORY.md` 索引和主题 Markdown。模型工具只允许 view/create/str_replace，删除只通过管理后台。
- **压缩与记忆事务**：Compact 同时输出 `<summary>` 和 `<memory_operations>`；文件批次与 SQLite 压缩提交均成功后才替换内存。手动和自动压缩共用同一流程。
- **Schema 迁移**：`runtime_meta` 表存版本号 `v3_daily_compression`，不匹配时删除除 `bot_contact_state` 和 `runtime_meta` 外的所有表重建

### App 适配器机制

- **三路路由**：`resolve_app_remote_agent` 根据 `remoteAgentId` 将消息路由到
  `default`（内置 Agent）、`attached_session`（外部 HTTP 回调）或 `claude_channel`（MCP 通道）。
- **Channel Pending Inbox**：纯内存队列，Claude Code 通过长轮询获取消息，ack 后删除。
  重启丢失，App 可重试（DB 层已持久化消息历史）。
- **Sticker 白名单**：`runtime/sticker_catalog.py` 维护 Phase 1 支持的 8 个 sticker ID。
- **Phone Context 缓存**：App 上传的快照保留最近 50 条，`GET /api/phone-context/latest`
  返回最新有效快照；Claude 通道可通过 `aspocket_get_phone_context` 工具读取。

### 定时与主动机制

- **定时提醒**：`create_reminder` / `list_reminders` / `cancel_reminder` 工具，
  后端由 `memory/reminder_store.py` + `runtime/task_scheduler.py`（APScheduler）驱动。
- **主动对话**：`schedule_next_proactive` 工具仅在 proactive turns 可用，
  由 TaskScheduler 按时间窗口和冷却机制触发。

## 决策与错误记录

- 对架构、协议和重要实现作出选择时，不只记录最终方案，还要在对应设计文档中集中记录被否决或暂缓的方案及原因。
- 开发过程中遇到的错误、失败尝试和关键修复原因，应集中记录到对应任务文档或 bug 文档，避免未来重复犯错。
- 重新讨论已有设计前，先查阅相关文档中的“已否决方案”“已知错误”或同类章节；除非前提已经变化，不重复选择已被否决的路径。
- 记录必须说明当时的约束和取舍，不能只写“不要这样做”，以便未来判断原结论是否仍然适用。
