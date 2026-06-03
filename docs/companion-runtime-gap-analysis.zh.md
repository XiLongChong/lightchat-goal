# 关系型陪伴系统当前差距分析

> 日期：2026-06-03
> 状态：初版差距分析
> 依据：
> - `docs/MODULE_BOUNDARIES.md`
> - `docs/RUNTIME_FLOW.md`
> - 当前源码快速索引：`main.py`, `agent/chat_agent.py`, `agent/tool_registry.py`, `agent/scheduler.py`, `memory/store.py`
> - 目标文档：`companion-runtime-module-contracts.zh.md`, `companion-runtime-data-contracts.zh.md`, `companion-runtime-state-machines.zh.md`

---

## 0. 结论

当前系统已经有一些重要能力：

- 有 `LLMGateway`。
- 有 native tool calls 测试。
- 有 `ToolRegistry`。
- 有 proactive scheduler。
- 有 turn diagnostics、LLM traces、history、outbound 记录。
- 有 `TemporalContextBuilder`。
- 有主动消息 delivered-only 写历史的意识。

但当前系统距离目标架构仍有核心差距：

- `main.py` 仍是运行时真相中心。
- `ChatAgent` 仍承担状态机、上下文、工具阶段、主动消息特殊规则。
- `ContextBuilder` 不是所有模型输入的唯一入口。
- 工具命名仍是 `wechat_reply` / `done` / `skip_proactive`，尚未去渠道化。
- 历史、turn-local、tool result、delivery receipt 的边界尚未成为统一数据契约。
- `MemoryStore` 太大，缺少目标语义 facade。
- 主动任务、用户提醒、仪式任务仍依赖旧表和兼容 payload。
- 测试仍大量围绕旧工具名和旧状态机。

---

## 1. 当前主要代码事实

### main.py

快速索引显示：

- `main.py` 创建 `MemoryStore`、`ChatAgent`、`ProactiveScheduler`。
- `main.py` 定义 `/chat`、admin、memory、wechat、proactive 多类 routes。
- `main.py` 中仍有 `build_enriched_system_prompt()`。
- `main.py` 中仍有 proactive heartbeat loop。
- `main.py` 中仍构造 proactive write history / post turn hooks。
- `main.py` 中存在 `wechat_sender()`。

目标差距：

```text
目标：main.py 只负责启动、依赖构造、路由挂载、lifespan。
现状：main.py 仍负责大量运行时编排、上下文、发送、历史和后台任务。
```

### agent/chat_agent.py

文件体积约 111 KB。

现有职责包括：

- 状态机主循环。
- LLM 调用编排。
- 工具分类。
- proactive phase validation。
- wechat_reply 验证、发送链路。
- skip_proactive 验证、执行链路。
- done 终态处理。
- 协议重试。
- proactive context injection。

目标差距：

```text
目标：TurnEngine 驱动状态机，但不负责上下文构建和工具暴露策略。
现状：ChatAgent 仍包含上下文构建、主动阶段工具规则和大量状态处理。
```

### agent/tool_registry.py

现有工具包括：

- `get_time`
- `get_weather`
- `schedule_wake_up`
- `wechat_reply`
- `done`
- `skip_proactive`
- deprecated `send_wechat_messages`
- deprecated `skip_reply`

目标差距：

```text
目标：send_text_message / finish_turn / skip_initiative 等渠道无关工具。
现状：核心工具仍使用 wechat_reply、done、skip_proactive。
```

### agent/context_builder.py

当前职责较轻：

- `compact_history`
- `build_chat_system_prompt`

目标差距：

```text
目标：ContextBuilder 是所有模型输入唯一入口，输出 BuiltContext + ContextSnapshot。
现状：context_builder.py 不是目标意义上的上下文打包器。
```

### memory/store.py

文件体积约 136 KB。

目标差距：

```text
目标：RuntimeStore / MemorySystem facade 提供清晰语义接口。
现状：MemoryStore 承担大量表和 CRUD，业务语义分散在调用方。
```

---

## 2. 按目标模块列差距

### InboundAdapter / InboundNormalizer

现状：

- 微信桥接通过 HTTP POST `/chat`。
- `/chat` 内部做 normalize。
- 输入仍偏 `messages` 列表。

目标：

- 所有输入先变成 `InboundMessageBatch`。
- 用户文本、媒体、系统事件、admin 事件明确区分。

差距：

- 需要抽出 normalizer。
- 需要兼容旧 `/chat` body，但入口处立刻转换。

### TurnClassifier

现状：

- route selection、reminder intent、proactive source 判断散在 `main.py` / `ChatAgent` / scheduler。

目标：

- `TurnClassifier` 产出 `TurnClassification`。

差距：

- 需要先做 facade，不要一开始替换全部业务。

