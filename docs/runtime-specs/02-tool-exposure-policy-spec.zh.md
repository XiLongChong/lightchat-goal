# ToolExposurePolicy 可执行实现规格

> 日期：2026-06-03
> 状态：可执行规格
> 上游文档：
> - `docs/companion-architecture-requirements.zh.md`（第 3, 4, 9.5, 13.7 节）
> - `docs/companion-architecture-design-decisions.zh.md`（第 3, 6, 9.2 节）
> - `docs/companion-runtime-module-contracts.zh.md`（第 9 节）
> - `docs/companion-runtime-data-contracts.zh.md`（第 9 节）
> - `docs/companion-runtime-state-machines.zh.md`（第 2, 3, 4, 5, 6 节）
> - `docs/companion-runtime-gap-analysis.zh.md`（第 3.2 节）
> - `docs/companion-runtime-implementation-plan.zh.md`（Phase 2, 11）
> - `docs/companion-runtime-test-plan.zh.md`（第 2 节）
>
> 实现者应能在仅阅读本文档的情况下，构建出完整的 `ToolExposurePolicy` 模块。

---

## 1. 模块目标

`ToolExposurePolicy` 是本轮模型可见工具的**唯一决策入口**。

它不执行工具、不构建完整上下文、不调用模型。它只有一个职责：**根据本轮运行时参数，决定模型能看到哪些工具，并同时产出 prompt contract 文本和 native tool schemas，二者必须同源**。

```text
ToolExposurePolicy.resolve(request) -> ToolExposure
  |
  +-- exposed_tool_names: list[str]          # 暴露给模型的工具名列表
  +-- native_tool_schemas: list[NativeToolSchema]  # provider 级别的 tool schema
  +-- tool_contract_block: str               # 注入 prompt 的工具说明
  +-- denied_tools: list[DeniedTool]         # 存在但本轮不暴露的工具及原因
```

关键原则：

1. **prompt 里说的工具 = native schema 里的工具**。不能出现 prompt 说有 `send_text_message` 但 schema 里没有，或者 schema 里有但 prompt 没说的情况。
2. **工具在 registry 中存在不等于本轮暴露**。每个 turn 独立决策。
3. **决策必须可解释**。每个 denied tool 要有 reason；每个 exposed tool 要有决策依据。

---

## 2. 完整输入输出对象

### 2.1 输入：`ToolExposureRequest`

```python
@dataclass(slots=True)
class ToolExposureRequest:
    """ToolExposurePolicy.resolve() 的输入参数。

    所有字段由 TurnEngine 在 CLASSIFY_TURN 之后、BUILD_CONTEXT 之前填入。
    """

    # ── 本轮基础分类 ──
    turn_type: TurnType                        # 必需。枚举见下方。
    trigger_source: str                        # 必需。"user" | "scheduler" | "admin" | "bridge"

    # ── 本轮局部状态 ──
    current_turn_local_state: CurrentTurnLocalState | None  # 已发送消息数、已调用工具等

    # ── 用户和媒体意图 ──
    user_intent: str                           # 必需。"chat" | "request_reminder" | "request_image"
                                               # | "forget_directive" | "relationship_feedback"
                                               # | "task_feedback" | "unknown"
    media_intent: str | None                   # 用户本轮是否涉及图片/表情/语音等媒体。
                                               # None | "image_requested" | "image_provided"
                                               # | "voice_provided"

    # ── 主动和提醒状态（仅相关 turn type 时有值） ──
    initiative_state: InitiativeState | None   # 上次主动决策、连续skip次数等
    reminder_state: ReminderState | None       # 当前提醒的 ID、类型等

    # ── 运行时配置 ──
    runtime_config: dict[str, Any] | None      # features.image_generation, features.weather,
                                               # intensity_profile 等。None 时使用硬编码安全默认值。

    # ── 系统时间注入状态 ──
    system_time_available: bool                # True = ContextBuilder 已通过 SystemTimeBlock
                                               # 注入当前时间。False = degraded mode。
```

### 2.2 `TurnType` 枚举

```python
class TurnType(StrEnum):
    USER_CHAT = "user_chat"
    USER_MEDIA_CHAT = "user_media_chat"
    USER_REMINDER_REQUEST = "user_reminder_request"
    RELATIONSHIP_PROACTIVE = "relationship_proactive"
    USER_REMINDER_DUE = "user_reminder_due"
    SCHEDULED_RITUAL = "scheduled_ritual"
    ADMIN_MANUAL_TRIGGER = "admin_manual_trigger"
    MEMORY_MANAGEMENT = "memory_management"
```

### 2.3 `InitiativeState` 最小结构

```python
@dataclass
class InitiativeState:
    last_decision: str | None           # 上次主动决策："spoke" | "skipped" | None（首次）
    consecutive_skip_count: int         # 连续跳过次数
    intensity_profile: str              # "quiet" | "normal" | "caring" | "companion"
```

### 2.4 输出：`ToolExposure`

```python
@dataclass(slots=True)
class ToolExposure:
    """ToolExposurePolicy.resolve() 的输出。

    同时作为 ContextBuilder 和 ProviderGateway 的输入源。
    """

    exposure_id: str                          # 本 exposure 决策的唯一 ID
    turn_id: str                              # 关联 turn ID
    phase: str                                # 当前阶段：
                                              # "normal_chat" | "skippable" | "forced_speak"
                                              # | "after_first_message" | "reminder_delivery"
                                              # | "memory_management" | "admin"

    exposed_tool_names: list[str]             # 模型可见的工具名列表（按优先级排列）

    native_tool_schemas: list[NativeToolSchema]  # provider 级别 schema。
                                                 # 只包含 exposed_tool_names 中的工具。

    tool_contract_block: str                  # prompt 中 inject 给模型的工具说明文本。
                                              # 必须只描述 exposed_tool_names 中的工具。

    denied_tools: list[DeniedTool]            # 存在但本轮不暴露的工具及原因
    reasons: list[str]                        # 本轮暴露/拒绝的决策摘要
```

### 2.5 辅助类型

```python
@dataclass(slots=True)
class NativeToolSchema:
    name: str
    description: str
    parameters_schema: dict        # JSON Schema 对象
    terminal: bool                 # 调用此工具是否导致 turn 进入终态
    side_effect: str               # "none" | "sends_message" | "writes_state" | "reads_external"
    category: str                  # "output" | "turn_control" | "state" | "information"


@dataclass(slots=True)
class DeniedTool:
    tool_name: str
    reason: str                    # 人可读的拒绝原因
    would_expose_if: str | None    # 什么条件下会暴露（用于调试）


@dataclass(slots=True)
class ToolExposureBuildError(Exception):
    """当 ToolExposure 无法安全构建时抛出。

    调用方应停止本轮，不调用模型。"""
    reason: str
    details: dict | None = None
```

---

## 3. 不可违反的不变量（至少 12 条）

实现者 MUST 确保以下条件在所有路径下成立。违反任何一条即视为 bug，不应提交。

**INV-1: prompt contract 工具名与 native schema 工具名一致**
> `tool_contract_block` 中提及的每个 tool name 必须精确出现在 `native_tool_schemas` 中，且 `native_tool_schemas` 中的每个 tool name 也必须在 `tool_contract_block` 中被描述。
> 验证：`set(tool_contract_block_mentioned_names) == set(s.name for s in native_tool_schemas)`

**INV-2: 每个 exposed tool 必须有 executor**
> 对于 `exposed_tool_names` 中的每个 name，必须在 `ToolRegistry` 中存在对应注册项，且该注册项必须能产生可执行的 handler。
> 如果某 tool 有 schema 但无 executor，抛出 `ToolExposureBuildError(reason="executor_missing")`。

**INV-3: 注册表中的工具不等于本轮暴露**
> `ToolRegistry` 中存在 `generate_image_message` 注册项，不意味着它在 `user_chat` turn 中自动暴露。每个 turn 必须独立评估。

**INV-4: 主动回合（relationship_proactive）永远不暴露图片/表情工具**
> `generate_image_message`、`send_image_message`、`send_sticker` 在 `turn_type == RELATIONSHIP_PROACTIVE` 时，无论任何 phase，always in `denied_tools`，never in `exposed_tool_names`。

