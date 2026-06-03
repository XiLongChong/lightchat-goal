# TurnEngine 可执行规格说明书

> 日期：2026-06-03
> 状态：可执行实现规格
> 上游文档：
> - `docs/companion-architecture-requirements.zh.md` (尤其 sections 2, 3, 4, 5, 13.3)
> - `docs/companion-architecture-design-decisions.zh.md` (尤其 sections 3, 4, 9.4)
> - `docs/companion-runtime-module-contracts.zh.md` (尤其 sections 7, 10, 11, 12)
> - `docs/companion-runtime-data-contracts.zh.md` (尤其 sections 4, 5, 7, 8, 12, 13, 14, 17)
> - `docs/companion-runtime-state-machines.zh.md` (全文)
> - `docs/companion-runtime-gap-analysis.zh.md` (尤其 sections 1, 3.1, 3.3)
> - `docs/companion-runtime-implementation-plan.zh.md` (尤其 phases 5, 6, 7, 12)
> - `docs/companion-runtime-test-plan.zh.md` (尤其 sections 5, 6)
> 目的：TurnEngine 的完整可执行规格，驱动所有实现和测试。

---

## 1. 模块目标

`TurnEngine` 是一轮对话回合的状态机驱动器。它编排整个 turn 循环：

```text
build context -> call model -> validate output -> execute tools
-> update turn-local state -> check terminal -> loop or finish
```

### TurnEngine 负责

| 职责 | 说明 |
|------|------|
| 状态机流转 | 驱动 turn 从一个 state 到下一个 state |
| 中断检查 | 在 6 个关键检查点检查是否被新事件 supersede |
| 预算控制 | 检查 model_call_count、tool_call_count、delivered_message_count、send_attempt_count、elapsed_seconds 是否超出上限 |
| 输出校验 | 确保模型输出是结构化 native tool calls，拒绝纯文本 assistant.content |
| 工具调用校验 | 校验工具是否在 exposed 集合内、参数是否合法、阶段是否允许 |
| turn-local 状态管理 | 维护 CurrentTurnLocalState，区分已完成历史与本轮临时状态 |
| 终态判断 | 根据 turn type、工具调用结果、送达状态决定终态 |
| 硬兜底 | 当不可恢复错误发生时进入 HARD_FALLBACK |
| Post-terminal pipeline | 执行 WRITE_MODEL_HISTORY + POST_TURN_HOOKS |
| 锁释放 | turn 结束后解除用户锁 |

### TurnEngine 不负责（委托给其他模块）

| 职责 | 委托给 |
|------|--------|
| 拼系统提示词 | ContextBuilder |
| 组装 provider messages | ContextBuilder |
| 工具暴露决策 | ToolExposurePolicy |
| 直接执行工具 | ToolExecutor |
| 发消息到渠道 | DeliveryService |
| 长期记忆读写 | MemorySystem |
| 上下文快照保存 | ContextSnapshotStore |
| 模型 API 调用 | ProviderGateway |
| 心跳/定时任务触发 | SchedulerRuntime |
| turn_type 分类 | TurnClassifier |
| 输入归一化 | InboundNormalizer |

---

## 2. 完整输入输出对象

### 2.1 TurnRequest（进入 TurnEngine 的请求）

```text
TurnRequest
├── turn_id: str                         # 唯一 turn 标识
├── turn_type: TurnType                  # user_chat | user_media_chat | user_reminder_request
│                                        # | relationship_proactive | user_reminder_due
│                                        # | scheduled_ritual | admin_manual_trigger | memory_management
├── user_id: str                         # 用户标识
├── bot_id: str                          # bot 标识
├── inbound_batch: InboundMessageBatch | null
│   ├── batch_id: str
│   ├── user_id: str
│   ├── bot_id: str
│   ├── source_channel: str              # wechat | admin | scheduler | bridge
│   ├── received_at: datetime
│   ├── items: list[InboundMessageItem]
│   │   ├── item_id: str
│   │   ├── kind: text | image | voice | file | system_event
│   │   ├── role_origin: user | admin | scheduler | bridge
│   │   ├── text: str | null
│   │   ├── media: MediaAttachment | null
│   │   ├── received_at: datetime
│   │   └── metadata: dict
│   └── batch_reason: single_message | debounced_messages | scheduler_event | admin_event
├── runtime_trigger: RuntimeTrigger | null
│   ├── trigger_id: str
│   ├── trigger_type: heartbeat | due_reminder | scheduled_ritual | manual_admin | post_user_turn
│   ├── source_task_id: str | null
│   ├── reason: str
│   ├── due_at: datetime | null
│   └── claimed_at: datetime | null
├── classification: TurnClassification
│   ├── turn_type: TurnType
│   ├── trigger_source: user | scheduler | admin | bridge
│   ├── user_intent: chat | request_reminder | request_image | forget_directive | relationship_feedback | task_feedback | unknown
│   ├── media_intent: MediaIntent | null
│   ├── confidence: float
│   └── reason: str
├── tool_exposure: ToolExposure
│   ├── exposure_id: str
│   ├── turn_id: str
│   ├── phase: skippable | after_first_message | forced_speak | reminder_delivery | normal_chat | memory_management | admin
│   ├── exposed_tool_names: list[str]
│   ├── native_tool_schemas: list[NativeToolSchema]
│   ├── tool_contract_block: str
│   ├── denied_tools: list[DeniedTool]
│   └── reasons: list[str]
├── terminal_policy: TerminalPolicy
│   ├── turn_type: TurnType
│   ├── required_terminal_tools: list[str]          # 必须调用的终态工具，如 ["finish_turn"]
│   ├── allowed_terminal_states: list[str]           # 允许的终态状态列表
│   ├── requires_next_wake: bool                     # 是否需要设置下一次唤醒时间
│   ├── min_delivered_messages: int                  # 最少需送达的消息数（0 表示可选）
│   ├── max_delivered_messages: int                  # 最大允许送达的消息数（默认 15）
│   ├── allows_skip: bool                            # 是否允许 skip（仅主动回合）
│   └── forced_speak: bool                           # 是否强制开口（上次 skip 后）
├── memory_selection: MemorySelection
│   ├── profile_memory: list[MemoryRecord]
│   ├── relationship_memory: list[MemoryRecord]
│   ├── boundary_memory: list[MemoryRecord]
│   ├── episode_memory: list[MemoryRecord]
│   ├── open_thread_memory: list[MemoryRecord]
│   ├── sent_content_memory: list[SentContentRecord]
│   └── selection_reasons: list[str]
├── recent_history: RecentConversationHistory
│   └── messages: list[ConversationHistoryMessage]
│       ├── message_id: str
│       ├── turn_id: str
│       ├── role: user | assistant
│       ├── content: str
│       ├── source: user_message | assistant_reply | assistant_proactive | reminder_reply
│       ├── delivery_status: delivered | not_applicable
│       └── created_at: datetime
├── relationship_state: RelationshipState | null
│   ├── stage: str
│   ├── intimacy_level: str
│   ├── current_mood_between_them: str
│   ├── allowed_teasing_level: str
│   ├── jealousy_allowed: bool
│   ├── conflict_mode: str
│   └── active_boundaries: list[str]
├── initiative_state: InitiativeState | null
│   ├── last_trigger_at: float | null
│   ├── last_spoke_at: float | null
│   ├── last_skipped_at: float | null
│   ├── consecutive_skip_count: int
│   ├── next_wake_after: float | null
│   ├── next_wake_before: float | null
│   ├── intensity_profile: str
│   └── last_decision: spoke | skipped | null
├── reminder_state: ReminderState | null
├── config_snapshot: dict                    # 运行时配置快照
├── runtime_config: RuntimeConfig
│   ├── max_model_calls: int                 # 默认 20
│   ├── max_tool_calls: int                  # 默认 40
│   ├── max_delivered_messages: int          # 默认 15
│   ├── max_send_attempts: int               # 默认 3
│   ├── max_elapsed_seconds: float           # 默认 120.0
│   ├── max_protocol_retries: int            # 默认 1
│   ├── test_mode: bool
│   └── debug_mode: bool
├── interrupt_checker: InterruptChecker | null  # 生产环境必填
├── wechat_sender: WeChatSender | null          # 消息发送回调
└── created_at: datetime
```

### 2.2 CurrentTurnLocalState（turn 内部可变状态）

TurnEngine 在 turn 生命周期内维护此状态。**不在 turn 结束前写入持久历史。**

```text
CurrentTurnLocalState
├── turn_id: str
├── model_call_count: int                     # 本轮已调用模型的次数
├── tool_call_count: int                      # 本轮已执行工具调用次数
├── delivered_message_count: int              # 本轮已成功送达的消息数（受 15 条硬上限控制）
├── send_attempt_count: int                   # 本轮发送尝试次数（含失败重试）
├── failed_send_attempt_count: int            # 本轮发送失败次数
├── sent_messages_this_turn: list[TurnLocalSentMessage]
│   ├── message_id: str                       # msg_{turn_id}_{index}
│   ├── tool_call_id: str                     # 对应的模型 tool_call_id
│   ├── text: str                             # 发送的文本
│   ├── status: delivered | failed | unknown
│   ├── delivered_at: datetime | null
│   └── delivery_receipt_id: str | null
├── tool_results_this_turn: list[ToolExecutionResult]
│   ├── tool_call_id: str
│   ├── turn_id: str
│   ├── tool_name: str
│   ├── status: success | rejected | failed
│   ├── result_payload: dict
│   ├── delivery_receipt: DeliveryReceipt | null
│   ├── error_code: str | null
│   ├── error_message: str | null
│   └── visible_to_model: bool
├── rejected_tool_calls: list[RejectedToolCall]
│   ├── tool_call_id: str
│   ├── tool_name: str
│   ├── reason: tool_not_exposed | invalid_arguments | invalid_phase | budget_exceeded
│   └── exposed_tool_names: list[str]
├── budget_state: TurnBudgetState
│   ├── model_calls_remaining: int
│   ├── tool_calls_remaining: int
│   ├── delivered_messages_remaining: int
│   ├── send_attempts_remaining: int
│   ├── elapsed_seconds: float
│   └── exhausted_budget_type: str | null     # 哪个预算先耗尽
├── terminal_status: not_finished | finishing | finished
├── interrupted: bool                         # 本轮是否被中断过
├── interrupt_token: str                      # it_{turn_id}_{timestamp_ms}
├── last_model_output: ModelCallResult | null # 最后一次模型调用结果
├── context_snapshot_ids: list[str]           # 本轮所有 context snapshot ID
├── model_call_ids: list[str]                 # 本轮所有模型调用 ID
├── diagnostics: dict                         # 诊断信息（不进入模型上下文）
└── last_tool_call_classification: str        # fact | terminal | state | invalid | protocol_violation
```

### 2.3 TurnBudget（本轮预算控制）

```text
TurnBudget
├── max_model_calls: int = 20              # 最多 20 次模型调用
├── max_tool_calls: int = 40               # 最多 40 次工具执行
├── max_delivered_messages: int = 15       # 最多 15 条已送达消息（硬上限）
├── max_send_attempts: int = 3             # 最多 3 次发送尝试
├── max_protocol_retries: int = 1          # 最多 1 次协议违规重试
├── max_elapsed_seconds: float = 120.0     # 最多 120 秒 wall-clock 时间
├── model_call_count: int = 0              # 已用模型调用次数
├── tool_call_count: int = 0               # 已用工具调用次数
├── delivered_message_count: int = 0       # 已送达消息数
├── send_attempt_count: int = 0            # 已用发送尝试次数
├── protocol_retry_count: int = 0          # 已用协议重试次数
└── start_time: float = now()              # wall-clock 开始时间
```

### 2.4 TerminalPolicy（每种 turn type 的终态策略）

```text
TerminalPolicy (按 turn_type 配置)
├── turn_type: TurnType
├── required_terminal_tools: list[str]     # 终态需要调用的工具，如 ["finish_turn"]
├── allowed_terminal_states: list[str]     # 此 turn type 合法终态列表
├── requires_next_wake: bool               # 是否必须在 finish_turn 中传 next_wake_minutes
├── min_delivered_messages: int            # 最少需送达的消息数
├── max_delivered_messages: int            # 最大允许送达的消息数
├── allows_skip: bool                      # 是否允许 skip_initiative
├── forced_speak: bool                     # 是否强制开口（禁止 skip）
├── allows_fact_tools: bool                # 是否暴露信息工具
├── allows_image_tools: bool               # 是否暴露图片输出工具
└── allows_reminder_tools: bool            # 是否暴露提醒工具
```

各 turn type 默认 TerminalPolicy：

| turn_type | required_terminal_tools | min_delivered | max_delivered | allows_skip | forced_speak | requires_next_wake |
|-----------|------------------------|--------------|--------------|-------------|-------------|-------------------|
| user_chat | [finish_turn] | 0 | 15 | false | false | false |
| user_media_chat | [finish_turn] | 0 | 15 | false | false | false |
| user_reminder_request | [finish_turn] | 0 | 15 | false | false | false |
| user_reminder_due | [finish_turn] | 1 | 5 | false | false | false |
| relationship_proactive (可跳过) | [finish_turn] | 0 | 5 | true | false | true |
| relationship_proactive (强制开口) | [finish_turn] | 1 | 5 | false | true | true |
| scheduled_ritual | [finish_turn] | 1 | 3 | false | false | false |
| admin_manual_trigger | [finish_turn] | 0 | 15 | false | false | false |
| memory_management | [finish_turn] | 0 | 5 | false | false | false |

### 2.5 TurnResult（TurnEngine 输出）

```text
TurnResult
├── turn_id: str
├── terminal_state: str                      # 终态标识符
├── success: bool                            # 是否至少有一条消息成功送达
├── delivered_messages: list[TurnLocalSentMessage]  # 已成功送达的消息列表
├── skipped: bool                            # 是否主动跳过（仅 proactive/skip 路径）
├── failed: bool                             # 是否完全失败（无任何送达消息）
├── model_call_count: int                    # 本轮总模型调用次数
├── tool_call_count: int                     # 本轮总工具调用次数
├── delivered_message_count: int             # 本轮成功送达消息数
├── send_attempt_count: int                  # 本轮发送尝试次数（含失败）
├── failed_send_attempt_count: int           # 本轮发送失败次数
├── context_snapshot_ids: list[str]          # 所有 context snapshot ID
├── model_call_ids: list[str]                # 所有模型调用 ID
├── budget_state: TurnBudgetState            # 预算状态（哪些已耗尽）
├── diagnostics: dict
│   ├── fallback_events: list[dict]          # fallback 事件
│   ├── context_warnings: list[dict]         # 上下文构建警告
│   ├── rejected_tool_calls: list[dict]      # 被拒绝的工具调用
│   ├── protocol_retry_count: int            # 协议违规重试次数
│   ├── elapsed_ms: int                      # 总耗时 ms
│   ├── token_usage: dict                    # token 使用统计
│   └── interrupt_events: list[dict]         # 中断事件记录
└── next_wake_after: float | null            # 下一次允许唤醒的时间戳（仅主动回合）
```

---

## 3. 不可违反的不变量

以下不变量在任何情况下都不得违反。测试必须覆盖每一项。

### INV-01: Turn 状态机必须有明确的开始、状态、预算、终态和审计痕迹

每轮 turn 必须满足五个条件：有开始（TURN_CREATED）、有状态（每个 state 过渡可追踪）、有预算（TurnBudget 控制上限）、有终态（不可逆的状态结束）、有审计证据（context_snapshot_ids 至少一个）。

违反示例：turn 在中途因异常直接返回而没有记录终态。

### INV-02: 模型输出必须是结构化格式

模型输出**必须**是 native tool calls（或 provider 级 JSON schema / response_format）。纯自然语言 assistant.content（无 tool calls）**不可接受**。

