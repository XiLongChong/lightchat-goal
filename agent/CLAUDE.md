# CLAUDE.md

本文件夹包含 LLM 交互层的全部代码——从 LLM 调用到工具执行到回复发送。

## 文件职责

```
chat_agent.py              — 核心对话循环：委托 ContextBuilder → LLM 调用 → 工具执行 → guard → 发送
state_machine.py           — Turn 状态类型：TurnScratchpad, TurnResult, TurnState, ModelProfile, ModelAttempt, VisibleMessage, DeliveryStatus
llm_gateway.py             — 供应商无关的 LLM 网关：DeepSeek 适配器 + trace 回调
llm_types.py               — LLM 请求/响应数据类：LLMRequest, LLMResponse, LLMUsage, LLMError
providers/base.py          — ProviderAdapter 协议 + ProviderConfig
providers/deepseek.py      — DeepSeek 适配器（httpx 直接调用 Anthropic 兼容 SSE 端点，OpenAI SDK 仅复用错误类型）
providers/__init__.py      — Provider 包初始化
compressor.py              — 每日对话压缩：伪造 user 消息触发总结，摘要写入 ConversationContext，DB 只存摘要（active=1）
context_builder.py         — 全局协议文案 + build_chat_system_prompt 工具函数
runtime_context_builder.py — ContextBuilder：build_for_chat() 统一入口，收拢全部 context 构造逻辑
tool_registry.py           — ToolDef 注册表 + native tool call spec 生成（含受限 memory 工具）
tool_exposure_policy.py    — 每 turn 暴露哪些工具（当前固定暴露全部）
tools.py                   — 工具实现分发（get_time, get_weather, memory, web_search, todo, reminder, proactive）
weather_provider.py        — 天气 Provider 协议 + QWeather 实现（GeoAPI + 实时 + 预报 + 三级缓存）
search_provider.py         — Tavily Web 搜索 + 网页抓取 Provider，带本地 LRU 缓存
todo_provider.py           — Microsoft To Do Provider — Graph API 任务管理（Device Code Flow 认证）
output_guard.py            — 输出清洗：敏感信息过滤、长度截断、Markdown 去格式化
__init__.py                — Agent 包初始化
```

## 对话流程（chat_agent.chat）

1. 创建 TurnScratchpad，写 turn_started 事件（DB + 内存）
2. 构建上下文（`ContextBuilder.build_for_chat(prebuilt_messages=...)` — 从 ConversationContext 取消息数组，一次调用完成 prompt 构造）
3. 循环（最多 MAX_LLM_STEPS_PER_TURN=20 轮）：
   - 通过 LLMGateway 调用 LLM（stream=True）
   - 累积 tool_calls，执行工具
   - 每个事件即写 raw_events + 追加 ConversationContext
   - 如果有 send_text_message 结果，加到 visible_messages
   - 检查 interrupt_checker
   - 无 tool_call 时隐式结束 turn
4. 通过 wechat_sender 回调发送消息
5. 执行 post_turn_hooks（诊断日志、模型尝试日志）
6. 返回 TurnResult

## 关键常量

- `MAX_LLM_STEPS_PER_TURN = 20` — 单 turn 最多 LLM 调用轮次
- `MAX_REPLY_MESSAGES_PER_TURN = 10` — 单 turn 最多回复条数（对齐 iLink 回复窗口上限）
- `MAX_MESSAGE_LEN = 500` (output_guard) — 单条消息最大字符数

## 工具注册（tool_registry）

模块加载时注册 13 个工具：

| 工具名 | 功能 | 类别 | 副作用 |
|--------|------|------|--------|
| `send_text_message` | 发送文本消息给用户 | action | high |
| `get_time` | 获取时区当前时间 | fact | none |
| `get_weather` | 获取城市实时天气或预报 | fact | none |
| `memory` | 读取/维护长期记忆文件（view/create/str_replace） | memory | high |
| `web_search` | 搜索网页信息（Tavily） | fact | none |
| `web_fetch` | 抓取特定网页完整内容 | fact | none |
| `todo_list_lists` | 获取 Microsoft To Do 任务列表 | fact | none |
| `todo_list_tasks` | 获取任务列表中的任务 | fact | none |
| `todo_create_task` | 在 Microsoft To Do 中创建任务 | action | high |
| `todo_complete_task` | 标记任务为完成 | action | high |
| `create_reminder` | 创建定时提醒 | action | high |
| `list_reminders` | 列出当前用户的待处理提醒 | fact | none |
| `cancel_reminder` | 取消待处理提醒 | action | high |
| `schedule_next_proactive` | 建议下次主动检查延迟（仅 proactive turns） | action | high |

ToolDef 新增字段：
- `side_effect_level`: "none" | "high" — 副作用级别
- `tool_category`: "fact_tool" | "action_tool" | "memory_tool" — 工具类别
- `native_tool_call_eligible`: bool — 是否支持原生 tool call

## 子系统

### Web 搜索（search_provider.py）
- **Provider**: Tavily (`TAVILY_API_KEY`)
- **API**: `/search` + `/extract`
- **缓存**: 本地 LRU（默认 5 分钟 TTL）
- `web_search(query, max_results, include_answer)` — 返回来源摘要
- `web_fetch(url)` — 抓取单页完整 Markdown

### Microsoft To Do（todo_provider.py）
- **认证**: Azure AD Device Code Flow（`msal-extensions` 加密 token 缓存）
- **API**: Microsoft Graph API v1.0
- 支持操作：get_lists, get_tasks, create_task, complete_task
- 带模糊匹配和歧义消解

### 天气（weather_provider.py）
- **Provider**: QWeather（`QWEATHER_API_KEY`）
- **三级缓存**: 位置（24h）+ 实时观测（10min）+ 预报（30min）
- **智能歧义消解**: 同名城市启发式判断（排名差 3 倍、同省下属区县自动消除）
- `WeatherProvider` 协议化设计，支持未来替换 Provider

### 定时提醒
- 工具实现入口在 `tools.py`（create_reminder / list_reminders / cancel_reminder）
- 后端持久化在 `memory/reminder_store.py`（独立 SQLite 表）
- 调度由 `runtime/task_scheduler.py`（APScheduler）驱动

### 主动对话调度
- `schedule_next_proactive` 仅在 `source in {"proactive", "proactive_speak"}` 时暴露
- 设置 `context.turn.proactive_interval_candidate` 属性
- 由 TaskScheduler 按时间窗口和冷却机制触发

## 注意事项

- `chat_agent.py` 是脏代码最集中的地方——约 480 行，包含完整的 LLM 对话循环和 fallback 逻辑
- LLM 请求使用 `stream=True`，但工具调用的流式处理需要 `response_mode="text"`
- output_guard 会过滤 API key 泄露、tool_call_id 泄露、Markdown 格式
- 每日 Compact 不运行工具循环。压缩模型一次返回 `<summary>` 与 `<memory_operations>`，runtime 校验证据和数量后调用长期记忆文件服务执行
