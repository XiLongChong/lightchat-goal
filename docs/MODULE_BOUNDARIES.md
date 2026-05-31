# Module Boundaries — 模块边界分析

> 最后更新: 2026-05-31
> 基于实际代码 import 和调用关系 (v3.1 proactive pipeline)

---

## 1. `main.py` — 主入口 + 路由 + 编排

**路径**: `main.py`

**当前职责**:
- FastAPI app 创建和生命周期管理 (lifespan, v3.1: +proactive heartbeat loop)
- 所有 HTTP 路由定义 (/chat, /admin/*, /memories, /wechat, v3.1: /admin/proactive/*)
- 配置加载和合并 (config.yaml + runtime_settings.json, v3.1: +proactive section)
- 全局对象创建 (LLMGateway, MemoryStore, ChatAgent, WeChatBridge, ProactiveScheduler)
- per-user 锁管理 (_user_turn_locks)
- 中断检查器工厂 (_make_interrupt_checker)
- 对话历史管理 (conversation_histories, append_history)
- wechat_sender 回调 (微信发送)
- post-terminal 回调构造 (_build_chatagent_write_model_history, _build_chatagent_post_turn_hooks)
- v3.1: proactive 回调构造 (_build_proactive_write_model_history, _build_proactive_post_turn_hooks)
- v3.1: proactive test impulse builder (_build_test_impulse_meta)
- 记忆导入/导出 (parse_memory_upload, build_memory_export_zip)
- 管理面板数据聚合 (build_admin_event_dashboard, build_usage_dashboard)
- 人设变更确认 (handle_pending_persona_confirmation)

**不应该负责什么**:
- 不应包含 LLM 调用逻辑 (已委托给 LLMGateway)
- 不应包含状态机逻辑 (已委托给 ChatAgent)
- 不应包含工具执行逻辑 (已委托给 tools.py)
- 不应包含输出守卫逻辑 (已委托给 output_guard.py)
- 不应直接操作 ProactiveScheduler 锁 (已委托给 scheduler.py)

**输入**: HTTP 请求, config.yaml + .env + runtime_settings.json

**输出**: HTTP 响应, 微信消息 (通过 wechat_sender → WeChatBridge)

**依赖的模块**:
- `agent.chat_agent` — ChatAgent, 路由/上下文策略常量
- `agent.context_builder` — compact_history, build_chat_system_prompt
- `agent.llm_gateway` — LLMGateway
- `agent.llm_types` — LLMRequest
- `agent.output_guard` — guard_messages, guard_single_message
- `agent.scheduler` — ProactiveScheduler (v3.1)
- `agent.state_machine` — ModelProfile, TurnResult, TurnScratchpad, TurnState, SOURCE_PROACTIVE_IMPULSE
- `agent.tools` — configure_runtime, configure_tools
- `agent.memory_worker` — MemoryWorker (条件导入)
- `agent.memory_hook` — MemoryHook (条件导入)
- `bridge.wechat_poller` — WeChatBridge
- `bridge.ilink_client` — get_qr_code, poll_qr_status (延迟导入)
- `memory.store` — MemoryStore

**被谁调用**: uvicorn (ASGI server), WeChatBridge (内部 POST /chat)

**当前边界问题**:
1. main.py 体积大 (~3000 行): 聚合路由、编排、配置、历史管理、记忆导入导出、管理面板数据等
2. 对话历史管理在 main.py: conversation_histories dict 和 append_history 函数应在独立模块
3. post-terminal 回调构造在 main.py: 是 ChatAgent 的编排逻辑但实现在 main.py

---

## 2. `agent/scheduler.py` — 主动消息调度器 (v3.1)

**路径**: `agent/scheduler.py`

**当前职责**:
- ProactiveScheduler 类: 主动消息全生命周期编排
- 锁管理: admin trigger 和 heartbeat 共用同一把 asyncio.Lock
- run_manual_trigger: admin 手动触发入口
- run_heartbeat: 心跳入口 (内部资格检查)
- _check_eligibility: 8 项系统前置判定 (无 LLM)
- _select_impulse: 冲动候选 pipeline (生成→去重→过期→评分→选择)
- impulse 生成: ECHO / TOPIC_CONTINUE / PRESENCE / SCHEDULED_RITUAL
- dedup/expire/score/select: 规则引擎
- update_relationship_temperature: 关系热度更新+衰减
- attribute_user_reply_to_proactive: 用户回复溯源
- get_metrics: 指标快照

**不应该负责什么**:
- 不应包含 LLM 调用 (委托给 ChatAgent)
- 不应包含微信发送 (通过注入的 wechat_sender_provider)
- 不应直接操作 main.py 的锁 (锁归 scheduler 内部管理)

**输入**: user_id, bot_id, impulse_meta, 注入的 providers

**输出**: 主动回合执行结果 dict

**依赖的模块**:
- `agent.state_machine` — SOURCE_PROACTIVE_IMPULSE
- `memory.store` — MemoryStore (通过注入)

**被谁调用**: `main.py` — proactive_scheduler.run_manual_trigger() / run_heartbeat()

**当前边界问题**: Phase 7 刚完成, 无明显问题

---

## 3. `agent/chat_agent.py` — 状态机主循环

**路径**: `agent/chat_agent.py`

**当前职责**:
- v3.1 状态机主循环 (30+ state handler, 含 proactive 专用状态)
- 状态分发 (_dispatch)
- LLM 调用编排 (_do_call_llm, v3.1: +proactive dynamic allowed_tools + max_llm_steps check)
- 工具分类路由 (CLASSIFY_ONE_TOOL_CALL → FACT/ACTION/TERMINAL/skip_proactive)
- v3.1: proactive phase validation (A/B/C, 后端双层校验)
- FACT_TOOL 并行执行 (_do_exec_fact_tools)
- wechat_reply 验证/守卫/发送链路
- skip_proactive 验证/执行链路 (v3.1)
- done 终态处理 (VALIDATE_DONE_ARGS)
- 协议重试 (PROTOCOL_RETRY)
- 硬兜底 (HARD_FALLBACK, v3.1: proactive 抑制)
- 模型路由 (v1.3 _select_model)
- post-terminal pipeline 执行 (_run_post_terminal_pipeline)
- 上下文构建 (_do_build_context, v3.1: +proactive context injection)

**不应该负责什么**:
- 不应包含具体工具实现 (已委托给 tools.py)
- 不应包含输出守卫规则 (已委托给 output_guard.py)
- 不应包含 LLM API 调用细节 (已委托给 llm_gateway.py)
- 不应包含微信发送细节 (通过 wechat_sender 回调注入)

**输入**: user_message (可为 None), history, system_prompt, proactive_meta (v3.1), wechat_sender, interrupt_checker, write_model_history, post_turn_hooks

**输出**: TurnResult (含 proactive_delivered_count/texts 等 v3.1 字段)

**依赖的模块**:
- `agent.state_machine` — TurnState, TurnScratchpad, TurnResult, VisibleMessage, 常量, SOURCE_PROACTIVE_IMPULSE
- `agent.tool_classifier` — classify_single_tool_call
- `agent.tool_registry` — TOOL_REGISTRY
- `agent.llm_types` — LLMRequest (延迟导入)
- `agent.output_guard` — guard_single_message, guard_proactive_output (v3.1)

**被谁调用**: `main.py` — chat_agent.chat(), `agent/scheduler.py` — _execute_proactive_turn()

**当前边界问题**:
1. _do_build_context 包含 proactive context 构建 (可独立但当前合理)
2. 硬兜底文案定义在 output_guard.py 但在 chat_agent.py 中引用

---

## 4. `agent/state_machine.py` — 类型定义

**路径**: `agent/state_machine.py`

**当前职责**:
- TurnState 枚举 (41 个状态, 含 7 个 v3.1 proactive 专用)
- TurnScratchpad (turn 本地可变状态, v3.1: +5 个 proactive 计数器)
- TurnResult (turn 结果, v3.1: +4 个 proactive 字段)
- VisibleMessage (单条可见消息)
- DeliveryStatus (六态送达模型)
- ContextBuildResult
- ModelProfile, ModelAttempt
- InterruptChecker / PostTerminalCallback 类型
- SOURCE_USER_MESSAGE / SOURCE_PROACTIVE_IMPULSE 常量 (v3.1)
- 常量 (MAX_LLM_STEPS_PER_TURN, MAX_PROTOCOL_RETRIES_PER_TURN 等)
- POST_SEND_DELAY_TIERS 等 v1.6 常量

**不应该负责什么**: 不应包含任何业务逻辑 (纯类型定义)

**输入**: 无 (纯类型模块)

**输出**: 类型定义, 供其他模块 import

**依赖的模块**: 无 (仅 Python 标准库)

**被谁调用**: `agent.chat_agent`, `main.py`, `agent/scheduler.py`, 多个 test 文件

**当前边界问题**: 无明显问题, 职责清晰

---

## 5. `agent/tool_registry.py` — 工具注册表

**路径**: `agent/tool_registry.py`

**当前职责**:
- ActionDef / ToolDef 数据类
- ActionRegistry / ToolRegistry 类
- TOOL_REGISTRY 全局实例
- 工具注册: get_time, get_weather, wechat_reply, done, skip_proactive (v3.1)
- 废弃工具保留: send_wechat_messages, skip_reply (native_tool_call_eligible=False)
- 工具查询 (get_tool, iter_defs, is_action, is_external_tool)
- native_tool_call_eligible 标记
- tool_category 分类 (fact_tool, action_tool, terminal_tool, state_tool)

**不应该负责什么**: 不应包含工具执行逻辑, 不应包含工具分类逻辑

**输入**: 工具定义 (模块加载时注册)

**输出**: 工具定义查询结果

**依赖的模块**: 无

**被谁调用**: `agent.chat_agent`, `agent.tools`, `agent.tool_classifier`, 多个 test 文件

**当前边界问题**: 无明显问题, 职责清晰

---

## 6. `agent/tool_classifier.py` — 工具分类

**路径**: `agent/tool_classifier.py`

**当前职责**:
- ToolCategory 枚举 (FACT/ACTION/TERMINAL/STATE)
- ClassifiedSingleToolCall 数据类
- classify_single_tool_call 函数
- 废弃工具提示 (_DEPRECATED_HINTS: send_wechat_messages, skip_reply)

**不应该负责什么**: 不应包含工具执行逻辑, 不应包含工具注册逻辑

**输入**: tool_call 对象 (来自 LLM 响应)

**输出**: ClassifiedSingleToolCall (分类结果)

**依赖的模块**: `agent.tool_registry` — TOOL_REGISTRY

**被谁调用**: `agent.chat_agent._do_classify_one_tool_call()`

**当前边界问题**: 无明显问题, 职责清晰

---

## 7. `agent/tools.py` — 工具实现

**路径**: `agent/tools.py`

**当前职责**:
- get_time 实现 (_get_time)
- get_weather 实现 (_get_weather)
- execute_tool 调度函数
- compute_message_delay 计算
- configure_tools / configure_runtime (运行时配置注入)
- 工具白名单和定义 (TOOL_WHITELIST, TOOL_DEFINITIONS)
- 天气缓存 (_WEATHER_CACHE)

**不应该负责什么**: 不应包含工具注册逻辑, 不应包含微信发送逻辑

**输入**: 工具名 + 参数

**输出**: 工具执行结果 (str)

**依赖的模块**: `agent.tool_registry` — TOOL_REGISTRY, get_all_tool_names

**被谁调用**: `main.py`, `agent.chat_agent._do_exec_fact_tools()`, 多个 test 文件

**当前边界问题**:
1. configure_tools/configure_runtime 是全局配置注入, 不属于工具实现, 应在独立配置模块
2. weather 缓存全局状态缺少缓存失效机制

---

## 8. `agent/output_guard.py` — 输出守卫

**路径**: `agent/output_guard.py`

**当前职责**:
- guard_messages (批量消息守卫)
- guard_single_message (单消息守卫)
- guard_proactive_output (v3.1: 主动消息专用守卫, 编造观察/机制暴露/通知味检测)
- guard_emotion_profanity (v2.2: 情绪粗口守卫)
- guard_send_message_batch (v1: send_wechat_messages 批量守卫, 保留兼容)
- validate_reply_text (v1.5: wechat_reply 参数校验)
- 协议泄露检测 (_has_protocol_leak, _looks_like_protocol_json)
- 敏感信息过滤 (_filter_sensitive)
- 长度/数量限制 (MAX_MESSAGE_LEN=500, MAX_MESSAGE_COUNT=8, MAX_TOTAL_CHARS=2000)
- 硬兜底文案 (HARD_FALLBACK_TEXT)
- markdown 清理 (_wechat_cleanup)
- 空消息检测

**不应该负责什么**: 不应包含发送逻辑, 不应包含 LLM 调用逻辑

**输入**: 原始消息文本/列表

**输出**: 守卫后的消息文本/列表, 或 (result, text) 元组 (guard_proactive_output)

**依赖的模块**: 无

**被谁调用**: `main.py`, `agent.chat_agent`

**当前边界问题**:
1. HARD_FALLBACK_TEXT 定义在此但语义上属于 chat_agent 业务逻辑

---

## 9. `agent/llm_gateway.py` — LLM 网关

**路径**: `agent/llm_gateway.py`

**当前职责**: LLMGateway 类 (provider-neutral), Provider 选择和配置, Trace 回调管理, 工厂方法

**不应该负责什么**: 不应包含具体 provider 实现 (已委托给 providers/)

**输入**: LLMRequest

**输出**: LLMResponse

**依赖的模块**: `agent.llm_types`, `agent.providers.base`, `agent.providers.deepseek`

**被谁调用**: `main.py`, `agent.chat_agent._do_call_llm()`, `agent.memory_worker`

**当前边界问题**: 无明显问题

---

## 10. `agent/llm_types.py` / `agent/providers/` — LLM 类型和适配器

无明显边界问题。DeepSeekAdapter 通过 OpenAI-compatible API 调用, LLMRequest/LLMResponse 类型稳定。

---

## 11. `agent/context_builder.py` — 上下文工具

**路径**: `agent/context_builder.py`

**当前职责**: compact_history (历史压缩), build_chat_system_prompt (系统提示词构建)

**被谁调用**: `main.py`

**当前边界问题**: build_chat_system_prompt 与 main.py 的 build_system_prompt_for_turn() 功能有交集

---

## 12. `agent/memory_worker.py` / `agent/memory_hook.py` — 记忆系统

条件启用 (features.memory=true), 无明显边界问题。

---

## 13. `bridge/wechat_poller.py` + `bridge/ilink_client.py` — 微信桥接

无明显边界问题。WeChatBridge 通过 HTTP 调用自己的 /chat 端点 (非直接调用 ChatAgent), 增加了一层序列化开销。

---

## 14. `memory/store.py` + `memory/dedup.py` — 持久化层

MemoryStore 承担 20+ 表 CRUD, 接口较大。dedup.py 职责清晰 (CJK Jaccard 去重)。

---

## 15. `utils/json_parser.py` — JSON 安全解析

未发现直接 import (可能在旧版代码中使用)。

---

## 模块依赖图

```
main.py
  ├── agent.scheduler (ProactiveScheduler)           ← v3.1
  │     └── agent.state_machine (类型)
  ├── agent.chat_agent (ChatAgent)
  │     ├── agent.state_machine (类型)
  │     ├── agent.tool_classifier
  │     │     └── agent.tool_registry
  │     ├── agent.tool_registry (TOOL_REGISTRY)
  │     ├── agent.output_guard (guard_single_message, guard_proactive_output)
  │     └── agent.llm_types
  ├── agent.context_builder
  ├── agent.llm_gateway (LLMGateway)
  │     ├── agent.llm_types
  │     ├── agent.providers.base
  │     └── agent.providers.deepseek
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
| 1 | main.py 体积大 (~3000 行) | main.py | 中 |
| 2 | 对话历史管理在 main.py | main.py | 低 |
| 3 | post-terminal 回调构造在 main.py | main.py, chat_agent.py | 低 |
| 4 | wechat_sender 实现细节在 main.py | main.py, bridge/ | 低 |
| 5 | HARD_FALLBACK_TEXT 跨模块引用 | chat_agent.py, output_guard.py | 低 |
| 6 | 系统提示词构建逻辑分散 | main.py, context_builder.py | 低 |
| 7 | WeChatBridge 通过 HTTP 调用自己的 /chat | wechat_poller.py, main.py | 中 |
| 8 | MemoryStore 接口过大 (20+ 表) | memory/store.py | 中 |
| 9 | MemoryWorker 绕过 ChatAgent 模型路由 | memory_worker.py, chat_agent.py | 低 |
| 10 | configure_tools/configure_runtime 在 tools.py | tools.py | 低 |
