# 关系型陪伴系统运行时模块契约

> 日期：2026-06-03
> 状态：可实现设计草案
> 上游文档：
> - `docs/companion-architecture-requirements.zh.md`
> - `docs/companion-architecture-design-decisions.zh.md`
> 目的：把架构方向拆成明确模块、通信方式和职责边界，避免后续实现时边干边猜。

---

## 0. 阅读顺序

本轮设计文档建议按这个顺序阅读：

1. `companion-runtime-module-contracts.zh.md`：系统有哪些模块，每个模块负责什么。
2. `companion-runtime-data-contracts.zh.md`：模块之间传什么数据。
3. `companion-runtime-state-machines.zh.md`：每种 turn 怎么流转和结束。
4. `companion-runtime-gap-analysis.zh.md`：当前代码和目标设计差在哪里。
5. `companion-runtime-implementation-plan.zh.md`：按什么批次开发。
6. `companion-runtime-test-plan.zh.md`：怎么验收，防止只做到“能跑”。

---

## 1. 总原则

运行时模块设计的核心原则是：

```text
外部输入可以复杂。
核心协议必须简单。
模块职责必须单一。
模型输入必须可审计。
工具调用必须可验证。
状态结束必须结构化。
```

系统不能再依赖“某个函数里顺手拼一段 prompt”或“模型应该能理解我刚刚说过什么”。

每一轮都必须能回答：

- 本轮是什么类型。
- 模型看到了哪些历史。
- 模型看到了哪些本轮临时状态。
- 模型能调用哪些工具。
- 模型返回了什么结构化动作。
- 哪些工具真的执行了。
- 哪些消息送达了。
- 本轮为什么结束。
- 哪些内容进入了记忆候选。

---

## 2. 模块总览

目标运行时由这些模块组成：

```text
InboundAdapter
  外部渠道输入解析。

InboundNormalizer
  把不同渠道消息统一成 InboundMessageBatch。

TurnClassifier
  判断本轮 turn_type。

TurnEngine
  驱动一轮 turn 的状态机。

ContextBuilder
  构建每次模型调用的上下文。

ToolExposurePolicy
  决定本轮模型能看到哪些工具。

ProviderGateway
  调用模型供应商，并强制结构化输出。

ToolExecutor
  执行被允许的工具。

DeliveryService
  负责把输出消息发到真实渠道，并返回送达结果。

MemorySystem
  选择本轮可用记忆，处理本轮结束后的记忆候选。

ContextSnapshotStore
  保存模型输入快照和调试摘要。

SchedulerRuntime
  心跳、定时任务领取、主动冷启动触发。

RuntimeStore
  对持久化层做目标语义封装。

ObservabilityRuntime
  关联 turn、模型请求、模型响应、工具调用和送达结果。
```

---

## 3. 模块依赖关系

目标依赖方向：

```text
Routes / Bridge
  -> InboundAdapter
  -> InboundNormalizer
  -> TurnClassifier
  -> TurnEngine
       -> ToolExposurePolicy
       -> ContextBuilder
            -> MemorySystem
            -> RuntimeStore
       -> ProviderGateway
       -> ToolExecutor
            -> DeliveryService
            -> RuntimeStore
       -> ContextSnapshotStore
       -> ObservabilityRuntime
```

禁止反向依赖：

- `ContextBuilder` 不能调用 `ToolExecutor`。
- `MemorySystem` 不能调用 `DeliveryService`。
- `ProviderGateway` 不能知道主动消息规则。
- `ToolExecutor` 不能自己补 prompt。
- `DeliveryService` 不能自己写对话历史。
- `Routes` 不能直接拼模型输入。

---

## 4. InboundAdapter

### 职责

`InboundAdapter` 负责把外部渠道事件解析出来。

例如：

- 微信文字。
- 微信图片。
- 微信语音。
- 管理台手动触发。
- HTTP `/chat` 调用。
- scheduler 内部事件。

### 输入

```text
RawChannelEvent
- channel
- raw_payload
- received_at
- auth_context
```

### 输出

