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
compressor.py              — 每日对话压缩：伪造 user 消息触发总结，摘要写入 ConversationContext，DB 只存摘要（active=1）
context_builder.py         — 全局协议文案 + build_chat_system_prompt 工具函数
runtime_context_builder.py — ContextBuilder：build_for_chat() 统一入口，收拢全部 context 构造逻辑
tool_registry.py           — ToolDef 注册表 + native tool call spec 生成（含受限 memory 工具）
tool_exposure_policy.py    — 每 turn 暴露哪些工具（当前固定 3 个）
tools.py                   — 工具实现（get_time, get_weather, memory 分发）
weather_provider.py        — 天气 Provider 协议 + QWeather 实现（GeoAPI + 实时 + 预报 + 缓存）
output_guard.py            — 输出清洗：敏感信息过滤、长度截断、Markdown 去格式化
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

四个工具在模块加载时注册：send_text_message、get_time、get_weather、memory。memory 只允许读取、创建和精确替换当前用户的长期记忆文件，不向模型开放删除。

每日 Compact 不运行工具循环。压缩模型一次返回 `<summary>` 与 `<memory_operations>`，runtime 校验证据和数量后调用长期记忆文件服务执行。

## 注意事项

- `chat_agent.py` 是脏代码最集中的地方——约 480 行，包含完整的 LLM 对话循环和 fallback 逻辑
- LLM 请求使用 `stream=True`，但工具调用的流式处理需要 `response_mode="text"`
- output_guard 会过滤 API key 泄露、tool_call_id 泄露、Markdown 格式