- 模型返回 `assistant.content = "我决定回复..."` 且无 tool_call → **PROTOCOL_RETRY**
- 模型返回自然语言 JSON 字符串（非 native tool call）→ **PROTOCOL_RETRY**
- 模型必须通过 native tool calls 机制表达所有指令

### INV-03: assistant.content alone (无 tool calls) → PROTOCOL_RETRY，永不发给用户

如果模型只输出 `assistant.content`（文本）而没有 tool_calls，系统**绝不能**把这段文本当作用户可见消息发送。

流程：
```text
assistant.content only, no tool_calls
  → PROTOCOL_RETRY（在协议重试预算内）
  → 重试耗完后 → HARD_FALLBACK
```

### INV-04: 不在 exposed 集合中的工具 → 拒绝（tool_not_exposed），永不执行

模型可能请求调用某个工具。如果该工具不在本轮 `exposed_tool_names` 中，系统**不能**执行它。

流程：
```text
tool_name NOT IN exposed_tool_names
  → rejected (reason=tool_not_exposed)
  → 记录到 rejected_tool_calls
  → 反馈给模型（在协议重试预算内）
```

### INV-05: 预算耗尽 → 安全终态，不产生新模型调用，不产生新外部副作用

当 TurnBudget 中任何一个计数器达到上限时：
- 停止所有新模型调用
- 停止所有外部副作用（不发送新消息、不写状态、不创建 reminder）
- 记录 budget_exhausted 到 diagnostics
- 进入安全终态（budget_exhausted terminal state）
- 已发送的消息仍然按正常路径处理历史写入

### INV-06: delivered_message_count 硬上限为 15

无论何种 turn type，`delivered_message_count`（成功送达的消息数）不得超过 15。

- 达到 15 时：TurnEngine 不再暴露 `send_text_message` 工具（由 ToolExposurePolicy 在下一轮 context 构建前生效，或 TurnEngine 直接拒绝超过上限的发送请求）
- 15 是兜底硬上限，不是推荐数量

### INV-07: 失败的发送尝试不计入 delivered_message_count

`send_attempt_count` 和 `failed_send_attempt_count` 独立计数。只有 `delivery_status == delivered` 的消息才计入 `delivered_message_count`。

- 发送失败 → send_attempt_count++，failed_send_attempt_count++，delivered_message_count 不变
- 重试某条失败消息 → send_attempt_count++，delivered_message_count 不变（除非重试成功）

### INV-08: 已发送消息先进入 turn-local state，历史写入仅在 turn 结束后

同一轮内模型发送的消息在 turn 结束前**只**存在于 `CurrentTurnLocalState.sent_messages_this_turn`。它们**不能**进入 `RecentConversationHistory`。

- 模型第二次调用时，通过 `current_turn_local_state` block 看到上一轮发送的内容
- turn 结束后，只有 `status == delivered` 的消息写入真实 conversation history
- failed/unknown 消息不写历史

### INV-09: 只有已送达消息进入历史；failed/unknown 不进入

ConversationHistory 中的 `role=assistant` 消息**必须**满足 `delivery_status == delivered`。

- `status=failed` → 不写 assistant history
- `status=unknown` → **不自动视为 delivered**，不写 assistant history
- 只有 `DeliveryReceipt.status == delivered` 的消息才能写入

### INV-10: finish_turn 是唯一到达终态的方式（不能靠自然语言）

模型不能通过自然语言表达"本轮结束"。必须调用 `finish_turn` 工具。

- 模型说"我就不多说了，拜拜"且无 finish_turn → turn 继续，模型必须再调 finish_turn
- 模型调用 skip_initiative 但未调 finish_turn → turn 继续
- 只有 ToolExecutor 执行了 `finish_turn` 工具调用后，CHECK_TERMINAL 才判定为 terminal

### INV-11: skip_initiative 只能在主动回合的可跳过阶段调用

`skip_initiative` 工具只在以下条件**同时满足**时暴露和执行：
- turn_type == relationship_proactive
- TerminalPolicy.allows_skip == true
- TerminalPolicy.forced_speak == false

其他任何 turn type 不得暴露或执行 skip_initiative。

### INV-12: 同一次模型调用中多个终态工具 → PROTOCOL_RETRY

如果模型在一次调用中同时返回了多个工具调用，且其中包含两个或以上终态工具（finish_turn + skip_initiative，或 finish_turn + send_text_message 当后者也是终态时），这是一个协议违规：

```text
tool_calls: [finish_turn, skip_initiative]
  → PROTOCOL_RETRY
  → 告诉模型只能调用一个终态工具
```

注：send_text_message 在大多数 turn type 中不是终态工具。只有在特定 turn type 中，某些工具组合被禁止。

### INV-13: 终态工具 + 任何其他工具在同一次调用 → PROTOCOL_RETRY

如果模型在一次调用中同时调用了终态工具和任何其他工具：

```text
tool_calls: [finish_turn, send_text_message]
  → 这是允许的（finish_turn + send_text_message 是标准终态模式）

tool_calls: [finish_turn, get_weather]
  → PROTOCOL_RETRY（终态后不应再查天气）

tool_calls: [finish_turn, skip_initiative]
  → PROTOCOL_RETRY（INV-12）
```

验证规则：
1. 如果 tool_calls 中包含 finish_turn，则其余工具只能是 send_text_message（且最多一个）
2. ~~如果 tool_calls 中包含 skip_initiative，则其余工具只能是 finish_turn（且只能一个）~~ **（已删除）skip_initiative 和 finish_turn 必须分两次模型调用。规则见 INV-12a**
3. 有其他终态组合 → PROTOCOL_RETRY

### INV-12a: skip_initiative 和 finish_turn 必须分两次模型调用

`skip_initiative` 声明本轮不说话。模型调用 `skip_initiative` 后，系统将 `skip_initiative` 的 tool result 写入 `CurrentTurnLocalState`，然后再次调用模型。模型在第二次调用中看到 skip 已执行，然后调用 `finish_turn(next_wake_minutes)`。

```text
# 第一次模型调用
tool_calls: [skip_initiative]
  → 执行，记录 result，回到模型

# 第二次模型调用（同一 turn 内）
tool_calls: [finish_turn]
  → 终态

# 禁止：
tool_calls: [skip_initiative, finish_turn]  ← PROTOCOL_RETRY
```

同一轮中，skip_initiative 必须单独在一次模型调用中出现。它不能和任何其他工具（包括 finish_turn）同在一次 tool_calls 中。

**并行工具的规则**：同一次模型调用中，仅允许并行调用多个 FACT_TOOL（如 get_time + get_weather）。终态工具（finish_turn, skip_initiative）必须独占一次模型调用，不能与任何其他工具并行。

### INV-14: 每个 turn 必须产生至少一个 ContextSnapshot

每次模型调用前，TurnEngine 必须通过 ContextSnapshotStore 保存一个 context snapshot（摘要级）。snapshot 至少包含：

- turn_id
- call_id
- 暴露的工具名称列表
- 使用的记忆 ID 列表
- 使用的历史消息 ID 列表
- turn_local_state 摘要
- terminal_policy 摘要

### INV-15: POST_TURN_HOOKS 失败不改变终态

Post-terminal pipeline 中的任何 hook 失败时：
- 不改变 terminal_state
- 不影响已写入的历史
- 不影响 TurnResult 中 delivered_messages 的内容
- 单个 hook 异常只记录到 diagnostics，不阻断后续 hooks
- 不触发新的微信消息发送

### INV-16: interrupt_token 机制必须覆盖全部 6 处检查点

TurnEngine 必须在以下 6 个位置检查中断：

1. TURN_CREATED 后 → 第一次 INTERRUPT_CHECK
2. CALL_MODEL 返回后 → POST_MODEL_INTERRUPT_CHECK
3. EXEC_FACT_TOOLS 返回后 → POST_FACT_INTERRUPT_CHECK
4. GUARD_OUTPUT 前 → PRE_GUARD_INTERRUPT_CHECK
5. EXEC_SEND_WECHAT 前 → PRE_SEND_INTERRUPT_CHECK
6. HARD_FALLBACK 发送前 → PRE_FALLBACK_INTERRUPT_CHECK

### INV-17: 数据边界不交叉

以下数据必须严格隔离，不得交叉污染：

- `CurrentTurnLocalState` 不能当作 `RecentConversationHistory` 使用
- `ToolExecutionResult` 不能伪装成用户消息
- `MediaAttachment.summary` 不能写入 `InboundMessageItem.text`
- `DeliveryReceipt.failed` 不能构造成 assistant history
- `MemoryRecord` 不能当作真实聊天历史

---

## 4. 通用状态机流转

### 4.1 主状态机图

```
TURN_CREATED
    │
    v
LOAD_RUNTIME_STATE ───── 加载 InitiativeState、ReminderState、RelationshipState
    │
    v
CLASSIFY_TURN ────────── 确认 turn_type（若 TurnRequest 已有则跳过）
    │
    v
RESOLVE_TOOL_EXPOSURE ── 通过 ToolExposurePolicy 确定暴露工具集合
    │
    v
SELECT_MEMORY ────────── 通过 MemorySystem 选择本轮所需记忆
    │
    v
BUILD_CONTEXT ────────── 通过 ContextBuilder 构建 BuiltContext
    │
    v
SAVE_CONTEXT_SNAPSHOT ── 通过 ContextSnapshotStore 保存上下文快照
    │
    v
CALL_MODEL ───────────── 通过 ProviderGateway 调用模型
    │
    v
VALIDATE_MODEL_OUTPUT ─── 校验结构化输出
    │
    ├──(invalid)─→ STRUCTURE_REPAIR ─→ CALL_MODEL（在 retry_budget 内）
    │
    v (valid)
VALIDATE_TOOL_CALL ────── 校验工具是否暴露、参数合法、阶段允许
    │
    ├──(rejected: tool_not_exposed/invalid_args/invalid_phase)
    │       └─→ CALL_MODEL（在 retry_budget 内，附带 rejection reason）
    │
    v (valid)
EXECUTE_TOOL ──────────── 通过 ToolExecutor 执行工具
    │
    v
UPDATE_TURN_LOCAL_STATE ─ 记录工具结果、送达状态、更新计数器
    │
    v
CHECK_TERMINAL ────────── 检查是否满足终态条件
    │
    ├──(not terminal)─→ BUILD_CONTEXT（带上更新后的 turn-local state）
    │
    v (terminal)
PERSIST_TURN_RESULT ───── 写历史、写 outbound 记录、写 turn diagnostics
    │
    v
POST_TURN_MEMORY ──────── 通过 MemorySystem 处理记忆候选
    │
    v
RELEASE_LOCK ──────────── 释放用户 turn 锁
```

### 4.2 15 个状态详细说明

#### S1: TURN_CREATED
- **输入**: TurnRequest
- **动作**: 生成 turn_id（若 TurnRequest 未提供），初始化 CurrentTurnLocalState，初始化 TurnBudget
- **输出 state**: LOAD_RUNTIME_STATE
- **不可跳过**: 即使 TurnRequest 已含 turn_id，此状态仍必须执行以初始化内部状态

#### S2: LOAD_RUNTIME_STATE
- **输入**: user_id, bot_id
- **动作**: 加载 InitiativeState、ReminderState、RelationshipState 等运行时状态
- **输出 state**: CLASSIFY_TURN
- **错误处理**: 加载失败不阻塞（降级为空状态），记录 warning
- **注意**: 若 TurnRequest 已含 initiative_state/reminder_state，此步仅做验证和合并

#### S3: CLASSIFY_TURN
- **输入**: TurnRequest.classification（若已有）或 InboundMessageBatch + RuntimeTrigger
- **动作**: 若 TurnRequest 已含 classification 则直接使用；否则调用 TurnClassifier
- **输出 state**: RESOLVE_TOOL_EXPOSURE
- **产物**: 确认的 TurnClassification

#### S4: RESOLVE_TOOL_EXPOSURE
- **输入**: TurnClassification, InitiativeState, CurrentTurnLocalState
- **动作**: 调用 ToolExposurePolicy.resolve() 确定本轮暴露工具
- **输出 state**: SELECT_MEMORY
- **产物**: ToolExposure（含 exposed_tool_names 和 native_tool_schemas）

#### S5: SELECT_MEMORY
- **输入**: user_id, bot_id, turn_type, inbound_batch, recent_history, relationship_state, forget_directives
- **动作**: 调用 MemorySystem.load_for_turn()
- **输出 state**: BUILD_CONTEXT
- **产物**: MemorySelection

#### S6: BUILD_CONTEXT
- **输入**: BuildContextRequest（含所有前述产物）
- **动作**: 调用 ContextBuilder.build() → 产出 BuiltContext
- **输出 state**: SAVE_CONTEXT_SNAPSHOT
- **产物**: BuiltContext（含 provider_messages, exposed_tool_schemas, tool_contract_block, context_snapshot）

#### S7: SAVE_CONTEXT_SNAPSHOT
- **输入**: BuiltContext.context_snapshot
- **动作**: 调用 ContextSnapshotStore.save()，将 snapshot_id 加入 CurrentTurnLocalState.context_snapshot_ids
- **输出 state**: CALL_MODEL
- **约束**: snapshot 不保存 API key、不保存原始媒体

#### S8: CALL_MODEL
- **输入**: BuiltContext.provider_messages + exposed_tool_schemas
- **动作**: 调用 ProviderGateway.chat() → 产出 ModelCallResult
- **输出 state**: VALIDATE_MODEL_OUTPUT
- **产物**: ModelCallResult（含 parsed_tool_calls, parsed_text, usage, latency_ms）
- **计数器**: model_call_count++, model_calls_remaining--
- **中断检查**: 模型返回后执行 POST_MODEL_INTERRUPT_CHECK

#### S9: VALIDATE_MODEL_OUTPUT
- **输入**: ModelCallResult
- **动作**: 检查模型输出是否合法
  - `validation_status == valid` → 继续
  - `validation_status == invalid_structure` → STRUCTURE_REPAIR
  - `parsed_tool_calls` 为空且 `parsed_text` 非空（纯文本输出）→ PROTOCOL_RETRY
  - `validation_status == provider_error` → HARD_FALLBACK
- **输出 state**: VALIDATE_TOOL_CALL（合法）或 STRUCTURE_REPAIR（需修复）或 HARD_FALLBACK

#### S9a: STRUCTURE_REPAIR
- **输入**: 原 ModelCallResult + 修复指令
- **动作**: 在协议重试预算内，带上修复指令重新调用模型
- **计数器**: protocol_retry_count++
- **重试耗完后**: HARD_FALLBACK
- **输出 state**: CALL_MODEL（重试）或 HARD_FALLBACK（预算耗尽）

#### S10: VALIDATE_TOOL_CALL
- **输入**: ModelCallResult.parsed_tool_calls, ToolExposure.exposed_tool_names, CurrentTurnLocalState
- **动作**: 逐条校验每个 tool_call
  1. tool_name 是否在 exposed_tool_names
  2. arguments 是否符合 NativeToolSchema.parameters_schema
  3. 当前 turn phase 是否允许此工具（例如 skip_initiative 是否在 forced_speak 阶段被调用）
  4. 工具组合是否合法（INV-12, INV-13）
- **输出 state**: EXECUTE_TOOL（全部合法）或 CALL_MODEL（部分被拒，在 retry_budget 内）或 HARD_FALLBACK（不可恢复）
- **产物**: validated_tool_calls（合法的）和 rejected_tool_calls（被拒的）

#### S11: EXECUTE_TOOL
- **输入**: validated_tool_calls, CurrentTurnLocalState
- **动作**: 通过 ToolExecutor 逐条或并行执行工具调用
  - FACT_TOOL 类工具并行执行（asyncio.gather）
  - 输出类工具串行执行（send_text_message 逐条发）
  - 终态工具最后执行（finish_turn）