```text
ParsedInboundEvent
- channel
- external_user_id
- bot_id
- raw_items
- source_metadata
```

### 不负责

- 不判断是否要回复。
- 不构建 prompt。
- 不读取长期记忆。
- 不调用模型。
- 不发送回复。

### 反应规则

- 无法识别渠道 payload：返回 `inbound_parse_failed`，不进入模型。
- 缺少用户标识：返回 `missing_user_identity`，不进入模型。
- 媒体还没下载：保留 `media_ref`，由后续媒体处理模块决定是否短期缓存。

---

## 5. InboundNormalizer

### 职责

把不同渠道输入统一成 `InboundMessageBatch`。

目标是让核心 runtime 不关心消息来自微信、管理台还是测试端。

### 输入

```text
ParsedInboundEvent
```

### 输出

```text
InboundMessageBatch
```

### 关键规则

- 多条短时间连续消息应合并为一个 batch。
- 每个 item 必须有 `role_origin=user|system_event|admin_event`。
- 媒体 item 必须标明 `kind=image|voice|file`。
- 媒体摘要和用户原文必须分开。

### 不负责

- 不决定本轮工具。
- 不决定是否主动开口。
- 不做记忆检索。

---

## 6. TurnClassifier

### 职责

判断本轮属于哪种 `turn_type`。

### 输入

```text
InboundMessageBatch
RuntimeTrigger
CurrentUserState
```

### 输出

```text
TurnClassification
- turn_type
- trigger_source
- confidence
- reason
```

### turn_type 初始集合

```text
user_chat
user_reminder_request
user_media_chat
relationship_proactive
user_reminder_due
scheduled_ritual
admin_manual_trigger
memory_management
```

### 反应规则

- 用户真实消息优先于后台主动事件。
- 用户授权提醒到期优先于普通主动冷启动。
- 用户明确“别记这个”应进入 `memory_management` 或在普通聊天中附带 `forget_directive`。
- 无法判断时默认为 `user_chat`，但 classification reason 必须记录。

### 不负责

- 不拼上下文。
- 不选择工具 schema。
- 不调用模型。

---

## 7. TurnEngine

### 职责

`TurnEngine` 是一轮 turn 的主状态机。

它负责组织：

```text
build context
-> call model
-> validate structured output
-> execute tool
-> update turn local state
-> maybe call model again
-> finish turn
```

### 输入

```text
TurnRequest
- turn_id
- turn_type
- inbound_batch
- trigger
- user_id
- bot_id
- runtime_config
```

### 输出

```text
TurnResult
- terminal_state
- delivered_messages
- skipped
- failed
- tool_results
- context_snapshot_ids
- memory_candidate_ids
- diagnostics
```

### 必须维护的本轮状态

```text
CurrentTurnLocalState
- model_call_count
- tool_call_count
- delivered_message_count
- send_attempt_count
- failed_send_attempt_count
- sent_messages_this_turn
- tool_results_this_turn
- last_model_output
- terminal_candidate
```

### 不负责

- 不自己决定长期记忆写入。
- 不直接拼 provider messages。
- 不直接访问渠道 API。
- 不自己决定工具暴露规则。

### 反应规则

- 模型输出非法：不执行工具，进入结构修复重试。
- 工具未暴露：拒绝执行，记录 `tool_not_exposed`。
- 工具参数非法：拒绝执行，记录 `invalid_tool_arguments`。
- 发送失败：记录 delivery failure，允许按 turn policy 决定是否重试。
- 到达预算上限：进入安全终态，不继续调用模型。

---

## 8. ContextBuilder

### 职责

`ContextBuilder` 负责每次模型调用前打包模型实际看到的内容。

它是所有模型输入的唯一入口。

### 输入

```text
BuildContextRequest
- turn_id
- turn_type
- user_id
- bot_id
- inbound_batch
- turn_classification
- current_turn_local_state
- tool_exposure
- terminal_policy
- memory_selection
- recent_history
- relationship_state
- media_context
- system_time
- debug_mode
```

### 输出