**INV-5: forced-speak 阶段不暴露 `skip_initiative`**
> 当 `initiative_state.last_decision == "skipped"` 且 `turn_type == RELATIONSHIP_PROACTIVE` 时，`skip_initiative` MUST NOT be in `exposed_tool_names`。

**INV-6: 普通聊天 turn 不暴露 `skip_initiative`**
> `skip_initiative` 只能出现在 `RELATIONSHIP_PROACTIVE` 的 skippable phase 中。任何 `USER_CHAT`、`USER_MEDIA_CHAT`、`USER_REMINDER_REQUEST`、`USER_REMINDER_DUE`、`SCHEDULED_RITUAL`、`ADMIN_MANUAL_TRIGGER`、`MEMORY_MANAGEMENT` 下 MUST NOT 暴露 `skip_initiative`。

**INV-7: `get_time` 不是默认暴露**
> `get_time` 在 `turn_type == USER_CHAT` 且 `system_time_available == True` 且用户未提出 timezone/time-calculation 需求时，MUST NOT 进入 `exposed_tool_names`。详见第 6 节专项决策表。

**INV-8: `finish_turn` 在所有 turn type 下必须暴露**
> 每个 turn 都必须有一条可到达终态的路径。`finish_turn` 是所有 turn 的通用终态工具，在 `exposed_tool_names` 中永远存在。

**INV-9: `send_text_message` 在所有允许发消息的 turn type 下必须暴露**
> 除了极罕见的 `ADMIN_MANUAL_TRIGGER` dry-run 模式外，`send_text_message` 必须在 `exposed_tool_names` 中。

**INV-10: `schedule_user_reminder` 只在用户提醒请求 turn 中暴露**
> 其他任何 turn type 下 MUST NOT 暴露此工具。

**INV-11: `mark_reminder_delivered` 只在用户提醒到期 turn 中暴露**
> 其他任何 turn type 下 MUST NOT 暴露此工具。

**INV-12: 输出和拒绝对称**
> `exposed_tool_names` 和 `denied_tools` 的并集应包含所有被评估过的工具。`denied_tools` 中的每个条目必须有 non-empty `reason`。

**INV-13: 图片工具只在用户请求且非主动回合时暴露**
> `generate_image_message`、`send_image_message`、`send_sticker` 仅在 `turn_type in (USER_CHAT, USER_MEDIA_CHAT, MEMORY_MANAGEMENT)` 且 `user_intent == "request_image"` 且 `trigger_source == "user"` 时暴露。其他条件 MUST NOT 暴露。

**INV-14: `apply_forget_directive` 只在 memory_management turn 暴露**
> `apply_forget_directive` 仅在 `turn_type == MEMORY_MANAGEMENT` 或 `user_intent == "forget_directive"` 时暴露。

---

## 4. 工具分类

每个工具属于以下四类之一。分类决定工具的 `category`、`terminal` 和 `side_effect` 属性。

### 4.1 分类表

| 工具名 | category | terminal | side_effect | 说明 |
|--------|---------|----------|-------------|------|
| `send_text_message` | output | false | sends_message | 发送一条文字消息到用户。不结束 turn。 |
| `send_image_message` | output | false | sends_message | 发送已有图片到用户。不结束 turn。 |
| `generate_image_message` | output | false | sends_message | 生成图片并发送。不结束 turn。 |
| `send_sticker` | output | false | sends_message | 发送表情包。不结束 turn。 |
| `finish_turn` | turn_control | **true** | writes_state | 结束本轮。所有 turn type 的通用终态。 |
| `skip_initiative` | turn_control | false | writes_state | 声明本次主动机会跳过。仅 RELATIONSHIP_PROACTIVE skippable phase。 |
| `schedule_user_reminder` | state | false | writes_state | 创建用户授权的提醒任务。 |
| `mark_reminder_delivered` | state | **true** | writes_state | 标记提醒已送达。通常与 finish_turn 配合。 |
| `get_time` | information | false | reads_external | 获取指定时区当前时间。 |
| `get_weather` | information | false | reads_external | 获取指定城市天气信息。 |
| `apply_forget_directive` | state | false | writes_state | 应用"别记这个"指令并屏蔽记忆。 |

### 4.2 分类说明

- **output tools**：产生外部可见消息的工具。side_effect = sends_message。不结束 turn。
- **turn_control tools**：改变 turn 流转路径的工具。`finish_turn` 是 terminal（调用后 turn 结束）；`skip_initiative` 不是 terminal（跳过后仍需 `finish_turn`）。
- **state tools**：修改系统持久状态的工具。不直接产生用户可见消息。`mark_reminder_delivered` 是 terminal（通常标记后本轮就结束）。
- **information tools**：只读外部信息的工具。不产生 user-visible side effects。结果回灌给模型。

---

## 5. 核心决策表

以下决策表为 `resolve()` 的权威规则来源。优先按 turn_type 查找，找不到时 fallback 到 `user_chat` 默认集。

**图例**：`Y` = 暴露，`N` = 不暴露，`C` = 条件暴露（见右侧注释）

### 5.1 主决策表

| # | turn_type | user_intent | initiative_state | phase | send_text | finish_turn | skip_init | schedule_rem | mark_deliv | get_time | get_weather | gen_img | send_img | send_sticker | apply_forget | 注释 |
|---|-----------|-------------|-------------------|-------|-----------|-------------|-----------|--------------|------------|----------|-------------|---------|----------|--------------|---------------|------|
| 1 | user_chat | chat | -- | normal_chat | Y | Y | N | N | N | C | C | N | N | N | N | get_time 见§6；get_weather 见§7；均仅用户有对应意图时暴露 |
| 2 | user_chat | request_image | -- | normal_chat | Y | Y | N | N | N | C | C | Y | Y | Y | N | 用户明确要求图片：全部图片工具暴露 |
| 3 | user_chat | request_reminder | -- | normal_chat | Y | Y | N | Y | N | C | C | N | N | N | N | 用户要求提醒：暴露 schedule |
| 4 | user_chat | forget_directive | -- | memory_management | Y | Y | N | N | N | N | N | N | N | N | Y | "别记这个"触发：暴露 apply_forget |
| 5 | user_media_chat | chat | -- | normal_chat | Y | Y | N | N | N | C | C | N | N | N | N | 用户发图片/语音：媒体摘要进上下文；回复用文字 |
| 6 | user_media_chat | request_image | -- | normal_chat | Y | Y | N | N | N | C | C | Y | Y | Y | N | 用户发图后又要求生成图 |
| 7 | user_reminder_request | request_reminder | -- | normal_chat | Y | Y | N | Y | N | Y | C | N | N | N | N | 提醒请求轮：绝对时间校验需要 get_time=Y |
| 8 | user_reminder_due | (N/A) | -- | reminder_delivery | Y | Y | N | N | Y | N | N | N | N | N | N | 提醒到期：只需发消息+标记+结束 |
| 9 | relationship_proactive | (N/A) | last_decision=None\|spoke | skippable | Y | Y | Y | N | N | N | N | N | N | N | N | 首次/上次已说：可跳过可发消息 |
| 10 | relationship_proactive | (N/A) | last_decision=skipped | forced_speak | Y | Y | N | N | N | N | N | N | N | N | N | 上次跳过：强制开口，skip先调后仍须finish |
| 11 | relationship_proactive | (N/A) | (any) | after_first_message | Y | Y | N | N | N | N | N | N | N | N | N | 已发至少一条后：只能继续发或结束 |
| 12 | scheduled_ritual | (N/A) | -- | normal_chat | Y | Y | N | N | N | N | C | N | N | N | N | 早安/晚安：只发文字；get_weather 仅在用户历史中有天气关注习惯时暴露 |
| 13 | admin_manual_trigger | (any) | -- | admin | Y | Y | N | N | N | C | C | N | N | N | N | admin触发：默认同user_chat，但标记来源 |
| 14 | memory_management | forget_directive | -- | memory_management | Y | Y | N | N | N | N | N | N | N | N | Y | 记忆管理：apply_forget + 文字回复 |
| 15 | (unknown/fallback) | unknown | -- | normal_chat | Y | Y | N | N | N | N | Y | N | N | N | N | 未知 turn_type：使用安全最小集 |

### 5.2 决策表补充说明

**行 9-11 的 phase 转换逻辑（实现此逻辑时必须精确遵循）**：