- **输出 state**: UPDATE_TURN_LOCAL_STATE
- **产物**: list[ToolExecutionResult]（每个工具的完整执行结果）
- **计数器**: tool_call_count += len(validated_tool_calls)

#### S12: UPDATE_TURN_LOCAL_STATE
- **输入**: list[ToolExecutionResult]
- **动作**: 更新 CurrentTurnLocalState
  - 将 send_text_message 的结果追加到 sent_messages_this_turn
  - 更新 delivered_message_count（仅计入 status=delivered 的消息）
  - 更新 send_attempt_count 和 failed_send_attempt_count
  - 更新 budget_state 中所有剩余计数器
  - 将所有 ToolExecutionResult 追加到 tool_results_this_turn
- **输出 state**: CHECK_TERMINAL
- **约束**: 此状态不写历史，不写持久化存储

#### S13: CHECK_TERMINAL
- **输入**: CurrentTurnLocalState, TerminalPolicy
- **动作**: 检查是否满足终态条件
  1. 最近一次工具调用是否包含 `finish_turn`？
  2. delivered_message_count >= TerminalPolicy.min_delivered_messages？
  3. 所有 required_terminal_tools 是否都已调用？
  4. TerminalPolicy.requires_next_wake 时，finish_turn 是否提供了 next_wake_minutes？
  5. 预算是否耗尽？
- **满足终态** → PERSIST_TURN_RESULT
- **不满足** → BUILD_CONTEXT（继续循环，带上更新后的 turn-local state）
- **预算耗尽但未达到终态** → PERSIST_TURN_RESULT（安全终态）

#### S14: PERSIST_TURN_RESULT
- **输入**: CurrentTurnLocalState, TurnResult
- **动作**: 
  1. 将 sent_messages_this_turn 中 status=delivered 的消息写入 conversation history
  2. 将 outbound 消息写入持久存储
  3. 将 turn diagnostics 写入持久存储
  4. 固化 context snapshots
- **输出 state**: POST_TURN_MEMORY
- **约束**: 此状态不可逆，写入后即完成

#### S15a: POST_TURN_MEMORY
- **输入**: TurnResult, delivered_messages, inbound_batch, context_snapshot
- **动作**: 调用 MemorySystem.write_after_turn() 处理记忆候选
- **输出 state**: RELEASE_LOCK
- **约束**: 记忆写入失败不影响 turn 终态

#### S15b: RELEASE_LOCK
- **输入**: user_id, turn_id
- **动作**: 释放该用户的 turn 执行锁
- **输出**: TurnResult 返回给调用方
- **约束**: 必须确保锁被释放（使用 try-finally 或 finally block）

---

## 5. 每种 Turn Type 的完整状态机

### 5.1 user_chat — 普通用户聊天

**触发**: 用户发送文字消息（InboundMessageBatch 包含 kind=text 的 items）。

**工具暴露阶段**: normal_chat

默认暴露工具：
```
send_text_message
finish_turn
get_time
get_weather
```

用户请求图片时额外暴露：
```
generate_image_message
send_image_message
send_sticker
```

**状态流转图**:
```
TURN_CREATED → LOAD_RUNTIME_STATE → CLASSIFY_TURN
  → RESOLVE_TOOL_EXPOSURE (normal_chat)
  → SELECT_MEMORY → BUILD_CONTEXT → SAVE_CONTEXT_SNAPSHOT → CALL_MODEL
  → VALIDATE_MODEL_OUTPUT → VALIDATE_TOOL_CALL
  → EXECUTE_TOOL
      │
      ├─ send_text_message → DELIVERY → UPDATE_TURN_LOCAL_STATE
      │   └─→ CHECK_TERMINAL (not terminal, more messages allowed)
      │       └─→ BUILD_CONTEXT (with turn-local sent messages)
      │           └─→ CALL_MODEL
      │
      ├─ get_time / get_weather → UPDATE_TURN_LOCAL_STATE
      │   └─→ CHECK_TERMINAL (not terminal)
      │       └─→ BUILD_CONTEXT → CALL_MODEL
      │
      └─ finish_turn → UPDATE_TURN_LOCAL_STATE
          └─→ CHECK_TERMINAL (terminal!)
              └─→ PERSIST_TURN_RESULT → POST_TURN_MEMORY → RELEASE_LOCK
```

**终端条件**:
- finish_turn 已被执行
- 可选 delivered_messages >= 0（允许 0 条消息，即 skip/no_reply）
- 如果 delivered_messages == 0，状态标记为 END_NO_REPLY
- 如果 delivered_messages > 0，状态标记为 END_REPLIED

**历史写入规则**:
- 所有 sent_messages_this_turn 中 status=delivered 的消息写入 assistant history
- source = "assistant_reply"
- 每条写入消息标记 turn_id 和 delivery_status=delivered

**状态更新**:
- 无 InitiativeState 更新
- 触发的 post_turn_memory 提取（异步或同步）

**用户发图片/语音时的变体 (user_media_chat)**:
- turn_type = user_media_chat
- inbound_batch 包含 kind=image/voice 的 items
- media_context block 进入 ContextBuilder
- 暴露的工具集合同 user_chat
- 终端条件同 user_chat
- 所有其他行为一致

**禁止行为**:
- 不允许调用 skip_initiative
- 不允许在用户未请求图片时主动暴露图片工具
- 不允许把 finish_turn 和 fact_tool 组合在同一调用中（INV-13）

### 5.2 user_reminder_request — 用户安排提醒

**触发**: 用户说"十分钟后提醒我喝水"、"晚上九点叫我睡觉"等。

TurnClassifier 识别 user_intent = request_reminder。

**工具暴露阶段**: normal_chat

暴露工具：
```
send_text_message
schedule_user_reminder
finish_turn
get_time
```

**状态流转图**:
```
TURN_CREATED → LOAD_RUNTIME_STATE → CLASSIFY_TURN
  → RESOLVE_TOOL_EXPOSURE (normal_chat + schedule_user_reminder exposed)
  → SELECT_MEMORY → BUILD_CONTEXT → SAVE_CONTEXT_SNAPSHOT → CALL_MODEL
  → VALIDATE_MODEL_OUTPUT → VALIDATE_TOOL_CALL
  → EXECUTE_TOOL
      │
      ├─ schedule_user_reminder(args: {remind_at, content, recurrence})
      │   └─→ EXECUTE (create scheduled task with due time)
      │       └─→ UPDATE_TURN_LOCAL_STATE
      │           └─→ CHECK_TERMINAL (not terminal yet, need confirmation msg)
      │               └─→ BUILD_CONTEXT → CALL_MODEL
      │
      ├─ send_text_message (confirmation: "好的宝宝，十点叫你")
      │   └─→ DELIVERY → UPDATE_TURN_LOCAL_STATE
      │       └─→ CHECK_TERMINAL (not terminal unless finish_turn follows)
      │
      └─ finish_turn
          └─→ CHECK_TERMINAL (terminal!)
              └─→ PERSIST_TURN_RESULT → POST_TURN_MEMORY → RELEASE_LOCK
```

**典型调用序列**:
```
Model call 1: send_text_message("好的，我记下了，十点提醒你喝水")
Model call 2: schedule_user_reminder({remind_at: "2026-06-03T22:00:00", content: "喝水时间到了"})
Model call 3: finish_turn
```

或者：
```
Model call 1: schedule_user_reminder({...})
Model call 2: send_text_message("安排好了")
Model call 3: finish_turn
```

或者（用户未提供足够信息）：
```
Model call 1: send_text_message("你想几点提醒你呀？")
Model call 2: finish_turn(status="needs_clarification")
```

**终端条件**:
- finish_turn 已被执行
- schedule_user_reminder 成功执行（创建了 scheduled task）或模型明确表达需要澄清

**失败路径**:
- schedule_user_reminder 执行失败（时间无法解析等）→ 工具返回错误 → 模型尝试修正或 finish_turn(status="failed_to_schedule")
- 不能在 schedule_user_reminder 失败时假装已安排

**历史写入规则**: 同 user_chat，source = "assistant_reply"

### 5.3 user_reminder_due — 用户提醒到期

**触发**: SchedulerRuntime claim 到一个到期的用户授权提醒。

**工具暴露阶段**: reminder_delivery

暴露工具：
```
send_text_message
mark_reminder_delivered
finish_turn
```

**状态流转图**:
```
TURN_CREATED → LOAD_RUNTIME_STATE → CLASSIFY_TURN
  → RESOLVE_TOOL_EXPOSURE (reminder_delivery)
  → SELECT_MEMORY → BUILD_CONTEXT → SAVE_CONTEXT_SNAPSHOT → CALL_MODEL
  → VALIDATE_MODEL_OUTPUT → VALIDATE_TOOL_CALL
  → EXECUTE_TOOL
      │
      ├─ send_text_message ("宝宝，到时间喝水了")
      │   └─→ DELIVERY → UPDATE_TURN_LOCAL_STATE
      │       └─→ CHECK_TERMINAL (not terminal)
      │           └─→ BUILD_CONTEXT → CALL_MODEL
      │
      ├─ mark_reminder_delivered
      │   └─→ EXECUTE (update scheduled task status to delivered)
      │       └─→ UPDATE_TURN_LOCAL_STATE
      │           └─→ CHECK_TERMINAL (not terminal without finish_turn)
      │
      └─ finish_turn
          └─→ CHECK_TERMINAL (terminal!)
              └─→ PERSIST_TURN_RESULT → POST_TURN_MEMORY → RELEASE_LOCK
```

**终端条件**:
- finish_turn 已被执行
- 至少 1 条消息成功送达（min_delivered_messages = 1）
- mark_reminder_delivered 已被执行

**关键规则**:
- 用户授权提醒**不受**主动冷启动 skip 规则影响
- 如果发送失败，不能标记 delivered
- 如果 mark_reminder_delivered 失败，必须记录到 diagnostics，避免下次重复发送
- 不暴露 skip_initiative

**历史写入规则**:
- 送达消息写入 history，source = "assistant_reply"
- reminder 任务状态更新为 delivered

### 5.4 relationship_proactive — 关系型主动（可跳过阶段）

**触发**: SchedulerRuntime 判断用户长时间未互动，主动 gate 允许。

**前置条件**:
- InitiativeState.consecutive_skip_count == 0（上次没有 skip 或上次 spoke）
- 或上次 spoke，间隔时间已到

**工具暴露阶段**: skippable（可跳过阶段）

暴露工具：
```
send_text_message
skip_initiative
finish_turn
```

**状态流转图**:
```
TURN_CREATED → LOAD_RUNTIME_STATE → CLASSIFY_TURN
  → RESOLVE_TOOL_EXPOSURE (skippable, skippable)
  → SELECT_MEMORY → BUILD_CONTEXT (with initiative context)
  → SAVE_CONTEXT_SNAPSHOT → CALL_MODEL
  → VALIDATE_MODEL_OUTPUT → VALIDATE_TOOL_CALL
  → EXECUTE_TOOL
      │
      ├─ skip_initiative(reason="今天没什么新鲜事")
      │   └─→ UPDATE_TURN_LOCAL_STATE
      │       └─→ CHECK_TERMINAL (not terminal!)
      │           └─→ BUILD_CONTEXT → CALL_MODEL
      │               └─→ model must call finish_turn(next_wake_minutes=45)
      │
      ├─ send_text_message ("今天过得怎么样呀？")
      │   └─→ DELIVERY → UPDATE_TURN_LOCAL_STATE
      │       └─→ CHECK_TERMINAL (not terminal)
      │           └─→ BUILD_CONTEXT → CALL_MODEL
      │               └─→ model may continue or call finish_turn
      │
      └─ finish_turn(next_wake_minutes=45, status="spoke")
          └─→ CHECK_TERMINAL (terminal!)
              └─→ PERSIST_TURN_RESULT → POST_TURN_MEMORY → RELEASE_LOCK
              └─→ UPDATE InitiativeState (last_spoke_at, consecutive_skip_count=0, next_wake_after)
```

**典型调用序列（跳过）**:
```
Model call 1: skip_initiative(reason="用户可能还在忙")
Model call 2: finish_turn(next_wake_minutes=45) → END_SKIPPED
```

**典型调用序列（说话）**:
```
Model call 1: send_text_message("今天天气不错，想出去走走吗？")
Model call 2: finish_turn(next_wake_minutes=60, status="spoke") → END_REPLIED
```

**终端条件**:
- finish_turn 已被执行
- 必须提供 next_wake_minutes（requires_next_wake = true）
- 如果调用了 skip_initiative → 状态为 END_SKIPPED
- 如果至少一条消息送达 → 状态为 END_REPLIED

**状态更新**:
- InitiativeState.last_trigger_at = now
- InitiativeState.last_decision = "spoke" 或 "skipped"
- InitiativeState.consecutive_skip_count = (skipped ? 上次 + 1 : 0)
- InitiativeState.last_spoke_at = now (若 spoke)
- InitiativeState.last_skipped_at = now (若 skipped)
- InitiativeState.next_wake_after = next_wake_minutes 对应的时间戳

**历史写入规则**:
- 送达消息写入 history，source = "assistant_proactive"
- skip 不写任何消息到 history

**禁止行为**:
- 不暴露图片/语音/表情工具
- next_wake_minutes 必须在系统允许的范围内

### 5.5 relationship_proactive — 关系型主动（强制开口阶段）

**触发**: 上一次 relationship_proactive turn 结果为 skipped。

**前置条件**:
- InitiativeState.consecutive_skip_count >= 1
- 或 InitiativeState.last_decision == "skipped"

**工具暴露阶段**: forced_speak

暴露工具（**不暴露 skip_initiative**）：
```
send_text_message
finish_turn
```

**状态流转图**:
```
TURN_CREATED → LOAD_RUNTIME_STATE → CLASSIFY_TURN
  → RESOLVE_TOOL_EXPOSURE (forced_speak — NO skip_initiative)
  → SELECT_MEMORY → BUILD_CONTEXT (context says "must speak")
  → SAVE_CONTEXT_SNAPSHOT → CALL_MODEL
  → VALIDATE_MODEL_OUTPUT → VALIDATE_TOOL_CALL
  → EXECUTE_TOOL
      │
      ├─ send_text_message (must send at least 1)
      │   └─→ DELIVERY → UPDATE_TURN_LOCAL_STATE
      │       └─→ CHECK_TERMINAL (not terminal)
      │           └─→ BUILD_CONTEXT → CALL_MODEL
      │
      ├─ (model attempts skip_initiative) → REJECTED (tool_not_exposed)
      │   └─→ UPDATE_TURN_LOCAL_STATE (rejected_tool_calls++)
      │       └─→ CHECK_TERMINAL (not terminal)
      │           └─→ CALL_MODEL (with rejection feedback)
      │
      └─ finish_turn(next_wake_minutes=60)
          └─→ CHECK_TERMINAL (terminal, must have min 1 delivered)
              └─→ PERSIST_TURN_RESULT → POST_TURN_MEMORY → RELEASE_LOCK
```

**终端条件**:
- finish_turn 已被执行
- 至少 1 条消息成功送达（min_delivered_messages = 1，forced_speak = true）
- 必须提供 next_wake_minutes

**ContextBuilder 必须在上下文中告诉模型**:
- 这是上一次已经跳过后的主动机会
- 本轮不能继续跳过
- 需要自然地发送至少一条轻量消息
- 如果没有新话题，也要用关系型方式轻轻开口

**状态更新**: 同可跳过阶段，但 consecutive_skip_count 重置为 0。

### 5.6 scheduled_ritual — 固定仪式

**触发**: SchedulerRuntime 触发固定时间的仪式，如早安、晚安。