```text
BuiltContext
- provider_messages
- exposed_tool_schemas
- tool_contract_block
- context_snapshot
- debug_summary
- token_budget_report
```

### 打包顺序

模型看到的上下文必须按固定顺序：

```text
1. fixed_system_protocol
2. fixed_output_protocol
3. fixed_tool_protocol
4. persona
5. relationship_state
6. boundary_memory
7. selected_profile_memory
8. selected_episode_memory
9. selected_open_threads
10. recent_conversation_history
11. current_turn_local_state
12. media_context
13. current_input
14. exposed_tools_contract
15. terminal_policy
```

### 最近历史格式

`recent_conversation_history` 只包含已经完成的真实历史。

每条历史必须有：

```text
ConversationHistoryMessage
- message_id
- turn_id
- role: user | assistant
- content
- source
- delivery_status
- created_at
```

关键要求：

- 用户消息必须是 `role=user`。
- bot 已送达消息必须是 `role=assistant` 且 `delivery_status=delivered`。
- 未送达消息不能伪装成历史。
- 工具结果不能伪装成用户消息。
- 媒体摘要不能伪装成用户原话。

### 本轮临时状态格式

本轮刚发生的事放在 `current_turn_local_state`。

例如同一轮内已经发出一条消息：

```text
Current turn sent messages:
- status: delivered
  text: "今晚早点睡。"
  delivered_count_this_turn: 1
```

这条消息在本轮结束前不能混进 `recent_conversation_history`。

### 上下文污染防线

ContextBuilder 必须防止这些污染：

- 同一条 assistant 消息同时出现在 history 和 turn-local。
- 工具返回内容被当成用户说的话。
- 媒体摘要被当成用户原文。
- debug snapshot 内容进入模型。
- 未送达消息进入已完成历史。
- 旧 prompt 中的工具说明和本轮工具 schema 不一致。

### 不负责

- 不执行工具。
- 不发送消息。
- 不调用模型。
- 不写长期记忆。
- 不修改 turn state。

---

## 9. ToolExposurePolicy

### 职责

决定本轮模型能看到哪些工具。

### 输入

```text
ToolExposureRequest
- turn_type
- trigger_source
- current_turn_local_state
- user_intent
- media_intent
- initiative_state
- reminder_state
- runtime_config
```

### 输出

```text
ToolExposure
- exposed_tool_names
- native_tool_schemas
- tool_contract_block
- reasons
- denied_tools
```

### 基础规则

- 普通聊天：`send_text_message`, `finish_turn`, 必要的信息工具。
- 用户请求图片：额外暴露 `generate_image_message` 或 `send_image_message`。
- 主动消息可跳过阶段：`send_text_message`, `skip_initiative`, `finish_turn`。
- 主动消息强制开口阶段：`send_text_message`, `finish_turn`。
- 用户提醒到期：`send_text_message`, `mark_reminder_delivered`, `finish_turn`。

### 不负责

- 不执行工具。
- 不判断工具参数是否合法。
- 不构建完整上下文。

### 反应规则

- 如果配置中存在工具但当前 turn 不允许，加入 `denied_tools`，不暴露给模型。
- 如果 prompt contract 和 native schema 不一致，构建失败，不调用模型。
- 如果工具没有 executor，构建失败，不调用模型。

---

## 10. ProviderGateway

### 职责

调用模型供应商，并把系统协议映射到 provider API。

### 输入

```text
ModelCallRequest
- turn_id
- call_id
- provider_messages
- native_tool_schemas
- response_format
- model_profile
- timeout
```

### 输出

```text
ModelCallResult
- raw_request_ref
- raw_response_ref
- parsed_tool_calls
- parsed_message
- usage
- latency_ms
- validation_status
```

### 必须支持

- native tool calls。
- JSON schema / response_format。
- 原始 request/response trace。
- provider 错误归一化。

### 不负责

- 不知道主动消息规则。
- 不决定工具是否可执行。
- 不写对话历史。

---

## 11. ToolExecutor

### 职责

执行通过校验的工具调用。

### 输入

