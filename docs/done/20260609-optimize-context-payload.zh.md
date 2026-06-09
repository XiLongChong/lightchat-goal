# 优化上下文打包器与系统提示词（2026-06-09）

## 变更

### 1. 移除 payload 中三个参数

`agent/providers/deepseek.py` — `_build_payload()`：

| 移除 | 原因 |
|------|------|
| `thinking: {"type": ...}` | DeepSeek 不支持 thinking 模式 |
| `temperature` | 使用 provider 默认值，不显式传参 |
| `tool_choice` | 使用 provider 默认行为 |

同时删除不再使用的 `_to_anthropic_tool_choice()` 函数（~12 行死代码）。

### 2. 更新系统提示词

`agent/context_builder.py` — `ORDINARY_CHAT_GLOBAL_PROTOCOL`：
- 更换为 LightChat 标准协议文案
- 新增安全护栏（自伤/伤人应急指引）
- 新增详细示例（正确/错误行为对比）
- 明确 turn 边界与 `sent` 结果的处理语义

### 3. 优化工具定义

`agent/tool_registry.py` — `send_text_message`：
- 描述更详细：说明每条消息的确切语义、多消息使用场景、禁止包含的内容
- `args_schema` 补充 `"type": "object"` 和 `"additionalProperties": false`（完整 JSON Schema）
- 保留 `"minLength": 1`（防止空文本）

### 4. 测试更新

- `test_thinking_explicitly_disabled_in_payload` → `test_thinking_not_in_payload`
- `test_thinking_explicitly_enabled_in_payload` → `test_thinking_enabled_not_in_payload`
- `test_config_thinking_default_enabled_overrides_request` → `test_config_thinking_not_in_payload`
- 三个测试从「验证 thinking 被发送」改为「验证 thinking 不被发送」

## 验证

全量 90 测试通过。
