# ContextBuilder 解耦设计（2026-06-09）

## 目标

将提示词打包逻辑从 ChatAgent 完全剥离到 ContextBuilder，使 chat_agent 只做编排，不接触 context 构造细节。

## 当前耦合点

```
chat_agent._build_context()  (~60行)
  ├── 计算时区 → 应下沉
  ├── 构建 SystemTime → 应下沉
  ├── 转换 history dict → ConversationHistoryMessage → 应下沉
  ├── channel 消息包装 (_channel_message) → 应下沉
  ├── 组装 BuildContextRequest → 应下沉
  ├── 调用 self._context_builder.build()  ← 唯一的正确职责
  └── 将 BuiltContext 转回 turn.messages dict → 应下沉
```

额外问题：
- `chat_agent.py` 直接 import `ORDINARY_CHAT_GLOBAL_PROTOCOL`（来自 context_builder）
- `main.py` 直接 import `build_chat_system_prompt`（来自 context_builder）
- `_channel_message()` 是 ChatAgent 的静态方法，但属于 context 构造

## 设计方案

### 新接口

```python
class ContextBuilder:
    def build_for_chat(
        self,
        *,
        persona: str,
        personality: str,
        history: list[dict],
        user_message: str,
        bot_id: str,
    ) -> BuiltContextForTurn:
        ...
```

### BuiltContextForTurn（新返回类型）

```python
@dataclass
class BuiltContextForTurn:
    messages: list[dict]         # 可直接给 LLM 的 messages（含 user message）
    system: list[dict]           # 可直接给 LLM 的 system prompt
    tool_schemas: list[dict]     # native tool call 规格
    warnings: list[str]          # 构建警告
```

### 调用方变化

```python
# 之前 (~25行胶水)
self._build_context(turn, history, system_prompt, bot_id)

# 之后 (1行委托)
built = self._context_builder.build_for_chat(
    persona=self.persona,
    personality=self.personality,
    history=history,
    user_message=turn.user_message,
    bot_id=bot_id,
)
turn.messages = built.messages
turn._provider_system = built.system
```

### ContextBuilder 内部承担的职责

1. 组装 system prompt（persona + personality + 全局协议 + 时间块）
2. 转换 history（dict → ProviderMessage，过滤非 user/assistant，处理 channel 包装）
3. 包装当前 user_message 为 channel 格式
4. 构建最终 messages 列表（system + history + current user）
5. 持有 `ORDINARY_CHAT_GLOBAL_PROTOCOL`（外部不再直接 import）

## 不变量（回归测试锚点）

1. **相同输入 → 相同 turn.messages**：重构前后 `turn.messages` 和 `turn._provider_system` 必须一致
2. **channel 格式不变**：`<channel source="wechat">\n{text}\n</channel>`
3. **history 过滤规则不变**：只保留 role=user/assistant，过滤空 content
4. **system prompt 顺序不变**：protocol → persona → time_block → tool_contract
5. **工具 schema 不变**：ToolExposurePolicy 的输出格式不受影响

## 实施记录（2026-06-09）

| 步骤 | 内容 | 状态 |
|------|------|------|
| 1 | 在 ContextBuilder 新增 `build_for_chat()` 方法 | ✓ 完成 |
| 2 | 在 ContextBuilder 内部移动 `_channel_message` | ✓ 完成 |
| 3 | ContextBuilder `build_for_chat()` 内部引用 `ORDINARY_CHAT_GLOBAL_PROTOCOL` | ✓ 完成 |
| 4 | 修改 `chat_agent._build_context()` 调用新接口（65行→20行） | ✓ 完成 |
| 5 | 删除 `chat_agent._channel_message()` | ✓ 完成 |
| 6 | 移除 `chat_agent` 对 `ORDINARY_CHAT_GLOBAL_PROTOCOL` 的 import | ✓ 完成 |
| 7 | 清理 `chat_agent` 中不再需要的 import (BuildContextRequest, ConversationHistoryMessage, SystemTime, TurnType, timezone, timedelta) | ✓ 完成 |

## 变更摘要

**`agent/runtime_context_builder.py`**：
- 新增 `BuiltContextForTurn` dataclass（messages + system + warnings）
- 新增 `build_for_chat()` 方法——收拢所有胶水逻辑
- 新增 `_channel_message()` 静态方法——从 chat_agent 移入

**`agent/chat_agent.py`**：
- `_build_context()` 从 65 行胶水代码缩减为 20 行纯委托
- 删除 `_channel_message()` 静态方法
- 移除 6 个不再需要的 import

**`main.py`**：
- 保持不变。`build_chat_system_prompt` 仍在 `context_builder.py` 中供 main.py 使用（P2 后续优化）

## 回归测试

- 全量 92 测试通过，零回归
- 新增 18 个 ContextBuilder 单元测试 (`tests/test_context_builder.py`)
- 端到端测试 `test_send_result_is_fed_back_then_turn_ends_implicitly` 通过

## 不变的范围

- `TurnScratchpad`、`TurnResult`、`TurnState` 类型不变
- `ChatAgent.chat()` 的主循环逻辑不变
- `ToolExposurePolicy` 不变
- `LLMGateway` 调用方式不变
- WeChat bridge 不变