模型在 `relationship_proactive` turn 内多次调用工具时，phase 会随 `current_turn_local_state.delivered_message_count` 变化：

```text
进入 turn 时:
  if initiative_state.last_decision == "skipped":
      phase = "forced_speak"    # 不暴露 skip_initiative
  else:
      phase = "skippable"       # 暴露 skip_initiative

模型调用 send_text_message (delivered) 后:
  phase = "after_first_message" # 不再暴露 skip_initiative
  # 注意：从 skippable 到 after_first_message 的转换是单向的。
  # 即使 sent 失败（delivery_failed），如果 count > 0 也切换。
```

**行 8（user_reminder_due）特殊规则**：

```text
reminder_due 不使用主动 skip 规则。
即使模型想跳过也不能暴露 skip_initiative。
提醒是用户授权的承诺，模型不能单方面跳过。
```

**行 12（scheduled_ritual）**：
固定仪式不暴露 skip。如果系统 gate 允许仪式"可选"，由 ContextBuilder 在 terminal_policy 中告知模型可以用 `finish_turn(status=skipped)` 而非调用专门的 skip 工具。

---

## 6. `get_time` 专项决策表

`get_time` 的暴露逻辑独立于其他工具。核心原则：**当前时间默认已由 ContextBuilder 通过 SystemTimeBlock 注入，不需要模型主动查询。只在特定场景下暴露 `get_time`。**

### 6.1 决策矩阵

| # | turn_type | system_time_available | 用户触发条件 | expose get_time? | reason |
|---|-----------|----------------------|-------------|-----------------|--------|
| T1 | user_chat | true | 无 timezone/time-calc 需求 | **N** | SystemTimeBlock 已提供当前时间 |
| T2 | user_chat | true | 用户明确问其他时区（如"纽约现在几点"） | **Y** | 需要非默认时区时间 |
| T3 | user_chat | true | 用户做时间计算（如"3小时后几点"） | **Y** | 计算型查询 |
| T4 | user_chat | false | 任何 | **Y** | degraded mode：SystemTimeBlock 不可用，降级暴露 |
| T5 | user_chat | true | 用户问"现在几点"（本地时间） | **N** | SystemTimeBlock 内已包含本地时间；模型应从上下文读取 |
| T6 | user_reminder_request | true | -- | **Y** | 需要绝对时间校验（"明天下午3点"的解析、时间zone 判断） |
| T7a | user_reminder_due | true | -- | **Y** | 可能需要验证当前时间是否在提醒窗口内 |
| T7b | user_reminder_due | false | -- | **N** | 后台回合不应靠模型调用 get_time 修复；进入 safe terminal |
| T8a | relationship_proactive | true | -- | **N** | 主动消息不需要时间查询 |
| T8b | relationship_proactive | false | -- | **N** | 后台回合 system_time 不可用 → safe terminal，不暴露 get_time |
| T9a | scheduled_ritual | true | -- | **N** | 仪式回合不需要时间查询 |
| T9b | scheduled_ritual | false | -- | **N** | 后台回合 system_time 不可用 → safe terminal，不暴露 get_time |
| T10 | user_media_chat | true | 同 user_chat | **C** | 按 user_chat 规则 |
| T11 | memory_management | false | -- | **Y** | 同 user_chat degraded 规则：用户回合，降级暴露 |

### 6.2 判断"用户有 timezone/time-calc 需求"的规则

实现者不应在 `ToolExposurePolicy` 内部做 NLP。`ToolExposureRequest` 的 `user_intent` 字段应由 `TurnClassifier` 预先填好。`get_time` 暴露规则只检查以下条件：

```python
def _should_expose_get_time(request: ToolExposureRequest) -> bool:
    # 后台回合（proactive, reminder_due, ritual, admin）：system_time 不可用时不应暴露 get_time
    # 这些回合应进入 safe terminal，而不是靠模型调用 get_time 修复系统状态
    BACKGROUND_TURN_TYPES = {
        TurnType.RELATIONSHIP_PROACTIVE,
        TurnType.USER_REMINDER_DUE,
        TurnType.SCHEDULED_RITUAL,
        TurnType.ADMIN_MANUAL_TRIGGER,
    }

    if not request.system_time_available:
        if request.turn_type in BACKGROUND_TURN_TYPES:
            return False  # safe_fail, don't expose
        return True  # 用户回合：降级暴露

    # 2. Turn type 本身需要时间校验
    if request.turn_type in (TurnType.USER_REMINDER_REQUEST, TurnType.USER_REMINDER_DUE):
        return True

    # 3. 用户明确有 timezone/time-calc 需求（由 TurnClassifier 预先填入标记）
    # 具体标记方式：在 runtime_config 中设置 "user_has_timezone_query": true
    # 或通过 user_intent 细化到 "time_query"
    if request.runtime_config and request.runtime_config.get("user_has_timezone_query"):
        return True

    # 4. 默认不暴露
    return False
```

### 6.3 `get_time` 注入 prompt 的说明

当 `get_time` 被暴露时，`tool_contract_block` 中必须说明"只在需要非默认时区或时间计算时使用，当前本地时间已通过系统消息提供"。

当 `get_time` 未被暴露时，`denied_tools` 中应包含：
```text
DeniedTool(
    tool_name="get_time",
    reason="当前时间已通过系统上下文注入，不需要模型查询",
    would_expose_if="用户询问其他时区时间，或进行时间计算"
)
```

---

## 7. `get_weather` 专项决策表

`get_weather` 与 `get_time` 遵循相同的原则：**不作为默认工具暴露。只在用户明确有天气意图时暴露。** 天气信息不是每轮对话都需要的上下文，默认暴露会增加模型不必要的工具调用倾向。

### 7.1 决策矩阵

| # | turn_type | system_time_available | 用户触发条件 | expose get_weather? | reason |
|---|-----------|----------------------|-------------|--------------------|--------|
| W1 | user_chat | any | 用户明确问天气（如"今天天气怎么样""上海热不热""会下雨吗"） | **Y** | 用户有天气意图 |
| W2 | user_chat | any | 用户提及地点+体感（如"北京冷不冷"） | **Y** | 隐含天气查询 |
| W3 | user_chat | any | 无天气相关意图 | **N** | 默认不暴露 |
| W4 | user_media_chat | any | 同 user_chat 规则 | **C** | 按 user_chat 规则 |
| W5 | user_reminder_request | any | -- | **N** | 提醒请求主要关注时间，不涉及天气 |
| W6 | user_reminder_due | any | -- | **N** | 后台回合不暴露 |
| W7 | relationship_proactive | any | -- | **N** | 主动消息不暴露 |
| W8 | scheduled_ritual | any | 用户历史中有天气关注习惯（由 MemorySystem 标记） | **Y** | 早安/晚安场景中天气可作为自然话题 |
| W9 | scheduled_ritual | any | 无天气关注习惯 | **N** | 默认不暴露 |
| W10 | memory_management | any | -- | **N** | 记忆管理不涉及天气 |
| W11 | admin_manual_trigger | any | 同 user_chat 规则 | **C** | 按 user_chat 规则 |

### 7.2 判断"用户有天气意图"的规则

```python
def _should_expose_get_weather(request: ToolExposureRequest) -> bool:
    # 用户明确天气意图（由 TurnClassifier 在 user_intent 中标记）
    if request.user_intent == "request_weather":
        return True

    # 仅对用户回合 + 普通聊天（或媒体聊天）暴露
    if request.turn_type not in (TurnType.USER_CHAT, TurnType.USER_MEDIA_CHAT):
        return False

    # 定时仪式：用户历史中有天气关注习惯时暴露
    if request.turn_type == TurnType.SCHEDULED_RITUAL:
        return request.runtime_config.get("user_has_weather_habit", False)

    # 默认不暴露
    return False
```

### 7.3 `get_weather` 注入 prompt 的说明

当 `get_weather` 未被暴露时，`denied_tools` 中应包含：
```text
DeniedTool(
    tool_name="get_weather",
    reason="get_weather 仅在用户询问天气时暴露，不作为默认工具",
    would_expose_if="用户询问天气或提及地点+天气相关"
)
```

---

## 8. 主动消息阶段工具差异

`relationship_proactive` turn 有三个 distinct phase。每个 phase 的工具暴露不同。

### 7.1 三阶段详细对比