**工具暴露阶段**: skippable

暴露工具：
```
send_text_message
finish_turn
```

**状态流转图**:
```
TURN_CREATED → LOAD_RUNTIME_STATE → CLASSIFY_TURN
  → RESOLVE_TOOL_EXPOSURE (ritual — no skip, no fact tools)
  → SELECT_MEMORY → BUILD_CONTEXT (with ritual context)
  → SAVE_CONTEXT_SNAPSHOT → CALL_MODEL
  → VALIDATE_MODEL_OUTPUT → VALIDATE_TOOL_CALL
  → EXECUTE_TOOL
      │
      ├─ send_text_message (早安/晚安消息)
      │   └─→ DELIVERY → UPDATE_TURN_LOCAL_STATE
      │       └─→ CHECK_TERMINAL (not terminal)
      │           └─→ BUILD_CONTEXT → CALL_MODEL
      │
      └─ finish_turn
          └─→ CHECK_TERMINAL (terminal — must have min 1 delivered)
              └─→ PERSIST_TURN_RESULT → POST_TURN_MEMORY → RELEASE_LOCK
```

**终端条件**:
- finish_turn 已被执行
- 至少 1 条消息成功送达
- 不需要 next_wake_minutes

**规则**:
- 不机械重复（ContextBuilder 应注入最近 ritual 内容）
- 看最近发送内容和关系状态
- 用户处于勿扰或关闭仪式时不触发（SchedulerRuntime gate 层面阻止）
- 不暴露 skip

**历史写入规则**:
- 消息写入 history，source = "assistant_proactive"

### 5.7 admin_manual_trigger — 管理台手动触发

**触发**: 管理员通过 POST /admin/proactive/trigger 或 POST /admin/send 手动触发。

**工具暴露阶段**: normal_chat 或自定义

**关键分支**:

#### Dry-run 路径
```
TURN_CREATED → LOAD_RUNTIME_STATE → CLASSIFY_TURN
  → RESOLVE_TOOL_EXPOSURE → SELECT_MEMORY
  → BUILD_CONTEXT → SAVE_CONTEXT_SNAPSHOT → CALL_MODEL
  → ... (正常流转到终态) ...
  → PERSIST_TURN_RESULT (不写真实 history，使用 dry_run 标记)
  → POST_TURN_MEMORY (skip)
  → RELEASE_LOCK
  → 返回 TurnResult (但不实际发送)
```

#### Real-send 路径
```
TURN_CREATED → ... (正常流转) ...
  → EXECUTE_TOOL (真实发送)
  → PERSIST_TURN_RESULT (写入特殊标记的 history)
```

**规则**:
- 必须标记 `trigger_source=admin`
- dry-run 仍生成 context snapshot
- dry-run 不污染真实用户历史
- real-send 写入 history 时标记 source="admin"

### 5.8 memory_management — 记忆管理

**触发**: 用户说"别记这个"、"忘了刚才那些"、"不要再提这件事"。

**工具暴露阶段**: normal_chat

暴露工具：
```
apply_forget_directive
send_text_message
finish_turn
```

**状态流转图**:
```
TURN_CREATED → ... → EXECUTE_TOOL
      │
      ├─ apply_forget_directive({scope: "local_window", window_before: 5, window_after: 5})
      │   └─→ EXECUTE (标记前后 5 轮对话禁止记忆)
      │       └─→ UPDATE_TURN_LOCAL_STATE
      │           └─→ CHECK_TERMINAL (not terminal)
      │
      ├─ send_text_message ("好呀宝宝，我听你的，刚才那些我不记了。")
      │   └─→ DELIVERY → UPDATE_TURN_LOCAL_STATE
      │
      └─ finish_turn
          └─→ CHECK_TERMINAL (terminal)
              └─→ POST_TURN_MEMORY (MemorySystem 检查 forget directives)
```

**规则**:
- apply_forget_directive 成功后，POST_TURN_MEMORY 阶段 MemorySystem 必须检查并阻断记忆写入
- 不追问原因
- 不追问应该记住什么
- 自动转移话题
- 回复语气由 persona 控制

---

## 6. 终态矩阵

| turn_type | terminal_state | conditions | history_write_rules | state_updates |
|-----------|---------------|-----------|--------------------|--------------|
| user_chat | END_REPLIED | finish_turn + delivered_messages >= 1 | 写所有 delivered msgs 为 assistant_reply | 无特殊状态更新 |
| user_chat | END_NO_REPLY | finish_turn + delivered_messages == 0 | 不写 assistant 消息（turn 仍写入日志） | 无特殊状态更新 |
| user_media_chat | END_REPLIED | 同上 + media_context 处理 | 同上 | 无特殊状态更新 |
| user_media_chat | END_NO_REPLY | finish_turn + 0 messages | 同上 | 无特殊状态更新 |
| user_reminder_request | END_REPLIED | schedule_user_reminder 成功 + finish_turn | 写 assistant_reply 消息 | 创建 scheduled task |
| user_reminder_request | END_FAILED | schedule 失败且无法恢复 | 不写 assistant history | 记录 scheduler failure |
| user_reminder_due | END_REPLIED | send + mark_reminder_delivered + finish_turn + min 1 delivered | 写 assistant_reply 消息 | reminder 状态→delivered |
| user_reminder_due | END_PARTIAL | send 送达但 mark 失败 | 写 assistant_reply（标记 reminder delivery failed 到 diagnostics） | reminder 状态不变 |
| relationship_proactive (可跳过) | END_SKIPPED | 模型第一次调用 skip_initiative，第二次调用 finish_turn(next_wake_minutes)（两次独立模型调用，不是同一 tool_calls） | 不写任何 assistant 消息 | InitiativeState: skipped++, next_wake 设置 |
| relationship_proactive (可跳过) | END_REPLIED | send_text_message >= 1 + finish_turn(next_wake_minutes) | 写 assistant_proactive 消息 | InitiativeState: spoke, consecutive_skip=0, next_wake 设置 |
| relationship_proactive (强制开口) | END_REPLIED | send_text_message >= 1 + finish_turn(next_wake_minutes) | 写 assistant_proactive 消息 | InitiativeState: spoke, consecutive_skip=0 |
| relationship_proactive (强制开口) | END_FAILED | 发送失败且重试耗完 | 不写 assistant history | InitiativeState: skip 计数不增加，记录 failure |
| scheduled_ritual | END_REPLIED | send_text_message >= 1 + finish_turn | 写 assistant_proactive 消息 | 更新 last_ritual_at |
| admin_manual_trigger (dry-run) | END_DRY_RUN | finish_turn, 不真实发送 | 不写入真实 history | 无持久状态更新 |
| admin_manual_trigger (real-send) | END_REPLIED | finish_turn, 已真实发送 | 写 assistant history（source=admin） | 无特殊状态更新 |
| memory_management | END_REPLIED | apply_forget_directive + send_text_message + finish_turn | 写 assistant_reply 消息 | 创建 ForgetDirective, 阻断前后窗口记忆 |
| 所有类型 | END_FAILED | 硬兜底失败（wechat_sender 为 None 或发送完全失败） | 不写 assistant history | 无状态更新 |
| 所有类型 | END_CANCELLED | 中断检查 detected 新事件 supersede | 不写任何本轮内容 | 无状态更新 |
| 所有类型 | END_FALLBACK_SENT | 硬兜底文案成功发送 | 写 assistant:fallback 消息 | 记录 fallback_event |
| 所有类型 | END_BUDGET_EXHAUSTED | 预算耗尽（model/tool/delivery/time） | 只写已成功送达的消息 | 记录 budget_exhausted 到 diagnostics |

---

## 7. 送达模型（六态）

```
                          ┌─────────────────┐
                          │     PENDING      │  初始状态，尚未尝试发送
                          └────────┬────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
                    v              v              v
           ┌────────────┐  ┌────────────┐  ┌──────────────┐
           │  GUARD     │  │  SEND      │  │  SEND        │
           │  BLOCKS    │  │  FAILS     │  │  SUCCEEDS    │
           └─────┬──────┘  └─────┬──────┘  └──────┬───────┘
                 │               │                 │
                 v               v                 v
          ┌─────────────┐ ┌──────────────┐  ┌───────────────┐
          │ BLOCKED     │ │ FAILED       │  │ SENT_         │
          │ _BY_GUARD   │ │ _BEFORE_SEND │  │ CONFIRMED     │
          └─────────────┘ └──────┬───────┘  └───────────────┘
                                 │
                        (retry?) │
                                 v
                         ┌──────────────┐
                         │  SEND BUT    │
                         │  RESULT      │
                         │  UNKNOWN     │
                         └──────┬───────┘
                                │
                    ┌───────────┼───────────┐
                    │           │           │
                    v           v           v
             ┌──────────┐ ┌──────────┐ ┌──────────┐
             │ SENT_    │ │ FAILED_  │ │ UNKNOWN  │
             │ CONFIRMED│ │ CONFIRMED│ │ _AFTER_  │
             │          │ │          │ │ ATTEMPT  │
             └──────────┘ └──────────┘ └──────────┘
```

| 状态 | 含义 | 可重试 | 写成功历史 | 说明 |
|------|------|--------|-----------|------|
| PENDING | 待发送 | -- | 否 | 初始状态 |
| FAILED_BEFORE_SEND | 发送前失败（网络不可达、参数错误等） | 是 | 否 | 属于临时失败，可重试 |
| UNKNOWN_AFTER_ATTEMPT | 发送后状态未知（网络超时但不确定是否送达） | **否** | 否 | 默认不重试，防止重复发送 |
| SENT_CONFIRMED | 确认已送达 | 否 | 是 | 唯一可写入 assistant history 的状态 |
| FAILED_CONFIRMED | 确认发送失败 | 是 | 否 | 收到明确的失败响应 |
| BLOCKED_BY_GUARD | 被输出保护拦截 | 否 | 否 | 内容不符合 guard 规则 |

### 重试规则

- **FAILED_BEFORE_SEND**: 可重试，消耗 send_attempt_count，不消耗 delivered_message_count
- **FAILED_CONFIRMED**: 可重试（不同内容或修正后的内容），但消耗 send_attempt_count
- **UNKNOWN_AFTER_ATTEMPT**: 不重试（不确定是否送达，重试可能造成重复）
- **BLOCKED_BY_GUARD**: 不重试（内容被 guard 拒绝）
- max_send_attempts 耗尽后，该消息永久标记为 failed

### 历史写入规则

只有 `SENT_CONFIRMED` 的消息进入 conversation history 和 SentContentMemory：
- role = assistant
- delivery_status = delivered
- source 依据 turn type 设置

---

## 8. 中断检查机制

### 8.1 interrupt_token 格式

```text
interrupt_token = "it_{turn_id}_{timestamp_ms}"
```

其中 `timestamp_ms` 是 TURN_CREATED 时的时间戳（毫秒级）。

### 8.2 InterruptChecker 接口

```python
InterruptChecker = Callable[[str], bool]
# 输入: interrupt_token (str)
# 输出: True 表示该 turn 已过期，应被取消
#       False 表示该 turn 仍然有效，继续执行
```

Checker 实现（main.py 注入）比较 `interrupt_token` 中的时间戳与 `_user_latest_message_ts`（用户最新消息到达时间戳）。若 interrupt_token 的时间戳早于 `_user_latest_message_ts`，则返回 True。

### 8.3 6 处检查点

| # | 检查点位置 | 检查时机 | 中断后行为 |
|---|-----------|---------|----------|
| 1 | TURN_CREATED 后 | 第一个 INTERRUPT_CHECK state | → END_CANCELLED |
| 2 | CALL_MODEL 返回后 | 模型调用返回，进入 VALIDATE_MODEL_OUTPUT 前 | → END_CANCELLED |
| 3 | EXEC_FACT_TOOLS 返回后 | 事实工具执行完毕，进入 VALIDATE_TOOL_CALL 前（若事实工具后需要更多模型调用） | → END_CANCELLED |
| 4 | GUARD_OUTPUT 前 | 输出 guard 检查前 | → END_CANCELLED |
| 5 | EXEC_SEND_WECHAT 前 | 即将调用 wechat_sender 发送消息前 | → END_CANCELLED |
| 6 | HARD_FALLBACK 发送前 | 硬兜底即将发送 fallback 消息前 | → END_CANCELLED |

### 8.4 中断后的清理行为

当任何检查点检测到中断（checker 返回 True）时：
1. 立即转换到 END_CANCELLED
2. 跳过所有后续状态（包括 POST_TURN_HOOKS 中的发送相关操作）
3. 仍然执行:
   - ContextSnapshot 固化（已保存的 snapshots 保留）
   - Turn diagnostics 写入
   - RELEASE_LOCK
4. 不执行:
   - 任何新的微信消息发送
   - assistant history 写入（因为没有 delivered 消息）
   - MemorySystem 记忆提取（因为 turn 未产生有效输出）
   - InitiativeState 更新

### 8.5 生产环境强制要求

- 生产环境（test_mode=False）中，interrupt_checker 必须注入
- 如果 checker 为 None 且 test_mode=False → 抛出 ValueError
- 如果 checker 为 None 且 test_mode=True → 默认返回 False（不中断）

---

## 9. 预算耗尽处理

### 9.1 预算类型

| 预算类型 | 默认上限 | 耗尽后行为 |
|---------|---------|----------|
| max_model_calls | 20 | 停止新模型调用，进入安全终态。已送达消息正常写入历史。 |
| max_tool_calls | 40 | 停止新的工具执行请求。允许当前已请求的工具完成。进入安全终态。 |
| max_delivered_messages | 15 | 不再暴露 send_text_message 工具（由 ToolExposurePolicy 生效，或 TurnEngine 拒绝新的发送请求）。模型必须调用 finish_turn。 |
| max_send_attempts | 3 | 停止新发送尝试。当前消息标记为 failed。进入安全终态。 |
| max_elapsed_seconds | 120 | 当前正在进行的 send 操作允许完成（不截断）。完成后进入安全终态。 |
| max_protocol_retries | 1 | 不再重试协议违规。进入 HARD_FALLBACK 或安全终态。 |

### 9.2 预算耗尽的终态映射

| 耗尽类型 | 终态 |
|---------|------|
| max_model_calls 耗尽 + 有 delivered 消息 | END_REPLIED (带 budget_exhausted 标记) |
| max_model_calls 耗尽 + 无 delivered 消息 | END_FAILED (带 budget_exhausted 标记) |
| max_tool_calls 耗尽 | 同上 |
| max_delivered_messages 耗尽 (已送达 15 条) | END_REPLIED (带 max_limit_reached 标记) |
| max_send_attempts 耗尽 + 有 delivered 消息 | END_PARTIAL (部分消息送达) |
| max_send_attempts 耗尽 + 无 delivered 消息 | END_FAILED |
| max_elapsed_seconds 耗尽 + 有 delivered 消息 | END_REPLIED (带 timeout 标记) |
| max_elapsed_seconds 耗尽 + 无 delivered 消息 | END_FAILED (带 timeout 标记) |
| max_protocol_retries 耗尽 | END_PROTOCOL_ERROR |

### 9.3 安全终态原则

预算耗尽时的安全终态必须满足：
1. **不产生新的外部副作用**: 不发送新的微信消息、不创建新的 reminder、不写新的状态
2. **已完成的副作用不撤销**: 已成功送达的消息仍然写入历史
3. **记录耗尽原因**: diagnostics 中必须包含 exhausted_budget_type 和当时的计数器值
4. **不阻塞系统**: budget exhausted 的 turn 必须正常释放锁，不导致资源泄露

---

## 10. 硬兜底（HARD_FALLBACK）

### 10.1 触发条件

HARD_FALLBACK 在以下情况下触发：

