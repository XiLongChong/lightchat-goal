# 关系型陪伴系统运行时数据契约

> 日期：2026-06-03
> 状态：可实现设计草案
> 上游文档：`docs/companion-runtime-module-contracts.zh.md`
> 目的：定义模块之间传递的数据结构，减少实现时的隐式猜测。

---

## 0. 设计原则

数据结构要做到三件事：

1. **角色明确**：用户消息、assistant 消息、系统事件、工具结果不能混在一起。
2. **生命周期明确**：已完成历史、本轮临时状态、审计快照、长期记忆不能混在一起。
3. **来源明确**：每个进入模型的内容都能追溯到来源。

---

## 1. 标识符

所有核心对象都应有稳定 id：

```text
turn_id
call_id
tool_call_id
message_id
snapshot_id
memory_id
delivery_receipt_id
batch_id
item_id
```

id 的作用不是美观，而是让诊断链能串起来：

```text
turn_id
-> context_snapshot_id
-> model_call_id
-> tool_call_id
-> delivery_receipt_id
-> history_message_id
```

---

## 2. InboundMessageBatch

外部输入统一结构。

```text
InboundMessageBatch
- batch_id: str
- user_id: str
- bot_id: str
- source_channel: str
- received_at: datetime
- items: list[InboundMessageItem]
- raw_event_refs: list[str]
- batch_reason: single_message | debounced_messages | scheduler_event | admin_event
```

### InboundMessageItem

```text
InboundMessageItem
- item_id: str
- kind: text | image | voice | file | system_event
- role_origin: user | admin | scheduler | bridge
- text: str | null
- media: MediaAttachment | null
- received_at: datetime
- raw_ref: str | null
- metadata: dict
```

### 约束

- `kind=text` 时 `text` 必须存在。
- `kind=image|voice|file` 时 `media` 必须存在。
- 媒体摘要不能写入 `text` 伪装成用户原话。
- scheduler 触发不能伪装成用户消息。

---

## 3. MediaAttachment

```text
MediaAttachment
- media_id: str
- kind: image | voice | file
- mime_type: str
- size_bytes: int | null
- duration_ms: int | null
- width: int | null
- height: int | null
- short_term_ref: str | null
- summary: str | null
- transcript: str | null
- tone_metadata: dict
- retention_policy: short_term_only | summary_only | do_not_store
```

### 约束

- 原始图片和原始语音默认不长期保存。
- `summary` 和 `transcript` 可进入 context snapshot。
- `short_term_ref` 只能用于当轮或短期追问。

---

## 4. TurnRequest

```text
TurnRequest
- turn_id: str
- turn_type: TurnType
- user_id: str
- bot_id: str
- inbound_batch: InboundMessageBatch | null
- runtime_trigger: RuntimeTrigger | null
- classification: TurnClassification
- config_snapshot: dict
- created_at: datetime
```

### TurnType

```text
user_chat
user_media_chat
user_reminder_request
relationship_proactive
user_reminder_due
scheduled_ritual
admin_manual_trigger
memory_management
```

---

## 5. RuntimeTrigger

后台触发统一结构。

```text
RuntimeTrigger
- trigger_id: str
- trigger_type: heartbeat | due_reminder | scheduled_ritual | manual_admin | post_user_turn
- source_task_id: str | null
- reason: str
- due_at: datetime | null
- claimed_at: datetime | null
- metadata: dict
```

### 约束

- 到期任务必须先 claim，再进入 turn。
- 没有 claim 成功的任务不能调用模型。
- heartbeat 本身不生成文案。

---

## 6. TurnClassification

```text
TurnClassification
- turn_type: TurnType
- trigger_source: user | scheduler | admin | bridge
- user_intent: UserIntent
- media_intent: MediaIntent | null
- confidence: float
- reason: str
```

### UserIntent

```text
chat
request_reminder
request_image
forget_directive
relationship_feedback
task_feedback
unknown
```

---

## 7. CurrentTurnLocalState