| 维度 | skippable phase | forced-speak phase | after-first-message phase |
|------|----------------|-------------------|--------------------------|
| **触发条件** | `last_decision != "skipped"` | `last_decision == "skipped"` | `delivered_message_count >= 1` |
| **暴露工具** | `send_text_message`, `skip_initiative`, `finish_turn` | `send_text_message`, `finish_turn` | `send_text_message`, `finish_turn` |
| **skip_initiative** | **暴露** | **不暴露** | **不暴露** |
| **图片/表情工具** | 不暴露 | 不暴露 | 不暴露 |
| **get_time** | 不暴露 | 不暴露 | 不暴露 |
| **get_weather** | 不暴露 | 不暴露 | 不暴露 |
| **模型必须** | 先选 send 或 skip，然后 finish_turn（注：skip_initiative 和 finish_turn 必须分两次模型调用；send_text_message 可以和 finish_turn 同一次调用） | send at least 1，然后 finish_turn | 继续 send 或 finish_turn |
| **终态工具** | `finish_turn(next_wake_minutes=...)` | `finish_turn(next_wake_minutes=...)` | `finish_turn(next_wake_minutes=...)` |
| **next_wake_minutes** | 必填 | 必填 | 必填 |

### 7.2 阶段转换状态机

```text
 ┌──────────────┐
 │ TURN_CREATED │
 └──────┬───────┘
        │
        ▼
 ┌──────────────────────────────────┐
 │ last_decision == "skipped"?      │
 └────┬─────────────────┬───────────┘
      │ YES             │ NO
      ▼                 ▼
┌──────────┐    ┌────────────┐
│ forced_  │    │ skippable  │
│ speak    │    │ phase      │
└────┬─────┘    └─────┬──────┘
     │                │
     │   model calls  │   model calls
     │   send_text    │   send_text OR
     │   _message     │   skip_initiative
     │      │         │      │
     │      ▼         │      ▼
     │  ┌─────────────┤  ┌──┴──────────────┐
     │  │ delivered   │  │  skip_initiative │
     │  │ count >= 1  │  │  called          │
     │  └──────┬──────┘  └──┬───────────────┘
     │         │            │
     │         │            │   model MUST
     │         │            │   call finish_turn
     │         │            │   after skip
     │         ▼            ▼
     │  ┌──────────────────────┐
     │  │ after-first-message  │
     │  │ phase                │
     │  └──────────┬───────────┘
     │             │
     │             │ model calls
     │             │ send_text or
     │             │ finish_turn
     │             │
     └─────────────┤
                   ▼
            ┌──────────┐
            │  finish  │
            │  _turn   │
            └──────────┘
```

### 7.3 关键边界

- `delivered_message_count` >= 1 的含义：已成功送达至少一条。如果 send 失败（delivery_status != delivered），不应立即切换 phase；但如果后续 model call 再次发送成功，则切换到 after-first-message。
- 实现时建议：当 `sent_messages_this_turn` 中至少有一条 `status == "delivered"` 时，phase 切换。
- 如果 forced-speak phase 中模型始终不 send（持续调用 finish_turn 而不先 send），这违反 `min_delivered_messages = 1` 的 terminal_policy。TurnEngine 应在 validate 阶段拒绝不满足最小消息数的 finish_turn。

---

## 8. 迁移映射表

### 8.1 旧名到新名完整映射

| 旧工具名 | 新工具名 | 迁移策略 | 何时拒绝旧名 |
|---------|---------|---------|-------------|
| `wechat_reply` | `send_text_message` | Phase 11 迁移期间内部分发到旧 executor | Phase 13 清理后从 native schema 删除 |
| `done` | `finish_turn` | Phase 11 迁移期间内部分发到旧 executor | Phase 13 清理后删除 |
| `skip_proactive` | `skip_initiative` | Phase 11 迁移期间内部分发到旧 executor | Phase 13 清理后删除 |
| `schedule_wake_up` | `schedule_user_reminder` | Phase 11 迁移期间内部分发到旧 executor | Phase 13 清理后删除 |
| `send_wechat_messages` | (无新名) | 已是 deprecated，native_tool_call_eligible=False | 已拒绝（返回 PROTOCOL_RETRY） |
| `skip_reply` | (无新名) | 已是 deprecated，native_tool_call_eligible=False | 已拒绝（返回 PROTOCOL_RETRY） |

### 8.2 迁移适配器行为

在 Phase 11 期间，ToolExposurePolicy 应暴露**新名**给模型。模型返回新名工具调用后，ToolExecutor 内部通过 adapter 映射：

```python
# 迁移 adapter（Phase 11 内存在，Phase 13 删除）
_MIGRATION_MAP: dict[str, str] = {
    "send_text_message": "wechat_reply",
    "finish_turn": "done",
    "skip_initiative": "skip_proactive",
    "schedule_user_reminder": "schedule_wake_up",
}
```

但 **prompt contract block 和 native schemas 中永远只出现新名**。旧名不出现在 `exposed_tool_names`、`native_tool_schemas` 或 `tool_contract_block` 中。

### 8.3 旧名拒绝策略

| 旧名 | 模型能否调用 | 系统如何响应 |
|-----|------------|-------------|
| `send_wechat_messages` | 若 prompt 泄露旧名，模型调用 | `tool_not_exposed` → rejected_tool_call |
| `skip_reply` | 若 prompt 泄露旧名，模型调用 | `tool_not_exposed` → rejected_tool_call |
| `wechat_reply` | Phase 11 前：能（它是当前 exposed 名） Phase 11+：不能 | Phase 13：`tool_not_exposed` |
| `done` | Phase 11 前：能 Phase 11+：不能 | Phase 13：`tool_not_exposed` |
| `skip_proactive` | Phase 11 前：能 Phase 11+：不能 | Phase 13：`tool_not_exposed` |
| `schedule_wake_up` | Phase 11 前：能 Phase 11+：不能 | Phase 13：`tool_not_exposed` |

---

## 9. 伪代码：`resolve()` 方法