| 触发条件 | 说明 |
|---------|------|
| 上下文构建 FATAL 错误 | ContextBuildResult.fatal_error 非空 |
| 工具注册表不可用 | TOOL_REGISTRY 为空 |
| 无终态工具注册 | 没有 native_tool_call_eligible 的 terminal_tool |
| 工具 schema 构建失败 | 无法为 allowed_tools 构建 native tool specs |
| ProviderGateway 返回 provider_error | 模型 API 不可达或返回致命错误 |
| 协议重试耗尽 | max_protocol_retries 用完后模型输出仍然非法 |
| 未知 state | _dispatch 收到未注册的 state |
| 异常逃逸 | try-except 捕获的未预期异常 |
| 模型调用全部失败 | 所有 CALL_MODEL 尝试都以 error 结束 |

### 10.2 硬兜底流程

```text
HARD_FALLBACK
  ├── 1. INTERRUPT_CHECK（第 6 处检查点）
  │     └── 若中断 → END_CANCELLED
  ├── 2. 构造硬兜底消息（固定文案）
  │     文本: "我刚刚卡了一下 重新说"
  │     永不使用模型原文！
  ├── 3. GUARD_OUTPUT（硬兜底消息也必须过 guard）
  │     └── 若被 guard BLOCK → 记录 BLOCKED，不发送
  ├── 4. 尝试发送（通过 wechat_sender）
  │     ├── 发送成功 → END_FALLBACK_SENT
  │     └── 发送失败 或 wechat_sender 为 None → END_FAILED
  └── 5. 进入 PERSIST_TURN_RESULT
```

### 10.3 硬兜底关键规则

1. **文案固定**: 永远使用 `"我刚刚卡了一下 重新说"`，不参考模型原始输出
2. **必经 guard**: 即使固定文案，也必须经过输出 guard（guard_single_message）
3. **历史标记特殊**: 硬兜底消息写入 history 时 `source = "assistant:fallback"`（不同于普通 assistant 的 source）
4. **不计为正常送达**: END_FALLBACK_SENT 是独立终态，不等同于 END_REPLIED
5. **不触发记忆提取**: 硬兜底 turn 不进入 MemorySystem memory 候选
6. **不更新 InitiativeState**: 即使是主动回合触发硬兜底

### 10.4 硬兜底终态

| 终态 | 条件 |
|------|------|
| END_FALLBACK_SENT | 固定文案成功发送 |
| END_FAILED | 无 wechat_sender 或发送彻底失败 |

---

## 11. Post-terminal pipeline

Post-terminal pipeline 在 CHECK_TERMINAL 判定为终态后、TurnResult 组装前执行。

### 11.1 执行顺序

```text
terminal_state 确定
  │
  ├── Phase 1: WRITE_MODEL_HISTORY
  │     └── 根据终态矩阵决定写哪些消息到 conversation history
  │
  └── Phase 2: POST_TURN_HOOKS (串行，5 个 hook)
        ├── UPDATE_SUMMARY
        ├── UPDATE_MEMORY
        ├── UPDATE_EMOTION
        ├── WRITE_DEV_LOGS
        └── QUALITY_METRICS
```

### 11.2 WRITE_MODEL_HISTORY 行为矩阵

| terminal_state | 写用户消息 | 写 assistant 消息 | source 标记 |
|---------------|-----------|-----------------|------------|
| END_REPLIED | 是 | 是 (所有 delivered msgs) | assistant_reply |
| END_NO_REPLY | 是 | 否 | -- |
| END_SKIPPED | 是（trigger） | 否 | -- |
| END_FAILED | 是 | 否 | -- |
| END_FALLBACK_SENT | 是 | 是 (固定文案) | assistant:fallback |
| END_CANCELLED | 否 | 否 | -- |
| END_PARTIAL_SENT | 是 | 是 (仅 delivered msgs) | assistant_reply |
| END_BUDGET_EXHAUSTED | 是 | 是 (仅 delivered msgs) | assistant_reply |
| END_PROTOCOL_ERROR | 是 | 否 | -- |

### 11.3 POST_TURN_HOOKS 详细说明

#### UPDATE_SUMMARY
- **职责**: 更新会话摘要（recap）
- **触发条件**: 每 N turns 或每次 turn 结束（取决于配置）
- **失败影响**: 不影响终端状态，只记录 warning

#### UPDATE_MEMORY
- **职责**: 触发 MemorySystem.write_after_turn() 处理记忆候选
- **注意**: 实际记忆写入由 MemorySystem 控制（本 hook 只是触发）
- **失败影响**: 不影响终端状态

#### UPDATE_EMOTION
- **职责**: 更新 RelationshipState 中的情绪/关系状态
- **失败影响**: 不影响终端状态

#### WRITE_DEV_LOGS
- **职责**: 写入开发者日志（不进入模型上下文）
- **注意**: 此 hook 输出不污染 conversation history
- **失败影响**: 不影响终端状态

#### QUALITY_METRICS
- **职责**: 记录质量指标（token 用量、latency、budget 使用率）
- **失败影响**: 不影响终端状态

### 11.4 Hook 执行规则

1. **串行执行**: 5 个 hook 按顺序执行（不是并行）
2. **单失败不阻塞**: 如果 UPDATE_SUMMARY 失败，UPDATE_MEMORY 仍继续执行
3. **不改变终态**: 任何 hook 失败都不能修改 terminal_state
4. **不补发微信**: hook 不能触发新的微信消息发送
5. **异常隔离**: 每个 hook 用 try-catch 包裹，异常记录到 diagnostics
6. **pipeline 标记**: 执行后 TurnResult.post_terminal_completed = True

---

## 12. 伪代码

### 12.1 TurnEngine.run() 伪代码

```python
async def run(self, request: TurnRequest) -> TurnResult:
    """TurnEngine 主入口。"""

    # Step 0: 验证 TurnRequest
    self._validate_turn_request(request)

    # Step 1: 初始化
    turn_state = self._init_turn_local_state(request)
    budget = TurnBudget(
        max_model_calls=request.runtime_config.max_model_calls,
        max_tool_calls=request.runtime_config.max_tool_calls,
        max_delivered_messages=request.runtime_config.max_delivered_messages,
        max_send_attempts=request.runtime_config.max_send_attempts,
        max_elapsed_seconds=request.runtime_config.max_elapsed_seconds,
        max_protocol_retries=request.runtime_config.max_protocol_retries,
    )
    interrupt_token = f"it_{request.turn_id}_{int(time.time() * 1000)}"
    turn_state.interrupt_token = interrupt_token

    # Step 2: 状态机主循环
    state = TurnState.LOAD_RUNTIME_STATE
    terminal_policy = request.terminal_policy

    while not self._is_terminal(state):
        # 2a: 预算检查
        if budget.model_call_count >= budget.max_model_calls:
            state = self._handle_budget_exhausted("max_model_calls", turn_state)
            break
        if budget.elapsed_seconds > budget.max_elapsed_seconds:
            state = self._handle_budget_exhausted("max_elapsed_seconds", turn_state)
            break

        # 2b: 中断检查（检查点 1：循环开始时）
        if self._check_interrupted(interrupt_token, request.interrupt_checker):
            state = TurnState.END_CANCELLED
            break

        # 2c: 状态分发
        state = await self._dispatch(state, request, turn_state, budget, terminal_policy)

    # Step 3: Post-terminal pipeline
    turn_state.terminal_status = "finished"
    await self._run_post_terminal_pipeline(request, turn_state, state)

    # Step 4: 组装 TurnResult
    result = self._build_turn_result(request, turn_state, state, budget)

    # Step 5: 释放锁
    await self._release_lock(request.user_id)

    return result


async def _dispatch(self, state, request, turn_state, budget, terminal_policy):
    """状态分发器。"""

    if state == TurnState.LOAD_RUNTIME_STATE:
        # 加载 InitiativeState, ReminderState, RelationshipState
        runtime_state = await self.runtime_store.load_runtime_state(
            request.user_id, request.bot_id
        )
        turn_state.initiative_state = runtime_state.initiative_state
        turn_state.relationship_state = runtime_state.relationship_state
        return TurnState.CLASSIFY_TURN

    elif state == TurnState.CLASSIFY_TURN:
        # 使用 TurnRequest 中的 classification 或重新分类
        classification = request.classification or await self.turn_classifier.classify(
            request.inbound_batch, request.runtime_trigger, turn_state
        )
        turn_state.classification = classification
        return TurnState.RESOLVE_TOOL_EXPOSURE

    elif state == TurnState.RESOLVE_TOOL_EXPOSURE:
        # 确定本轮暴露的工具集合
        exposure = await self.tool_exposure_policy.resolve(
            turn_type=request.turn_type,
            turn_state=turn_state,
            initiative_state=turn_state.initiative_state,
            user_intent=request.classification.user_intent,
            config=request.runtime_config,
        )
        turn_state.tool_exposure = exposure
        return TurnState.SELECT_MEMORY

    elif state == TurnState.SELECT_MEMORY:
        # 选择本轮所需记忆
        memory_selection = request.memory_selection or \
            await self.memory_system.load_for_turn(
                user_id=request.user_id,
                bot_id=request.bot_id,
                turn_type=request.turn_type,
                inbound_batch=request.inbound_batch,
                forget_directives=turn_state.forget_directives,
            )
        turn_state.memory_selection = memory_selection
        return TurnState.BUILD_CONTEXT

    elif state == TurnState.BUILD_CONTEXT:
        # 构建模型输入
        built_context = await self.context_builder.build(
            turn_id=request.turn_id,
            turn_type=request.turn_type,
            turn_state=turn_state,
            terminal_policy=terminal_policy,
            tool_exposure=turn_state.tool_exposure,
            memory_selection=turn_state.memory_selection,
            recent_history=request.recent_history,
            relationship_state=turn_state.relationship_state,
            inbound_batch=request.inbound_batch,
        )
        turn_state.built_context = built_context
        return TurnState.SAVE_CONTEXT_SNAPSHOT

    elif state == TurnState.SAVE_CONTEXT_SNAPSHOT:
        # 保存上下文快照
        snapshot_id = await self.snapshot_store.save(
            turn_state.built_context.context_snapshot
        )
        turn_state.context_snapshot_ids.append(snapshot_id)
        return TurnState.CALL_MODEL

    elif state == TurnState.CALL_MODEL:
        # 调用模型
        model_result = await self.provider_gateway.chat(
            turn_id=request.turn_id,
            call_id=f"call_{request.turn_id}_{budget.model_call_count}",
            messages=turn_state.built_context.provider_messages,
            tools=turn_state.tool_exposure.native_tool_schemas,
            response_format="tool_calls",
        )
        turn_state.last_model_output = model_result
        turn_state.model_call_ids.append(model_result.call_id)
        budget.model_call_count += 1
        budget.model_calls_remaining -= 1
        turn_state.model_call_count = budget.model_call_count

        # 中断检查点 2：模型返回后
        if self._check_interrupted(interrupt_token, request.interrupt_checker):
            return TurnState.END_CANCELLED

        return TurnState.VALIDATE_MODEL_OUTPUT

    elif state == TurnState.VALIDATE_MODEL_OUTPUT:
        # 校验模型输出
        result = turn_state.last_model_output

        if result.validation_status == "provider_error":
            return TurnState.HARD_FALLBACK

        if result.validation_status == "invalid_structure":
            return self._handle_invalid_output(
                turn_state, budget, "invalid_structure"
            )

        # 检查 assistant.content only (无 tool calls)
        if not result.parsed_tool_calls and result.parsed_text:
            return self._handle_invalid_output(
                turn_state, budget, "no_tool_calls_text_only"
            )

        # 检查空输出
        if not result.parsed_tool_calls and not result.parsed_text:
            return self._handle_invalid_output(
                turn_state, budget, "empty_output"
            )

        return TurnState.VALIDATE_TOOL_CALL

    elif state == TurnState.VALIDATE_TOOL_CALL:
        # 校验每个工具调用
        tool_calls = turn_state.last_model_output.parsed_tool_calls
        exposed_names = set(turn_state.tool_exposure.exposed_tool_names)
        rejected = []
        validated = []

        for tc in tool_calls:
            # 检查 1: 工具是否暴露
            if tc.name not in exposed_names:
                rejected.append(RejectedToolCall(
                    tool_call_id=tc.tool_call_id,
                    tool_name=tc.name,
                    reason="tool_not_exposed",
                    exposed_tool_names=list(exposed_names),
                ))
                continue

            # 检查 2: 参数是否合法
            if not self._validate_tool_args(tc, turn_state.tool_exposure):
                rejected.append(RejectedToolCall(
                    tool_call_id=tc.tool_call_id,
                    tool_name=tc.name,
                    reason="invalid_arguments",
                ))
                continue

            # 检查 3: 阶段是否允许
            if not self._validate_tool_phase(tc, turn_state, terminal_policy):
                rejected.append(RejectedToolCall(
                    tool_call_id=tc.tool_call_id,
                    tool_name=tc.name,
                    reason="invalid_phase",
                ))
                continue

            validated.append(tc)

        # 检查 4: 工具组合是否合法
        if not self._validate_tool_combination(validated, terminal_policy):
            return self._handle_invalid_output(
                turn_state, budget, "invalid_tool_combination"
            )

        turn_state.rejected_tool_calls.extend(rejected)
        turn_state.validated_tool_calls = validated

        # 如果有被拒且不是全部被拒，继续执行合法工具
        if not validated and rejected:
            return self._handle_invalid_output(
                turn_state, budget, "all_tools_rejected"
            )

        return TurnState.EXECUTE_TOOL

    elif state == TurnState.EXECUTE_TOOL:
        # 执行合法工具
        results = await self.tool_executor.execute_batch(
            turn_id=request.turn_id,
            tool_calls=turn_state.validated_tool_calls,
            turn_state=turn_state,
        )
        budget.tool_call_count += len(results)

        # 中断检查点 3：事实工具执行后
        if self._check_interrupted(interrupt_token, request.interrupt_checker):
            return TurnState.END_CANCELLED

        turn_state.tool_results_this_turn.extend(results)
        return TurnState.UPDATE_TURN_LOCAL_STATE

    elif state == TurnState.UPDATE_TURN_LOCAL_STATE:
        # 更新 turn-local 状态
        for result in turn_state.tool_results_this_turn[-len(turn_state.validated_tool_calls):]:
            if result.tool_name == "send_text_message":
                sent_msg = TurnLocalSentMessage(
                    message_id=f"msg_{request.turn_id}_{len(turn_state.sent_messages_this_turn)}",
                    tool_call_id=result.tool_call_id,
                    text=result.result_payload.get("text", ""),
                    status=result.delivery_receipt.status if result.delivery_receipt else "failed",
                    delivery_receipt_id=result.delivery_receipt.delivery_receipt_id if result.delivery_receipt else None,
                )
                turn_state.sent_messages_this_turn.append(sent_msg)
                budget.send_attempt_count += 1

                if sent_msg.status == "delivered":
                    budget.delivered_message_count += 1
                else:
                    budget.failed_send_attempt_count += 1

            elif result.tool_name == "skip_initiative":
                turn_state.skipped = True

        turn_state.tool_call_count = budget.tool_call_count
        turn_state.delivered_message_count = budget.delivered_message_count
        turn_state.send_attempt_count = budget.send_attempt_count
        turn_state.failed_send_attempt_count = budget.failed_send_attempt_count
        turn_state.budget_state = self._build_budget_state(budget)

        return TurnState.CHECK_TERMINAL

    elif state == TurnState.CHECK_TERMINAL:
        # 检查是否满足终态条件
        if self._should_terminate(turn_state, budget, terminal_policy):
            return self._resolve_terminal_state(turn_state, terminal_policy)
        else:
            # not terminal → 继续循环，重新构建上下文
            return TurnState.BUILD_CONTEXT

    else:
        return TurnState.HARD_FALLBACK


def _should_terminate(self, turn_state, budget, terminal_policy):
    """判断是否到达终态。"""

    # 条件 1: budget 耗尽 → 强制终态
    if budget.exhausted_budget_type:
        return True

    # 条件 2: finish_turn 已被执行
    has_finish_turn = any(
        r.tool_name == "finish_turn" and r.status == "success"
        for r in turn_state.tool_results_this_turn
    )

    # 条件 3: delivered msgs 满足 min 要求
    meets_min = (
        budget.delivered_message_count >= terminal_policy.min_delivered_messages
    )

    # 条件 4: 如果 requires_next_wake，检查 finish_turn 是否提供了
    next_wake_ok = True
    if terminal_policy.requires_next_wake:
        finish_results = [r for r in turn_state.tool_results_this_turn if r.tool_name == "finish_turn"]
        next_wake_ok = any(
            "next_wake_minutes" in r.result_payload
            for r in finish_results if r.status == "success"
        )

    return has_finish_turn and meets_min and next_wake_ok
```