本轮内临时状态，只在本轮结束前给模型看，不混入真实历史。

```text
CurrentTurnLocalState
- turn_id: str
- model_call_count: int
- tool_call_count: int
- delivered_message_count: int
- send_attempt_count: int
- failed_send_attempt_count: int
- sent_messages_this_turn: list[TurnLocalSentMessage]
- tool_results_this_turn: list[ToolExecutionResult]
- rejected_tool_calls: list[RejectedToolCall]
- budget_state: TurnBudgetState
- terminal_status: not_finished | finishing | finished
```

### TurnLocalSentMessage

```text
TurnLocalSentMessage
- message_id: str
- tool_call_id: str
- text: str
- status: delivered | failed | unknown
- delivered_at: datetime | null
- delivery_receipt_id: str | null
```

### 约束

- `sent_messages_this_turn` 在 turn 结束前不进入 `RecentConversationHistory`。
- 只有 `status=delivered` 的消息可在 turn 结束后写入真实历史。

---

## 8. RecentConversationHistory

已完成 turn 的真实历史。

```text
RecentConversationHistory
- messages: list[ConversationHistoryMessage]
- window_policy: count | time | token_budget
- source: persistent_history
```

### ConversationHistoryMessage

```text
ConversationHistoryMessage
- message_id: str
- turn_id: str
- role: user | assistant
- content: str
- source: user_message | assistant_reply | assistant_proactive | reminder_reply
- delivery_status: delivered | not_applicable
- created_at: datetime
- metadata: dict
```

### 约束

- `assistant` 历史必须代表用户已经看见或系统确认送达的内容。
- 未送达 assistant 文本不能进入 history。
- 工具结果不能进入 history。
- debug 信息不能进入 history。

---

## 9. ToolExposure

```text
ToolExposure
- exposure_id: str
- turn_id: str
- phase: before_first_message | after_message | forced_speak | reminder_delivery | normal_chat
- exposed_tool_names: list[str]
- native_tool_schemas: list[NativeToolSchema]
- tool_contract_block: str
- denied_tools: list[DeniedTool]
- reasons: list[str]
```

### NativeToolSchema

```text
NativeToolSchema
- name: str
- description: str
- parameters_schema: dict
- terminal: bool
- side_effect: none | sends_message | writes_state | reads_external
```

### 约束

- `tool_contract_block` 和 `native_tool_schemas` 必须来自同一个 `ToolExposure`。
- schema 中有的工具必须有 executor。
- prompt 中说有的工具必须在 schema 中。

---

## 10. BuiltContext

```text
BuiltContext
- call_id: str
- turn_id: str
- provider_messages: list[ProviderMessage]
- exposed_tool_schemas: list[NativeToolSchema]
- context_blocks: list[ContextBlock]
- context_snapshot: ContextSnapshot
- token_budget_report: TokenBudgetReport
```

### ProviderMessage

```text
ProviderMessage
- role: system | user | assistant | tool
- content: str | list[ContentPart]
- name: str | null
- tool_call_id: str | null
```

### ContextBlock

```text
ContextBlock
- block_id: str
- kind: fixed_protocol | persona | relationship_state | memory | recent_history | turn_local | media | current_input | tool_contract | terminal_policy
- cache_class: fixed | low_frequency | turn_dynamic
- content_summary: str
- content_ref: str | null
- source: str
- enters_model: bool
- enters_snapshot: bool
```

---

## 11. ContextSnapshot

```text
ContextSnapshot
- snapshot_id: str
- turn_id: str
- call_id: str
- created_at: datetime
- block_summaries: list[ContextBlock]
- exposed_tool_names: list[str]
- history_message_ids: list[str]
- memory_ids: list[str]
- turn_local_summary: dict
- terminal_policy: TerminalPolicy
- full_payload_ref: str | null
```

### 约束

- 默认保存摘要，不保存完整原始媒体。
- debug 模式可保存完整 provider payload，但必须短期保留。
- snapshot 是审计材料，不自动进入下一轮模型上下文。