```python
class ToolExposurePolicy:
    """本模块的唯一公有类。

    一次 resolve() 调用产生一个 ToolExposure 对象。
    调用方（TurnEngine）在每轮 CALL_MODEL 前调用一次。
    当 phase 在本轮内变化时（如发送一条消息后），需要重新 resolve。
    """

    def __init__(self, tool_registry: "ToolRegistry", runtime_store: "RuntimeStore"):
        self._registry = tool_registry
        self._store = runtime_store  # 用于读取 enabled_check 和功能开关

    def resolve(self, request: ToolExposureRequest) -> ToolExposure:
        """主入口：根据 request 决定本轮模型可见工具。

        Returns:
            ToolExposure: 包含 exposed_tool_names、native_tool_schemas、
                          tool_contract_block、denied_tools 的完整决策对象。

        Raises:
            ToolExposureBuildError: 当 schema 与 executor 不一致、无法安全构建时。
        """
        # ── Step 1: 确定 phase ──
        phase = self._determine_phase(request)

        # ── Step 2: 根据 turn_type + user_intent + phase 计算工具候选集 ──
        candidates = self._compute_candidate_tools(request, phase)

        # ── Step 3: get_time 专项决策 ──
        if not self._should_expose_get_time(request):
            candidates.discard("get_time")
            self._add_denied("get_time", "当前时间已通过系统上下文注入", "用户询问其他时区时间")

        # ── Step 3b: get_weather 专项决策 ──
        if not self._should_expose_get_weather(request):
            candidates.discard("get_weather")
            self._add_denied("get_weather", "get_weather 仅在用户有天气意图时暴露", "用户询问天气或提及地点+天气")

        # ── Step 4: 构造 denied_tools 列表 ──
        # 所有 registry 中存在但不在 candidates 中的工具，记录 denied 原因
        denied = self._build_denied_list(candidates, request)

        # ── Step 5: 获取 schema + 验证 executor ──
        exposed = sorted(candidates)  # 按工具名稳定排序
        schemas = self._build_native_schemas(exposed)
        # 验证：每个 schema 对应的工具在 registry 中必须有 executor
        for s in schemas:
            if not self._registry.has_executor(s.name):
                raise ToolExposureBuildError(
                    reason=f"executor_missing: {s.name}",
                    details={"tool_name": s.name}
                )

        # ── Step 6: 生成 tool_contract_block ──
        contract_block = self._build_contract_block(exposed, schemas, phase, request)

        # ── Step 7: 验证一致性 ──
        self._validate_consistency(exposed, schemas, contract_block)

        # ── Step 8: 返回 ──
        return ToolExposure(
            exposure_id=_generate_exposure_id(),
            turn_id=request.current_turn_local_state.turn_id if request.current_turn_local_state else "",
            phase=phase,
            exposed_tool_names=exposed,
            native_tool_schemas=schemas,
            tool_contract_block=contract_block,
            denied_tools=denied,
            reasons=self._build_reasons(exposed, denied, phase, request),
        )

    # ── Private helpers ──

    def _determine_phase(self, request: ToolExposureRequest) -> str:
        """根据 turn_type + initiative_state + current_turn_local_state 确定 phase。"""
        if request.turn_type == TurnType.RELATIONSHIP_PROACTIVE:
            # 检查是否已经发送过消息
            sent_count = (
                request.current_turn_local_state.delivered_message_count
                if request.current_turn_local_state else 0
            )
            if sent_count > 0:
                return "after_first_message"

            # 检查上次决策
            if (
                request.initiative_state is not None
                and request.initiative_state.last_decision == "skipped"
            ):
                return "forced_speak"

            return "skippable"

        if request.turn_type == TurnType.USER_REMINDER_DUE:
            return "reminder_delivery"

        if request.turn_type == TurnType.MEMORY_MANAGEMENT:
            return "memory_management"

        if request.turn_type == TurnType.ADMIN_MANUAL_TRIGGER:
            return "admin"

        return "normal_chat"

    def _compute_candidate_tools(
        self, request: ToolExposureRequest, phase: str
    ) -> set[str]:
        """计算本轮候选工具集（不含 get_time 专项判断）。"""
        tt = request.turn_type

        # ── 基础输出工具 ＋ 通用终态 ──
        tools = {"send_text_message", "finish_turn"}

        if tt == TurnType.USER_CHAT or tt == TurnType.USER_MEDIA_CHAT:
            # get_weather 不默认暴露，由 _should_expose_get_weather 条件控制

            # 用户要求提醒
            if request.user_intent == "request_reminder":
                tools.add("schedule_user_reminder")

            # 用户要求图片
            if request.user_intent == "request_image" and request.trigger_source == "user":
                tools.add("generate_image_message")
                tools.add("send_image_message")
                tools.add("send_sticker")

            # 用户说"别记这个"
            if request.user_intent == "forget_directive":
                tools.add("apply_forget_directive")

            # get_time 和 get_weather 由各自的专项函数统一处理
            tools.add("get_time")   # 临时加入，_should_expose_get_time 决定是否移除
            tools.add("get_weather")  # 临时加入，_should_expose_get_weather 决定是否移除

        elif tt == TurnType.USER_REMINDER_REQUEST:
            tools.add("schedule_user_reminder")
            # get_weather 不默认暴露
            tools.add("get_weather")  # 临时加入，条件判断决定是否移除

        elif tt == TurnType.USER_REMINDER_DUE:
            tools.add("mark_reminder_delivered")

        elif tt == TurnType.RELATIONSHIP_PROACTIVE:
            if phase == "skippable":
                tools.add("skip_initiative")
            # 注意：forced_speak 和 after_first_message 不加 skip_initiative
            # 永远不加 image/sticker 工具
            # 永远不加 get_time/get_weather（主动消息只需发文字）
            # get_time 通过 step 3 也会被移除

        elif tt == TurnType.SCHEDULED_RITUAL:
            # get_weather 不默认暴露，仅当用户历史中有天气关注习惯时由条件判断暴露
            tools.add("get_weather")  # 临时加入，_should_expose_get_weather 决定是否移除

        elif tt == TurnType.MEMORY_MANAGEMENT:
            tools.add("apply_forget_directive")

        elif tt == TurnType.ADMIN_MANUAL_TRIGGER:
            # 默认同 user_chat，不默认加 get_weather
            tools.add("get_weather")  # 临时加入，条件判断决定是否移除

        else:
            # fallback: 最小安全集（不含 get_time/get_weather）
            pass

        return tools

    def _should_expose_get_time(self, request: ToolExposureRequest) -> bool:
        """get_time 专项决策（见第 6 节）。"""
        if not request.system_time_available:
            return True
        if request.turn_type in (TurnType.USER_REMINDER_REQUEST, TurnType.USER_REMINDER_DUE):
            return True
        if request.runtime_config and request.runtime_config.get("user_has_timezone_query"):
            return True
        return False

    def _build_native_schemas(self, exposed: list[str]) -> list[NativeToolSchema]:
        """从 ToolRegistry 中提取 exposed tools 的 native schema。"""
        schemas = []
        for name in exposed:
            tool_def = self._registry.get(name)
            if tool_def is None:
                raise ToolExposureBuildError(
                    reason=f"registry_missing: {name}",
                    details={"tool_name": name}
                )
            schemas.append(NativeToolSchema(
                name=name,
                description=tool_def.description,
                parameters_schema=self._registry.build_parameters_schema(name),
                terminal=self._registry.is_terminal(name),
                side_effect=self._registry.get_side_effect(name),
                category=self._registry.get_category(name),
            ))
        return schemas

    def _build_contract_block(
        self, exposed: list[str], schemas: list[NativeToolSchema],
        phase: str, request: ToolExposureRequest
    ) -> str:
        """生成注入 prompt 的工具说明文本。

        必须只描述 exposed 中的工具，并根据 phase 添加专门的约束说明。
        """
        # 实现细节：遍历 schemas，为每个工具生成一行说明。
        # 根据 phase 在开头添加约束：
        #  - forced_speak: "本轮是上次跳过后的强制开口回合，你不能跳过。必须至少发送一条消息。"
        #  - skippable: "你可以通过 send_text_message 发消息，或通过 skip_initiative 声明本轮跳过。无论选择哪种，最后必须调用 finish_turn。"
        #  - after_first_message: "你已经发送了消息。可以继续发，或调用 finish_turn 结束。"
        # get_time 如果暴露: "当前本地时间已通过系统消息提供。只在需要非默认时区或时间计算时使用 get_time。"
        ...

    def _validate_consistency(
        self, exposed: list[str], schemas: list[NativeToolSchema], contract_block: str
    ) -> None:
        """验证 INV-1（prompt contract 与 native schema 一致）。

        Raises:
            ToolExposureBuildError: 如果不一致。
        """
        schema_names = {s.name for s in schemas}
        exposed_set = set(exposed)
        if schema_names != exposed_set:
            raise ToolExposureBuildError(
                reason="prompt_schema_divergence",
                details={
                    "exposed_not_in_schema": list(exposed_set - schema_names),
                    "schema_not_in_exposed": list(schema_names - exposed_set),
                }
            )
        # 验证 contract block 中的工具名与 exposed 一致
        ...

    def _build_denied_list(
        self, candidates: set[str], request: ToolExposureRequest
    ) -> list[DeniedTool]:
        """为所有 registry 中存在但不在此轮 candidates 中的工具生成 DeniedTool。"""
        denied = []
        all_registered = self._registry.names()
        for name in all_registered:
            if name not in candidates:
                reason, would_if = self._deny_reason(name, request)
                denied.append(DeniedTool(name, reason, would_if))
        return denied

    def _deny_reason(
        self, tool_name: str, request: ToolExposureRequest
    ) -> tuple[str, str | None]:
        """获取某个工具被 deny 的原因。"""
        # 按工具类型返回具体原因
        if tool_name in ("generate_image_message", "send_image_message", "send_sticker"):
            if request.turn_type == TurnType.RELATIONSHIP_PROACTIVE:
                return ("主动消息禁止发图片/表情", "用户回合且用户明确要求图片时")
            return ("用户未要求图片", "user_intent=request_image")
        if tool_name == "skip_initiative":
            if request.turn_type != TurnType.RELATIONSHIP_PROACTIVE:
                return ("非主动回合不可跳过", "relationship_proactive turn")
            return ("当前 phase 不允许跳过", "skippable phase")
        if tool_name == "schedule_user_reminder":
            return ("非提醒请求回合", "user_reminder_request turn")
        if tool_name == "mark_reminder_delivered":
            return ("非提醒到期回合", "user_reminder_due turn")
        if tool_name == "apply_forget_directive":
            return ("非记忆管理回合", "memory_management turn 或 forget_directive intent")
        return ("由当前 turn 配置决定不暴露", None)
```