### 12.2 关键辅助方法伪代码

```python
def _handle_invalid_output(self, turn_state, budget, reason):
    """处理非法模型输出。"""
    budget.protocol_retry_count += 1

    if budget.protocol_retry_count > budget.max_protocol_retries:
        logger.warning(f"Protocol retry budget exhausted: {reason}")
        return TurnState.HARD_FALLBACK

    # 记录 invalid output
    turn_state.diagnostics["protocol_violations"].append({
        "reason": reason,
        "attempt": budget.protocol_retry_count,
    })

    # 返回到 CALL_MODEL（带修复指令）
    return TurnState.CALL_MODEL  # 实际上应该是重新构建带修复指令的 context


def _handle_budget_exhausted(self, budget_type, turn_state):
    """处理预算耗尽。"""
    turn_state.budget_state.exhausted_budget_type = budget_type
    turn_state.diagnostics["budget_exhausted"] = budget_type
    logger.warning(f"Budget exhausted: {budget_type}")

    # 不再调用模型，直接进入安全终态
    return self._resolve_terminal_state(turn_state, turn_state.terminal_policy)


async def _run_post_terminal_pipeline(self, request, turn_state, terminal_state):
    """执行 post-terminal pipeline。"""
    # Phase 1: WRITE_MODEL_HISTORY
    if request.write_model_history:
        try:
            await request.write_model_history(turn_state, terminal_state)
        except Exception as e:
            logger.error(f"write_model_history failed: {e}")
            turn_state.diagnostics["hook_errors"].append({
                "hook": "write_model_history",
                "error": str(e),
            })

    # Phase 2: POST_TURN_HOOKS (串行)
    for hook in request.post_turn_hooks or []:
        try:
            await hook(turn_state, terminal_state)
        except Exception as e:
            logger.error(f"post_turn_hook failed: {e}")
            turn_state.diagnostics["hook_errors"].append({
                "hook": hook.__name__,
                "error": str(e),
            })
```

---

## 13. 决策表

### 13.1 模型输出校验结果决策表

| 输出类型 | validation_status | parsed_tool_calls | parsed_text | 判定 | 动作 |
|---------|-------------------|------------------|-------------|------|------|
| 正常 tool calls | valid | 非空 | 可能为空 | VALID | 进入 VALIDATE_TOOL_CALL |
| 纯文本，无 tool calls | valid | 空 | 非空 | INVALID (text_only) | PROTOCOL_RETRY |
| 空输出 | valid | 空 | 空 | INVALID (empty) | PROTOCOL_RETRY |
| 非法 JSON | invalid_structure | -- | -- | INVALID (structure) | STRUCTURE_REPAIR |
| Provider 错误 (timeout/5xx) | provider_error | -- | -- | FATAL | HARD_FALLBACK |
| JSON 可解析但字段缺失 | invalid_structure | 空 | 可能非空 | INVALID (structure) | STRUCTURE_REPAIR |
| tool arguments 非合法 JSON | invalid_structure | 可能非空 | -- | INVALID (args) | STRUCTURE_REPAIR |

### 13.2 工具调用校验结果决策表

| 场景 | 判定 | 动作 |
|------|------|------|
| 工具在 exposed 集合中 + args 合法 + phase 允许 | VALID | EXECUTE_TOOL |
| 工具不在 exposed 集合中 | REJECTED (tool_not_exposed) | 记录到 rejected_tool_calls，反馈给模型 |
| args 不符合 schema | REJECTED (invalid_arguments) | 记录到 rejected_tool_calls，反馈给模型 |
| 工具在 wrong phase（如 forced_speak 阶段调 skip_initiative） | REJECTED (invalid_phase) | 记录到 rejected_tool_calls，反馈给模型 |
| 终态工具 + 非 send_text_message 工具在同一调用 | INVALID_COMBINATION | PROTOCOL_RETRY |
| 多个终态工具在同一调用 | INVALID_COMBINATION | PROTOCOL_RETRY |
| 全部工具被拒 | ALL_REJECTED | PROTOCOL_RETRY（在 retry budget 内） |
| 部分工具被拒 | PARTIAL_REJECTED | 继续执行合法工具 |

### 13.3 终态检查决策表（按 turn type 和最后的工具调用）

| turn_type | 最后工具调用 | delivered >= min | next_wake OK | 判定 |
|-----------|------------|-----------------|-------------|------|
| user_chat | finish_turn | N/A (min=0) | N/A | TERMINAL → END_REPLIED 或 END_NO_REPLY |
| user_chat | send_text_message (未调 finish_turn) | -- | -- | NOT TERMINAL → BUILD_CONTEXT |
| user_chat | get_time | -- | -- | NOT TERMINAL → BUILD_CONTEXT |
| user_reminder_request | schedule_user_reminder + finish_turn | N/A | N/A | TERMINAL → END_REPLIED |
| user_reminder_request | 未调 schedule_user_reminder + finish_turn | N/A | N/A | TERMINAL → END_REPLIED (needs_clarification) |
| user_reminder_due | send + mark_reminder_delivered + finish_turn | Yes (>=1) | N/A | TERMINAL → END_REPLIED |
| user_reminder_due | send + finish_turn (未调 mark) | Yes | N/A | TERMINAL → END_PARTIAL |
| relationship_proactive (可跳过) | skip_initiative（第一次模型调用）→ finish_turn（第二次模型调用） | Yes (0) | Yes | TERMINAL → END_SKIPPED |
| relationship_proactive (可跳过) | send + finish_turn | Yes (>=0) | Yes | TERMINAL → END_REPLIED |
| relationship_proactive (可跳过) | 未调 finish_turn | -- | -- | NOT TERMINAL |
| relationship_proactive (强制开口) | send + finish_turn | Yes (>=1) | Yes | TERMINAL → END_REPLIED |
| relationship_proactive (强制开口) | finish_turn 但 no delivered | No (0) | Yes | NOT TERMINAL → 必须重试 |
| scheduled_ritual | send + finish_turn | Yes (>=1) | N/A | TERMINAL → END_REPLIED |
| admin_manual_trigger (dry-run) | finish_turn | N/A | N/A | TERMINAL → END_DRY_RUN |
| memory_management | apply_forget_directive + send + finish_turn | N/A | N/A | TERMINAL → END_REPLIED |
| 所有类型 | 任何（budget 耗尽） | -- | -- | TERMINAL → END_BUDGET_EXHAUSTED |
| 所有类型 | 中断检测 | -- | -- | TERMINAL → END_CANCELLED |
| 所有类型 | HARD_FALLBACK 发送成功 | -- | -- | TERMINAL → END_FALLBACK_SENT |
| 所有类型 | HARD_FALLBACK 发送失败 | -- | -- | TERMINAL → END_FAILED |

### 13.4 历史写入规则决策表（按终态）

| terminal_state | 写入用户消息 | 写入 assistant 消息 | source 标记 | role |
|---------------|------------|-------------------|------------|------|
| END_REPLIED | 是 | 是 (所有 delivered msgs) | assistant_reply | assistant |
| END_NO_REPLY | 是 | 否 | -- | -- |
| END_SKIPPED | 是 (trigger 作为 system) | 否 | -- | -- |
| END_PARTIAL_SENT | 是 | 是 (仅 delivered msgs) | assistant_reply | assistant |
| END_FAILED | 是 | 否 | -- | -- |
| END_FALLBACK_SENT | 是 | 是 (固定文案) | assistant:fallback | assistant |
| END_CANCELLED | 否 | 否 | -- | -- |
| END_PROTOCOL_ERROR | 是 | 否 | -- | -- |
| END_BUDGET_EXHAUSTED | 是 | 是 (仅 delivered msgs) | assistant_reply | assistant |
| END_DRY_RUN | 否 | 否 | -- | -- |

注意：
- trigger != "user" 时，系统触发任务以 system role 写入（不进入 user role）
- assistant:fallback 标记确保硬兜底消息可区分于正常回复

### 13.5 重试预算表（按错误类型）

| 错误类型 | 消耗的预算 | 最大重试次数 | 重试策略 |
|---------|-----------|------------|---------|
| protocol_violation (纯文本输出) | protocol_retry_count | 1 | 带上修复指令重新 CALL_MODEL |
| protocol_violation (非法工具组合) | protocol_retry_count | 1 | 带上修复指令重新 CALL_MODEL |
| invalid_structure (非法 JSON) | protocol_retry_count | 1 | STRUCTURE_REPAIR → CALL_MODEL |
| tool_not_exposed | protocol_retry_count | 1 | 反馈给模型，带上可用工具列表 |
| invalid_arguments | protocol_retry_count | 1 | 反馈给模型，带上参数 schema |
| delivery_failed (FAILED_BEFORE_SEND) | send_attempt_count | 2 (max_send_attempts) | 重试发送（相同或修正内容） |
| delivery_failed (FAILED_CONFIRMED) | send_attempt_count | 2 (max_send_attempts) | 重试发送（修正后内容） |
| delivery_failed (UNKNOWN_AFTER_ATTEMPT) | -- | 0 | **不重试** |
| provider_error (timeout/5xx) | model_call_count | 0 | HARD_FALLBACK (不重试模型) |
| llm_error (rate limit) | model_call_count | 0 | 记录错误，进入 HARD_FALLBACK |

---

## 14. 禁止行为

以下行为在 TurnEngine 中**绝对禁止**：

### FORBID-01: 纯文本 assistant.content 作为用户可见消息发送
模型输出的自然语言文本永不直接发送给微信用户。必须通过 native tool calls（send_text_message 或 finish_turn）。

### FORBID-02: 在本轮结束前将发送的消息写入 conversation history
当前 turn 发送的消息在 turn 结束前只存在于 CurrentTurnLocalState.sent_messages_this_turn。绝不能混入 RecentConversationHistory。

### FORBID-03: 执行不在 exposed_tool_names 中的工具
即使工具存在于代码注册表中，如果不在本轮 ToolExposure 中，TurnEngine 绝不能执行它。

### FORBID-04: 在 non-proactive turn 中暴露或执行 skip_initiative
skip_initiative 仅用于 relationship_proactive turn，且仅在可跳过阶段暴露。

### FORBID-05: 在主动消息 turn 中暴露图片/语音/表情输出工具
主动消息（relationship_proactive、scheduled_ritual、user_reminder_due）永远不暴露 generate_image、send_image、send_sticker。

### FORBID-06: 将失败的发送伪装成已送达
只有 DeliveryReceipt.status == delivered 的消息才能标记为 SENT_CONFIRMED 并写入 assistant history。failed/unknown 绝不能伪装为 delivered。

### FORBID-07: 在预算耗尽后继续调用模型或产生外部副作用
一旦任何预算计数器达到上限，停止所有新模型调用和外部副作用（新消息发送、新状态写入）。

### FORBID-08: 将工具执行结果伪装成用户消息
ToolExecutionResult 的内容绝不能以 role=user 进入 conversation history 或传给模型。

### FORBID-09: 将媒体摘要伪装成用户原文
MediaAttachment.summary 绝不能写入 InboundMessageItem.text 或 role=user 消息中。

### FORBID-10: 在主动提醒到期 turn 中暴露或允许 skip
用户授权提醒到期 turn（user_reminder_due）不受主动冷启动 skip 规则影响。不暴露 skip_initiative。

### FORBID-11: 在强制开口阶段暴露 skip_initiative
如果上一轮主动 skip，本轮 forced_speak = true，ToolExposurePolicy 必须不暴露 skip_initiative。TurnEngine 如收到 skip_initiative 调用，必须拒绝。

### FORBID-12: 在硬兜底中使用模型原文
HARD_FALLBACK 永远使用固定文案 "我刚刚卡了一下 重新说"，不参考模型原始输出中的任何文本。

### FORBID-13: 在 turn 结束后仍持有用户锁
RELEASE_LOCK 必须在所有情况下执行（包括异常路径），使用 try-finally 确保锁不被泄露。

### FORBID-14: post_turn_hook 失败改变 terminal_state 或触发新微信发送
POST_TURN_HOOKS 中的任何 hook 失败不能修改 terminal_state，也不能调用 wechat_sender 发送新消息。

### FORBID-15: 绕过 ContextBuilder 直接拼接模型输入
所有模型输入必须通过 ContextBuilder.build() 生成。TurnEngine 不能自己拼 provider_messages。

---

## 15. 具体输入输出例子

### 例 1: 简单 user_chat - 一条消息，finish

**TurnRequest**:
```python
TurnRequest(
    turn_id="turn_001",
    turn_type="user_chat",
    user_id="user_1",
    bot_id="default",
    inbound_batch=InboundMessageBatch(
        batch_id="batch_001",
        user_id="user_1",
        bot_id="default",
        items=[InboundMessageItem(kind="text", text="你好呀", role_origin="user")],
    ),
    classification=TurnClassification(turn_type="user_chat", user_intent="chat"),
    terminal_policy=TerminalPolicy(
        turn_type="user_chat",
        required_terminal_tools=["finish_turn"],
        min_delivered_messages=0,
        max_delivered_messages=15,
        allows_skip=False,
    ),
    tool_exposure=ToolExposure(
        exposed_tool_names=["send_text_message", "finish_turn", "get_time"],
        phase="normal_chat",
    ),
    recent_history=RecentConversationHistory(messages=[]),
)
```

**模型调用序列**:
```
Model call 1:
  tool_calls: [send_text_message(text="你好呀！今天怎么样？")]
  → VALIDATE_TOOL_CALL: VALID
  → EXECUTE_TOOL: send_text_message → delivered
  → UPDATE_TURN_LOCAL_STATE: delivered_message_count=1
  → CHECK_TERMINAL: not terminal (finish_turn not yet called)

Model call 2:
  tool_calls: [finish_turn]
  → VALIDATE_TOOL_CALL: VALID
  → EXECUTE_TOOL: finish_turn → success
  → CHECK_TERMINAL: terminal!

PERSIST_TURN_RESULT:
  → write user msg "你好呀" to history
  → write assistant msg "你好呀！今天怎么样？" to history (source=assistant_reply)
```

**TurnResult**:
```python
TurnResult(
    turn_id="turn_001",
    terminal_state="END_REPLIED",
    success=True,
    delivered_messages=[
        TurnLocalSentMessage(text="你好呀！今天怎么样？", status="delivered"),
    ],
    skipped=False,
    failed=False,
    model_call_count=2,
    tool_call_count=2,
    delivered_message_count=1,
    send_attempt_count=1,
    failed_send_attempt_count=0,
    diagnostics={"protocol_retry_count": 0, "elapsed_ms": 3200},
)
```

### 例 2: user_chat 带 fact tool（查天气）后再回复

**TurnRequest**: 同上，用户说"今天上海天气怎么样？"