```text
ToolExecutionRequest
- turn_id
- tool_call_id
- tool_name
- arguments
- current_turn_local_state
- exposed_tool_names
```

### 输出

```text
ToolExecutionResult
- tool_call_id
- tool_name
- status
- result_payload
- side_effect
- delivery_receipt
- error
```

### 执行前校验

```text
1. tool_name 是否在 exposed_tool_names。
2. arguments 是否符合 schema。
3. 当前 turn state 是否允许。
4. 是否超过 turn budget。
5. 是否会违反主动消息/图片/提醒边界。
```

### 不负责

- 不选择工具。
- 不拼 prompt。
- 不直接让模型重试。

---

## 12. DeliveryService

### 职责

把 `send_text_message` 等输出工具真正发送到渠道。

### 输入

```text
OutboundMessage
- kind
- text
- media_ref
- target_user
- channel
- delivery_policy
```

### 输出

```text
DeliveryReceipt
- status: delivered | failed | skipped | unknown
- provider_message_id
- delivered_at
- error
```

### 关键规则

- 只有 `delivered` 才能进入真实历史。
- `failed` 可以进入诊断，但不能伪装成 assistant 历史。
- `unknown` 不能默认当作 delivered。

---

## 13. MemorySystem

### 职责

MemorySystem 分两段工作：

```text
模型调用前：选择哪些记忆进入上下文。
本轮结束后：处理哪些内容成为记忆候选或写入长期记忆。
```

### 调用前输入

```text
MemorySelectionRequest
- user_id
- bot_id
- turn_type
- inbound_batch
- recent_history
- relationship_state
- forget_directives
```

### 调用前输出

```text
MemorySelection
- profile_memory
- relationship_memory
- boundary_memory
- episode_memory
- open_thread_memory
- sent_content_memory
- selection_reasons
```

### 本轮结束后输入

```text
MemoryWriteRequest
- turn_result
- delivered_messages
- inbound_batch
- context_snapshot
- forget_directives
```

### 关键规则

- 长期记忆写入必须严格。
- “别记这个”必须优先于抽取。
- 负面情绪默认短期衰减。
- sent content memory 不是关系记忆，但要参与去重。
- 模型可以提供候选，最终写入由 MemorySystem 决定。

---

## 14. ContextSnapshotStore

### 职责

保存“模型当时看到了什么”的审计记录。

### 保存内容

```text
ContextSnapshot
- snapshot_id
- turn_id
- call_id
- block_summaries
- exposed_tool_names
- terminal_policy
- memory_ids
- history_message_ids
- turn_local_state_summary
- token_budget_report
```

### 默认策略

- 默认保存摘要。
- debug 模式可短期保存完整 provider messages。
- 原始媒体不长期保存。
- API key、认证 token 永远不保存。

---

## 15. SchedulerRuntime

### 职责

处理后台心跳、定时任务和主动冷启动。

### 心跳顺序

```text
heartbeat tick
-> check global proactive switch
-> claim due user reminder or scheduled ritual
-> if claimed, create due-task turn
-> else check relationship proactive eligibility
-> if eligible, create proactive turn
-> else end heartbeat
```

### 不负责

- 不生成文案。
- 不拼上下文。
- 不直接调用模型。

---

## 16. ObservabilityRuntime

### 职责

把系统内部 trace 和模型原始 I/O 关联起来。

### 必须关联

```text
turn_id
call_id
context_snapshot_id
raw_model_request_ref
raw_model_response_ref
tool_call_id
delivery_receipt_id
```

这能回答：

- 模型到底收到了什么。
- 模型原样返回了什么。
- 系统为什么拒绝某个工具。
- 哪条消息真的送达。

---

## 17. 完成标准

本模块契约完成后，后续实现不得违反：

- 所有模型输入经过 ContextBuilder。
- 所有工具暴露经过 ToolExposurePolicy。
- 所有工具执行经过 ToolExecutor。
- 所有外部发送经过 DeliveryService。
- 所有长期记忆写入经过 MemorySystem。
- 所有模型调用可以用 turn_id 查到 context snapshot。

