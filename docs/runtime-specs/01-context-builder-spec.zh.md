# ContextBuilder 可执行实现规格

> 日期：2026-06-03
> 状态：可执行实现规格 V1.0
> 上游文档：
> - `docs/companion-architecture-requirements.zh.md`（尤其第 6, 7, 11, 13 节）
> - `docs/companion-architecture-design-decisions.zh.md`（尤其第 2, 4, 5, 9.2, 9.3 节）
> - `docs/companion-runtime-module-contracts.zh.md`（尤其第 3, 8 节）
> - `docs/companion-runtime-data-contracts.zh.md`（尤其第 2, 7, 8, 10, 11 节）
> - `docs/companion-runtime-state-machines.zh.md`（尤其第 1, 2, 5 节）
> - `docs/companion-runtime-gap-analysis.zh.md`（尤其第 3.1 节）
> - `docs/companion-runtime-implementation-plan.zh.md`（尤其 Phase 3, 5）
> - `docs/companion-runtime-test-plan.zh.md`（尤其第 3 节）
> 目的：另一个 agent 可以**只读这份文档**就正确实现 ContextBuilder，无需猜测任何边界条件。

---

## 目录

1. [模块目标](#1-模块目标)
2. [完整输入输出对象](#2-完整输入输出对象)
3. [不可违反的不变量](#3-不可违反的不变量)
4. [上下文打包顺序](#4-上下文打包顺序)
5. [SystemTimeBlock 规则](#5-systemtimeblock-规则)
6. [上下文严格区分规则](#6-上下文严格区分规则)
7. [决策表](#7-决策表)
8. [伪代码](#8-伪代码)
9. [不同条件下的反应](#9-不同条件下的反应)
10. [失败路径](#10-失败路径)
11. [禁止行为](#11-禁止行为)
12. [具体输入输出例子](#12-具体输入输出例子)
13. [当前代码迁移点](#13-当前代码迁移点)
14. [必须测试的用例列表](#14-必须测试的用例列表)

---

## 1. 模块目标

ContextBuilder 是**所有模型输入的唯一构造入口**。系统中任何需要调用 LLM 的路径必须通过 ContextBuilder 组装输入，禁止任何模块自行拼接 provider messages。

### 1.1 ContextBuilder 负责什么

| 负责 | 说明 |
|------|------|
| 按固定顺序组装所有 context block | 15 个 block 的顺序不可变（见第 4 节） |
| 区分 "已完成历史" 与 "本轮临时状态" | recent_conversation_history vs current_turn_local_state |
| 注入系统时间作为独立 block | SystemTimeBlock，非 tool call |
| 注入媒体上下文作为独立 block | media_context，明确标注来源 |
| 生成 context_snapshot | audit 用途的摘要记录 |
| 生成 TokenBudgetReport | token 预算消耗的实时报告 |
| 在 token 预算超限时按规则截断 | 截断顺序见决策表 7.2 |
| 验证 prompt contract 工具名与 native schema 工具名一致 | 不一致时构造失败 |
| 按 turn_type 选择 terminal_policy | 主动/普通/提醒的终态策略不同 |
| 在 forced_speak 阶段不暴露 skip_initiative | 工具暴露由 ToolExposurePolicy 输入决定，ContextBuilder 负责将其渲染进 tool_contract_block |

### 1.2 ContextBuilder 不负责什么

| 不负责 | 由谁负责 |
|--------|----------|
| 决定本轮暴露哪些工具 | ToolExposurePolicy |
| 执行工具 | ToolExecutor |
| 调用模型 | ProviderGateway |
| 发送消息 | DeliveryService |
| 写入长期记忆 | MemorySystem |
| 修改 turn state | TurnEngine |
| 判断 turn_type | TurnClassifier |
| 创建 turn_id | TurnEngine |

### 1.3 ContextBuilder 的调用入口

**唯一调用入口：**

```python
class ContextBuilder:
    def build(self, request: BuildContextRequest) -> BuiltContext:
        ...
```

禁止通过其他方法绕过 `build()` 构建 provider messages。任何 model call 前都必须调用 `ContextBuilder.build()`。

---

## 2. 完整输入输出对象

### 2.1 BuildContextRequest

```python
@dataclass
class BuildContextRequest:
    """ContextBuilder 的输入。TurnEngine 调用 ContextBuilder.build() 前负责组装。"""

    # ── 标识 ────────────────────────────────────────────
    turn_id: str
    """必填。本轮的 turn_id。示例: "turn_abc123_1717400000" """

    call_id: str
    """必填。本次 model call 的唯一 id。同一 turn 内每次 LLM 调用有独立 call_id。
       本 field 唯一允许重复的情况是结构化输出修复重试（repair retry）。
       示例: "call_abc123_1" """

    turn_type: TurnType
    """必填。本轮类型。
       取值: user_chat | user_media_chat | user_reminder_request |
             relationship_proactive | user_reminder_due | scheduled_ritual |
             admin_manual_trigger | memory_management """

    user_id: str
    """必填。用户标识。示例: "wxid_abc123" """

    bot_id: str
    """必填。Bot 标识。示例: "default_bot" """

    # ── 当前输入 ────────────────────────────────────────
    inbound_batch: InboundMessageBatch | None
    """可选。用户本轮输入。user_chat / user_media_chat 时必须存在。
       proactive / reminder / ritual 时为 None。"""

    trigger: RuntimeTrigger | None
    """可选。后台触发信息。proactive / reminder_due / scheduled_ritual 时必须存在。
       user_chat 时为 None。"""

    # ── turn 本地状态 ───────────────────────────────────
    current_turn_local_state: CurrentTurnLocalState
    """必填。本轮到目前为止的临时状态。
       第一轮 model call 时可能只含计数器。
       后续 model call 时含已发送消息、工具结果、送达状态。"""

    # ── 工具暴露策略 ────────────────────────────────────
    tool_exposure: ToolExposure
    """必填。本轮允许暴露的工具集合。
       由 ToolExposurePolicy.resolve() 产出。
       含 exposed_tool_names / native_tool_schemas / tool_contract_block。"""

    # ── 终态策略 ────────────────────────────────────────
    terminal_policy: TerminalPolicy
    """必填。本轮终态规则。
       含 required_terminal_tools / allows_skip / forced_speak / min_delivered_messages。"""

    # ── 记忆选择 ────────────────────────────────────────
    memory_selection: MemorySelection
    """必填。本轮可选用的记忆块。
       即使没有记忆可选中也要传入 MemorySelection.empty()。
       含 boundary / profile / relationship / episode / open_thread / sent_content。"""

    # ── 对话历史 ────────────────────────────────────────
    recent_history: RecentConversationHistory
    """必填。已完成 turn 的真实历史（只有 delivered 的 assistant 消息）。
       即使空历史也要传入 RecentConversationHistory.empty()。"""

    # ── 关系状态 ────────────────────────────────────────
    relationship_state: RelationshipState | None
    """可选。当前 bot-user 的关系状态。缺失时 ContextBuilder 记录 warning 并继续。"""

    # ── 人设 ────────────────────────────────────────────
    persona: str
    """必填。Bot 的性格/语气/身份描述。中文人设文本。
       示例: "你是用户的女友，性格温柔带一点小调皮..." """

    # ── 固定提示词 ──────────────────────────────────────
    fixed_system_protocol: str
    """必填。固定系统规则。英文协议文本。由 prompts/base_protocol.md 提供。"""

    fixed_output_protocol: str
    """必填。固定输出规则。英文输出协议。由 prompts/output_protocol.md 提供。"""

    fixed_tool_protocol: str
    """必填。固定工具规则。英文工具协议。由 prompts/tool_policy.md 提供。"""

    # ── 系统时间 ────────────────────────────────────────
    system_time: SystemTime | None
    """可选。当前系统时间。None 时进入 degraded mode，改用 get_time 工具暴露。
       示例: SystemTime(datetime(2026, 6, 3, 14, 30, 0), "Asia/Shanghai") """

    # ── 媒体上下文 ──────────────────────────────────────
    media_context: list[MediaContextItem]
    """可选。本轮的媒体摘要列表。user sends image/voice 时填充。
       空列表时 media_context block 不出现在 provider_messages 中。"""

    # ── 配置 ────────────────────────────────────────────
    debug_mode: bool
    """默认 False。True 时 context_snapshot 保存完整 provider payload。"""

    token_budget: int
    """默认 8000。本轮 model call 的最大 token 预算（不含 response）。
       ContextBuilder 用此预算判断是否需要截断。"""

    default_city: str
    """可选。用户默认城市，写入 system block。示例: "上海" """

    # ── 主动消息状态 ────────────────────────────────────
    initiative_state: InitiativeState | None
    """可选。active turn 时的主动状态。用于 forced_speak 判断和 sent_content 展示。"""

    # ── 提醒状态 ────────────────────────────────────────
    reminder_state: ReminderState | None
    """可选。reminder_due turn 时的提醒状态。"""
```

### 2.2 BuiltContext

```python
@dataclass
class BuiltContext:
    """ContextBuilder.build() 的输出。"""

    # ── 标识 ────────────────────────────────────────────
    turn_id: str
    """回传 BuildContextRequest.turn_id"""

    call_id: str
    """回传 BuildContextRequest.call_id"""

    # ── 给 ProviderGateway 的 payload ────────────────────
    provider_messages: list[ProviderMessage]
    """按固定顺序组装的完整 messages 列表。
       每条 message 的 role 必定是 system / user / assistant / tool 之一。
       禁止出现非标准 role。"""

    # ── 工具 ────────────────────────────────────────────
    exposed_tool_schemas: list[NativeToolSchema]
    """本轮原生 tool schema 列表。直接传给 ProviderGateway。
       必须与 tool_contract_block 中描述的工具完全一致。"""

    # ── 工具约定块（文本，嵌入 provider_messages）─────────
    tool_contract_block: ProviderMessage | None
    """如果 tool_contract_block 作为独立 system message 嵌入 provider_messages，
       则此处保存其引用（用于 snapshot 中单独标注）；否则为 None。"""

    # ── 上下文块（结构化描述）───────────────────────────
    context_blocks: list[ContextBlock]
    """所有 context block 的结构化列表。每个 block 含 kind / cache_class / content_summary。
       用于 debug 和 snapshot，不直接注入 model。"""

    # ── 审计快照 ────────────────────────────────────────
    context_snapshot: ContextSnapshot
    """本轮模型输入的审计摘要。
       保存到 ContextSnapshotStore，**不**回灌给下一轮模型。"""

    # ── token 预算报告 ──────────────────────────────────
    token_budget_report: TokenBudgetReport
    """实际消耗、截断详情、预算剩余。
       若 budget_exhausted=True，provider_messages 已按规则截断。"""

    # ── 终态策略 ────────────────────────────────────────
    terminal_policy: TerminalPolicy
    """回传 BuildContextRequest.terminal_policy（不做修改）。
       供 TurnEngine 使用。"""

    # ── 警告 ────────────────────────────────────────────
    warnings: list[ContextBuildWarning]
    """构建过程中记录的非致命问题。每条含 source / detail / severity。
       severity 取值: info / warning / error。
       error 级别表示已自动降级处理但需关注。"""
```

### 2.3 ContextBlock

```python
@dataclass
class ContextBlock:
    """描述 provider_messages 中的一个逻辑块。

    每个 context block 必须明确：
    - 它来自哪里（source）。
    - 它是否进入模型（enters_model）。
    - 它是否进入 snapshot（enters_snapshot）。
    - 它的 cache class（用于 provider prompt caching 优化）。
    """

    block_id: str
    """唯一 block 标识。示例: "fixed_system_protocol", "current_input", "memory_boundary_1" """

    kind: str
    """block 类别。
       取值：fixed_protocol | persona | relationship_state | memory | recent_history |
             turn_local | media | system_time | current_input | tool_contract | terminal_policy """

    cache_class: str
    """缓存分类。
       fixed: turn 间不变（system protocol / persona）。
       low_frequency: 记忆/关系等低频变化。
       turn_dynamic: 每轮变化（current input / turn_local / recent_history tail）。"""

    content_summary: str
    """该 block 内容的摘要文本。中文，≤200 字符。用于 snapshot 人类可读。
       示例: "system protocol (248 tokens)" """

    content_ref: str | None
    """对完整内容的引用路径。debug 模式下保存全文引用。
       示例: "snapshot:snap_xxx:full_payload:blocks:13" """

    source: str
    """该 block 内容的来源模块。示例: "ContextBuilder", "MemorySystem", "ToolExposurePolicy" """

    enters_model: bool
    """该 block 是否进入 provider_messages。
       正常情况都是 True。
       只有 snapshot-only block（纯审计记录）才是 False。"""

    enters_snapshot: bool
    """该 block 是否进入 context_snapshot。
       都是 True。API key 等敏感信息不应通过 ContextBlock 传递，
       而是在 snapshot 外独立处理。"""
```

### 2.4 ProviderMessage

```python
@dataclass
class ProviderMessage:
    """一条发送给模型供应商的消息。

    直接映射到 OpenAI / Anthropic / DeepSeek 的 messages 格式。
    注意：role=system 可以有多条（多 system prompt 注入），
          role=user/assistant 交替组成对话历史。
    """

    role: str
    """必填。取值: system | user | assistant | tool """

    content: str | list[ContentPart]
    """消息内容。
       system: 纯文本 str。
       user/assistant: 纯文本 str，或含图片/音频的多模态 ContentPart 列表。
       tool: 纯文本 str，工具执行结果。"""

    name: str | None
    """role=tool 时必填。调用该工具的 tool_name。
       示例: "send_text_message", "get_time" """

    tool_call_id: str | None
    """role=tool 时必填。对应 ModelToolCall.tool_call_id。"""
```

### 2.5 ContentPart

```python
@dataclass
class ContentPart:
    """ProviderMessage 的内容片段，用于多模态输入。"""

    type: str
    """取值: text | image | audio """

    text: str | None
    """type=text 时的文本内容。"""

    image_url: str | None
    """type=image 时的图片引用（base64 data URI 或 URL）。"""

    audio_url: str | None
    """type=audio 时的音频引用。"""
```

### 2.6 ContextSnapshot

```python
@dataclass
class ContextSnapshot:
    """本轮模型输入的审计摘要。保存到 ContextSnapshotStore，不进入下一轮模型上下文。"""

    snapshot_id: str
    """唯一标识。格式: "snap_{turn_id}_{call_id}" """

    turn_id: str
    """所属 turn_id。"""

    call_id: str
    """所属 call_id。"""

    created_at: datetime
    """快照创建时间（UTC）。"""

    block_summaries: list[ContextBlock]
    """所有 context block 的摘要列表（block 数量 = 实际打包的 block 数）。"""

    exposed_tool_names: list[str]
    """本轮暴露的工具名列表。示例: ["send_text_message", "finish_turn", "get_time"] """

    history_message_ids: list[str]
    """进入 recent_history block 的历史消息 message_id 列表。
       用于追查"模型到底看到了哪些历史"。"""

    memory_ids: list[str]
    """进入各 memory block 的记忆 memory_id 列表。"""

    turn_local_summary: dict
    """current_turn_local_state 的摘要。
       格式: {"delivered_count": 2, "failed_count": 0, "tool_results_count": 2, "rejected_count": 0} """

    terminal_policy: TerminalPolicy
    """本轮终态策略的简化记录。"""

    full_payload_ref: str | None
    """debug 模式下保存完整 provider_messages payload 的引用。
       正常模式下为 None。"""

    token_budget_report: TokenBudgetReport | None
    """token 预算报告。"""
```

### 2.7 TokenBudgetReport

```python
@dataclass
class TokenBudgetReport:
    """token 预算消耗报告。ContextBuilder 在构建结束时生成。"""

    budget: int
    """本轮分配的 token 预算（不含 response）。"""

    used: int
    """实际使用的 token 数量（估算）。"""

    remaining: int
    """剩余预算。used + remaining = budget。"""

    budget_exhausted: bool
    """是否预算耗尽（remaining <= 0）。"""

    truncation_applied: bool
    """是否执行了截断（实际 token > budget 时）。"""

    truncation_details: list[TruncationRecord]
    """截断详情。每条记录说明哪个 block 被截断了多少。"""

@dataclass
class TruncationRecord:
    """单条截断记录。"""

    block_id: str
    """被截断的 block 的 block_id。"""

    original_estimated_tokens: int
    """该 block 原始估算 token 数。"""

    after_truncation_tokens: int
    """截断后 token 数。"""

    strategy: str
    """截断策略。取值: head_trim | tail_only | exclude_block """

    detail: str
    """人类可读的截断描述。示例: "reduced recent history from 20 turns to 10 turns (-450 tokens)" """
```

### 2.8 SystemTimeBlock

```python
@dataclass
class SystemTimeBlock:
    """ContextBuilder 注入的系统时间 block。作为独立 system message 注入 provider_messages。"""

    system_time: SystemTime
    """当前系统时间。"""

    last_user_ts: float | None
    """最近一次用户消息的时间戳。无历史时为 None。"""

    last_assistant_ts: float | None
    """最近一次 assistant 可见回复的时间戳。无历史时为 None。"""

    gap_seconds: float | None
    """距离上次可见互动的秒数。无历史时为 None。"""

    resume_type: str
    """恢复类型。取值: cold_start | continuous_chat | short_resume |
               same_day_resume | next_day_resume | long_gap_resume """

    has_previous: bool
    """是否有历史互动记录。"""
```

### 2.9 SystemTime

```python
@dataclass
class SystemTime:
    """系统时间。"""

    dt: datetime
    """当前时间 datetime 对象。"""

    timezone: str
    """时区标识。示例: "Asia/Shanghai" """

    weekday_cn: str
    """中文星期。示例: "周三" """
```

### 2.10 MediaContextItem

```python
@dataclass
class MediaContextItem:
    """媒体上下文条目。用户发送的图片/语音摘要。"""

    item_id: str
    """关联的 InboundMessageItem.item_id。"""

    kind: str
    """媒体类型。取值: image | voice """

    summary: str | None
    """媒体摘要文本。图片 = vision 描述；语音 = STT 转写。"""

    metadata: dict
    """附加信息。语音含 tone / emotion_hint / duration；
       图片含 width / height / mime_type。"""
```

### 2.11 ContextBuildWarning

```python
@dataclass
class ContextBuildWarning:
    """ContextBuilder 构建过程中的非致命警告。"""

    source: str
    """警告来源。示例: "memory_load", "relationship_state_missing", "media_summary_unavailable" """

    detail: str
    """警告详情。英文描述，≤200 字符。"""

    severity: str
    """严重程度。取值: info | warning | error """

    block_affected: str | None
    """受影响的 block_id。整个构建受影响的全局警告为 None。"""
```

### 2.12 BuildContextError

```python
class BuildContextError(Exception):
    """ContextBuilder 无法完成构建时的异常。

    调用方（TurnEngine）应捕获并进入安全终态。
    不应被静默吞掉。
    """

    reason: str
    """失败原因。英文描述。"""

    missing_fields: list[str]
    """缺失的必填字段列表。"""

    def __init__(self, reason: str, missing_fields: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.missing_fields = missing_fields or []
```

---

## 3. 不可违反的不变量 (Non-negotiable Invariants)

### I-1: 唯一入口
**所有模型输入必须通过 ContextBuilder.build() 构造。** 禁止任何模块调用 `ProviderGateway.chat()` 时自行拼接 messages。

### I-2: 工具名一致性
**prompt contract 中描述的工具名集合必须等于 native_tool_schemas 中的工具名集合。** 不能出现 "prompt 说可用 get_time 但 schema 没暴露" 或反过来。

### I-3: 工具 executor 存在性
**native_tool_schemas 中的每个工具必须有对应的 executor 注册。** schema 中有但 executor 不存在的工具不能暴露给模型。

### I-4: 助理消息不重复
**同一条 assistant 消息绝对不能同时出现在 recent_conversation_history 和 current_turn_local_state 两个 block 中。** 在 turn 结束前，本轮已发送消息只能出现在 turn_local_state block。

### I-5: 未送达不写历史
**failed / unknown 送达状态的消息绝对不能作为 assistant conversation history。** recent_conversation_history block 中的每条 assistant 消息的 delivery_status 必须为 "delivered"。

### I-6: 工具结果不是用户消息
**tool_result 必须以 role=tool 的消息格式出现，绝对不能伪装成 role=user。** 禁止 tool result 和 user message 共享 role。

### I-7: 媒体摘要不是用户文本
**media_summary 必须出现在 media_context block 中（role=system），绝对不能出现在 role=user 的消息中。** 禁止把图片摘要写入 InboundMessageItem.text 或 user content。

### I-8: context_snapshot 不回灌
**context_snapshot 仅用于审计和调试，绝对不能作为下一轮模型调用或同一次模型调用的上下文。** 禁止 context_snapshot 中的任何内容回流入 provider_messages。

### I-9: 固定块左移，动态块右移
**固定内容（protocol / persona / 低频 memory / relationship_state）必须排列在左侧（前面），动态内容（recent_history / turn_local / current_input）必须排列在右侧（后面）。** 这是 provider prompt caching 正确命中的前提。

### I-10: 本轮触发源不能伪装成用户消息
**proactive / reminder / ritual / system_event 触发时，不能生成 role=user 的消息。** 非用户触发的 content 必须以 role=system 注入。trigger != "user" 时不得出现 role=user。

### I-11: 终态策略必须嵌入
**terminal_policy 必须在 provider_messages 中以独立的 system message 呈现。** 不能依赖模型从 tool contract 中推断终态规则。terminal_policy block 必须明确告知模型：什么算结束、允许什么终态工具、是否必须发送消息。

### I-12: 系统时间注入形式
**系统时间以独立的 system message 注入，不作为 tool call 结果，也不作为 user 或 assistant 消息。** SystemTimeBlock 永远是 role=system 的一条消息。除非处于 degraded mode，否则 get_time 工具默认不暴露。

### I-13: 敏感信息不入 snapshot
**API key、auth token、密码等凭证绝对不能出现在 context_snapshot 的任何字段中。** 包括 full_payload_ref 引用的全文中也不得包含。ContextBuilder 在生成 snapshot 前必须剔除敏感信息。

### I-14: 拒绝用户注入 system prompt
**用户输入（inbound_batch）的任何内容绝对不能拼接到 system role 消息中。** user text 只能进入 role=user 或 role=tool 的消息。protocol 块（role=system 的规则文本）必须来自固定的提示词文件，不来自用户输入。

### I-15: 每轮独立构建
**每次 model call 都必须重新调用 ContextBuilder.build() 生成全新的 BuiltContext。** 禁止缓存或复用上一轮或上一 call 的 provider_messages。每轮的 current_turn_local_state 不同，context 必须不同。

### I-16: block 类型必须准确
**每个 context block 的 kind 字段必须是固定枚举值之一，不得使用自定义字符串。** 枚举值：fixed_protocol | persona | relationship_state | memory | recent_history | turn_local | media | system_time | current_input | tool_contract | terminal_policy。使用自定义字符串的 block 在 snapshot 中将不可识别。

### I-17: forced_speak 阶段不暴露 skip
**如果 terminal_policy.forced_speak = True，tool_contract_block 中绝对不能出现 skip_initiative 工具。** 强制开口阶段的工具暴露必须排除 skip 选项。

### I-18: 多模态必须使用 ContentPart
**如果 media_context 有内容且 provider 支持多模态，图片/音频必须以 ContentPart 格式嵌入 user content 中，不是纯文本替换。** 对于不支持多模态的 provider，media_context 保留为独立 system block 的文本摘要。

---

## 4. 上下文打包顺序 (Fixed Order of Context Blocks)

provider_messages 的顺序是固定的。此顺序经设计确保 "固定内容在左，动态内容在右" 以最大化 prompt cache hit rate。

### 4.1 完整 15 部分顺序

| 序号 | block 名称 | kind | cache_class | role | 说明 |
|------|-----------|------|-------------|------|------|
| 1 | `fixed_system_protocol` | fixed_protocol | fixed | system | 系统身份声明 + 核心规则 |
| 2 | `fixed_output_protocol` | fixed_protocol | fixed | system | 输出格式规则 |
| 3 | `fixed_tool_protocol` | fixed_protocol | fixed | system | 工具调用规则 |
| 4 | `persona` | persona | fixed | system | 中文人设/性格/语气 |
| 5 | `relationship_state` | relationship_state | low_frequency | system | 当前关系状态。缺失时跳过，记录 warning |
| 6 | `system_time_block` | system_time | low_frequency | system | **当前时间 + resume_type。放在 memory 和 history 之前，因为时间影响对关系状态和历史间隔的理解** |
| 7 | `boundary_memory` | memory | low_frequency | system | 用户明确边界。永远在 profile/episode 之前 |
| 8 | `selected_profile_memory` | memory | low_frequency | system | 用户画像记忆 |
| 9 | `selected_episode_memory` | memory | low_frequency | system | 近期重要事件记忆 |
| 10 | `selected_open_threads` | memory | low_frequency | system | 未完成上下文线索 |
| 11 | `recent_conversation_history` | recent_history | turn_dynamic | user/assistant 交替 | 已完成 turn 的真实对话 |
| 12 | `current_turn_local_state` | turn_local | turn_dynamic | system | 本轮已发送消息 + 工具结果 + 送达状态 |
| 13 | `media_context` | media | turn_dynamic | system (仅文本 provider) 或 user (多模态 provider) | 用户发送的图片/语音摘要 |
| 14 | `current_input` | current_input | turn_dynamic | user (用户消息) 或 system (系统触发) | 本轮输入。trigger=user 时 role=user，否则 role=system |
| 15 | `exposed_tools_contract` | tool_contract | turn_dynamic | system | 本轮可用工具声明 + native schema 对应 |
| 16 | `terminal_policy` | terminal_policy | turn_dynamic | system | 终态规则：怎么结束、跳过规则、消息下限 |

### 4.2 顺序不可变的原因

1. **Prompt caching**: fixed / low_frequency 块在最前面，这些内容在多个 turn 间不变，provider 可以缓存。把 current_input 放在最后，保证每次 call 的缓存前缀都是相同的。
2. **注意力衰减**: 最近的动态内容（current_input / turn_local）放在 fixed block 之后 but 终端策略之前，确保模型在看完上下文后、做决策前看到 terminal_policy。
3. **记忆优先于历史**: relationship_state / boundary_memory / profile_memory 放在 recent_history 之前，确保模型在回忆对话前先建立"这个人是谁"的理解。
4. **当前输入 > 终端策略**: current_input 在 terminal_policy 之前，给模型完整的上下文再做终态决策。

### 4.3 block 缺失时的处理

| 缺失 block | 行为 |
|-----------|------|
| relationship_state | 跳过该 block，记录 `ContextBuildWarning(severity=warning, source="relationship_state_missing")` |
| boundary_memory | 跳过该 block，不记录 warning（无边界记忆是正常的） |
| selected_profile_memory | 跳过该 block，不记录 warning（新用户无画像记忆是正常的） |
| selected_episode_memory | 跳过该 block，不记录 warning |
| selected_open_threads | 跳过该 block，不记录 warning |
| recent_history | 跳过该 block，记录 `ContextBuildWarning(severity=info, source="empty_history")` |
| current_turn_local_state | 不能缺失——第一轮 model call 时也要传入空的 CurrentTurnLocalState |
| media_context | 跳过该 block，不记录 warning |
| system_time | 进入 degraded mode（见第 5 节），记录 `ContextBuildWarning(severity=warning, source="system_time_unavailable")` |
| current_input | 不能缺失——即使主动回合也必须提供 trigger 作为 system content |
| tool_exposure | 不能缺失——抛出 `BuildContextError("tool_exposure is required")` |
| terminal_policy | 不能缺失——抛出 `BuildContextError("terminal_policy is required")` |

---

## 5. SystemTimeBlock 规则

### 5.1 核心原则

系统时间**总是**由 ContextBuilder 以 `SystemTimeBlock`（独立的 `role=system` 消息）形式注入 provider_messages。`get_time` 工具**默认不暴露**给正常聊天。

### 5.2 SystemTimeBlock 格式

```
当前时间：2026年06月03日 14:30 周三
[如果有历史互动]
上次用户消息：06月03日 14:22
上次你的回复：06月03日 14:25
距离上次互动：约 5 分钟
resume_type: continuous_chat
使用规则：时间差只是事实，只有和用户当前消息有关时才自然提及，不要刻意表演"我一直在等你"。
[如果无历史互动]
这是首次对话，无历史互动记录。
resume_type: cold_start
```

### 5.3 get_time 工具暴露规则

| 条件 | get_time 是否暴露 |
|------|------------------|
| 正常用户聊天，system_time 正常注入 | **不暴露** |
| 用户明确询问其他时区的当前时间 | **暴露** |
| 用户要求时间换算/计算（如 "北京时间下午 3 点相当于纽约几点"） | **暴露** |
| system_time 不可用（degraded mode） | **暴露** |
| turn_type 为 user_reminder_due（需要绝对时间验证） | **暴露**（如果 tool_exposure 允许） |
| turn_type 为 relationship_proactive | **不暴露**（除非 degraded mode） |

### 5.4 SystemTimeBlock 中的 resume_type 判定

```python
# 判定逻辑（来自 temporal_context.py，保留）
if no previous interaction:
    resume_type = "cold_start"
elif gap < 600 seconds (10 min):
    resume_type = "continuous_chat"
elif gap < 2700 seconds (45 min):
    resume_type = "short_resume"
elif same calendar day:
    resume_type = "same_day_resume"
elif gap <= 604800 seconds (7 days):
    resume_type = "next_day_resume"
else:
    resume_type = "long_gap_resume"
```

### 5.5 Degraded Mode

当 `BuildContextRequest.system_time` 为 None 时：
1. SystemTimeBlock 仍然注入，但内容变为：`"系统时间不可用。如果需要时间信息，使用 get_time 工具获取。"`
2. 记录 `ContextBuildWarning(severity=warning, source="system_time_unavailable")`
3. 如果 tool_exposure 中有 get_time，则正常暴露（此条件应在 ToolExposurePolicy 中处理，ContextBuilder 只负责渲染 SystemTimeBlock 内容）

---

## 6. 上下文严格区分规则

### 6.1 recent_conversation_history

**内容范围：**
- 只包含已完成的 turn 的真实对话。
- 每条历史消息有 `ConversationHistoryMessage` 格式（含 message_id / turn_id / role / content / source / delivery_status / created_at）。
- 用户消息：`role=user`，`source=user_message`。
- Bot 已送达消息：`role=assistant`，`delivery_status=delivered`，`source=assistant_reply | assistant_proactive | reminder_reply`。

**禁止进入：**
- 本轮刚发送的消息（还在 turn 进行中）。
- delivery_status != "delivered" 的 assistant 消息。
- 工具结果（role=tool 不进入 history 流）。
- 系统提示、规则、时间块。
- 媒体摘要。
- debug 或诊断信息。

**分组方式：**
- 按时间顺序排列，user/assistant 交替。
- 可在相邻可见消息间插入系统时间分割线（role=system, _temporal_divider=True）。

**token 截断时：**
- 从最早的历史 turn 开始移除，保留最近的 turn。

### 6.2 current_turn_local_state

**内容范围（以 system role block 呈现）：**
- 本轮已发送消息的文本和送达状态。
- 本轮工具调用的结果。
- 本轮计数器和预算。
- 本轮已触发的 delivery receipt。

**格式示例：**
```
[本轮已发送消息]
1. [delivered] "今晚早点睡。"
2. [failed] "记得明天早上要去..." → 发送失败，用户未看到本消息

[本轮工具结果]
- get_weather("上海"): 上海 25°C, 晴

[本轮计数]
已发送: 1 条 (送达), 1 条 (失败)
剩余预算: 13 条消息 / 3 次 LLM 调用
```

**禁止进入：**
- 已完成的对话历史（应该在 recent_history 中）。
- 下一轮的信息。

**生命周期：**
- 本轮第一次 model call 时可能为近乎空（只有计数器和预算）。
- 随工具执行更新（通过 TurnEngine 更新 CurrentTurnLocalState 后重新调用 ContextBuilder.build()）。
- 本轮结束后销毁，不持久化入 recent_history（但其中的 delivered 消息会写入 recent_history 和持久存储）。

### 6.3 tool_result

**内容范围：**
- `role=tool` 的消息。
- 每条 tool result 必须有 `tool_call_id` 对应模型调用。
- 格式化为 model 可读的文本，例如 `{"status": "success", "result": {...}}`。

**格式化规则：**
```python
# send_text_message 的结果
{"status": "delivered", "delivered_text": "今晚早点睡。", "delivered_count_this_turn": 2, "remaining_message_budget": 13}

# get_time 的结果
{"timezone": "Asia/Shanghai", "current_time": "2026-06-03 14:30:00", "weekday": "周三"}

# get_weather 的结果
{"city": "上海", "temperature": 25, "condition": "晴", "humidity": 60}
```

**禁止行为：**
- 绝对不能以 `role=user` 出现。
- 绝对不能混入 recent_history。
- 工具返回中的文本绝对不能伪装成用户说了什么。

### 6.4 delivery_receipt

**内容范围：**
- 每条送达消息的状态：delivered / failed / unknown。

**对 history 的决定性影响：**
- `delivered` → 该消息可写入 assistant history。
- `failed` → 不可写入 history。进入 diagnostics。
- `unknown` → 不可写入 history。作为异常处理。

**在 ContextBuilder 中的使用：**
- turn_local block 展示所有消息和其 status。
- recent_history 只包含 delivered 消息。
- failed 消息在 turn_local 中明确标注 "发送失败，用户未看到"。

### 6.5 media_summary

**内容范围：**
- 用户发送的图片/语音的摘要文本。
- location: media_context block。

**内容位置：**
- 文本-only provider：以独立 `role=system` 消息注入，格式：
  ```
  [用户发送了一张图片]
  summary: 图片中是一只白猫坐在窗台上，窗外有阳光。
  ```
- 多模态 provider：图片/音频以 `ContentPart` 格式嵌入 `role=user` 消息中，同时 `media_context block` 在 snapshot 中保存摘要用于审计。

**禁止行为：**
- 绝对不能以 `role=user` 的纯文本出现（伪装成用户说的原文）。
- 绝对不能不做标注就混入 system prompt。

### 6.6 context_snapshot

**内容范围：**
- 所有 context block 的摘要。
- 暴露工具名列表。
- 历史消息 ID 列表。
- 记忆 ID 列表。
- turn_local 摘要（数字和状态，不含原文）。
- terminal_policy。
- token 预算报告。

**生命周期：**
- 在 ContextBuilder.build() 结束时生成。
- 保存到 ContextSnapshotStore。
- **绝对不进入下一轮或同一轮的任何 provider_messages。**

**隐私限制：**
- 不保存 API key / auth token。
- 默认不保存完整 provider_messages 全文。
- debug 模式下可短期保存全文引用，但仍不保存 API key。

---

## 7. 决策表 (Decision Tables)

### 7.1 消息进入 recent_history vs turn_local vs 不进入

| 消息来源 | delivery_status | 进入 recent_history | 进入 turn_local | 不进入任何 | 说明 |
|---------|----------------|---------------------|-----------------|-----------|------|
| 已完成的用户消息 | N/A（user 角色） | `role=user` | 否 | 否 | 已在历史中 |
| 本轮发送 → delivered | delivered | 否（turn 结束后写入） | `delivered` 标注 | 否 | turn 期间只在 turn_local |
| 本轮发送 → failed | failed | 否 | `failed` 标注 | 否 | 用户未看到，不写历史 |
| 本轮发送 → unknown | unknown | 否 | `unknown` 标注 | 否 | 不视为 delivered |
| 已被 guard 拦截 | BLOCKED_BY_GUARD | 否 | `blocked` 标注 | 否 | 未被发送 |
| 工具执行结果 | N/A | 否 | `tool_result` 标注 | 否 | role=tool, 在 tool_result 区 |
| 系统提示词 | N/A | 否 | 否（不在 turn_local 中） | 否（在 system block 中） | role=system |
| 媒体摘要 | N/A | 否 | 否 | 否 | 在 media_context block 中 |

### 7.2 Token 预算超限时的截断顺序

当实际 token 估算超过 `BuildContextRequest.token_budget` 时，按以下优先级从低到高截断（即越靠后的步骤越先用）：

| 步骤 | 对象 | 策略 | 减少量估算 |
|------|------|------|-----------|
| 1 | selected_episode_memory | 移除优先级最低的 episode，保留最重要的 1 条 | ~50-100 tokens |
| 2 | selected_open_threads | 移除优先级最低的 thread，保留最重要的 1 条 | ~50-100 tokens |
| 3 | selected_profile_memory | 移除优先级最低的 profile，保留最重要的 2 条 | ~50-150 tokens |
| 4 | recent_conversation_history | 从最早的 turn 开始移除，每次移除 1 个 turn（user + assistant 一对） | ~50-200 tokens/turn |
| 5 | recent_conversation_history | 若所有历史 turn 移除后仍超限，将每个历史消息截断到 300 字符 | ~视消息长度而定 |
| 6 | recent_conversation_history | 若仍超限，只保留最后 3 个 turn | ~视历史长度而定 |
| 7 | media_context | 缩短摘要文本（≤200 字符 → ≤50 字符） | ~50-150 tokens |
| 8 | 全局 | 标记 budget_exhausted=True | — |

**不可截断的内容：**
1. fixed_system_protocol / fixed_output_protocol / fixed_tool_protocol
2. persona
3. boundary_memory
4. relationship_state
5. system_time_block
6. current_input
7. terminal_policy
8. exposed_tools_contract

如果上述不可截断内容的总 token 已经超过预算，不执行截断——标记 `budget_exhausted=True` 并在 `token_budget_report` 中记录 "fixed_blocks_too_large" 诊断信息。TurnEngine 应对此情况做特殊处理（不调用模型，安全结束）。

### 7.3 记忆块缺失/加载失败时的处理

| 情况 | 行为 |
|------|------|
| boundary_memory 为空 | 跳过该 block，不记录 warning |
| profile_memory 为空 | 跳过该 block，不记录 warning |
| episode_memory 为空 | 跳过该 block，不记录 warning |
| open_threads 为空 | 跳过该 block，不记录 warning |
| 记忆加载抛出异常 | 跳过所有 memory block，记录 `ContextBuildWarning(severity=error, source="memory_load_failed")`，继续构建 |
| MemorySelection 为 None | 同所有 memory block 为空，不记录 warning |

### 7.4 根据 turn_type 的格式差异

| turn_type | trigger 来源 | current_input 的 role | SystemTimeBlock | 额外 block | 记忆选择 |
|-----------|-------------|----------------------|-----------------|-----------|---------|
| user_chat | 无 trigger | `user` | 正常注入 | 无 | 正常 |
| user_media_chat | 无 trigger | `user` (multimodal 含 ContentPart) | 正常注入 | media_context (位于 current_input 之前) | 正常 |
| user_reminder_request | 无 trigger | `user` | 正常注入 | 无 | 正常 |
| relationship_proactive | RuntimeTrigger | `system` | 正常注入 | initiative_state 内容融入 system block | sent_content_memory 优先 |
| user_reminder_due | RuntimeTrigger | `system` | 正常注入（不暴露 get_time；system_time 不可用时 safe terminal，不靠模型查时间） | reminder_state 内容融入 system block | 提醒相关记忆优先 |
| scheduled_ritual | RuntimeTrigger | `system` | 正常注入 | 无 | sent_content_memory 优先 |
| admin_manual_trigger | RuntimeTrigger | `system` | 正常注入 | 无 | 按 admin 配置 |
| memory_management | 无 trigger 或 RuntimeTrigger | `user` 或 `system` | 正常注入 | forget_directive 融入 system block | boundary_memory 优先 |

### 7.5 get_time 暴露 vs SystemTimeBlock 注入

| 条件 | SystemTimeBlock 注入 | get_time 工具暴露 |
|------|---------------------|------------------|
| system_time 可用 + 正常聊天 | 是 | **否** |
| system_time 可用 + 用户明确问其他时区 | 是 | **是** |
| system_time 可用 + 用户要求时间计算 | 是 | **是** |
| system_time 不可用 | 是（degraded message） | **是** |
| system_time 可用 + reminder_due turn | 是 | 是（由 ToolExposurePolicy 决定） |
| system_time 可用 + proactive turn | 是 | **否** |

注意：get_time 是否暴露是 ToolExposurePolicy 的决策。ContextBuilder 负责根据 system_time 是否可用来决定 SystemTimeBlock 的内容（正常 vs degraded），以及根据 tool_exposure 中是否有 get_time 来决定 tool_contract_block 中是否描述 get_time。

---

## 8. 伪代码 (Pseudocode)

### 8.1 ContextBuilder.build() 主流程

```python
class ContextBuilder:
    def __init__(self, persona_file_path: str, protocol_dir: str):
        """初始化 ContextBuilder.
        
        加载固定提示词文件（只在启动时加载一次，不在每次 build 时读文件）。
        """
        self._fixed_system_protocol = self._load_protocol("base_protocol")
        self._fixed_output_protocol = self._load_protocol("output_protocol")
        self._fixed_tool_protocol = self._load_protocol("tool_policy")

    def build(self, request: BuildContextRequest) -> BuiltContext:
        """主构建方法。"""
        self._validate_request(request)  # 抛出 BuildContextError 如果必填缺失
        blocks: list[ContextBlock] = []
        messages: list[ProviderMessage] = []
        warnings: list[ContextBuildWarning] = []

        # ── Phase 1: Fixed blocks ──────────────────────────────
        # 这些 block 构成 cache-able prefix

        # 1. fixed_system_protocol
        msg, blk = self._make_fixed_system_block(request, self._fixed_system_protocol)
        messages.append(msg); blocks.append(blk)

        # 2. fixed_output_protocol
        msg, blk = self._make_fixed_system_block(request, self._fixed_output_protocol, label="output")
        messages.append(msg); blocks.append(blk)

        # 3. fixed_tool_protocol
        msg, blk = self._make_fixed_system_block(request, self._fixed_tool_protocol, label="tool")
        messages.append(msg); blocks.append(blk)

        # 4. persona
        msg, blk = self._make_persona_block(request)
        messages.append(msg); blocks.append(blk)

        # 5. relationship_state (optional — skip if None)
        if request.relationship_state is not None:
            msg, blk = self._make_relationship_state_block(request)
            messages.append(msg); blocks.append(blk)
        else:
            warnings.append(ContextBuildWarning(
                source="relationship_state_missing",
                detail="relationship_state is None",
                severity="warning",
                block_affected="relationship_state",
            ))

        # 6. system_time_block（紧接 relationship_state 后，memory/history 前）
        # 时间上下文影响对关系状态和历史间隔的理解
        msg, blk = self._make_system_time_block(request, warnings)
        messages.append(msg)
        blocks.append(blk)

        # ── Phase 2: Memory blocks ─────────────────────────────
        mem_blocks = self._make_all_memory_blocks(request, warnings)
        for msg, blk in mem_blocks:
            messages.append(msg)
            blocks.append(blk)

        # ── Phase 3: Dynamic blocks ────────────────────────────
        # 这些 block 构成 turn-dynamic suffix

        # 11. recent_conversation_history
        msg, blk = self._make_recent_history_block(request)
        if msg is not None:
            messages.append(msg)
            blocks.append(blk)
        else:
            warnings.append(ContextBuildWarning(
                source="empty_history",
                detail="no recent conversation history available",
                severity="info",
                block_affected="recent_conversation_history",
            ))

        # 12. current_turn_local_state
        msg, blk = self._make_turn_local_block(request)
        messages.append(msg)
        blocks.append(blk)

        # 13. media_context (optional)
        if request.media_context:
            msg, blk = self._make_media_context_block(request)
            messages.append(msg)
            blocks.append(blk)

        # 14. current_input
        msg, blk = self._make_current_input_block(request)
        messages.append(msg)
        blocks.append(blk)

        # ── Phase 4: Tool contract and terminal policy ─────────
        # 15. exposed_tools_contract
        msg, blk = self._make_tool_contract_block(request)
        messages.append(msg)
        blocks.append(blk)

        # 16. terminal_policy
        msg, blk = self._make_terminal_policy_block(request)
        messages.append(msg)
        blocks.append(blk)

        # ── Phase 5: Token budget enforcement ──────────────────
        budget_report = self._check_token_budget(
            messages, blocks, request.token_budget, warnings
        )
        if budget_report.truncation_applied:
            messages, blocks = self._apply_truncation(
                messages, blocks, budget_report, request.token_budget
            )

        # ── Phase 6: Validation ────────────────────────────────
        self._validate_tool_consistency(request.tool_exposure, warnings)

        # ── Phase 7: Snapshot ──────────────────────────────────
        snapshot = self._build_snapshot(request, blocks)

        # ── Phase 8: Anti-pollution check ──────────────────────
        self._verify_no_duplicate_assistant(blocks, warnings)
        self._verify_tool_results_not_user_role(messages, warnings)
        self._verify_media_not_user_text(blocks, warnings)
        self._verify_snapshot_isolation(snapshot, messages, warnings)

        return BuiltContext(
            turn_id=request.turn_id,
            call_id=request.call_id,
            provider_messages=messages,
            exposed_tool_schemas=request.tool_exposure.native_tool_schemas,
            tool_contract_block=self._find_tool_contract_msg(messages),
            context_blocks=blocks,
            context_snapshot=snapshot,
            token_budget_report=budget_report,
            terminal_policy=request.terminal_policy,
            warnings=warnings,
        )
```

### 8.2 关键子方法签名

```python
    def _validate_request(self, request: BuildContextRequest) -> None:
        """检查必填字段。缺失 → BuildContextError。"""
        required = ["turn_id", "call_id", "turn_type", "user_id", "bot_id",
                     "current_turn_local_state", "tool_exposure", "terminal_policy",
                     "memory_selection", "recent_history", "persona",
                     "fixed_system_protocol", "fixed_output_protocol", "fixed_tool_protocol"]
        missing = [f for f in required if getattr(request, f, None) is None]
        if missing:
            raise BuildContextError(f"missing required fields: {missing}", missing)

    def _make_fixed_system_block(self, request, content, label="system") -> tuple:
        """创建固定的 system role block。cache_class=fixed。"""

    def _make_persona_block(self, request) -> tuple:
        """创建 persona block。cache_class=fixed。"""

    def _make_relationship_state_block(self, request) -> tuple:
        """从 relationship_state 创建 block。cache_class=low_frequency。"""

    def _make_all_memory_blocks(self, request, warnings) -> list:
        """按顺序创建 boundary / profile / episode / open_threads block。
        cache_class=low_frequency。缺失时静默跳过。"""

    def _make_recent_history_block(self, request) -> tuple:
        """创建 recent_conversation_history block。
        过滤：只有 role=user/assistant + delivery_status=delivered 的消息。
        插入时间分割线。
        主动 turn 时读取 initiative_state.sent_content_memory 排除重复。
        cache_class=turn_dynamic。"""

    def _make_turn_local_block(self, request) -> tuple:
        """创建 current_turn_local_state block。
        展示本轮已发送消息 + 工具结果 + 计数器。
        标明每条消息的 delivery status。
        cache_class=turn_dynamic。"""

    def _make_media_context_block(self, request) -> tuple:
        """创建 media_context block。
        纯文本 provider：role=system，文本格式。
        多模态 provider：嵌入 content part 或单独 system block（取决于 provider 能力）。
        cache_class=turn_dynamic。"""

    def _make_system_time_block(self, request, warnings) -> tuple:
        """创建 SystemTimeBlock。
        system_time 可用 = 正常格式。
        system_time None = degraded mode。
        cache_class=turn_dynamic。"""

    def _make_current_input_block(self, request) -> tuple:
        """创建 current_input block。
        trigger=user → role=user。
        其他 → role=system。
        cache_class=turn_dynamic。"""

    def _make_tool_contract_block(self, request) -> tuple:
        """创建 exposed_tools_contract block。
        用 tool_exposure.tool_contract_block 的内容。
        必须与 native_tool_schemas 的 name 完全一致。
        cache_class=turn_dynamic。"""

    def _make_terminal_policy_block(self, request) -> tuple:
        """创建 terminal_policy block。
        内容由 terminal_policy 决定：
        - 终态工具名和参数
        - skip 是否允许
        - forced_speak 是否激活
        - 最少/最多消息数
        - 是否需要 next_wake
        cache_class=turn_dynamic。"""

    def _check_token_budget(self, messages, blocks, budget, warnings) -> TokenBudgetReport:
        """估算当前 token 消耗，判断是否需要截断。"""

    def _apply_truncation(self, messages, blocks, budget_report, budget) -> tuple:
        """按决策表 7.2 的优先级执行截断，返回 (削减后的 messages, 削减后的 blocks)。"""

    def _verify_no_duplicate_assistant(self, blocks, warnings) -> None:
        """验证 I-4: 无 assistant 消息在 recent_history 和 turn_local 中重复。"""

    def _verify_tool_results_not_user_role(self, messages, warnings) -> None:
        """验证 I-6: 无 tool result 以 role=user 出现。"""

    def _verify_media_not_user_text(self, blocks, warnings) -> None:
        """验证 I-7: 无 media_summary 在 user content 中。"""

    def _verify_snapshot_isolation(self, snapshot, messages, warnings) -> None:
        """验证 I-8: snapshot 内容未被注入 messages。"""

    def _validate_tool_consistency(self, tool_exposure, warnings) -> None:
        """验证 I-2: prompt contract 工具名 == native schema 工具名。
        不一致 → ContextBuildWarning.severity=error。"""
```

---

## 9. 不同条件下的反应

### 9.1 正常 user_chat turn

```yaml
输入条件:
  turn_type: user_chat
  trigger: null
  inbound_batch: { items: [{kind: text, text: "今天好累啊"}] }
  recent_history: [最近 10 轮对话]
  system_time: 可用

ContextBuilder 行为:
  - current_input: role=user, content="今天好累啊"
  - SystemTimeBlock: 正常格式（当前时间 + resume_type + gap）
  - memory_blocks: 按优先级展示 profile / episode / open_threads / boundary
  - recent_history: 最近 turns 的 user/assistant 交替 + 时间分割线
  - turn_local: 最小化（model_call_count=0, 无已发送消息）
  - tool_contract: send_text_message, finish_turn, (get_weather if in tool_exposure)
  - terminal_policy: allows_skip=false, min_delivered_messages=0, requires_next_wake=false
  - media_context: 无
```

### 9.2 主动 turn, 可跳过阶段

```yaml
输入条件:
  turn_type: relationship_proactive
  trigger: {trigger_type: heartbeat}
  initiative_state: {consecutive_skip_count: 0}
  terminal_policy: {allows_skip: true, forced_speak: false, requires_next_wake: true, min_delivered_messages: 0}
  current_input: 无 (inbound_batch=null)

ContextBuilder 行为:
  - current_input: role=system, content="这是一个主动回合。用户暂无新消息。[状态快照] ..."
  - tool_contract: send_text_message, skip_initiative, finish_turn
  - terminal_policy: 明确描述 skip 规则和下次唤醒时间范围
  - SystemTimeBlock: 正常格式
  - memory_blocks: sent_content_memory 优先展示（防重复）
  - turn_local: 最小化
```

### 9.3 主动 turn, 强制开口阶段

```yaml
输入条件:
  turn_type: relationship_proactive
  initiative_state: {consecutive_skip_count: 1, last_skipped_at: timestamp}
  terminal_policy: {allows_skip: false, forced_speak: true, requires_next_wake: true, min_delivered_messages: 1}

ContextBuilder 行为:
  - current_input: role=system, content="主动回合。上一次主动机会选择了跳过，本轮必须发送至少一条消息。[状态快照] ..."
  - tool_contract: send_text_message, finish_turn
  - terminal_policy: 明确描述"必须发消息，不能跳过"，不暴露 skip_initiative
```

### 9.4 提醒到期 turn

```yaml
输入条件:
  turn_type: user_reminder_due
  trigger: {trigger_type: due_reminder, source_task_id: "rem_001"}
  reminder_state: {reminder_text: "喝水", due_at: timestamp}

ContextBuilder 行为:
  - current_input: role=system, content="提醒到期：[喝水]，用户设定的提醒已到时间。"
  - tool_contract: send_text_message, mark_reminder_delivered, finish_turn (不暴露 skip_initiative)
  - terminal_policy: 必须发送提醒消息
  - SystemTimeBlock: 正常格式，get_time 可能也暴露（如 tool_exposure 允许）
  - turn_local: 最小化
```

### 9.5 同一轮内第二次 model call（多消息 turn）

```yaml
输入条件:
  turn_type: user_chat
  current_turn_local_state:
    model_call_count: 1
    delivered_message_count: 1
    sent_messages_this_turn:
      - msg_id: "msg_1"
        text: "辛苦了宝宝，今天怎么这么累？"
        status: delivered
    tool_results_this_turn: []
  recent_history: [与上一 call 相同]
  current_input: 略（同一轮不再有用户输入）

ContextBuilder 行为:
  - turn_local block 展示: "[delivered] 辛苦了宝宝，今天怎么这么累？" + 计数器
  - recent_history: 不变 → "msg_1" 不在其中
  - current_input: 用上次相同的 input（或 null，取决于 TurnEngine 策略）→ role=system, content="[继续本轮，无需额外输入]"
  - tool_contract: 更新（已发消息后可用工具可能变化）
  - 验证：I-4 通过 — "辛苦了宝宝..." 只出现在 turn_local
```

### 9.6 Token 预算接近上限

```yaml
输入条件:
  token_budget: 8000
  estimated_usage: 7800  (budget - usage < 500)

ContextBuilder 行为:
  - 不执行截断（还没超限）
  - token_budget_report: {budget_exhausted: false, remaining: 200, truncation_applied: false}
  - 无需截断详情
  - 记录 ContextBuildWarning(severity=info, source="near_budget_limit",
      detail="only 200 tokens remaining in budget")
```

### 9.7 关系状态缺失

```yaml
输入条件:
  relationship_state: None

ContextBuilder 行为:
  - 跳过 relationship_state block
  - 记录 ContextBuildWarning(severity=warning, source="relationship_state_missing")
  - 其余 block 顺序不变（boundary_memory 在 persona 之后仍然是第 6 位）
  - 不影响其余构建流程
```

### 9.8 空历史（冷启动）

```yaml
输入条件:
  recent_history.messages: []
  inbound_batch.items: [{kind: text, text: "你好"}]

ContextBuilder 行为:
  - recent_history block: 跳过，记录 ContextBuildWarning(severity=info, source="empty_history")
  - SystemTimeBlock: resume_type=cold_start, "这是首次对话，无历史互动记录。"
  - memory_blocks: 全部为空（新用户无记忆）
  - 其余 block 正常组装
```

### 9.9 用户发送图片

```yaml
输入条件:
  turn_type: user_media_chat
  inbound_batch.items: [{kind: image, media: {summary: "一只白猫在窗台上"}}, {kind: text, text: "看看我的猫"}]

ContextBuilder 行为:
  - media_context block: role=system, content="[用户发送了一张图片] summary: 一只白猫在窗台上"
  - current_input: role=user, content="看看我的猫"
  - 图片原始文件在 provider_messages 中以 ContentPart 嵌入（如果 provider 支持多模态），或在 snapshot 中记录
  - 验证 I-7: media summary 不在 user content 中
```

### 9.10 Debug mode

```yaml
输入条件:
  debug_mode: true

ContextBuilder 行为:
  - 所有 block 行为不变
  - context_snapshot.full_payload_ref = "snapshot:snap_xxx:full_payload"
  - 将完整的 provider_messages 序列化并保存到 context_snapshot 引用的位置
  - 排除所有 API key（如果 provider_messages 中包含任何 auth header 或 key，必须在保存前剔除）
  - token_budget_report 额外包含 per-block token 估算
```

---

## 10. 失败路径

### 10.1 Token 预算完全耗尽

```yaml
条件:
  token_budget: 8000
  不可截断内容的 token > 8000

行为:
  - 不执行截断（截断后仍超限）
  - token_budget_report:
      budget_exhausted: true
      used: 估算值
      remaining: 负数
      truncation_applied: false
      truncation_details: []
  - 记录 ContextBuildWarning(severity=error, source="token_budget_exhausted",
      detail="fixed blocks alone exceed budget of 8000 tokens")
  - 仍然返回 BuiltContext（不抛异常）
  - provider_messages 中包含所有 block（即使超限）
  - TurnEngine 对 budget_exhausted=True 时的处理：不调用模型，进入安全终态
```

### 10.2 记忆加载失败

```yaml
条件:
  MemorySystem.load_for_turn() 抛出异常

行为:
  - MemorySelection 为 MemorySelection.empty()
  - 所有 memory block 跳过
  - 记录 ContextBuildWarning(severity=error, source="memory_load_failed",
      detail="exception: {error_message}")
  - 继续构建，不抛异常
```

### 10.3 关系状态缺失

```yaml
条件:
  relationship_state: None

行为:
  - 跳过 relationship_state block
  - 记录 ContextBuildWarning(severity=warning, source="relationship_state_missing")
  - 继续构建
```

### 10.4 媒体摘要不可用

```yaml
条件:
  media_context 中有 item 但 summary 为空

行为:
  - media_context block 的内容仅含媒体类型标注，不含摘要
  - 内容示例: "[用户发送了一张图片] (summary unavailable)"
  - 记录 ContextBuildWarning(severity=warning, source="media_summary_unavailable",
      detail=f"missing summary for media item {item_id}")
  - 继续构建
```

### 10.5 系统时间不可用（degraded mode）

```yaml
条件:
  BuildContextRequest.system_time: None

行为:
  - SystemTimeBlock 注入 degraded message:
    "系统时间不可用。如果需要时间信息，使用 get_time 工具获取。"
  - resume_type: "unknown"
  - has_previous: 根据 history 是否有记录判断
  - 记录 ContextBuildWarning(severity=warning, source="system_time_unavailable")
  - 继续构建
```

### 10.6 BuildContextRequest 缺失必填字段

```yaml
条件:
  任何必填字段为 None 或缺失

行为:
  - 抛出 BuildContextError(reason="missing required fields: ['field1', 'field2']",
      missing_fields=['field1', 'field2'])
  - TurnEngine 捕获后进入安全终态
  - 不返回 BuiltContext
  - 不调用模型
```

---

## 11. 禁止行为 (Forbidden Behaviors)

### 11.1 绕过 ContextBuilder 组装模型输入
禁止任何模块（包括 TurnEngine、ProviderGateway、main.py、ChatAgent）绕过 ContextBuilder.build() 自行拼接 provider_messages。所有模型输入必须经过 ContextBuilder。

### 11.2 将 turn-local 消息放入 recent_history
turn 结束前，本轮刚发送的消息（自 turn 开始后的所有 assistant 消息）绝对不能出现在 recent_conversation_history block 中。只能出现在 current_turn_local_state block。

### 11.3 将 failed/unknown delivery 当作 assistant history
近期的 assistant 文本如果 delivery_status != "delivered"，绝对不能出现在 recent_conversation_history 的 assistant role 中。不能伪造送达以让模型以为用户看到了。

### 11.4 工具结果伪装成用户消息
tool result 绝对不能以 role=user 出现。tool_execution_result 必须以 role=tool 出现，含对应的 tool_call_id。禁止将工具返回文本混入 user content。

### 11.5 媒体摘要伪装成用户文本
图片/语音的摘要绝对不能写入 role=user 的 content 中。必须在 media_context block（role=system）中明确标注。

### 11.6 将 context_snapshot 回灌给模型
context_snapshot 是审计 artifact，绝对不能：
- 作为 system message 注入 provider_messages
- 作为 debug 文本追加到 context
- 在下一轮 model call 中被读取和使用

### 11.7 工具名不一致
提示词中说可用某工具，但 native schema 中没暴露（或反过来）。必须是同一份工具列表驱动两处。

### 11.8 原始媒体进入全文 snapshot
在 debug 模式下保存完整 provider_messages 时，禁止保存原始图片 base64 数据或原始音频文件。snapshot 中只保存媒体引用和摘要。

### 11.9 API key 或 token 进入 snapshot
API key、auth token、签名密钥等绝对不能出现在 context_snapshot 的任何字段中。保存 full_payload_ref 时也必须剔除。

### 11.10 复用上一轮的 context
每轮 model call 都必须调用 ContextBuilder.build() 生成新的 BuiltContext。禁止缓存上一轮的结果、复用上一轮的 provider_messages、或增量修改上一轮 context。

### 11.11 系统触发伪装成用户消息
proactive / reminder / scheduled_ritual / admin 触发时，current_input block 的 role 必须是 system，不是 user。禁止产生任何 role=user 的虚假消息。

### 11.12 forced_speak 阶段暴露 skip
terminal_policy.forced_speak=True 时，tool_contract_block 中绝对不能列出 skip_initiative 工具。

### 11.13 同一 turn 内重复追加固定 block
同一 turn 的多次 model call（build() 被多次调用），每次 build() 都必须包含相同的固定 block（fixed_protocol / persona 等）。但 provider_messages 必须每次都从头构建，不能沿用旧列表 append。

---

## 12. 具体输入输出例子

### 例 1: 简单文本聊天（会话首条消息）

**输入 - BuildContextRequest:**
```yaml
turn_id: "turn_n1_1717400000"
call_id: "call_n1_0"
turn_type: user_chat
user_id: "wxid_abc123"
bot_id: "default_bot"
inbound_batch:
  items:
    - item_id: "item_001"
      kind: text
      text: "你好，最近怎么样"
current_turn_local_state:
  model_call_count: 0
  delivered_message_count: 0
  sent_messages_this_turn: []
  tool_results_this_turn: []
tool_exposure:
  exposed_tool_names: ["send_text_message", "finish_turn"]
  native_tool_schemas: [send_text_message_schema, finish_turn_schema]
terminal_policy:
  requires_next_wake: false
  min_delivered_messages: 0
  allows_skip: false
memory_selection:
  profile_memory: []
  boundary_memory: []
  episode_memory: []
  open_thread_memory: []
  sent_content_memory: []
recent_history:
  messages: []
relationship_state: null
persona: "你是一个温柔的女友，说话方式亲和自然..."
system_time: {dt: 2026-06-03T14:30:00+08:00, timezone: "Asia/Shanghai", weekday_cn: "周三"}
media_context: []
token_budget: 8000
```

**输出 - BuiltContext 关键结构:**
```yaml
provider_messages:
  # 1. fixed_system_protocol
  - role: system
    content: "You are a long-term romantic companion..."
  # 2. fixed_output_protocol
  - role: system
    content: "All assistant content must be delivered via native tool calls..."
  # 3. fixed_tool_protocol
  - role: system
    content: "Only call tools specified in the exposed tool list..."
  # 4. persona
  - role: system
    content: "你是一个温柔的女友，说话方式亲和自然..."
  # 5. relationship_state → 跳过 (null)
  # 6. system_time_block
  - role: system
    content: |
      当前时间：2026年06月03日 14:30 周三
      这是首次对话，无历史互动记录。
      resume_type: cold_start
  # 7-10. memory blocks → 跳过 (空)
  # 11. recent_history → 跳过 (空)
  # 12. current_turn_local_state
  - role: system
    content: "[本轮状态] 首次模型调用。暂无已发送消息。"
  # 13. media_context → 跳过 (空)
  # 14. current_input
  - role: user
    content: "你好，最近怎么样"
  # 15. exposed_tools_contract
  - role: system
    content: |
      本轮可用工具：send_text_message, finish_turn
      send_text_message(text): 发送一条文字消息给用户
      finish_turn(): 结束本轮对话
  # 16. terminal_policy
  - role: system
    content: |
      [终态规则]
      可以发送 0-N 条消息
      正常结束: 调用 finish_turn()
      不允许跳过

context_snapshot:
  block_summaries: 10 个 block (2 memory + 1 history 为空 block 仍记录)
  exposed_tool_names: ["send_text_message", "finish_turn"]
  history_message_ids: []
  memory_ids: []
  turn_local_summary: {delivered_count: 0, failed_count: 0}
  full_payload_ref: null

token_budget_report:
  used: 约420
  remaining: 7580
  budget_exhausted: false
  truncation_applied: false
```

### 例 2: 有最近历史记录的聊天

**关键输入差异：**
```yaml
recent_history:
  messages:
    - message_id: "m1"
      turn_id: "turn_p1"
      role: user
      content: "今天天气不错"
      delivery_status: delivered
      created_at: 2026-06-03T10:00:00
    - message_id: "m2"
      turn_id: "turn_p1"
      role: assistant
      content: "对呀，阳光很好～要不要出去走走？"
      source: assistant_reply
      delivery_status: delivered
      created_at: 2026-06-03T10:00:05
    - message_id: "m3"
      turn_id: "turn_p2"
      role: user
      content: "好啊，下午去公园吧"
      delivery_status: delivered
      created_at: 2026-06-03T10:05:00
    - message_id: "m4"
      turn_id: "turn_p2"
      role: assistant
      content: "好～那两点出发？"
      source: assistant_reply
      delivery_status: delivered
      created_at: 2026-06-03T10:05:05
recent_history:
  window_policy: count
```

**输出差异：**
```yaml
provider_messages:
  # ... 固定 block 不变 ...
  # 6. system_time_block（在 memory/history 之前）
  - role: system
    content: |
      当前时间：2026年06月03日 14:30 周三
      上次用户消息：06月03日 10:05
      上次你的回复：06月03日 10:05
      距离上次互动：约 4 小时 25 分钟
      resume_type: same_day_resume
  # 7-10. memory blocks ...
  # 11. recent_conversation_history block
  # 渲染为 user/assistant 交替序列
  - role: user
    content: "今天天气不错"
  - role: assistant
    content: "对呀，阳光很好～要不要出去走走？"
  - role: user
    content: "好啊，下午去公园吧"
  - role: assistant
    content: "好～那两点出发？"
  # 12. turn_local block
  - role: system
    content: "[本轮状态] 首次模型调用。暂无已发送消息。"
      使用规则：时间差只是事实，只有和用户当前消息有关时才自然提及
```

### 例 3: 同一轮内第二次模型调用（多消息 turn）

**关键输入差异：**
```yaml
call_id: "call_n1_1"  # 第二次调用
current_turn_local_state:
  model_call_count: 1
  delivered_message_count: 1
  sent_messages_this_turn:
    - message_id: "tl_msg_1"
      tool_call_id: "tc_n1_0_1"
      text: "我挺好的～想你了呢。你呢，有什么新鲜事吗？"
      status: delivered
      delivered_at: 2026-06-03T14:30:02
  tool_results_this_turn: []
  budget_state: {remaining_messages: 14, remaining_calls: 3}
recent_history: [与例 2 相同]
inbound_batch: null  # 第二轮没有新用户输入
```

**输出差异：**
```yaml
provider_messages:
  # ... 固定 block 不变 ...
  # 10. recent_history → 不变，与 call_n1_0 相同
  # 11. current_turn_local_state → 更新
  - role: system
    content: |
      [本轮状态]
      已发送消息：
      1. [delivered] "我挺好的～想你了呢。你呢，有什么新鲜事吗？"

      计数：
      - 模型调用次数: 1
      - 已发送消息: 1 (送达)
      - 剩余预算: 14 条消息 / 3 次模型调用
  # ... 其他不变 ...
  # 14. current_input
  - role: system
    content: "[继续本轮对话]"

context_snapshot:
  turn_local_summary: {delivered_count: 1, failed_count: 0, tool_results_count: 0, rejected_count: 0}
  history_message_ids: ["m1", "m2", "m3", "m4"]

# 关键验证: "我挺好的～想你了呢" 不出现在 recent_history 中
# 它只在 turn_local block 中出现
```

### 例 4: 主动 turn, 可跳过阶段

```yaml
BuildContextRequest:
  turn_type: relationship_proactive
  inbound_batch: null
  trigger: {trigger_type: heartbeat, reason: "cold_start_check"}
  initiative_state:
    consecutive_skip_count: 0
    last_spoke_at: 2026-06-03T08:00:00
  terminal_policy:
    requires_next_wake: true
    min_delivered_messages: 0
    max_delivered_messages: 3
    allows_skip: true
    forced_speak: false
    required_terminal_tools: ["finish_turn"]
  tool_exposure:
    exposed_tool_names: ["send_text_message", "skip_initiative", "finish_turn"]
  memory_selection:
    sent_content_memory:
      - text: "好～那两点出发？"
        sent_at: 2026-06-03T10:05:00

BuiltContext 关键输出:
  provider_messages:
    # 14. current_input
    - role: system
      content: |
        现在是 2026年06月03日 14:30。这是一个主动回合。
        用户暂无新消息。用户最后一条消息是 "好啊，下午去公园吧"（约4小时前）。
        你之后回复了 "好～那两点出发？"，用户还未回应。

        [最近已发送内容 - 避免重复]
        - "好～那两点出发？"

    # 15. exposed_tools_contract
    - role: system
      content: |
        可用工具: send_text_message, skip_initiative, finish_turn

        send_text_message(text): 发送文字消息
        skip_initiative(reason): 跳过本次主动机会（需说明原因）
        finish_turn(next_wake_minutes): 结束本轮，设置下次唤醒时间(30-480分)

    # 16. terminal_policy
    - role: system
      content: |
        [终态规则]
        阶段: 可跳过
        必须调用 finish_turn(next_wake_minutes) 结束
        next_wake_minutes 必须在 30-480 之间
        可以选择 silent: 第一次调用 skip_initiative → 第二次调用 finish_turn（必须两次独立模型调用，不可同一次 tool_calls）
        可以选择 speak: send_text_message → finish_turn（可同一次或分次调用）
        跳过不是失败，但要注意自然度
```

### 例 5: 主动 turn, 强制开口（上一轮 skipped）

```yaml
initiative_state:
  consecutive_skip_count: 1
  last_skipped_at: 2026-06-03T12:00:00
terminal_policy:
  allows_skip: false
  forced_speak: true
  min_delivered_messages: 1
tool_exposure:
  exposed_tool_names: ["send_text_message", "finish_turn"]
  # 注意: skip_initiative 不在列表中

# terminal_policy block 输出差异
- role: system
  content: |
    [终态规则]
    阶段: 强制开口（上一轮已选择跳过）
    必须发送至少 1 条消息
    不允许再次跳过 — skip_initiative 不可用
    必须调用 finish_turn(next_wake_minutes=30-480) 结束
    请自然地开启对话，避免重复最近已发送内容
```

### 例 6: 用户发送图片

```yaml
BuildContextRequest:
  turn_type: user_media_chat
  inbound_batch:
    items:
      - item_id: "img_001"
        kind: image
        media:
          summary: "一只白猫坐在窗台上，阳光洒在它身上"
      - item_id: "txt_001"
        kind: text
        text: "看看我的猫～"
  media_context:
    - item_id: "img_001"
      kind: image
      summary: "一只白猫坐在窗台上，阳光洒在它身上"

provider_messages:
  # ... 固定 block ...
  # 12. media_context
  - role: system
    content: |
      [用户发送了一张图片]
      summary: 一只白猫坐在窗台上，阳光洒在它身上
      [以上为图片摘要，非用户原文]

  # 14. current_input
  - role: user
    content: "看看我的猫～"
```

### 例 7: 提醒到期 turn

```yaml
BuildContextRequest:
  turn_type: user_reminder_due
  trigger: {trigger_type: due_reminder, source_task_id: "rem_001", reason: "用户：十分钟后提醒我喝水"}
  reminder_state:
    reminder_text: "喝水"
    due_at: 2026-06-03T14:40:00
  terminal_policy:
    min_delivered_messages: 1
    allows_skip: false
    required_terminal_tools: ["finish_turn", "mark_reminder_delivered"]
  tool_exposure:
    exposed_tool_names: ["send_text_message", "mark_reminder_delivered", "finish_turn"]

provider_messages:
  # current_input
  - role: system
    content: |
      [提醒到期] 用户设定的提醒已到时间。
      提醒内容: "喝水"
      这是一个用户授权提醒，不是主动消息。必须发送提醒内容。

  # terminal_policy
  - role: system
    content: |
      [终态规则]
      必须发送提醒消息 (send_text_message)
      必须标记提醒完成 (mark_reminder_delivered)
      最后 finish_turn()
      不允许跳过
```

### 例 8: Token 预算超限，历史被截断

```yaml
BuildContextRequest:
  token_budget: 8000
  recent_history:
    messages: [80 条消息, ~40 turns, ~9500 tokens]
  memory_selection: [6 条 profile memories, ~600 tokens]

token_budget_report 输出:
  budget: 8000
  used: 7920  # 截断后
  remaining: 80
  budget_exhausted: false  # 这次还是刚好够
  truncation_applied: true
  truncation_details:
    - block_id: "recent_conversation_history"
      original_estimated_tokens: 9500
      after_truncation_tokens: 7500
      strategy: head_trim
      detail: "removed 15 oldest turns (-2000 tokens), kept last 25 turns"
    - block_id: "selected_episode_memory"
      original_estimated_tokens: 300
      after_truncation_tokens: 150
      strategy: tail_only
      detail: "removed 3 low-priority episodes, kept 1 (-150 tokens)"

# provider_messages 中 recent_history 只含最后 25 turns
```

### 例 9: 冷启动（无历史、无记忆）

```yaml
BuildContextRequest:
  recent_history: {messages: []}
  memory_selection: {profile_memory: [], boundary_memory: [], episode_memory: [], open_thread_memory: [], sent_content_memory: []}
  relationship_state: null

BuiltContext:
  provider_messages: [12 条 messages]
  # block 顺序: fixed_system_protocol, fixed_output_protocol, fixed_tool_protocol,
  # persona, (跳过 relationship_state), system_time_block,
  # (跳过 boundary_memory), (跳过 profile_memory),
  # (跳过 episode_memory), (跳过 open_threads), (跳过 recent_history),
  # current_turn_local_state, (跳过 media_context),
  # current_input, exposed_tools_contract, terminal_policy
  warnings:
    - source: "relationship_state_missing"
      severity: warning
    - source: "empty_history"
      severity: info
  context_snapshot:
    block_summaries: 12 个 block
    history_message_ids: []
    memory_ids: []
```

### 例 10: 本条消息发送失败（验证不进入 history）

```yaml
当前是 user_chat turn 的第三次 model call:
current_turn_local_state:
  model_call_count: 2
  sent_messages_this_turn:
    - msg_id: "tl_1": text="辛苦了宝宝" status=delivered
    - msg_id: "tl_2": text="最近是不是项目很忙" status=failed
      delivery_receipt: {status: failed, error: "rate_limited"}
  tool_results_this_turn: [get_time result]

built_context 验证:
  turn_local block 内容:
    [本轮已发送消息]
    1. [delivered] "辛苦了宝宝"
    2. [failed - 用户未看到] "最近是不是项目很忙"

  recent_history block 内容:
    不包含 "辛苦了宝宝"   ← I-4
    不包含 "最近是不是项目很忙"  ← I-5

  # 两条消息都只在 turn_local block 中
  # failed 消息明确标注 "用户未看到"
```

---

## 13. 当前代码迁移点

### 13.1 `agent/context_builder.py` → 升级为 ContextBuilder

**当前状态：**
- `compact_history()` — 历史压缩函数
- `build_chat_system_prompt()` — 拼 system prompt（persona + tool_names + base_protocol + tool_policy）
- `_read_prompt_component()` — 从 prompts/ 读 .md 文件

**保留内容：**
- `compact_history()` 的截断逻辑 → 移入 `_apply_truncation()` 的 `recent_history` 阶段
- `build_chat_system_prompt()` 的固定内容组装 → 改为 `_make_fixed_system_block()` 和 `_make_persona_block()` 方法
- `_read_prompt_component()` → 移到 ContextBuilder.__init__() 启动时加载，不再每次 turn 读磁盘

**删除内容：**
- `compact_history()` 的 summary 生成逻辑（提取旧 turn 的摘要文本）→ 不再需要，recent_history 截断用 head_trim 策略
- 所有 "filter out sections that reference disabled features" 的 keyword 过滤逻辑 → 由 prompt 文件直接控制，不需要运行时过滤

**新增内容：**
- `ContextBuilder` 类，含 `build()` 方法
- 所有 `_make_*_block()` 子方法
- 所有 `_verify_*()` 验证方法
- `_check_token_budget()` 和 `_apply_truncation()`

### 13.2 `agent/temporal_context.py` → 合并入 ContextBuilder 的 SystemTimeBlock

**当前状态：**
- `TemporalContextBuilder` 类
- `TemporalContextResult` dataclass
- `build(now, history, current_user_ts)` 方法
- 时间分割线生成逻辑

**保留内容：**
- resume_type 判定逻辑（`_classify_resume_type`）→ 移入 `_make_system_time_block()`
- `_build_temporal_message()` 的格式化逻辑 → 移入 SystemTimeBlock 格式
- `_augment_history()` 的时间分割线生成 → 移入 `_make_recent_history_block()`
- gap threshold 常量和 time divider threshold 常量

**删除内容：**
- `TemporalContextBuilder` 独立类
- `TemporalContextResult` 独立 dataclass（合并进 SystemTimeBlock）
- 独立的 `build()` 方法（合并进 ContextBuilder.build()）

**新增内容：**
- `SystemTimeBlock` dataclass
- Degraded mode 处理逻辑

### 13.3 `main.py` 的 prompt assembly → 移入 ContextBuilder

**当前状态：**
- `build_system_prompt_for_turn()` — 组装 persona + personality + tool_names + base_protocol + tool_policy
- `build_enriched_system_prompt()` — 在 system prompt 后面追加 memory context
- `/chat` route 中: 调用 `build_enriched_system_prompt()` → 传给 ChatAgent

**迁移内容：**
- `build_system_prompt_for_turn()` 的全部逻辑 → BCR.fixed_system_protocol / fixed_output_protocol / fixed_tool_protocol 字段
- `build_enriched_system_prompt()` 的 memory context 追加 → MemorySelection 通过 BCR.memory_selection 传入，ContextBuilder 自己组织记忆 block
- `/chat` route 中不再构建 system_prompt 字符串
- `/chat` route 改为: 组装 BuildContextRequest → 调用 context_builder.build() → 传给 TurnEngine → 传给 ProviderGateway

**保留在 main.py 的内容：**
- persona 文件读取（`_read_persona()`, `_read_personality()`）→ 仍由 main.py 启动时读取并注入 BCR.persona
- environment config 读取（如 default_city）→ 注入 BCR

### 13.4 `agent/chat_agent.py` context injection → 移除，委托给 ContextBuilder

**当前状态：**
- `_do_build_context()` — 组装 system prompt + temporal context + history + user message + proactive context
- `_build_proactive_context_system_message()` — 独立模块级函数，组装主动消息的上下文

**迁移内容：**
- `_do_build_context()` 的整个逻辑 → ContextBuilder.build()
  - System prompt assembly → ContextBuilder fixed blocks
  - Temporal context call → SystemTimeBlock
  - History transcript assembly → recent_conversation_history block
  - User message injection → current_input block
  - Proactive context injection → current_input block (role=system)
  - Default city injection → system context block (融入 system_time_block 或独立 block)
- `_build_proactive_context_system_message()` → ContextBuilder._make_current_input_block() 内的主动消息分支
  - snapshot / trigger_kind / is_wake_up / min_next / max_next / 工具阶段描述
  - 这些信息应该通过 BuildContextRequest.current_turn_local_state + initiative_state + terminal_policy 组织

**删除内容：**
- `_do_build_context()` → 方法体清空或改为调用 ContextBuilder
- `_build_proactive_context_system_message()` → 模块级函数删除
- ChatAgent 的 system_prompt 参数 → 不再需要
- ChatAgent 内部的 `_default_system_prompt()` → 不再需要

**保留内容：**
- `TurnScratchpad` 的数据结构 → CurrentTurnLocalState 会对应映射
- 状态机的状态流转 → 不变

---

## 14. 必须测试的用例列表

### 14.1 结构测试

| # | 测试名 | 输入条件 | 预期断言 |
|---|--------|---------|---------|
| T1 | `test_build_context_missing_required_fields_raises_error` | BCR 缺少 turn_id / call_id / turn_type 等必填字段 | 抛出 `BuildContextError`, missing_fields 包含缺失项 |
| T2 | `test_build_context_produces_all_16_blocks_in_order` | 完整 BCR（含所有可选字段） | `blocks` 列表长度为 16，顺序与第 4 节完全一致 |
| T3 | `test_provider_messages_all_have_valid_roles` | 完整 BCR | 每条 message 的 role 在 {system, user, assistant, tool} 中 |
| T4 | `test_tool_contract_tool_names_equal_native_schema_tool_names` | BCR 含 tool_exposure | tool_contract_block 中描述的工具名集合 == native_tool_schemas 的 name 集合 |

### 14.2 上下文污染测试

| # | 测试名 | 输入条件 | 预期断言 |
|---|--------|---------|---------|
| T5 | `test_sent_message_not_in_recent_history_during_turn` | BCR.current_turn_local_state 含 1 条 delivered 消息在 sent_messages_this_turn；BCR.recent_history 为空 | sent_message 只出现在 turn_local block 中，不在 recent_history block |
| T6 | `test_failed_delivery_not_in_assistant_history` | BCR.current_turn_local_state 含 1 条 failed 消息；BCR.recent_history 为空 | failed 消息在 turn_local 中标记为 failed，不在 recent_history role=assistant 中 |
| T7 | `test_tool_result_not_as_user_message` | BCR.current_turn_local_state 含 1 条 tool_result (get_weather) | tool_result 在 provider_messages 中以 role=tool 出现，不在 role=user 中 |
| T8 | `test_media_summary_not_as_user_text` | BCR.media_context 含 1 条图片摘要 | 摘要出现在 media_context block (role=system)，不在 role=user 的任何 content 中 |
| T9 | `test_context_snapshot_not_in_provider_messages` | BCR.debug_mode=true | context_snapshot 内容不在任何 provider_messages 的 content 中 |

### 14.3 不同 turn_type 测试

| # | 测试名 | 输入条件 | 预期断言 |
|---|--------|---------|---------|
| T10 | `test_user_chat_current_input_is_role_user` | BCR.turn_type=user_chat, BCR.inbound_batch 有 text item | current_input block 的 role=user, content=用户文本 |
| T11 | `test_proactive_current_input_is_role_system` | BCR.turn_type=relationship_proactive, BCR.inbound_batch=null | current_input block 的 role=system, content 含 "主动回合" 描述 |
| T12 | `test_forced_speak_skip_not_exposed` | BCR.terminal_policy.forced_speak=true | tool_contract_block 内容不包含 "skip_initiative" 或 skip 工具描述 |
| T13 | `test_skippable_phase_skip_is_exposed` | BCR.terminal_policy.allows_skip=true, forced_speak=false | tool_contract_block 内容包含 "skip_initiative" 工具描述 |

### 14.4 边界条件测试

| # | 测试名 | 输入条件 | 预期断言 |
|---|--------|---------|---------|
| T14 | `test_cold_start_no_history_no_memory` | BCR.recent_history.messages=[], BCR.memory_selection 全空 | provider_messages 跳过 recent_history 和所有 memory block；warnings 含 empty_history |
| T15 | `test_relationship_state_missing_continues_with_warning` | BCR.relationship_state=None | relationship_state block 跳过；warnings 含 source="relationship_state_missing" |
| T16 | `test_system_time_unavailable_degraded_mode` | BCR.system_time=None | SystemTimeBlock 含 "系统时间不可用" 文本；warnings 含 source="system_time_unavailable" |
| T17 | `test_token_budget_exhausted_truncation_applied` | BCR.recent_history 含大量消息使估算 token > budget | token_budget_report.truncation_applied=true；provider_messages 中消息数 < 原始数 |
| T18 | `test_fixed_blocks_too_large_no_silent_truncation` | BCR.fixed_system_protocol + persona 等固定 block 的 token > budget | token_budget_report.budget_exhausted=true；不执行截断；warnings 含 "fixed_blocks_too_large" |
| T19 | `test_memory_load_failure_continues_without_memory` | 模拟 MemorySelection 抛出异常（或通过 BCR memory_selection 为空模拟） | 所有 memory block 跳过；warnings 含 source="memory_load_failed" |

### 14.5 Snapshot 测试

| # | 测试名 | 输入条件 | 预期断言 |
|---|--------|---------|---------|
| T20 | `test_snapshot_contains_all_block_summaries` | 完整 BCR | context_snapshot.block_summaries 数量 == 实际 block 数；每个 block 有 block_id / kind / cache_class / content_summary |
| T21 | `test_snapshot_excludes_api_key` | BCR.debug_mode=true（但 BCR 中不应有 API key 字段） | context_snapshot 的任何字段字符串中不含 "sk-" 或 "Bearer" 或类似 API key 格式；full_payload_ref 引用的内容也不含 |
| T22 | `test_snapshot_has_history_message_ids` | BCR.recent_history 含 4 条消息 | context_snapshot.history_message_ids == ["m1", "m2", "m3", "m4"] |

### 14.6 端到端验证

| # | 测试名 | 输入条件 | 预期断言 |
|---|--------|---------|---------|
| T23 | `test_full_user_chat_all_fields_roundtrip` | 完整 BCR（user_chat, 有历史, 有记忆, 有 relationship_state） | BuiltContext 无异常；provider_messages 数量正确；所有 invariants 通过 |
| T24 | `test_multi_call_turn_turn_local_accumulates` | 第 1 次 build, 第 2 次 build (current_turn_local_state 含 1 条 delivered) | 第 2 次 build 的 turn_local block 含新消息；recent_history block 不变 |
| T25 | `test_two_independent_builds_produce_different_snapshots` | 两次 build() 调用（不同 call_id） | context_snapshot.snapshot_id 不同；built_context 不共享任何可变对象引用 |

---

## 附录 A: 字段类型完整映射

| Python 类型 | JSON/序列化类型 | 示例 |
|------------|---------------|------|
| `str` | string | `"turn_abc123"` |
| `int` | integer | `42` |
| `float` | number | `1717400000.0` |
| `bool` | boolean | `true` |
| `datetime` | ISO-8601 string | `"2026-06-03T14:30:00+08:00"` |
| `list[T]` | array | `["a", "b"]` |
| `dict` | object | `{"key": "value"}` |
| `X \| None` | nullable | 字段缺失或 null |
| `list[ContentPart]` | array of objects | 用于多模态 provider 的 content 字段 |
| `TurnType` | string enum | `"user_chat"` |
| `DeliveryStatus` | string enum | `"delivered"` |

## 附录 B: 参考实现的文件位置

| 文件 | 内容 |
|------|------|
| `agent/runtime_context_builder.py` | ContextBuilder 主类实现 |
| `agent/temporal_context.py` | SystemTimeBlock 构建逻辑（合并后删除独立类） |
| `agent/context_builder.py` | 旧 builder → 在迁移后删除或改为 thin adapter |
| `agent/state_machine.py` | TurnScratchpad → 扩展为 CurrentTurnLocalState |
| `agent/tool_exposure_policy.py` | ToolExposure / ToolExposurePolicy → BCR.tool_exposure 的来源 |
| `memory/system.py` | MemorySystem → BCR.memory_selection 的来源 |
| `prompts/*.md` | 固定提示词文件 → BCR.fixed_system_protocol 等的来源 |
| `main.py` | BCR 组装 → 在 `/chat` route 等位置组装后调用 ContextBuilder.build() |