**模型调用序列**:
```
Model call 1:
  tool_calls: [get_weather(city="上海")]
  → VALIDATE_TOOL_CALL: VALID (fact_tool, in exposed set)
  → EXECUTE_TOOL: get_weather → "上海天气：晴 25°C 湿度50% 微风"
  → UPDATE_TURN_LOCAL_STATE: tool_results_this_turn += [get_weather result]
  → CHECK_TERMINAL: not terminal

Model call 2 (context includes weather result):
  tool_calls: [send_text_message(text="上海今天晴天，25度，挺舒服的~")]
  → VALIDATE_TOOL_CALL: VALID
  → EXECUTE_TOOL: send_text_message → delivered
  → UPDATE_TURN_LOCAL_STATE: delivered_message_count=1
  → CHECK_TERMINAL: not terminal

Model call 3:
  tool_calls: [finish_turn]
  → CHECK_TERMINAL: terminal!

PERSIST_TURN_RESULT:
  → 写入天气查询结果不入 history（只是 tool result）
  → 写入 assistant 消息 "上海今天晴天..." (source=assistant_reply)
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_REPLIED",
    success=True,
    delivered_message_count=1,
    model_call_count=3,
    tool_call_count=3,
    diagnostics={"tool_results": [{"tool": "get_weather", "status": "success"}]},
)
```

### 例 3: 多条消息 user_chat（3 条消息）

**TurnRequest**: 同上，但模型选择发多条消息。

**模型调用序列**:
```
Model call 1:
  tool_calls: [send_text_message(text="第一件事...")]
  → delivered → turn_local: msg_1 delivered
  → NOT TERMINAL

Model call 2 (context shows turn-local: msg_1 delivered, delivered_count=1/15):
  tool_calls: [send_text_message(text="然后第二件事...")]
  → delivered → turn_local: msg_2 delivered, delivered_count=2
  → NOT TERMINAL

Model call 3 (context shows turn-local: msg_1, msg_2 delivered, delivered_count=2/15):
  tool_calls: [send_text_message(text="最后一点...")]
  → delivered → turn_local: msg_3 delivered, delivered_count=3
  → NOT TERMINAL

Model call 4:
  tool_calls: [finish_turn]
  → TERMINAL
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_REPLIED",
    delivered_messages=[msg_1, msg_2, msg_3],
    delivered_message_count=3,
    model_call_count=4,
)
```

**关键验证**: Model call 2 时，msg_1 只在 current_turn_local_state block 中出现，不在 recent_conversation_history block 中出现。

### 例 4: 主动 turn — 模型选择跳过

**TurnRequest**:
```python
TurnRequest(
    turn_id="turn_proactive_001",
    turn_type="relationship_proactive",
    user_id="user_1",
    initiative_state=InitiativeState(
        consecutive_skip_count=0,
        last_decision=None,
    ),
    terminal_policy=TerminalPolicy(
        turn_type="relationship_proactive",
        required_terminal_tools=["finish_turn"],
        min_delivered_messages=0,
        max_delivered_messages=5,
        allows_skip=True,
        forced_speak=False,
        requires_next_wake=True,
    ),
    tool_exposure=ToolExposure(
        exposed_tool_names=["send_text_message", "skip_initiative", "finish_turn"],
        phase="skippable",
    ),
)
```

**模型调用序列**:
```
Model call 1:
  tool_calls: [skip_initiative(reason="用户可能还在忙")]
  → VALIDATE_TOOL_CALL: VALID (phase=skippable, allows_skip=True)
  → EXECUTE_TOOL: skip_initiative → success
  → UPDATE_TURN_LOCAL_STATE: skipped=True
  → CHECK_TERMINAL: not terminal (finish_turn not yet called)

Model call 2:
  tool_calls: [finish_turn(next_wake_minutes=45)]
  → CHECK_TERMINAL: terminal! (finish_turn + next_wake_minutes provided)
```

**TurnResult**:
```python
TurnResult(
    turn_id="turn_proactive_001",
    terminal_state="END_SKIPPED",
    success=False,
    delivered_messages=[],
    skipped=True,
    delivered_message_count=0,
    model_call_count=2,
    tool_call_count=2,
    next_wake_after=now + 45 * 60,  # 45 分钟后
)
```

**状态更新**: InitiativeState.last_skipped_at = now, consecutive_skip_count = 1, next_wake_after = now + 45min。

### 例 5: 主动 turn — 模型说话后结束

**TurnRequest**: 同上例。

**模型调用序列**:
```
Model call 1:
  tool_calls: [send_text_message(text="今天过得怎么样呀？")]
  → delivered → turn_local: msg_1 delivered
  → NOT TERMINAL

Model call 2:
  tool_calls: [finish_turn(next_wake_minutes=60)]
  → TERMINAL → END_REPLIED
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_REPLIED",
    success=True,
    delivered_messages=[msg_1],
    skipped=False,
    delivered_message_count=1,
    model_call_count=2,
    next_wake_after=now + 60 * 60,
)
```

**状态更新**: InitiativeState.last_spoke_at = now, consecutive_skip_count = 0。

### 例 6: 主动强制开口 — 上次 skipped，本次必须说话

**TurnRequest**:
```python
TurnRequest(
    turn_id="turn_proactive_002",
    turn_type="relationship_proactive",
    initiative_state=InitiativeState(
        consecutive_skip_count=1,
        last_decision="skipped",
    ),
    terminal_policy=TerminalPolicy(
        turn_type="relationship_proactive",
        required_terminal_tools=["finish_turn"],
        min_delivered_messages=1,   # MUST send
        max_delivered_messages=5,
        allows_skip=False,           # NO skip
        forced_speak=True,           # FORCED
        requires_next_wake=True,
    ),
    tool_exposure=ToolExposure(
        exposed_tool_names=["send_text_message", "finish_turn"],  # skip_initiative NOT exposed
        phase="forced_speak",
    ),
)
```

**模型调用序列**:
```
Model call 1:
  tool_calls: [skip_initiative(reason="...")]
  → VALIDATE_TOOL_CALL: REJECTED (tool_not_exposed — skip_initiative not in exposed_tool_names)
  → rejected_tool_calls += [reason: tool_not_exposed]
  → PROTOCOL_RETRY (在 retry budget 内)
  → 带上 "skip_initiative 不可用，本轮必须发送至少一条消息" 反馈

Model call 2 (with repair context):
  tool_calls: [send_text_message(text="想你啦，今天忙什么呢？")]
  → delivered → turn_local: msg_1
  → NOT TERMINAL

Model call 3:
  tool_calls: [finish_turn(next_wake_minutes=60)]
  → TERMINAL → END_REPLIED
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_REPLIED",
    success=True,
    delivered_messages=[msg_1],
    skipped=False,
    delivered_message_count=1,
    model_call_count=3,
    diagnostics={"rejected_tool_calls": [{"tool": "skip_initiative", "reason": "tool_not_exposed"}]},
)
```

### 例 7: 用户提醒到期 turn

**TurnRequest**:
```python
TurnRequest(
    turn_id="turn_reminder_001",
    turn_type="user_reminder_due",
    runtime_trigger=RuntimeTrigger(
        trigger_type="due_reminder",
        source_task_id="rem_001",
        reason="用户授权提醒到期：喝水",
    ),
    terminal_policy=TerminalPolicy(
        turn_type="user_reminder_due",
        required_terminal_tools=["finish_turn"],
        min_delivered_messages=1,
        max_delivered_messages=5,
        allows_skip=False,
        requires_next_wake=False,
    ),
    tool_exposure=ToolExposure(
        exposed_tool_names=["send_text_message", "mark_reminder_delivered", "finish_turn"],
        phase="reminder_delivery",
    ),
)
```

**模型调用序列**:
```
Model call 1:
  tool_calls: [send_text_message(text="宝宝，到时间喝水了~")]
  → delivered → turn_local: msg_1

Model call 2:
  tool_calls: [mark_reminder_delivered(reminder_id="rem_001")]
  → success

Model call 3:
  tool_calls: [finish_turn]
  → TERMINAL → END_REPLIED
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_REPLIED",
    success=True,
    delivered_messages=[msg_1],
    delivered_message_count=1,
    model_call_count=3,
    diagnostics={"reminder_delivered": "rem_001"},
)
```

### 例 8: 模型返回非法输出 → 修复重试成功

**模型调用 1**:
```
ModelCallResult(
    validation_status="valid",
    parsed_tool_calls=[],        # 无 tool calls！
    parsed_text="我觉得应该回复用户...",
)
```

**处理**:
```
VALIDATE_MODEL_OUTPUT:
  → parsed_tool_calls empty, parsed_text non-empty
  → protocol_violation: text_only_no_tool_calls
  → protocol_retry_count=1 (budget: max=1)
  → STRUCTURE_REPAIR: 带上修复指令
    "你必须使用 native tool calls 表达你的决定。不要只输出文本。"
  → CALL_MODEL
```

**模型调用 2** (修复后):
```
ModelCallResult(
    validation_status="valid",
    parsed_tool_calls=[send_text_message(text="好的收到！")],
    parsed_text=None,
)
```

**处理**:
```
VALIDATE_MODEL_OUTPUT: VALID
VALIDATE_TOOL_CALL: VALID
EXECUTE_TOOL: send_text_message → delivered
... → finish_turn → END_REPLIED
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_REPLIED",
    success=True,
    model_call_count=2,
    diagnostics={"protocol_retry_count": 1},
)
```

### 例 9: 预算耗尽 — 模型调用次数过多

**场景**: 模型在 20 次调用后仍未调用 finish_turn。

**模型调用 20** (model_call_count 达到 max_model_calls=20):
```
VALIDATE_MODEL_OUTPUT: VALID
EXECUTE_TOOL: send_text_message(...)
UPDATE_TURN_LOCAL_STATE: delivered_message_count=5

CHECK_TERMINAL:
  → budget.model_call_count (20) >= budget.max_model_calls (20)
  → budget_exhausted_type = "max_model_calls"
  → FORCE TERMINAL
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_BUDGET_EXHAUSTED",
    success=True,  # 有 delivered messages
    delivered_messages=[msg_1, msg_2, msg_3, msg_4, msg_5],
    delivered_message_count=5,
    model_call_count=20,
    diagnostics={
        "budget_exhausted": "max_model_calls",
        "max_model_calls": 20,
    },
)
```

### 例 10: 硬兜底触发

**场景**: ContextBuilder 构建上下文时出现 FATAL 错误（工具注册表为空）。

```
BUILD_CONTEXT: ContextBuildResult(fatal_error="tool_registry_unavailable")
CLASSIFY_CONTEXT_ERROR: FATAL → HARD_FALLBACK

HARD_FALLBACK:
  1. INTERRUPT_CHECK: not interrupted
  2. 消息文本: "我刚刚卡了一下 重新说"
  3. GUARD_OUTPUT: 检查通过 (PASSED)
  4. EXEC_SEND: wechat_sender 发送 → delivered
  5. END_FALLBACK_SENT
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_FALLBACK_SENT",
    success=False,  # 不算正常送达
    delivered_messages=[TurnLocalSentMessage(text="我刚刚卡了一下 重新说", status="delivered")],
    model_call_count=0,
    diagnostics={
        "fallback_events": [{"type": "fatal_context_error", "detail": "tool_registry_unavailable"}],
    },
)
```

**历史写入**: 用户消息正常写入，bot 消息以 source="assistant:fallback" 写入。

### 例 11: Turn 中途被中断

**场景**: 用户在 bot 正在处理第一条消息回复时发了新消息。

```
CALL_MODEL → 模型返回 send_text_message("第一句")
VALIDATE_TOOL_CALL: VALID
EXECUTE_TOOL: send_text_message → delivered
UPDATE_TURN_LOCAL_STATE: delivered_message_count=1

CHECK_TERMINAL: not terminal → BUILD_CONTEXT

在 BUILD_CONTEXT 前:
  用户新消息到达 → _user_latest_message_ts 更新
  → interrupt_token 中的 timestamp_ms < _user_latest_message_ts

INTERRUPT_CHECK (检查点 1):
  → checker(interrupt_token) → True
  → END_CANCELLED
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_CANCELLED",
    success=False,
    delivered_messages=[],  # 中断后不写任何内容
    diagnostics={"interrupt_events": [{"check_point": 1, "reason": "superseded"}]},
)
```

**注意**: 虽然 send_text_message 已经执行且消息已成功发送给用户，但由于中断检测到当前 turn 已被 supersede，该消息的 history 写入也被跳过。这是设计上的取舍：防止旧 turn 的 assistant 消息在新 turn 中造成上下文混乱。已发出的微信消息无法撤回（渠道层面的现实），但 conversation history 层面跳过写入。

### 例 12: 发送失败 → 重试 → 部分送达 → END_PARTIAL_SENT

**场景**: 模型想发 3 条消息，第一条送达，第二条失败两次，第三条约好但预算耗尽。

```
Model call 1: send_text_message("第一条") → delivered (delivered_count=1)
Model call 2: send_text_message("第二条") → FAILED_BEFORE_SEND (send_attempt=1)
  → retry: FAILED_BEFORE_SEND again (send_attempt=2)
  → retry: UNKNOWN_AFTER_ATTEMPT (send_attempt=3)

[send_attempt_count=3, max_send_attempts=3 → budget exhausted]

CHECK_TERMINAL:
  → send_attempt budget exhausted
  → delivered_message_count=1 (仅第一条)
  → END_PARTIAL_SENT
```

**TurnResult**:
```python
TurnResult(
    terminal_state="END_PARTIAL_SENT",
    success=True,  # 有至少一条送达
    delivered_messages=[msg_1],
    delivered_message_count=1,
    send_attempt_count=3,
    failed_send_attempt_count=2,
    diagnostics={
        "failed_deliveries": [
            {"text": "第二条", "status": "FAILED_BEFORE_SEND", "attempts": 2},
            {"text": "第二条", "status": "UNKNOWN_AFTER_ATTEMPT", "attempts": 1},
        ],
    },
)
```

**历史写入**: 只有 msg_1 ("第一条") 写入 assistant history。

---

## 16. 当前代码迁移点

从当前代码到目标 TurnEngine 的迁移涉及以下文件和位置：

### 16.1 agent/chat_agent.py（主要迁移源）

