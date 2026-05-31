# Module Boundaries — 模块边界分析

> 生成日期: 2026-05-31
> 基于实际代码 import 和调用关系

---

## 1. `main.py` — 主入口 + 路由 + 编排

**路径**: `main.py`

**当前职责**:
- FastAPI app 创建和生命周期管理 (lifespan)
- 所有 HTTP 路由定义 (/chat, /admin, /memories, /wechat, 等)
- 配置加载和合并 (config.yaml + runtime_settings.json)
- 全局对象创建 (LLMGateway, MemoryStore, ChatAgent, WeChatBridge)
- per-user 锁管理 (_user_turn_locks)
- 中断检查器工厂 (_make_interrupt_checker)
- 对话历史管理 (conversation_histories, append_history)
- wechat_sender 回调 (微信发送)
- post-terminal 回调构造 (_build_chatagent_write_model_history, _build_chatagent_post_turn_hooks)
- 记忆导入/导出 (parse_memory_upload, build_memory_export_zip)
- 管理面板数据聚合 (build_admin_event_dashboard, build_usage_dashboard)
- 人设变更确认 (handle_pending_persona_confirmation)

**不应该负责什么**:
- 不应包含 LLM 调用逻辑 (已委托给 LLMGateway)
- 不应包含状态机逻辑 (已委托给 ChatAgent)
- 不应包含工具执行逻辑 (已委托给 tools.py)
- 不应包含输出守卫逻辑 (已委托给 output_guard.py)

