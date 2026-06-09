# CLAUDE.md

共享数据契约——runtime、agent、context builder 之间传递的类型定义。

## 文件

```
types.py     — 枚举和 dataclass：TurnType, UserIntent, TurnPhase, ToolCategory, ToolSideEffect, DeliveryStatus, SystemTime, ProviderMessage, ConversationHistoryMessage, BuildContextRequest, BuiltContext, ToolExposure, NativeToolSchema
__init__.py  — 导出所有公共类型
```

## 核心类型

- **TurnType / UserIntent / TurnPhase** — 当前只有单一路径（USER_CHAT / CHAT / NORMAL_CHAT），为未来扩展预留
- **ToolExposure** — 描述某 turn 可用的工具集合（名称 + schema + 契约文本）
- **BuildContextRequest / BuiltContext** — context builder 的输入/输出接口
- **DeliveryStatus** — 不同于 agent/state_machine.py 中的 DeliveryStatus（那是发送状态枚举），这个是消息投递状态

## 注意事项

- 这是项目中类型最稳定的模块，修改时需确认所有引用方
- 与 `agent/state_machine.py` 中的类型有部分重叠（如 DeliveryStatus），存在两套定义
- `__init__.py` 负责统一导出，外部模块应 `from runtime_contracts import X` 而非直接 import types