---

## 10. 失败路径

### 10.1 工具有 schema 但无 executor

```text
条件：_build_native_schemas 时，registry.get(name) 返回 None，
      或 registry 中有注册但 executor 未就绪。

行为：抛出 ToolExposureBuildError(reason="executor_missing", details={"tool_name": name})。
      调用方（TurnEngine）捕获此异常后：
        1. 不调用模型（因为暴露的工具不可执行）。
        2. 记录 diagnostics。
        3. 以安全终态结束本轮（不产生 user-visible 副作用）。
        4. 如果可能，使用硬兜底文案通知用户"我卡住了"。
```

### 10.2 prompt contract 与 native schema 不一致

```text
条件：_validate_consistency 检测到 exposed_tool_names 与 native_tool_schemas
      的 name 集合不匹配。

行为：抛出 ToolExposureBuildError(reason="prompt_schema_divergence", details={...})。
      处理同上。
```

### 10.3 未知 turn_type

```text
条件：request.turn_type 不在 TurnType 枚举中。

行为：不要崩溃。默认使用 user_chat 的最小安全集（
      send_text_message, finish_turn；不含 get_time/get_weather）。
      记录 warning 日志和 diagnostics。
      在 reasons 中添加："unknown_turn_type_fallback: <原始值> → treated as user_chat"。
```

### 10.4 initiative_state 缺失但 turn_type 为 RELATIONSHIP_PROACTIVE

```text
条件：request.turn_type == RELATIONSHIP_PROACTIVE 但
      request.initiative_state is None。

行为：默认视为首次主动（phase = "skippable"，last_decision = None）。
      记录 warning：initiative_state_missing_defaulting_to_skippable。
      注意：这只应发生在系统刚启动或 initiative_state 未初始化的边缘情况。
```

### 10.5 runtime_config 缺失

```text
条件：request.runtime_config is None。

行为：使用硬编码安全默认值：
      - features.image_generation = True（但只限用户请求时暴露）
      - features.weather = True（表示 get_weather 工具在 registry 中存在，但**不作为默认工具暴露**，仅在用户有天气意图时由条件规则暴露）
      - user_has_timezone_query = False
      记录 info 日志：runtime_config_missing_using_defaults。
```

### 10.6 模型请求调用未暴露工具

```text
条件：模型返回的 tool_call 的 name 不在 exposed_tool_names 中。

行为：这不是 ToolExposurePolicy 的错误，而是 ToolExecutor 的职责。
      ToolExecutor 应：
        1. 不执行该工具。
        2. 返回 ToolExecutionResult(status="rejected", reason="tool_not_exposed")。
        3. TurnEngine 将此作为工具结果回灌模型，要求模型改用可用工具。
        4. 如果模型反复调用未暴露工具，在重试预算耗尽后安全结束。
```

---

## 11. 禁止行为（至少 10 条）

以下是实现者绝对不能做的事：

**PROHIBIT-1**: 不经过 `ToolExposurePolicy.resolve()` 直接构建 `native_tool_schemas` 或 `tool_contract_block`。
> 任何代码路径不得绕过 policy 层直接拼 tools。`BuildContextRequest.tool_exposure` 必须是 `ToolExposure` 对象，不能是手动拼的字典。

**PROHIBIT-2**: 在 `tool_contract_block` 中提到未暴露的工具。
> 如果 `generate_image_message` 不在 exposed 集合中，prompt 中绝对不能出现"你可以使用 generate_image_message 来生成图片"这样的文本。

**PROHIBIT-3**: 在注册表中注册了新工具但不更新 policy 决策逻辑。
> 新增工具到 ToolRegistry 后，必须同步更新 `_compute_candidate_tools()` 中的决策规则，并补充测试。

**PROHIBIT-4**: 硬编码 turn_type 判断散落各处。
> `ToolExposurePolicy._compute_candidate_tools()` 是判断"哪些工具暴露"的唯一位置。不要在 ContextBuilder、TurnEngine、main.py 或 prompt 文件中做工具选择的 if-else。

**PROHIBIT-5**: 以 `get_time` 的 presence 来判断是否注入 SystemTimeBlock。
> 这是反向依赖。SystemTimeBlock 的注入情况必须先于 `ToolExposureRequest.system_time_available`，不能反过来由工具暴露决定上下文构建。

**PROHIBIT-6**: 在 `tool_contract_block` 中写死固定的工具列表文本。
> 不能 hardcode 一个固定字符串 "你可以使用 send_text_message 和 finish_turn"。每次 resolve 必须根据 exposed tools 动态生成 contract block。即使 exposed tools 相同的两轮，block 文本也应根据 phase 不同而包含不同的约束说明。

**PROHIBIT-7**: 在非主动回合暴露 `skip_initiative`。
> 即使通过某种配置传入，也必须在 `_compute_candidate_tools` 中防御：非 RELATIONSHIP_PROACTIVE 的 turn 一律不加入 `skip_initiative`。

**PROHIBIT-8**: 在 resolve 失败时静默 fallback 到旧工具集而不记录错误。
> 任何 ToolExposureBuildError 必须被记录到 diagnostics，包含完整的 reason 和 details。

**PROHIBIT-9**: 把 deprecated 工具 (`send_wechat_messages`, `skip_reply`) 暴露在 native_tool_schemas 中。
> 这些工具的 `native_tool_call_eligible` 标记为 False，但仅靠这个标记不够。`_build_native_schemas` 在构建时必须显式过滤掉 deprecated tools。

**PROHIBIT-10**: 在 tool_contract_block 中泄露内部工具名。
> 不能在 prompt 中写 "内部名称 send_text_message 对应微信发送" 之类的渠道信息。prompt 应使用抽象描述："发送一条文字消息给用户"。

**PROHIBIT-11**: 用 `tool_contract_block` 代替 `terminal_policy`。
> `tool_contract_block` 负责告诉模型有哪些工具可用及约束。`terminal_policy` 负责告诉模型本轮怎么才算结束。它们是独立的 context block。不要把终态规则混进工具说明。

---

## 12. 至少 10 个具体输入输出例子

### 例 1：普通用户文字聊天

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.USER_CHAT,
    trigger_source="user",
    current_turn_local_state=None,  # 首轮调用
    user_intent="chat",
    media_intent=None,
    initiative_state=None,
    reminder_state=None,
    runtime_config={"features": {"weather": True, "image_generation": True}},
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="normal_chat",
    exposed_tool_names=["finish_turn", "send_text_message"],
    # get_time NOT exposed (SystemTimeBlock available, no timezone query)
    # get_weather NOT exposed (user_intent != request_weather)
    # image tools NOT exposed (user_intent != request_image)
    # skip_initiative NOT exposed (not proactive turn)
    denied_tools=[
        DeniedTool("get_time", "当前时间已通过系统上下文注入", "用户询问其他时区时间"),
        DeniedTool("generate_image_message", "用户未要求图片", "user_intent=request_image"),
        DeniedTool("send_image_message", "用户未要求图片", "user_intent=request_image"),
        DeniedTool("send_sticker", "用户未要求图片/表情", "user_intent=request_image"),
        DeniedTool("skip_initiative", "非主动回合不可跳过", "relationship_proactive turn"),
        DeniedTool("schedule_user_reminder", "非提醒请求回合", "user_reminder_request turn"),
        DeniedTool("mark_reminder_delivered", "非提醒到期回合", "user_reminder_due turn"),
        DeniedTool("apply_forget_directive", "非记忆管理回合", "memory_management turn 或 forget_directive intent"),
    ],
)
```

### 例 2：用户要求生成图片

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.USER_CHAT,
    trigger_source="user",
    user_intent="request_image",
    media_intent=None,
    initiative_state=None,
    runtime_config={"features": {"weather": True, "image_generation": True}},
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="normal_chat",
    exposed_tool_names=[
        "finish_turn",
        "generate_image_message",  # Y -- user requested
        "send_image_message",      # Y -- user requested
        "send_sticker",            # Y -- user requested
        "send_text_message",
    ],
    # get_time NOT exposed (no timezone query)
    # get_weather NOT exposed (user_intent != request_weather)
    # skip_initiative NOT exposed
    denied_tools=[...],
)
```