**输入**:
- HTTP 请求 (/chat, /admin/*, 等)
- config.yaml + .env + runtime_settings.json

**输出**:
- HTTP 响应
- 微信消息 (通过 wechat_sender → WeChatBridge)

**依赖的模块**:
- `agent.chat_agent` — ChatAgent, 路由常量, 上下文策略常量
- `agent.context_builder` — compact_history, build_chat_system_prompt
- `agent.llm_gateway` — LLMGateway
- `agent.llm_types` — LLMRequest
- `agent.output_guard` — guard_messages, guard_single_message
- `agent.state_machine` — ModelProfile, TurnResult, TurnScratchpad, TurnState
- `agent.tools` — configure_runtime, configure_tools, execute_tool, compute_message_delay
- `agent.memory_worker` — MemoryWorker (条件导入)
- `agent.memory_hook` — MemoryHook (条件导入)
- `bridge.wechat_poller` — WeChatBridge, login_qr
- `bridge.ilink_client` — get_qr_code, poll_qr_status (延迟导入)
- `memory.store` — MemoryStore

**被谁调用**:
- uvicorn (ASGI server)
- WeChatBridge (内部 POST /chat)

**当前边界问题**:
1. **main.py 过大** (~2800 行): 聚合了路由、编排、配置、历史管理、记忆导入导出、管理面板数据等, 职责过多
2. **对话历史管理在 main.py**: `conversation_histories` dict 和 `append_history` 函数应在独立模块
3. **post-terminal 回调构造在 main.py**: `_build_chatagent_write_model_history` 和 `_build_chatagent_post_turn_hooks` 是 ChatAgent 的编排逻辑, 但实现在 main.py
4. **wechat_sender 在 main.py**: 微信发送回调的实现细节 (delay, error handling) 应在 bridge 层

---

## 2. `agent/chat_agent.py` — 状态机主循环

**路径**: `agent/chat_agent.py`

**当前职责**:
- v1.5 状态机主循环 (23+ state handler)
- 状态分发 (_dispatch)
- LLM 调用编排 (_do_call_llm)
- 工具分类路由 (CLASSIFY_ONE_TOOL_CALL → FACT/ACTION/TERMINAL)
- FACT_TOOL 并行执行 (_do_exec_fact_tools)
- wechat_reply 验证/守卫/发送链路 (VALIDATE_REPLY_ARGS → GUARD_OUTPUT → EXEC_SEND_ONE)
- done 终态处理 (VALIDATE_DONE_ARGS)
- 协议重试 (PROTOCOL_RETRY)
- 硬兜底 (HARD_FALLBACK)
- 模型路由 (v1.3 _select_model)
- post-terminal pipeline 执行 (_run_post_terminal_pipeline)
- 上下文构建 (_do_build_context)

**不应该负责什么**:
- 不应包含具体工具实现 (已委托给 tools.py)
- 不应包含输出守卫规则 (已委托给 output_guard.py)
- 不应包含 LLM API 调用细节 (已委托给 llm_gateway.py)
- 不应包含微信发送细节 (通过 wechat_sender 回调注入)

**输入**:
- user_message: str
- history: list[dict]
- system_prompt: str
- wechat_sender: 回调函数
- interrupt_checker: 回调函数
- write_model_history / post_turn_hooks: 回调函数

**输出**:
- TurnResult (terminal_state, visible_messages, tool_results, diagnostics)

**依赖的模块**:
- `agent.state_machine` — TurnState, TurnScratchpad, TurnResult, VisibleMessage, 常量
- `agent.tool_classifier` — classify_single_tool_call
- `agent.tool_registry` — TOOL_REGISTRY
- `agent.llm_types` — LLMRequest (延迟导入)
- `agent.output_guard` — guard_single_message (通过导入)

**被谁调用**:
- `main.py` — `chat_agent.chat()`

**当前边界问题**:
1. **_do_build_context 在 ChatAgent 内**: 上下文构建逻辑 (system_prompt + history + user_message 组装) 可以独立为 ContextBuilder, 但目前直接嵌入状态机
2. **输出守卫直接调用**: ChatAgent 直接调用 `guard_single_message`, 而非通过独立的守卫层
3. **硬兜底文案硬编码**: `HARD_FALLBACK_TEXT` 定义在 output_guard.py, 但在 chat_agent.py 中也引用

---

## 3. `agent/state_machine.py` — 类型定义

**路径**: `agent/state_machine.py`

**当前职责**:
- TurnState 枚举 (所有状态)
- TurnScratchpad (turn 本地可变状态)
- TurnResult (turn 结果, 不可变)
- VisibleMessage (单条可见消息)
- DeliveryStatus (六态送达模型)
- ContextBuildResult
- ModelProfile, ModelAttempt
- InterruptChecker / PostTerminalCallback 类型
- 常量 (MAX_LLM_STEPS_PER_TURN, MAX_PROTOCOL_RETRIES_PER_TURN, 等)
- classify_error 函数

**不应该负责什么**:
- 不应包含任何业务逻辑 (纯类型定义)
- 不应包含状态处理函数

**输入**: 无 (纯类型模块)

**输出**: 类型定义, 供其他模块 import

**依赖的模块**: 无 (仅 Python 标准库)

**被谁调用**:
- `agent.chat_agent`
- `main.py`
- 多个 test 文件

**当前边界问题**: 无明显问题, 职责清晰

---

## 4. `agent/tool_registry.py` — 工具注册表

**路径**: `agent/tool_registry.py`

**当前职责**:
- ActionDef / ToolDef 数据类
- ActionRegistry / ToolRegistry 类
- TOOL_REGISTRY 全局实例
- 工具注册 (get_time, get_weather, wechat_reply, done)
- 工具查询 (get_tool, iter_defs, is_action, is_external_tool)
- native_tool_call_eligible 标记
- tool_category 分类 (fact_tool, action_tool, terminal_tool, state_tool)

**不应该负责什么**:
- 不应包含工具执行逻辑 (已委托给 tools.py)
- 不应包含工具分类逻辑 (已委托给 tool_classifier.py)

**输入**: 工具定义 (在模块加载时注册)

**输出**: 工具定义查询结果

**依赖的模块**: 无

**被谁调用**:
- `agent.chat_agent` — 查询工具定义
- `agent.tools` — 获取工具列表
- `agent.tool_classifier` — 查询工具分类
- 多个 test 文件

**当前边界问题**: 无明显问题, 职责清晰

---

## 5. `agent/tool_classifier.py` — 工具分类

**路径**: `agent/tool_classifier.py`

**当前职责**:
- ToolCategory 枚举 (FACT/ACTION/TERMINAL/STATE)
- ClassifiedSingleToolCall 数据类
- classify_single_tool_call 函数
- 废弃工具提示 (_DEPRECATED_HINTS)

**不应该负责什么**:
- 不应包含工具执行逻辑
- 不应包含工具注册逻辑

**输入**: tool_call 对象 (来自 LLM 响应)

**输出**: ClassifiedSingleToolCall (分类结果)

**依赖的模块**:
- `agent.tool_registry` — TOOL_REGISTRY (查询工具定义)

**被谁调用**:
- `agent.chat_agent._do_classify_one_tool_call()`

**当前边界问题**: 无明显问题, 职责清晰

---

## 6. `agent/tools.py` — 工具实现

**路径**: `agent/tools.py`

**当前职责**:
- get_time 实现 (_get_time)
- get_weather 实现 (_get_weather)
- execute_tool 调度函数
- compute_message_delay 计算
- configure_tools / configure_runtime (运行时配置注入)
- 工具白名单和定义 (TOOL_WHITELIST, TOOL_DEFINITIONS)
- 天气缓存 (_WEATHER_CACHE)

**不应该负责什么**:
- 不应包含工具注册逻辑 (已委托给 tool_registry.py)
- 不应包含微信发送逻辑

**输入**: 工具名 + 参数

**输出**: 工具执行结果 (str)

**依赖的模块**:
- `agent.tool_registry` — TOOL_REGISTRY, get_all_tool_names

**被谁调用**:
- `main.py` — configure_runtime, configure_tools, execute_tool, compute_message_delay
- `agent.chat_agent._do_exec_fact_tools()` (通过 execute_tool)
- 多个 test 文件

**当前边界问题**:
1. **configure_tools/configure_runtime 在 tools.py**: 这两个函数是全局配置注入, 不属于工具实现, 应在独立的配置模块
2. **weather 缓存全局状态**: `_WEATHER_CACHE` 是模块级全局变量, 缺少缓存失效机制

---

## 7. `agent/output_guard.py` — 输出守卫

**路径**: `agent/output_guard.py`

**当前职责**:
- guard_messages (批量消息守卫)
- guard_single_message (单消息守卫)
- 协议泄露检测 (_looks_like_protocol_json)
- 敏感信息过滤 (_filter_sensitive)
- 长度/数量限制 (MAX_MESSAGE_LEN, MAX_MESSAGE_COUNT, MAX_TOTAL_CHARS)
- 硬兜底文案 (HARD_FALLBACK_TEXT)
- markdown 清理
- 空消息检测

**不应该负责什么**:
- 不应包含发送逻辑
- 不应包含 LLM 调用逻辑

**输入**: 原始消息文本/列表

**输出**: 守卫后的消息文本/列表

**依赖的模块**: 无

**被谁调用**:
- `main.py` — guard_messages, guard_single_message
- `agent.chat_agent._do_guard_output()` — guard_single_message

**当前边界问题**:
1. **HARD_FALLBACK_TEXT 定义在 output_guard.py**: 硬兜底文案是 chat_agent 的业务逻辑, 但定义在 output_guard.py, 造成耦合

---

## 8. `agent/llm_gateway.py` — LLM 网关

**路径**: `agent/llm_gateway.py`

**当前职责**:
- LLMGateway 类 (provider-neutral 接口)
- Provider 选择和配置
- Trace 回调管理 (on_trace, _emit_trace)
- 工厂方法 (from_model_config)

**不应该负责什么**:
- 不应包含具体 provider 实现 (已委托给 providers/)

**输入**: LLMRequest

**输出**: LLMResponse

**依赖的模块**:
- `agent.llm_types` — LLMRequest, LLMResponse, LLMError
- `agent.providers.base` — ProviderAdapter, ProviderConfig
- `agent.providers.deepseek` — DeepSeekAdapter

**被谁调用**:
- `main.py` — 创建 LLMGateway 实例
- `agent.chat_agent._do_call_llm()` — 调用 complete()
- `agent.memory_worker` — 调用 complete() (延迟导入)

**当前边界问题**: 无明显问题, 职责清晰

---

## 9. `agent/llm_types.py` — LLM 类型

**路径**: `agent/llm_types.py`

**当前职责**:
- LLMRequest / LLMResponse / LLMUsage 数据类
- LLMError 异常类
- 类型字面量 (ResponseMode, ThinkingMode, LLMErrorKind)

**输入**: 无 (纯类型模块)

**输出**: 类型定义

**依赖的模块**: 无

**被谁调用**:
- `agent.llm_gateway`
- `agent.providers.*`
- `agent.memory_worker`
- `main.py`

**当前边界问题**: 无明显问题

---

## 10. `agent/context_builder.py` — 上下文工具

**路径**: `agent/context_builder.py`

**当前职责**:
- estimate_tokens (token 估算)
- compact_history (历史压缩)
- build_chat_system_prompt (系统提示词构建)

**不应该负责什么**:
- 不应包含记忆加载逻辑 (由 memory_hook 处理)

**输入**: 历史消息列表, persona/personality

**输出**: 压缩后的历史, 系统提示词

**依赖的模块**: 无

**被谁调用**:
- `main.py` — compact_history, build_chat_system_prompt

**当前边界问题**:
1. **build_chat_system_prompt 功能重叠**: main.py 的 `build_system_prompt_for_turn()` 和 `_read_persona()` 也参与系统提示词构建, 逻辑分散

---

## 11. `agent/turn_context.py` — ContextBuilder (v0.2)

**路径**: `agent/turn_context.py`

**当前职责**:
- ContextBuilder 类 (组装 LLM messages 数组)
- 扩展点预留 (MemoryLoader, EmotionLoader, SummaryLoader)

**输入**: system_prompt, history, user_message, trigger

**输出**: messages list

**依赖的模块**: 无

**被谁调用**:
- 【不确定】未发现直接 import

**当前边界问题**:
1. **可能未使用**: 未发现任何文件 import 此模块, 可能是 v0.2 预留但未集成
2. **与 _do_build_context 重叠**: chat_agent.py 的 `_do_build_context()` 做了类似的事

---

## 12. `agent/message_envelope.py` — MessageEnvelope

**路径**: `agent/message_envelope.py`

**当前职责**:
- MessageEnvelope 数据类 (内部统一消息结构)
- 兼容层, 不改变运行时行为

**输入**: 无 (纯类型模块)

**输出**: MessageEnvelope 类型

**依赖的模块**: 无

**被谁调用**:
- 【不确定】未发现直接 import (仅 test_message_envelope.py 测试)

**当前边界问题**:
1. **可能未使用**: 未发现业务代码 import 此模块, 可能是 Phase 4A 预留但未集成

---

## 13. `agent/memory_worker.py` — 记忆提取

**路径**: `agent/memory_worker.py`

**当前职责**:
- MemoryWorker 类 (后台异步记忆提取)
- LLM 调用提取记忆
- 去重检查 (is_duplicate)
- 存储到 MemoryStore

**输入**: user_message, assistant_reply

**输出**: 异步任务 (fire-and-forget)

**依赖的模块**:
- `agent.llm_types` — LLMRequest
- `memory.dedup` — is_duplicate
- `memory.store` — MemoryStore

**被谁调用**:
- `main.py` — 条件导入 (features.memory=true)

**当前边界问题**:
1. **直接调用 LLMGateway**: MemoryWorker 直接调用 llm_gateway.complete(), 绕过了 ChatAgent 的模型路由逻辑

---

## 14. `agent/memory_hook.py` — 记忆钩子

**路径**: `agent/memory_hook.py`

**当前职责**:
- MemoryHook 类
- load_context: 检索相关记忆, 返回上下文字符串
- update: 触发后台记忆提取

**输入**: user_message, user_id

**输出**: 记忆上下文字符串 (load_context), 异步任务 (update)

**依赖的模块**:
- `memory.store` — MemoryStore

**被谁调用**:
- `main.py` — 条件导入 (features.memory=true)

**当前边界问题**: 无明显问题

---

## 15. `agent/providers/base.py` — Provider 基类

**路径**: `agent/providers/base.py`

**当前职责**:
- ProviderConfig 数据类
- ProviderAdapter Protocol (接口定义)

**输入**: 无 (纯类型模块)

**输出**: 类型定义

**依赖的模块**:
- `agent.llm_types` — LLMRequest, LLMResponse

**被谁调用**:
- `agent.llm_gateway`
- `agent.providers.deepseek`

**当前边界问题**: 无明显问题

---

## 16. `agent/providers/deepseek.py` — DeepSeek 适配器

**路径**: `agent/providers/deepseek.py`

**当前职责**:
- DeepSeekAdapter 类 (OpenAI-compatible)
- 构建 API payload
- 解析 API 响应
- 错误分类和转换

**输入**: LLMRequest

**输出**: LLMResponse

**依赖的模块**:
- `agent.llm_types` — LLMRequest, LLMResponse, LLMUsage, LLMError
- `agent.providers.base` — ProviderConfig
- `openai` — AsyncOpenAI

**被谁调用**:
- `agent.llm_gateway.LLMGateway.from_model_config()`

**当前边界问题**: 无明显问题

---

## 17. `bridge/wechat_poller.py` — 微信轮询

**路径**: `bridge/wechat_poller.py`

**当前职责**:
- WeChatBridge 类
- 长轮询 iLink API (get_updates)
- 消息缓冲去抖 (buffer_debounce)
- 转发到 /chat (POST httpx)
- seq 管理 (_inbound_seq)
- send_text 发送

**输入**: iLink API 响应

**输出**: POST /chat 请求

**依赖的模块**:
- `bridge.ilink_client` — get_updates, send_message, get_config

**被谁调用**:
- `main.py` — 创建 WeChatBridge, 调用 start/stop/send_text

**当前边界问题**:
1. **直接 POST /chat**: WeChatBridge 通过 HTTP 调用自己的 /chat 端点, 而非直接调用 ChatAgent, 增加了一层不必要的序列化/反序列化
2. **seq 管理与 main.py 重复**: `_inbound_seq` 和 `_user_latest_inbound_seq` 都在追踪 seq, 但分布在两个模块

---

## 18. `bridge/ilink_client.py` — iLink API 客户端

**路径**: `bridge/ilink_client.py`

**当前职责**:
- iLink Bot API 调用 (7 个 HTTP 端点)
- get_updates (长轮询)
- send_message (发送消息)
- get_qr_code / poll_qr_status (登录)
- get_config (获取配置)

**输入**: token, 参数

**输出**: API 响应

**依赖的模块**:
- `httpx`

**被谁调用**:
- `bridge.wechat_poller`
- `main.py` (延迟导入: get_qr_code, poll_qr_status)

**当前边界问题**: 无明显问题, 职责清晰

---

## 19. `memory/store.py` — SQLite 记忆存储

**路径**: `memory/store.py`

**当前职责**:
- MemoryStore 类
- SQLite WAL 模式管理
- 对话历史 CRUD (conversation_history)
- Turn 记录 CRUD (turns)
- 出站消息 CRUD (outbound_messages)
- 记忆 CRUD (memories)
- Turn 诊断 (turn_diagnostics)
- LLM Trace (llm_traces)
- 使用事件 (usage_events)
- 关系状态 (contact_state)
- 历史摘要 (history_summaries)
- Recap (recaps)
- 共享事实 (shared_facts)
- 定时任务 (scheduled_tasks)
- Schema 迁移

**输入**: SQL 查询参数

**输出**: 查询结果

**依赖的模块**:
- `aiosqlite`
- `memory.dedup` — is_duplicate, compute_importance, jaccard_similarity

**被谁调用**:
- `main.py` — MemoryStore 实例
- `agent.memory_worker` — MemoryStore 实例
- `agent.memory_hook` — MemoryStore 实例

**当前边界问题**:
1. **职责过多**: MemoryStore 承担了 15+ 种数据的 CRUD, 接口过大
2. **没有分层**: 对话历史、记忆、诊断、定时任务等都在同一个类中

---

## 20. `memory/dedup.py` — CJK 去重

**路径**: `memory/dedup.py`

**当前职责**:
- jaccard_similarity (CJK 二元组 Jaccard 相似度)
- is_duplicate (去重判断)
- compute_importance (重要度计算)

**输入**: 文本字符串

**输出**: 相似度分数 / 布尔值 / 重要度分数

**依赖的模块**: 无

**被谁调用**:
- `memory.store` — is_duplicate, compute_importance, jaccard_similarity
- `agent.memory_worker` — is_duplicate

**当前边界问题**: 无明显问题, 职责清晰

---

## 21. `utils/json_parser.py` — JSON 安全解析

**路径**: `utils/json_parser.py`

**当前职责**:
- extract_json (从文本中提取 JSON)
- 多层回退 (markdown fence → JSON object → 原文)

**输入**: 文本

**输出**: JSON 字符串

**依赖的模块**: 无

**被谁调用**:
- 【不确定】未发现直接 import (可能在旧版代码中使用)

**当前边界问题**:
1. **可能未使用**: 未发现业务代码 import 此模块

---

## 22. `prompts/` — 提示词文件

**路径**: `prompts/`

**当前职责**:
- persona.md — 人设 (被 main.py 读取)
- base_protocol.md — 基础协议
- bot_identity_default.md — 默认 bot 人设
- relationship_context.md — 关系上下文模板
- memory_policy.md — 记忆策略
- memory_extract.md — 记忆提取提示词 (被 memory_worker.py 使用)
- tool_policy.md — 工具策略
- trigger_context.md — 触发源模板

**输入**: 无 (静态文件)

**输出**: 提示词文本

**依赖的模块**: 无

**被谁调用**:
- `main.py` — 读取 persona.md
- `agent.memory_worker` — 读取 prompts 目录

**当前边界问题**:
1. **多个文件可能未使用**: base_protocol.md, bot_identity_default.md, relationship_context.md, tool_policy.md, trigger_context.md 未发现直接 import
2. **提示词组装逻辑分散**: main.py 的 `build_system_prompt_for_turn()` 和 `_read_persona()` 都在组装提示词, 但 prompts/ 中的多个文件未被引用

---

## 23. `static/` — 管理界面

**路径**: `static/`

**当前职责**:
- admin.html/css/js — 管理面板
- index.html — 落地页

**输入**: 无 (静态文件)

**输出**: HTML/CSS/JS

**依赖的模块**: 无

**被谁调用**:
- `main.py` — StaticFiles mount + HTMLResponse

**当前边界问题**: 无明显问题

---

## 模块依赖图

```
main.py
  ├── agent.chat_agent (ChatAgent)
  │     ├── agent.state_machine (类型)
  │     ├── agent.tool_classifier
  │     │     └── agent.tool_registry
  │     ├── agent.tool_registry (TOOL_REGISTRY)
  │     ├── agent.output_guard (guard_single_message)
  │     └── agent.llm_types
  ├── agent.context_builder
  ├── agent.llm_gateway (LLMGateway)
  │     ├── agent.llm_types
  │     ├── agent.providers.base
  │     └── agent.providers.deepseek
  │           └── openai
  ├── agent.output_guard
  ├── agent.tools
  │     └── agent.tool_registry
  ├── agent.memory_worker (条件)
  │     ├── agent.llm_types
  │     ├── memory.store
  │     └── memory.dedup
  ├── agent.memory_hook (条件)
  │     └── memory.store
  ├── bridge.wechat_poller (WeChatBridge)
  │     └── bridge.ilink_client
  └── memory.store (MemoryStore)
        └── memory.dedup
```

---

## 跨模块边界问题汇总

| # | 问题 | 涉及模块 | 严重程度 |
|---|------|----------|----------|
| 1 | main.py 职责过多 (~2800 行) | main.py | 中 |
| 2 | 对话历史管理在 main.py | main.py | 低 |
| 3 | post-terminal 回调构造在 main.py | main.py, chat_agent.py | 低 |
| 4 | wechat_sender 实现细节在 main.py | main.py, bridge/ | 低 |
| 5 | _do_build_context 与 turn_context.py 功能重叠 | chat_agent.py, turn_context.py | 低 |
| 6 | HARD_FALLBACK_TEXT 跨模块引用 | chat_agent.py, output_guard.py | 低 |
| 7 | 系统提示词构建逻辑分散 | main.py, context_builder.py | 低 |
| 8 | WeChatBridge 通过 HTTP 调用自己的 /chat | wechat_poller.py, main.py | 中 |
| 9 | seq 管理分布在两个模块 | wechat_poller.py, main.py | 低 |
| 10 | MemoryStore 接口过大 (15+ 种数据) | memory/store.py | 中 |
| 11 | MemoryWorker 直接调用 LLMGateway (绕过模型路由) | memory_worker.py, chat_agent.py | 低 |
| 12 | 多个 prompts/ 文件可能未使用 | prompts/ | 低 |
| 13 | turn_context.py / message_envelope.py / json_parser.py 可能未使用 | agent/, utils/ | 低 |
