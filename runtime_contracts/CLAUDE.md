# CLAUDE.md

共享数据契约——runtime、agent、context builder 之间传递的类型定义。

## 文件

```
types.py     — 枚举和 dataclass：TurnType, UserIntent, TurnPhase, ToolCategory, ToolSideEffect, DeliveryStatus, SystemTime, ProviderMessage, ConversationHistoryMessage, BuildContextRequest, BuiltContext, ToolExposure, NativeToolSchema
__init__.py  — 导出所有公共类型
```

## 核心类型

### 枚举

| 类型 | 枚举值 | 说明 |
|------|--------|------|
| **TurnType** | `USER_CHAT` | 当前唯一值，为未来扩展预留 |
| **UserIntent** | `UNKNOWN`, `CHAT` | 用户意图分类 |
| **TurnPhase** | `NORMAL_CHAT` | 对话阶段标记 |
| **ToolCategory** | `OUTPUT`, `INFORMATION`, `MEMORY` | 工具分类（UI 展示用） |
| **ToolSideEffect** | `NONE`, `SENDS_MESSAGE`, `READS_EXTERNAL`, `WRITES_STATE` | 工具副作用类型 |
| **DeliveryStatus** | `DELIVERED`, `FAILED`, `UNKNOWN`, `NOT_APPLICABLE` | 消息投递状态 |

### Dataclass

| 类 | 用途 |
|----|------|
| **SystemTime** | 时区感知的系统时间（dt, timezone, weekday_cn） |
| **ProviderMessage** | LLM Provider 消息结构（role, content） |
| **ConversationHistoryMessage** | 历史消息记录（含 delivery_status, source, created_at） |
| **NativeToolSchema** | 原生工具定义 Schema（name, description, parameters_schema, terminal, side_effect, category） |
| **ToolExposure** | 某一回合可用的工具集合（exposed_tool_names, native_tool_schemas, tool_contract_block, denied_tools） |
| **BuildContextRequest** / **BuiltContext** | context builder 的输入/输出接口 |
| **ContextBuildWarning** | 上下文构建警告（source, detail, severity） |

## 注意事项

- 这是项目中类型最稳定的模块，修改时需确认所有引用方
- 与 `agent/state_machine.py` 中的类型有部分重叠（如 DeliveryStatus），存在两套定义
- `__init__.py` 负责统一导出，外部模块应 `from runtime_contracts import X` 而非直接 import types