### 例 3：用户询问其他时区时间（get_time 暴露）

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.USER_CHAT,
    trigger_source="user",
    user_intent="chat",
    initiative_state=None,
    runtime_config={"features": {"weather": True}, "user_has_timezone_query": True},
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="normal_chat",
    exposed_tool_names=[
        "finish_turn",
        "get_time",       # Y -- user_has_timezone_query
        "get_weather",
        "send_text_message",
    ],
    # tool_contract_block 中包含:
    # "get_time: 获取指定时区的当前时间。当前本地时间已通过系统消息提供，
    #  只在需要非默认时区或时间计算时使用。"
)
```

### 例 4：用户问"现在几点"（get_time 不暴露）

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.USER_CHAT,
    trigger_source="user",
    user_intent="chat",
    # runtime_config 中无 user_has_timezone_query 标记
    runtime_config={"features": {"weather": True}},
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="normal_chat",
    exposed_tool_names=["finish_turn", "send_text_message"],
    # get_time NOT exposed
    # get_weather NOT exposed
    denied_tools=[
        DeniedTool("get_time", "当前时间已通过系统上下文注入，不需要模型查询",
                   "用户询问其他时区时间，或进行时间计算"),
        ...
    ],
)
```

### 例 5：主动回合，首次机会（skippable phase）

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.RELATIONSHIP_PROACTIVE,
    trigger_source="scheduler",
    current_turn_local_state=None,
    user_intent="unknown",
    initiative_state=InitiativeState(
        last_decision=None,       # 首次主动（或上次已开口）
        consecutive_skip_count=0,
        intensity_profile="normal",
    ),
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="skippable",
    exposed_tool_names=["finish_turn", "send_text_message", "skip_initiative"],
    # skip_initiative IS exposed (skippable phase)
    # image tools NOT exposed (active message)
    # get_time NOT exposed
    # get_weather NOT exposed
    # tool_contract_block 中包含:
    # "你可以通过 send_text_message 发送一条消息，或通过 skip_initiative 声明本轮跳过。"
    # "无论选择哪种，最后必须调用 finish_turn(next_wake_minutes=...) 并指定下次唤醒分钟数。"
)
```

### 例 6：主动回合，上次已跳过（forced-speak phase）

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.RELATIONSHIP_PROACTIVE,
    trigger_source="scheduler",
    current_turn_local_state=None,
    user_intent="unknown",
    initiative_state=InitiativeState(
        last_decision="skipped",
        consecutive_skip_count=1,
        intensity_profile="normal",
    ),
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="forced_speak",
    exposed_tool_names=["finish_turn", "send_text_message"],
    # skip_initiative NOT exposed -- forced speak
    # tool_contract_block 中包含:
    # "本轮是上次跳过后的强制开口回合。你不能跳过。必须至少发送一条自然消息。"
)
```

### 例 7：主动回合，已发送一条消息后（after-first-message phase）

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.RELATIONSHIP_PROACTIVE,
    trigger_source="scheduler",
    current_turn_local_state=CurrentTurnLocalState(
        turn_id="t_abc",
        delivered_message_count=1,
        sent_messages_this_turn=[TurnLocalSentMessage(
            message_id="m_1", tool_call_id="tc_1",
            text="在干嘛呢~", status="delivered",
        )],
        ...
    ),
    user_intent="unknown",
    initiative_state=InitiativeState(
        last_decision="spoke",
        consecutive_skip_count=0,
        intensity_profile="normal",
    ),
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="after_first_message",
    exposed_tool_names=["finish_turn", "send_text_message"],
    # skip_initiative NOT exposed (already sent message)
    # tool_contract_block 中包含:
    # "你已经发送了 1 条消息。可以继续发送，或调用 finish_turn 结束本轮。"
)
```

### 例 8：用户提醒请求 turn

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.USER_REMINDER_REQUEST,
    trigger_source="user",
    current_turn_local_state=None,
    user_intent="request_reminder",
    initiative_state=None,
    reminder_state=None,
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="normal_chat",
    exposed_tool_names=[
        "finish_turn",
        "get_time",            # Y -- absolute time validation needed
        "get_weather",
        "schedule_user_reminder",  # Y -- core tool
        "send_text_message",
    ],
    # skip_initiative NOT exposed
)
```

### 例 9：用户提醒到期 turn

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.USER_REMINDER_DUE,
    trigger_source="scheduler",
    current_turn_local_state=None,
    user_intent="unknown",
    initiative_state=None,
    reminder_state=ReminderState(reminder_id="r_001", kind="user_reminder"),
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="reminder_delivery",
    exposed_tool_names=[
        "finish_turn",
        "mark_reminder_delivered",  # Y -- core tool
        "send_text_message",
    ],
    # get_time NOT exposed (不需要，提醒到期是 scheduler 驱动的)
    # skip_initiative NOT exposed (提醒不能跳过)
    # tool_contract_block 中包含:
    # "这是一个用户授权提醒。发送消息后必须调用 mark_reminder_delivered 标记送达。"
)
```

### 例 10：固定仪式 turn（早安）

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.SCHEDULED_RITUAL,
    trigger_source="scheduler",
    current_turn_local_state=None,
    user_intent="unknown",
    initiative_state=None,
    reminder_state=None,
    runtime_config={"features": {"weather": True}},
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="normal_chat",
    exposed_tool_names=["finish_turn", "send_text_message"],
    # skip_initiative NOT exposed (仪式不跳过)
    # image tools NOT exposed
    # get_time NOT exposed
    # get_weather NOT exposed（无天气关注习惯，按 W9 规则）
)
```