---

## 12. ModelCallResult

```text
ModelCallResult
- call_id: str
- turn_id: str
- provider: str
- model: str
- raw_request_ref: str | null
- raw_response_ref: str | null
- parsed_tool_calls: list[ModelToolCall]
- parsed_text: str | null
- validation_status: valid | invalid_structure | provider_error
- usage: dict
- latency_ms: int
```

### ModelToolCall

```text
ModelToolCall
- tool_call_id: str
- name: str
- arguments: dict
- raw_arguments: str | null
```

---

## 13. ToolExecutionResult

```text
ToolExecutionResult
- tool_call_id: str
- turn_id: str
- tool_name: str
- status: success | rejected | failed
- result_payload: dict
- delivery_receipt: DeliveryReceipt | null
- error_code: str | null
- error_message: str | null
- visible_to_model: bool
```

### RejectedToolCall

```text
RejectedToolCall
- tool_call_id: str
- tool_name: str
- reason: tool_not_exposed | invalid_arguments | invalid_phase | budget_exceeded
- exposed_tool_names: list[str]
```

---

## 14. DeliveryReceipt

```text
DeliveryReceipt
- delivery_receipt_id: str
- message_id: str
- channel: str
- status: delivered | failed | unknown
- provider_message_id: str | null
- delivered_at: datetime | null
- error_code: str | null
- error_message: str | null
```

### 约束

- 只有 `delivered` 可写入 assistant history。
- `failed` 进入 diagnostics。
- `unknown` 不能自动视为 delivered。

---

## 15. TerminalPolicy

```text
TerminalPolicy
- turn_type: TurnType
- required_terminal_tools: list[str]
- allowed_terminal_states: list[str]
- requires_next_wake: bool
- min_delivered_messages: int
- max_delivered_messages: int
- allows_skip: bool
- forced_speak: bool
```

### 示例

主动可跳过：

```text
requires_next_wake: true
min_delivered_messages: 0
allows_skip: true
required_terminal_tools: [finish_turn]
```

主动强制开口：

```text
requires_next_wake: true
min_delivered_messages: 1
allows_skip: false
forced_speak: true
required_terminal_tools: [finish_turn]
```

---

## 16. MemorySelection

```text
MemorySelection
- profile_memory: list[MemoryRecord]
- relationship_memory: list[MemoryRecord]
- boundary_memory: list[MemoryRecord]
- episode_memory: list[MemoryRecord]
- open_thread_memory: list[MemoryRecord]
- sent_content_memory: list[SentContentRecord]
- selection_reasons: list[str]
```

### MemoryRecord

```text
MemoryRecord
- memory_id: str
- category: profile | relationship | boundary | episode | open_thread
- content: str
- source_turn_id: str | null
- created_at: datetime
- updated_at: datetime
- retention_policy: long_term | short_term | decays | hidden
- privacy_level: normal | sensitive | do_not_surface
```

### SentContentRecord

```text
SentContentRecord
- sent_content_id: str
- turn_id: str
- source: assistant_reply | assistant_proactive | reminder_reply
- text: str
- normalized_text: str
- fingerprint: str
- sent_at: datetime
```

---

## 17. TurnResult

```text
TurnResult
- turn_id: str
- terminal_state: str
- success: bool
- delivered_messages: list[TurnLocalSentMessage]
- skipped: bool
- failed: bool
- model_call_count: int
- tool_call_count: int
- context_snapshot_ids: list[str]
- model_call_ids: list[str]
- diagnostics: dict
```

---

## 18. 禁止混用清单

这些结构不能互相替代：

- `CurrentTurnLocalState` 不能当作 `RecentConversationHistory`。
- `ContextSnapshot` 不能当作模型上下文来源。
- `ToolExecutionResult` 不能当作用户消息。
- `MediaAttachment.summary` 不能当作用户原文。
- `MemoryRecord` 不能当作真实历史。
- `DeliveryReceipt.failed` 不能当作 assistant 已说过。