| 当前实现 | 目标模块 | 迁移说明 |
|---------|---------|---------|
| `ChatAgent.chat()` 主入口 | `TurnEngine.run()` | 保留状态机主循环逻辑，剥离上下文构建和工具暴露 |
| `ChatAgent._dispatch()` | `TurnEngine._dispatch()` | 保留状态分发框架，重构 state handler 为模块化 |
| `_do_receive_event` | 归入 `TURN_CREATED` | 简化为入参校验 |
| `_do_create_turn` | 归入 `TURN_CREATED` | 保留 interrupt_token 生成 |
| `_do_interrupt_check` | 归入各检查点 | 保留 checker 逻辑，分散到 6 个位置 |
| `_do_build_context` | **委托给 ContextBuilder** | 移除内部拼 prompt 逻辑 |
| `_do_classify_context_error` | **委托给 ContextBuilder + ToolExposurePolicy** | 上下文错误分类保留，工具注册表检查移到启动时 |
| `_do_optional_preprocess` | 归入 ContextBuilder | 时间预取逻辑移到 ContextBuilder |
| `_do_call_llm` | **委托给 ProviderGateway** | 保留调用计数器，其余委托 |
| `_do_handle_model_output` | 归入 `VALIDATE_MODEL_OUTPUT` | 校验逻辑保留，增强为完整结构校验 |
| `_do_classify_one_tool_call` | 归入 `VALIDATE_TOOL_CALL` | 扩展为批量校验 + 组合合法性检查 + phase 检查 |
| `_do_exec_fact_tools` | **委托给 ToolExecutor** | 移除工具实现细节 |
| `_do_exec_state_tool` | **委托给 ToolExecutor** | 移除调度逻辑 |
| `_do_validate_reply_args` | 归入 `VALIDATE_TOOL_CALL` | 统一到参数校验 |
| `_do_guard_output` | 归入 `EXECUTE_TOOL` (send 前) | 保留 guard 逻辑 |
| `_do_pre_send_interrupt_check` | 归入中断检查点 5 | 保留 |
| `_do_exec_send_one` | **委托给 DeliveryService** | 移除 wechat_sender 直接调用 |
| `_do_append_send_result` | 归入 `UPDATE_TURN_LOCAL_STATE` | 保留 |
| `_do_post_send_thinking_delay` | 归入 TurnEngine 可选延迟 | 保留（interruptible delay） |
| `_do_post_send_interrupt_check` | 归入中断检查点 5 后 | 保留 |
| `_do_limit_check` | 归入预算检查 | 统一到 TurnBudget |
| `_do_validate_done_args` | 归入 `VALIDATE_TOOL_CALL` (finish_turn) | 统一到参数校验 |
| `_do_validate_skip_proactive_args` | 归入 `VALIDATE_TOOL_CALL` (skip_initiative) | 统一到参数校验 |
| `_do_exec_skip_proactive` | 归入 `EXECUTE_TOOL` (skip_initiative) | 保留 |
| `_do_protocol_retry` | 归入 `STRUCTURE_REPAIR` | 统一到协议重试流程 |
| `_do_flash_to_pro_upgrade` | 归入 ProviderGateway 路由 | 委托 |
| `_do_hard_fallback` | 归入 `HARD_FALLBACK` | 保留，但固定文案+guard+发送逻辑不变 |
| `_run_post_terminal_pipeline` | 归入 TurnEngine post-terminal | 保留 hook 执行顺序和异常隔离 |

### 16.2 agent/state_machine.py（类型迁移）

| 当前类型 | 目标类型 | 迁移说明 |
|---------|---------|---------|
| `TurnState` (enum) | 保留并扩展 | 添加新的中间状态：LOAD_RUNTIME_STATE, CLASSIFY_TURN, RESOLVE_TOOL_EXPOSURE, SELECT_MEMORY, SAVE_CONTEXT_SNAPSHOT, STRUCTURE_REPAIR, CHECK_TERMINAL, PERSIST_TURN_RESULT, POST_TURN_MEMORY, RELEASE_LOCK |
| `TurnScratchpad` | `CurrentTurnLocalState` | 重构为新的数据结构，分离 turn-local 状态与审计痕迹 |
| `TurnResult` | `TurnResult` | 扩充字段：delivered_messages (list[TurnLocalSentMessage]), budget_state, next_wake_after |
| `DeliveryStatus` | 保留 | 六态模型不变 |
| `VisibleMessage` | `TurnLocalSentMessage` | 重构为新的数据结构 |
| `InterruptChecker` | 保留 | 签名 `Callable[[str], bool]` 不变 |
| `PostTerminalCallback` | 保留 | 签名不变 |
| `MAX_REPLY_MESSAGES_PER_TURN` (15) | `max_delivered_messages` in TurnBudget | 移到 TurnBudget |
| `MAX_LLM_STEPS_PER_TURN` (20) | `max_model_calls` in TurnBudget | 移到 TurnBudget |
| `MAX_PROTOCOL_RETRIES_PER_TURN` (2) | `max_protocol_retries` in TurnBudget | 移到 TurnBudget |
| `MAX_SEND_FAILURES_PER_TURN` (2) | `max_send_attempts` in TurnBudget | 移到 TurnBudget |

### 16.3 agent/tools.py（工具执行迁移）

| 当前实现 | 目标模块 | 迁移说明 |
|---------|---------|---------|
| `execute_tool()` | `ToolExecutor.execute()` | 保留事实工具执行逻辑，添加 exposed_tool_names 检查 |
| `_get_time()` | 保留 | 不变 |
| `_get_weather()` | 保留 | 不变 |

### 16.4 main.py（钩子迁移）

| 当前实现 | 目标位置 | 迁移说明 |
|---------|---------|---------|
| `_write_model_history()` callback | Post-terminal pipeline Phase 1 | 调用方从 main.py 移到 TurnEngine |
| `_build_post_turn_hooks()` | Post-terminal pipeline Phase 2 | 5 个 hook 的构造移到 TurnEngine 或配置 |
| `check_interrupt()` | InterruptChecker | 注入方式不变，调用位置移到 TurnEngine |
| `wechat_sender()` | DeliveryService | 发送回调移到 DeliveryService |

---

## 17. 必须测试的用例列表

以下是 TurnEngine 实现时必须通过的全部测试用例（至少 20 个）：

### T01: 空输出 → PROTOCOL_RETRY → success
模型第一次返回纯文本（无 tool calls），修复后返回合法 send_text_message + finish_turn。验证 protocol_retry_count=1，最终 END_REPLIED。

### T02: 纯文本 assistant.content 不发送给用户
模型返回 `assistant.content = "我决定回复..."` 且无 tool calls。验证：不调用 wechat_sender，不写 assistant history，进入 PROTOCOL_RETRY。

### T03: 不在 exposed 中的工具被拒绝
ToolExposure.exposed_tool_names = ["send_text_message", "finish_turn"]，模型请求调用 skip_initiative。验证：rejected (tool_not_exposed)，不执行。

### T04: model_call_count 达到上限 → 安全终态
设置 max_model_calls=3，模拟模型在第 3 次调用时仍未调 finish_turn。验证：第 3 次调用后不发起第 4 次调用，进入 END_BUDGET_EXHAUSTED，已送达消息写入历史。

### T05: delivered_message_count 达到 15 上限
模型在 user_chat 中连续成功发 15 条消息后尝试发第 16 条。验证：第 16 条被拒绝（或 ToolExposurePolicy 不再暴露 send_text_message），delivered_count 永不超过 15。

### T06: 失败的发送不计入 delivered_message_count
send_text_message → FAILED_BEFORE_SEND → retry → SENT_CONFIRMED。验证：失败尝试不增加 delivered_message_count，只有确认送达才增加。

### T07: turn-local state 与 recent history 隔离
模型发送 msg_1 后继续调用。验证：第二次模型调用时，msg_1 只出现在 turn-local block，不出现在 recent_history block。

### T08: 终态后写历史，turn 中不写
验证：在 turn 结束前（CHECK_TERMINAL 通过之前），sent_messages_this_turn 中的消息未出现在 conversation history 表中。turn 结束后才写入。

### T09: failed delivery 不写入 assistant history
发送失败（UNKNOWN_AFTER_ATTEMPT 或 FAILED_CONFIRMED）。验证：turn 结束后 assistant history 中无该消息。

### T10: finish_turn 是唯一终态入口
模型发一条消息后不调 finish_turn，尝试用自然语言表示结束。验证：turn 不结束，CHEKCK_TERMINAL 返回 not terminal。

### T11: skip_initiative 只在可跳过阶段被允许
设置 TerminalPolicy(allows_skip=False, forced_speak=True)。模型调用 skip_initiative → rejected (invalid_phase)。

### T12: 多终态工具组合 → PROTOCOL_RETRY
模型在一次调用中返回 `[finish_turn, skip_initiative]`。验证：被拒绝，protocol_retry_count++。

### T13: finish_turn + get_weather 组合 → PROTOCOL_RETRY
模型在一次调用中返回 `[finish_turn, get_weather]`。验证：被拒绝（终态工具不能与非 send_text_message 工具组合）。

### T14: ContextSnapshot 每次模型调用前保存
模拟 3 次模型调用。验证：context_snapshot_ids 长度 = 3，每个快照包含 call_id 和 exposed_tool_names。

### T15: POST_TURN_HOOK 失败不改变终态
Post-turn hook 中抛异常。验证：terminal_state 不变，其他 hooks 继续执行，TurnResult.terminal_state 为正确的终态值。

### T16: 中断检查 — 第 1 检查点
TURN_CREATED 后立即检测到中断。验证：→ END_CANCELLED，不调用模型，不发送消息。

### T17: 中断检查 — 检查点 5（发送前）
模型返回 send_text_message，在 GUARD_OUTPUT 后、EXEC_SEND_ONE 前检测到中断。验证：→ END_CANCELLED，不执行发送。

### T18: HARD_FALLBACK — 使用固定文案
触发硬兜底。验证：发送的文本是"我刚刚卡了一下 重新说"，不是模型原始输出的任何内容。history 中 source="assistant:fallback"。

### T19: 主动可跳过 → 跳过 → 下一次强制开口
第一轮：allows_skip=True, min_delivered=0 → 模型第一次调用 skip_initiative，第二次调用 finish_turn(next_wake_minutes=45) → END_SKIPPED。
第二轮：consecutive_skip_count=1 → allows_skip=False, forced_speak=True → 模型尝试 skip_initiative 被拒绝。

### T20: 主动提醒到期 — 不受 skip 规则影响
Turn type = user_reminder_due，模型尝试调 skip_initiative。验证：skip_initiative 不在 exposed_tool_names 中（或被拒绝），发送正常进行。

### T21: 发送失败后重试 → 部分成功
3 条消息：msg_1 delivered, msg_2 FAILED_BEFORE_SEND x2 + UNKNOWN_AFTER_ATTEMPT x1, msg_3 未发送（budget 耗尽）。验证：END_PARTIAL_SENT，只写 msg_1 到历史。

### T22: memory_management — forget directive 阻断记忆
用户说"别记这个"，模型调用 apply_forget_directive + finish_turn。验证：POST_TURN_MEMORY 阶段检查 forget_directive，前后 5 轮对话不提取记忆。

### T23: 硬兜底消息也过 guard
触发硬兜底，但 guard_single_message 将固定文案判定为 BLOCKED（极端情况）。验证：不发送，记录 BLOCKED_BY_GUARD，终态为 END_FAILED。

### T24: budget 超时 — 当前操作允许完成
设置 max_elapsed_seconds=0.1（极短），但 send_text_message 正在执行中。验证：不截断正在进行的发送，完成后进入安全终态。

---

## 附录 A: 与当前 ChatAgent State Machine 的对照表

| 当前 State (v1.5) | 目标 State | 变化说明 |
|------------------|-----------|---------|
| RECEIVE_EVENT | TURN_CREATED | 合并，统一初始化 |
| CREATE_TURN | TURN_CREATED (子步骤) | 合并 |
| INTERRUPT_CHECK | 分散到 6 个检查点 | 不再作为独立 state，改为检查点 |
| BUILD_CONTEXT | BUILD_CONTEXT (委托 ContextBuilder) | 逻辑委托，TurnEngine 不再自己拼 prompt |
| CLASSIFY_CONTEXT_ERROR | 归入 ContextBuilder | 上下文错误分类移到 ContextBuilder |
| OPTIONAL_PREPROCESS | 归入 ContextBuilder | 时间预取移到 ContextBuilder |
| CALL_LLM | CALL_MODEL (委托 ProviderGateway) | 逻辑委托 |
| HANDLE_MODEL_OUTPUT | VALIDATE_MODEL_OUTPUT | 增强为完整结构校验 |
| CLASSIFY_ONE_TOOL_CALL | VALIDATE_TOOL_CALL | 扩展为批量校验 + 组合检查 + phase 检查 |
| EXEC_FACT_TOOLS | EXECUTE_TOOL (子步骤) | 合并到统一工具执行 |
| EXEC_STATE_TOOL | EXECUTE_TOOL (子步骤) | 合并 |
| VALIDATE_REPLY_ARGS | VALIDATE_TOOL_CALL (子步骤) | 合并到统一参数校验 |
| GUARD_OUTPUT | EXECUTE_TOOL (send 前 guard) | 保留，但位置调整 |
| PRE_SEND_INTERRUPT_CHECK | 检查点 5 | 保留 |
| EXEC_SEND_ONE | EXECUTE_TOOL (委托 DeliveryService) | 委托 |
| APPEND_SEND_RESULT | UPDATE_TURN_LOCAL_STATE | 保留 |
| POST_SEND_THINKING_DELAY | TurnEngine 可选延迟 | 保留 |
| POST_SEND_INTERRUPT_CHECK | 检查点 5 后 | 保留 |
| LIMIT_CHECK | 归入预算检查 | 统一到 TurnBudget |
| VALIDATE_DONE_ARGS | VALIDATE_TOOL_CALL (finish_turn) | 合并 |
| VALIDATE_SKIP_PROACTIVE_ARGS | VALIDATE_TOOL_CALL (skip_initiative) | 合并 |
| EXEC_SKIP_PROACTIVE | EXECUTE_TOOL (skip_initiative) | 保留 |
| PROTOCOL_RETRY | STRUCTURE_REPAIR | 统一 |
| FLASH_TO_PRO_UPGRADE | ProviderGateway 路由 | 委托 |
| HARD_FALLBACK | HARD_FALLBACK | 保留 |
| WRITE_MODEL_HISTORY | Post-terminal Phase 1 | 保留 |
| POST_TURN_HOOKS | Post-terminal Phase 2 | 保留 |
| (新) | LOAD_RUNTIME_STATE | 新增：加载运行时状态 |
| (新) | CLASSIFY_TURN | 新增：turn type 分类 |
| (新) | RESOLVE_TOOL_EXPOSURE | 新增：工具暴露决策 |
| (新) | SELECT_MEMORY | 新增：记忆选择 |
| (新) | SAVE_CONTEXT_SNAPSHOT | 新增：上下文快照保存 |
| (新) | CHECK_TERMINAL | 新增：明确终态判断 |
| (新) | PERSIST_TURN_RESULT | 新增：持久化收尾 |
| (新) | POST_TURN_MEMORY | 新增：记忆写入 |
| (新) | RELEASE_LOCK | 新增：锁释放 |
| (新) | STRUCTURE_REPAIR | 新增：结构修复状态 |
| END_REPLIED | END_REPLIED | 保留 |
| END_NO_REPLY | END_NO_REPLY | 保留 |
| END_FAILED | END_FAILED | 保留 |
| END_FALLBACK_SENT | END_FALLBACK_SENT | 保留 |
| END_CANCELLED | END_CANCELLED | 保留 |
| END_MAX_LIMIT | END_BUDGET_EXHAUSTED | 重命名为更准确的 budget exhausted |
| END_PROTOCOL_ERROR | END_PROTOCOL_ERROR | 保留 |
| (新) | END_SKIPPED | 新增：主动跳过终态 |
| (新) | END_PARTIAL_SENT | 新增：部分送达终态 |
| (新) | END_DRY_RUN | 新增：管理台 dry-run 终态 |

## 附录 B: TurnBudget 完整配置

```python
@dataclass
class TurnBudgetConfig:
    """TurnBudget 的默认配置值。"""

    # 模型调用上限（一次 turn 中最多调用模型几次）
    max_model_calls: int = 20

    # 工具调用上限（一次 turn 中最多执行几次工具）
    max_tool_calls: int = 40

    # 已送达消息上限（硬上限 15，需求文档 13.3）
    max_delivered_messages: int = 15

    # 发送尝试上限（含重试，同一消息可能多次尝试）
    max_send_attempts: int = 3

    # Wall-clock 时间上限（秒）
    max_elapsed_seconds: float = 120.0

    # 协议违规重试上限
    max_protocol_retries: int = 1

    # 主动消息默认软消息上限（推荐数量，非硬上限）
    proactive_soft_reply_limit: int = 3

    # 主动消息最大 LLM steps
    proactive_max_llm_steps: int = 8
```

---

> 规格结束。本文档是 TurnEngine 的唯一可执行规格来源。所有实现、测试和代码审查均以此文档为准。