### 例 11：记忆管理 turn（"别记这个"）

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.MEMORY_MANAGEMENT,
    trigger_source="user",
    current_turn_local_state=None,
    user_intent="forget_directive",
    initiative_state=None,
    reminder_state=None,
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="memory_management",
    exposed_tool_names=[
        "apply_forget_directive",  # Y -- core tool
        "finish_turn",
        "send_text_message",
    ],
    # get_time NOT exposed
    # get_weather NOT exposed
    # tool_contract_block 中包含:
    # "用户要求忘记某些内容。使用 apply_forget_directive 应用遗忘指令，然后用自然语气回复。"
)
```

### 例 12：管理员手动触发 turn

**输入：**
```python
ToolExposureRequest(
    turn_type=TurnType.ADMIN_MANUAL_TRIGGER,
    trigger_source="admin",
    current_turn_local_state=None,
    user_intent="unknown",
    initiative_state=None,
    runtime_config={"features": {"weather": True}, "admin_dry_run": False},
    system_time_available=True,
)
```

**输出：**
```python
ToolExposure(
    phase="admin",
    exposed_tool_names=["finish_turn", "send_text_message"],
    # get_time NOT exposed (admin won't need it)
    # get_weather NOT exposed (admin won't need it)
    # skip_initiative NOT exposed
    # image tools NOT exposed
    # 注意：source=admin 的消息有特殊标记，不污染用户历史
)
```

---

## 13. 当前代码迁移点

### 13.1 `agent/tool_registry.py`

**现状**：
- 工具注册使用旧名：`wechat_reply`, `done`, `skip_proactive`, `schedule_wake_up`
- 有 deprecated 工具：`send_wechat_messages`, `skip_reply`
- `ToolRegistry.get_native_tool_specs()` 按 `native_tool_call_eligible` 过滤，无 turn-type 感知

**需要改动**：
1. 新增新工具名注册项。Phase 11 期间新旧名共存，新名的 executor 先 alias 到旧 handler。
2. `ToolRegistry` 增加 `has_executor(name)`、`is_terminal(name)`、`get_side_effect(name)`、`get_category(name)` 方法。
3. `build_parameters_schema(name)` 方法：从 ToolDef.args_schema 生成标准 JSON Schema。
4. 在 `_build_default_registries()` 中新增所有新名工具注册项。

### 13.2 `agent/tool_classifier.py`

**现状**：
- `classify_single_tool_call()` 通过 `registry.get(tool_name)` 判断工具是否已知
- 使用 `registry_category`（fact_tool/action_tool/terminal_tool/state_tool）
- 有 `_DEPRECATED_HINTS` 字典

**需要改动**：
1. 新增 `classify_against_exposure(tool_call, tool_exposure)` 方法：不仅判断工具是否注册，还判断是否在本轮 `exposed_tool_names` 中。
2. 当工具未暴露时，返回 `is_exposed=False` 和 `rejection_reason`。
3. 更新 `_DEPRECATED_HINTS` 加入所有旧名映射。

### 13.3 `agent/chat_agent.py`

**现状**：
- `self.native_allowed_tools` 从配置读入（`config.yaml` 的 `model.native_tool_calls.allowed_tools`）
- `_build_native_tool_specs()` 直接使用 `self.native_allowed_tools` 生成 provider tools
- Proactive 回合有 `_get_proactive_allowed_tools()` 进行 phase-specific 过滤
- `classify_one_tool_call()` 中有 `skip_proactive` 的 phase 验证

**需要改动**：
1. 用 `ToolExposurePolicy.resolve()` 替换 `_build_native_tool_specs()` 和 `_get_proactive_allowed_tools()` 的调用。
2. 每次 `CALL_LLM` 前调用 `resolve()` 获取新的 `ToolExposure`。
3. 不再从 `self.native_allowed_tools` 直接生成 tools，而是通过 policy。
4. 移除分散在 chat_agent 中的 phase-specific 工具过滤逻辑（如 `skip_proactive` 的 phase 检查）。

### 13.4 `main.py`

**现状**：
- `allowed_tools` 在 `config.yaml` 中配置，默认 `["get_time", "get_weather", "wechat_reply", "done"]`
- `build_system_prompt_for_turn()` 从 config 读取 `allowed_tools` 并传给 `build_chat_system_prompt()`
- `native_allowed_tools_override` 参数在调用 chain 中传递

**需要改动**：
1. 移除 `build_system_prompt_for_turn()` 中的 `extra_tools` 参数概念——工具不再是由调用方"追加"的。
2. `ChatAgent` 构造时不再需要 `native_allowed_tools` 参数；改为接收 `ToolExposurePolicy` 实例。
3. `config.yaml` 中的 `allowed_tools` 字段降级为 feature flag（如 `features.image_generation`），不再直接是工具列表。
4. 所有 `native_allowed_tools_override` 逻辑由 `ToolExposureRequest.runtime_config` 替代。

### 13.5 Prompt 文件

**现状**：
- `prompts/tool_policy.md` 中使用了旧工具名
- `prompts/base_protocol.md` 中有工具规则描述

**需要改动**：
1. 不再在 prompt 文件中写死工具名或工具说明。
2. `tool_policy.md` 降解为通用规则（如"你必须通过工具调用发送消息，不能只输出自然语言"），不列出具体工具名。
3. 所有具体工具名和说明由 `ToolExposurePolicy._build_contract_block()` 动态生成。

---

## 14. 必须测试的用例列表（至少 15 条）

### 测试文件建议：`tests/test_tool_exposure_policy.py`

**TC-1: user_chat 默认工具集**
> `turn_type=USER_CHAT` 且 `system_time_available=True` 且无特殊 intent（无 timezone、无 weather、无 image、无 reminder 意图）时，暴露 `send_text_message`, `finish_turn`。不暴露 `get_time`, `get_weather`, `skip_initiative`, 图片工具。get_time 和 get_weather 均非默认工具，仅在用户有对应意图时由条件规则暴露。

**TC-2: user_chat 请求图片**
> `user_intent=request_image` 时，额外暴露 `generate_image_message`, `send_image_message`, `send_sticker`。

**TC-3: get_time 暴露——用户查询时区**
> `runtime_config.user_has_timezone_query=True` 时，`get_time` 出现在 `exposed_tool_names`。

**TC-4: get_time 不暴露——正常聊天**
> `user_intent=chat`, `system_time_available=True`, 无 timezone query 标记时，`get_time` 在 `denied_tools` 中，不在 `exposed_tool_names`。

**TC-5: relationship_proactive skippable phase**
> `last_decision=None`（首次主动）时，暴露 `send_text_message`, `skip_initiative`, `finish_turn`。

**TC-6: relationship_proactive forced-speak phase**
> `last_decision=skipped` 时，不暴露 `skip_initiative`。`exposed_tool_names` 只有 `send_text_message`, `finish_turn`。

**TC-7: relationship_proactive after-first-message phase**
> `delivered_message_count=1` 时，phase=`after_first_message`，不暴露 `skip_initiative`。

**TC-8: 主动回合永远不暴露图片工具**
> 任何 `turn_type=RELATIONSHIP_PROACTIVE` 下，`generate_image_message`, `send_image_message`, `send_sticker` 都在 `denied_tools` 中。

**TC-9: user_reminder_request 暴露 schedule**
> `turn_type=USER_REMINDER_REQUEST` 时，`schedule_user_reminder` 在 `exposed_tool_names`。

**TC-10: user_reminder_due 暴露 mark_reminder_delivered**
> `turn_type=USER_REMINDER_DUE` 时，`mark_reminder_delivered` 在 `exposed_tool_names`，不暴露 `skip_initiative`。

**TC-11: 非主动回合不暴露 skip_initiative**
> 测试所有 `turn_type != RELATIONSHIP_PROACTIVE` 的值，确保 `skip_initiative` 都在 `denied_tools` 中。

**TC-12: prompt contract 与 native schema 一致**
> 对每个 test case 验证：`tool_contract_block` 中提及的工具名集合 == `native_tool_schemas` 中的 name 集合 == `exposed_tool_names`。

**TC-13: executor_missing 抛出 BuildError**
> 当 `exposed_tool_names` 中包含一个 registry 中无 executor 的工具名时，`resolve()` 抛出 `ToolExposureBuildError(reason="executor_missing")`。

**TC-14: memory_management 暴露 apply_forget_directive**
> `turn_type=MEMORY_MANAGEMENT` 或 `user_intent=forget_directive` 时，`apply_forget_directive` 在 `exposed_tool_names`。

**TC-15: 未知 turn_type fallback 到 user_chat**
> `turn_type` 为未知字符串时，default 到 `send_text_message`, `finish_turn`（不含 get_time/get_weather，与 user_chat 默认集一致）。不崩溃。日志包含 `unknown_turn_type_fallback`。

**TC-16: runtime_config 缺失使用安全默认值**
> `runtime_config=None` 时不崩溃，默认 `features.image_generation=True`, `features.weather=True`, `user_has_timezone_query=False`。

**TC-17: finish_turn 在所有 turn type 下暴露**
> 遍历所有 `TurnType` 枚举值，确保每个 case 的 `exposed_tool_names` 都包含 `"finish_turn"`。

**TC-18: schedule_user_reminder 只在提醒请求回合暴露**
> 验证 `USER_CHAT`（chat intent）、`RELATIONSHIP_PROACTIVE` 等非提醒回合中 `schedule_user_reminder` 在 `denied_tools` 中。

**TC-19: get_time 在 system_time 不可用时对用户回合暴露，后台回合 safe_fail**
> - 对于用户回合（`user_chat`, `user_media_chat`, `user_reminder_request`, `memory_management`）：`system_time_available=False` 时 `get_time` 在 `exposed_tool_names` 中。
> - 对于后台回合（`relationship_proactive`, `user_reminder_due`, `scheduled_ritual`, `admin_manual_trigger`）：`system_time_available=False` 时 **不暴露 `get_time`**，应进入 safe terminal（无模型调用），因为后台回合不应依赖模型调用 get_time 来修复系统状态。

**TC-20: denied_tools 中每个条目有 non-empty reason**
> 对所有 test case 验证：`denied_tools` 中的每个 `DeniedTool` 的 `reason` 不为空字符串。

---

## 15. 完成定义

本规格的实现完成标志：

1. `agent/tool_exposure_policy.py` 文件存在，包含 `ToolExposurePolicy` 类和 `resolve()` 方法。
2. `resolve()` 接受 `ToolExposureRequest`，返回 `ToolExposure`。
3. 所有不变量（第 3 节）在全部 test cases 下成立。
4. 所有禁止行为（第 11 节）被遵守。
5. 第 14 节列出的 20 条测试全部通过。
6. 现有 `tests/test_chat_agent.py`（91 tests）和 `tests/test_tool_registry.py` 在迁移 adapter 存在的前提下不退化（或同步更新为新工具名）。
7. Phase 13 清理完成后，核心代码中不再出现 `wechat_reply`、`done`、`skip_proactive`、`send_wechat_messages`、`skip_reply` 字符串暴露给模型。