### ContextBuilder

现状：

- prompt 仍由 `main.py` 和 `agent/context_builder.py` 等位置共同参与。
- proactive 上下文仍在 `ChatAgent` 中注入。
- temporal context 有独立 builder，但不是统一 ContextBuilder。

目标：

- 输入 `BuildContextRequest`。
- 输出 `BuiltContext`。
- 明确区分 recent history、turn-local state、tool result、delivery receipt。

差距：

- 这是最高优先级设计落地点。
- 先包装现有 prompt，再逐步迁移，不要一次重写模型行为。

### ToolExposurePolicy

现状：

- 工具注册表存在。
- 工具动态暴露逻辑分散在 `main.py`、`ChatAgent`、scheduler/proactive 阶段。
- 测试仍断言旧工具名。

目标：

- `ToolExposurePolicy.resolve()` 是唯一工具暴露入口。
- prompt contract 和 native schema 同源。

差距：

- 需要先建立策略层，再逐步替换旧 allowed_tools。

### ProviderGateway

现状：

- 已有 `LLMGateway`、`LLMRequest`、provider adapter。
- 已有 trace callback。

目标：

- 继续沿用，但要接收 `BuiltContext` 产物。
- 原始 request/response 与 `turn_id/call_id/snapshot_id` 关联。

差距：

- 重点不是重写 gateway，而是加强 trace 关联和结构化输出校验边界。

### ToolExecutor / DeliveryService

现状：

- fact tools 在 `agent/tools.py`。
- `wechat_reply` 在 ChatAgent 发送链路中处理。
- `wechat_sender` 回调在 `main.py`。

目标：

- 工具执行统一入口。
- 发送工具通过 DeliveryService。
- `DeliveryReceipt` 决定是否写历史。

差距：

- 需要先封装，不要直接删除现有发送链路。

### MemorySystem

现状：

- MemoryWorker / MemoryHook 条件启用。
- MemoryStore 有大量 CRUD。
- sent content 有部分 proactive delivered texts 和 outbound 记录，但不是目标统一 `SentContentMemory`。

目标：

- `MemorySystem.load_for_turn()`。
- `MemorySystem.write_after_turn()`。
- 分类返回 profile、relationship、boundary、episode、open thread、sent content。

差距：

- 需要 facade 先行，底层表可先复用。

---

## 3. 高风险差距

### 3.1 上下文污染风险

现状风险：

- 历史、tool result、proactive delivered texts、diagnostics 的边界依赖调用方习惯。
- 模型可能无法稳定区分“已完成历史”和“本轮刚发送”。

目标修复：

- `RecentConversationHistory` 只含已完成 turn。
- `CurrentTurnLocalState` 存本轮已发送、工具结果、送达状态。
- `DeliveryReceipt` 决定是否可入历史。

### 3.2 旧工具名迁移风险

现状风险：

- 测试和 prompt 仍大量使用 `wechat_reply`、`done`。
- 直接改名会破坏大量测试。

目标修复：

- 先引入 channel-neutral aliases。
- 再迁 prompt/schema/tests。
- 最后删除旧工具。

### 3.3 main.py 拆分风险

现状风险：

- main.py 中包含 route、lifespan、runtime、hook、发送、history、admin 聚合。
- 直接拆会造成行为漂移。

目标修复：

- 先抽 runtime facade，保持 route 行为不变。
- 再迁 hook 和 sender。
- 最后瘦 main.py。

### 3.4 主动任务和提醒混淆风险

现状风险：

- scheduled_messages / wake_up payload 仍有兼容逻辑。
- 用户提醒、主动 wake-up、admin scheduled task 语义容易混。

目标修复：

- 明确 `ScheduledTask` 语义。
- 到期任务先 claim。
- user reminder 不受 autonomous proactive skip 规则影响。

---

## 4. 迁移优先级

优先级从高到低：

1. 数据契约和类型骨架。
2. ToolExposurePolicy facade。
3. ContextBuilder facade + ContextSnapshot。
4. Turn-local state 与 delivery receipt 边界。
5. ProviderGateway trace 关联。
6. MemorySystem facade。
7. ToolExecutor / DeliveryService facade。
8. Runtime 拆分。
9. 工具去渠道化命名。
10. 删除旧 prompt、旧工具、旧兼容。

---

## 5. 不建议的迁移顺序

不要先做：

- 直接重写 `ChatAgent`。
- 直接删 `wechat_reply`。
- 直接把 `main.py` 拆成很多 route 文件。
- 直接引入复杂向量记忆。
- 只接入 LiteLLM/Langfuse 就认为可审计完成。

原因：

这些动作看起来大，但不能先解决上下文污染、工具暴露同源、终态结构化这些核心问题。

