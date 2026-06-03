# 关系型陪伴系统架构设计决策

> 日期：2026-06-02
> 状态：设计决策草案
> 前置文档：`docs/companion-architecture-requirements.zh.md`
> 目的：把已确认的产品需求翻译成底层架构设计，不再停留在“拆文件”层面。

---

## 0. 总原则

这个系统的架构目标不是做一个普通 AI 助手，而是做一个长期关系型陪伴对象。

因此架构必须围绕四件事设计：

1. **关系连续性**：它要像一个长期熟悉用户的人。
2. **主动性**：它能主动找用户，但主动行为必须可控。
3. **可审计上下文**：每次模型为什么这样说，系统必须能查。
4. **选择性记忆**：它不能像数据库一样全知全记，要像恋人一样选择性记住、适度遗忘。

模型不是整个系统。模型只是系统里的一个决策器。

系统负责：

- 能不能开口。
- 本轮是什么类型。
- 该给模型哪些上下文。
- 该暴露哪些工具。
- 输出能不能发。
- 哪些记忆能写入。

模型负责：

- 用什么语气说。
- 说几条。
- 是否跳过。
- 下一次什么时候再尝试主动。
- 是否调用当前可用工具。

### 0.1 用白话描述最终系统怎么跑

最终系统不是“收到一句话就把所有东西塞给模型，然后等模型自由发挥”。

它更像一个有固定流程的运行机器：

```text
外部事件进入系统
-> 系统识别这是什么事件
-> 系统整理成统一的输入批次
-> ContextBuilder 构建本轮上下文
-> ToolExposurePolicy 决定本轮能用哪些工具
-> ProviderGateway 调用模型
-> 模型必须用结构化 JSON / native tool calls 表达决定
-> TurnEngine 执行模型选择的工具
-> 工具结果回到本轮上下文
-> 如果还没到终态，继续调用模型
-> 到终态后写历史、写记忆候选、写审计快照
```

这里最重要的是：模型不能直接决定“发不发”“写不写记忆”“什么时候再醒来”“能不能调用某个工具”。

模型只能在系统本轮允许的范围内做选择。

比如：

- 系统没有暴露图片生成工具，模型就不能生成图片。
- 系统没有暴露 skip 工具，模型就不能跳过。
- 系统要求本轮必须结束，模型就必须调用终态工具。
- 系统要求 JSON / native tool calls，模型就不能只输出一段自然语言让系统猜。

所以这个架构的核心不是“prompt 写得更长”，而是把模型关进一个明确的回合协议里。

### 0.2 一轮对话里的几个角色

为了避免后续实现混乱，先把几个角色讲清楚。

```text
InboundAdapter
  负责把外部消息变成系统能理解的输入。

TurnClassifier
  负责判断本轮是什么类型：普通聊天、主动触发、定时提醒、多模态输入等。

ContextBuilder
  负责把本轮模型需要看的东西整理出来。

ToolExposurePolicy
  负责决定本轮模型能看到哪些工具。

ProviderGateway
  负责和模型供应商通信，并强制使用结构化输出能力。

TurnEngine
  负责驱动“模型 -> 工具 -> 模型 -> 工具”的循环。

ToolExecutor
  负责真正执行工具，比如发消息、查时间、安排提醒、结束本轮。

MemorySystem
  负责在本轮结束后决定哪些内容可以进入长期记忆。

ContextSnapshotStore
  负责保存审计快照，方便以后查“模型当时到底看到了什么”。
```

这些角色不能互相抢职责。

例如：

- ContextBuilder 不负责真的发消息。
- TurnEngine 不负责自己拼 prompt。
- ToolExecutor 不负责偷偷追加模型没看到的上下文。
- MemorySystem 不负责改变本轮模型输出。
- ProviderGateway 不负责理解业务规则。

---

## 1. 模型供应商设计

### Decision

长期尽量使用一个支持多模态的主模型。

主模型尽量承担：

- 普通聊天。
- 主动消息生成。
- 图片理解。
- 语音理解。
- 工具调用决策。

图片生成可以作为单独 provider 或同 provider 的独立 endpoint。

### Reason

关系型陪伴最怕多个模型之间风格、记忆解释、关系状态理解不一致。

尽量用一个主模型的好处：

- 语气更一致。
- 对关系状态理解更连续。
- 多模态输入和文本上下文可以在同一轮里融合。
- 减少“图片模型看懂了，但聊天模型没接住”的断裂。

### Architecture requirement

即使长期尽量使用一个主模型，代码仍必须保留 provider adapter。

```text
ProviderGateway
  - chat_multimodal()
  - generate_image()
  - transcribe_or_understand_audio()
  - tool_calling_capabilities
```

原因：

- 图片生成能力可能和聊天模型不是同一个接口。
- 主模型某项能力失败时需要降级。
- 未来可能更换供应商。
- 不能把某个 provider 的 payload 格式写死进 ContextBuilder 或 TurnEngine。

### Structured output requirement

模型调用不能只靠“提示词里要求输出 JSON”。

提示词和示例可以继续保留，因为它们能帮助模型理解业务规则；但真正的结构化约束必须由 provider 能力承接：

```text
ProviderGateway
  -> response_format / JSON schema
  -> native tool calls
  -> tool schema validation
  -> retry or reject invalid model output
```

也就是说，模型每次输出必须落到系统可解析的数据结构里。

系统不能接受这种输出作为最终协议：

```text
我想回复用户：今晚早点睡。
然后我觉得下次 40 分钟后再看看。
```

系统需要的是这种可执行结构：

```json
{
  "tool_calls": [
    {
      "name": "send_text_message",
      "arguments": {
        "text": "今晚早点睡。"
      }
    }
  ]
}
```

或终态：

```json
{
  "tool_calls": [
    {
      "name": "finish_turn",
      "arguments": {
        "next_wake_minutes": 45,
        "reason": "用户已收到轻量陪伴消息，本轮结束"
      }
    }
  ]
}
```

如果模型返回的结构不合法，系统应该按固定策略处理：

1. 不执行任何可能产生外部影响的工具。
2. 记录 invalid output 到审计日志。
3. 在允许的重试预算内，用同一个上下文要求模型修正结构。
4. 重试仍失败时，以安全终态结束本轮。

### Rejected

- 把图片理解、语音理解、文本聊天长期拆成完全无关的模型：拒绝作为默认方案。
- 把 provider 细节写进业务 runtime：拒绝。

---

## 2. ContextBuilder 是否应该是唯一上下文入口

### Decision

是。ContextBuilder 应该是所有模型输入的唯一入口。

所有会调用模型的路径都必须经过 ContextBuilder：

- 普通聊天。
- 主动消息。
- 用户提醒。
- 固定仪式。
- 图片输入后的回应。
- 语音输入后的回应。
- 图片生成前的提示整理。
- 记忆抽取前后需要模型判断的场景。

### Plain logic

ContextBuilder 的作用可以理解成：每次模型开口前，系统先给它准备一份“本轮资料包”。

这份资料包不是随手拼的，而是按固定顺序组织：

```text
1. 固定系统规则
2. 固定工具规则
3. 角色设定和关系边界
4. 当前用户和 bot 的关系状态
5. 可用的长期记忆片段
6. 最近已完成的真实聊天历史
7. 本轮内已经发生但还没落入历史的 turn-local state
8. 当前输入内容
9. 本轮允许暴露的工具说明
10. 本轮必须遵守的终态规则
```

这样做的好处是：模型每次做决定前看到的东西是稳定的、可查的、可复现的。

如果以后发现模型说错话，要查的不是“模型是不是抽风”，而是：

- 它当时看到了哪些记忆。
- 它当时看到的最近历史是什么。
- 它当时被允许用哪些工具。
- 它当时有没有看到本轮已经发送过的消息。
- 它当时被要求的终态是什么。

### Why this is the best option for this system

对这个系统来说，ContextBuilder 作为唯一入口是最佳方案，原因有五个。

#### 1. 可审计

你现在最痛的问题之一是“不知道模型到底看见了什么”。

唯一入口可以保证每轮都有：

```text
ContextSnapshot
- fixed rules
- persona
- relationship state
- memory blocks
- recent history
- sent content
- media summaries
- exposed tools
- current turn payload
```

以后模型重复、忘事、发疯、语气不对，可以查输入，而不是猜。

#### 2. 工具和 prompt 一致

按需暴露工具必须和 prompt 绑定。

如果 ContextBuilder 不统一，容易出现：

- prompt 说能用某工具。
- schema 没暴露。
- executor 不存在。

唯一入口能保证 `ToolExposurePolicy` 的结果同时进入：

- native tool schema。
- tool contract block。
- debug snapshot。

#### 3. 缓存命中

固定内容左移，动态内容右移，只有 ContextBuilder 能稳定控制顺序。

```text
fixed system rules
fixed tool rules
persona
low-frequency memory
relationship state
recent history
current input
```

如果各模块自己拼 prompt，缓存结构一定会乱。

#### 4. 多模态统一

图片、语音、文字都应该变成统一的 context block。

如果 bridge 直接把图片摘要塞成一条文本，后面无法区分：

- 这是用户说的话。
- 这是图片摘要。
- 这是语音语气。
- 这是系统推断。

ContextBuilder 可以明确标注来源。

#### 5. 方便大型迁移

以后拆 `main.py`、瘦 `ChatAgent`、加记忆、加多模态，都不需要重新发明 prompt 拼接。

### Tradeoff

缺点是前期设计复杂，需要先定义数据结构。

但这不是额外复杂度，而是把原来散落在 `main.py`、`ChatAgent`、prompt 文件和 scheduler 里的隐式复杂度集中显式化。

### Non-negotiable

禁止任何 runtime 绕过 ContextBuilder 直接拼接模型输入。

### Context input and output contract

ContextBuilder 的输入不应该是一堆散落参数。

它应该接收一个明确的请求对象：

```text
BuildContextRequest
- bot_id
- user_id
- turn_id
- turn_type
- inbound_batch
- initiative_state
- reminder_state
- current_turn_local_state
- recent_history_window
- memory_query_result
- media_items
- system_time
```

它输出的也不只是 prompt 字符串，而应该包含：

```text
BuiltContext
- provider_messages
- tool_contract_block
- exposed_tool_schemas
- terminal_policy
- context_snapshot
- debug_summary
```

其中：

- `provider_messages` 给模型供应商。
- `exposed_tool_schemas` 给 native tool calls。
- `terminal_policy` 告诉 TurnEngine 本轮怎么才算结束。
- `context_snapshot` 存起来用于审计。
- `debug_summary` 给人看，方便排查。

---

## 3. 主动消息决策设计

### Decision

主动消息由系统和模型共同决策：

```text
System Gate:
  这次是否允许给模型一次主动开口机会？

Model Decision:
  如果允许，本轮说不说？说什么？下一次什么时候再唤醒？
```

主动消息只是系统里的一种 turn type。

它不能拥有一套绕过主流程的特殊实现。它仍然必须经过：

```text
TurnClassifier
-> ContextBuilder
-> ToolExposurePolicy
-> ProviderGateway
-> TurnEngine
-> ToolExecutor
-> ContextSnapshotStore
```

### Heartbeat logic in plain words

主动能力可以由一个固定心跳驱动，例如每 30 秒检查一次。

这个 30 秒不是“每 30 秒都找用户说话”，而是“每 30 秒检查一次有没有资格启动某个后台任务”。

白话流程是：

```text
每 30 秒醒一次
-> 先看主动功能总开关是否打开
-> 没打开就结束本次心跳
-> 打开后先看有没有快到期或已到期的定时任务
-> 有任务就尝试领取任务
-> 领取成功就按定时任务 turn 运行
-> 没有任务再看是否满足主动冷启动条件
-> 比如距离上次真实对话是否超过阈值
-> 不满足就结束本次心跳
-> 满足才创建主动 turn
```

这里有两个重点：

- 定时任务优先于普通主动冷启动。
- 心跳只负责“是否启动一轮”，不直接负责生成文案。

真正生成文案仍然要进入正常模型回合。

### Current desired behavior

冷启动或主动触发时：

1. 系统先判断是否允许主动。
2. 如果允许，给模型一次机会。
3. 模型可以选择说话，也可以选择不说。
4. 模型必须给出下一轮唤醒时间。
5. 这个时间必须在系统根据用户频率档位提供的范围内。
6. 如果上次主动机会模型没有开口，下一次主动机会不允许再次跳过。

### Why

这解决两个问题：

- 模型有沉默权，不会每次冷场都硬聊。
- 模型不能永远沉默，因为连续 skip 会被系统限制。

### Architecture

主动运行时需要维护：

```text
InitiativeState
- last_trigger_at
- last_spoke_at
- last_skipped_at
- consecutive_skip_count
- next_wake_after
- next_wake_before
- intensity_profile
- last_decision
```

ToolExposurePolicy 对主动回合暴露：

```text
主动可跳过阶段：
- send_text_message
- skip_initiative(reason)
- finish_turn(next_wake_minutes)

上次已经跳过后的强制开口阶段：
- send_text_message
- finish_turn(next_wake_minutes)

已发送至少一条后的结束阶段：
- send_text_message
- finish_turn(next_wake_minutes)
```

这里不再使用 `wechat_reply`、`wake_up` 这类渠道化或含混命名。

如果需要表达“告诉系统下一次什么时候醒来”，统一放进终态工具：

```text
finish_turn(next_wake_minutes)
```

也就是说，`finish_turn` 才是主动回合的正常终态工具。

`skip_initiative` 只表达“本次主动机会不发消息”。它不单独负责安排下一次醒来。模型选择跳过后，仍然必须调用 `finish_turn`。

主动可跳过阶段的固定顺序是：

```text
模型先选择：
  - send_text_message
  - 或 skip_initiative

然后必须选择：
  - finish_turn(next_wake_minutes)
```

如果模型先调用了 `send_text_message`，本轮状态变成 `spoke`。

如果模型先调用了 `skip_initiative`，本轮状态变成 `skipped`。

无论是 `spoke` 还是 `skipped`，最后都必须通过 `finish_turn` 给系统一个下一次检查时间。

这样设计比单独设计 `wake_up` 更清楚：

- `send_text_message` 负责发消息。
- `skip_initiative` 负责声明这次不说。
- `finish_turn` 负责结束本轮，并写入下次唤醒计划。

### Important

“不允许跳过”不等于必须机械发废话。

ContextBuilder 应告诉模型：

- 这是上次已经跳过后的主动机会。
- 这次必须自然开口。
- 不要重复最近内容。
- 如果没有新话题，也要用关系型方式轻轻开口，而不是工具式提醒。

### Forced speak after skip

如果上一轮主动机会选择了 `skip_initiative`，下一轮主动机会不能继续暴露 `skip_initiative`。

这时 ContextBuilder 应明确告诉模型：

```text
这是上一次已经跳过后的主动机会。
本轮不能继续跳过。
你需要自然地发送至少一条轻量消息。
发送后必须调用 finish_turn 安排下一次唤醒。
```

此时 ToolExposurePolicy 暴露：

```text
send_text_message
finish_turn
```

不暴露：

```text
skip_initiative
```

所以模型不是靠“自觉别跳过”，而是结构上看不到 skip 工具。

---

## 4. 多条消息与 turn-local context

### Decision

保留当前“发一条，再回模型继续思考”的机制。

这是适合微信气泡式聊天的。

### Clarification

你问的关键点是对的：

> 每次调用模型前已经会传最近几条真实聊天记录。那还要不要把 `send_text_message` 刚刚发的话再传给模型？会不会重复导致模型理解错误？

答案是：

**要传，但不能作为普通历史重复传。**

应该把它作为 turn-local state / tool result 传。

### Correct structure

同一轮内模型发出一条消息后，下一次 LLM call 应看到：

```text
Turn-local sent messages in this turn:
1. delivered: "..."
```

而不是把它混进 recent chat history，再让 recent history 和 tool result 重复出现。

### Why

模型需要知道它刚刚说过什么，否则容易：

- 重复同一句。
- 不知道本轮已经发了几条。
- 不知道该结束还是继续。

但如果同时在 recent history 和 tool result 里出现两遍，模型可能误解为：

- 用户重复说了这句话。
- assistant 在历史里已经完成了另一轮。
- 当前 turn 状态不清楚。

### Architecture requirement

ContextBuilder 要区分：

```text
RecentConversationHistory
  已完成 turn 的真实历史。

CurrentTurnLocalState
  本轮内刚刚发出的消息、工具结果、发送状态。
```

本轮结束前，刚发出的消息只进入 `CurrentTurnLocalState`。

本轮结束后，成功送达的消息才进入持久历史和 SentContentMemory。

### Tool result design

`send_text_message` 工具结果应返回：

```text
{
  "status": "delivered",
  "delivered_text": "...",
  "delivered_count_this_turn": 3,
  "remaining_message_budget": 12
}
```

然后 ContextBuilder 或 TurnEngine 在下一次模型调用前把它渲染成明确的 turn-local block。

### Turn loop logic

一轮 turn 不是只能调用一次模型。

它可以是这样的：

```text
模型第 1 次调用
-> 模型调用 send_text_message
-> 系统真的发送一条消息
-> 工具结果进入 CurrentTurnLocalState
-> ContextBuilder 带着本轮已发送消息再次构建上下文
-> 模型第 2 次调用
-> 模型决定继续发第二条，或调用 finish_turn
```

这就是微信气泡式聊天能自然出现多条消息的原因。

但必须有预算限制，避免模型无限循环：

```text
TurnBudget
- max_model_calls_per_turn
- max_messages_per_turn
- max_tool_calls_per_turn
- max_elapsed_seconds_per_turn
```

如果预算耗尽，TurnEngine 不能继续让模型自由运行。

它应该按固定策略收尾：

```text
预算耗尽
-> 不再执行新的外部发送工具
-> 记录 budget_exhausted
-> 如果需要，写入安全终态
-> 保存 context snapshot
```

### Terminal policy

每种 turn type 都必须有明确终态。

普通聊天可以是：

```text
send_text_message 至少 0 次
-> finish_turn
```

主动消息可以是：

```text
send_text_message 至少 1 次
-> finish_turn(next_wake_minutes)
```

或者在允许跳过时：

```text
skip_initiative
-> finish_turn(next_wake_minutes)
```

定时提醒可以是：

```text
send_text_message
-> mark_reminder_delivered
-> finish_turn
```

终态不能靠模型自然语言表达。

终态必须是一个结构化工具调用，因为系统后续要据此写状态、写历史、释放锁、安排下一次唤醒。

---

## 5. 多模态输入设计

### Decision

用户发图片和语音时，直接传给支持多模态的主模型。

同时系统仍然生成可审计摘要。

### Why both direct media and summary

只传摘要的问题：

- 图片摘要错了就无法纠正。
- 用户问细节时模型看不到原图。
- 关系型聊天中，有时图片细节很重要。

只传原始媒体的问题：

- snapshot 不可读。
- 隐私风险更高。
- 后续记忆系统不知道该保存什么。

所以最佳方案是：

```text
Raw media -> multimodal model input
Media summary -> context snapshot / memory candidate / debug view
```

### Runtime path

```text
iLink getupdates
-> MediaAdapter extracts image/voice item
-> MediaStore short-term cache
-> ContextBuilder creates multimodal message block
-> ProviderGateway sends media + text context to main model
-> Media summary is saved in ContextSnapshot
```

### InboundMessageBatch

无论用户一次发文字、图片、语音，还是短时间内连续发几条，都应该先进入统一输入批次。

```text
InboundMessageBatch
- batch_id
- user_id
- bot_id
- received_at
- source_channel
- items
```

每个 item 标明自己的类型：

```text
InboundMessageItem
- item_id
- type: text | image | voice | file | system_event
- raw_ref
- text
- media_ref
- summary
- metadata
```

这样后续 ContextBuilder 不需要猜：

- 哪些是用户真实说的话。
- 哪些是图片。
- 哪些是语音转写。
- 哪些是系统事件。
- 哪些只是媒体摘要。

多模态输入进入模型时，应该同时有两份东西：

```text
provider_messages:
  给模型看的原始媒体或短期媒体引用。

context_snapshot:
  给人审计用的媒体摘要、转写和 metadata。
```

### Persistence

默认：

- 原图不长期保存。
- 原始语音不长期保存。
- 短期缓存允许。
- 长期只保存摘要、语音转写、语气 metadata。

---

## 6. 输出工具命名

### Decision

输出工具不应继续叫 `wechat_reply`。

原因：

- 未来不一定只发 WeChat。
- 工具名应该描述模型动作，而不是底层渠道。
- WeChat 只是 transport adapter。

### New tool names

推荐：

```text
send_text_message
send_image_message
generate_image_message
send_sticker
finish_turn
skip_initiative
schedule_user_reminder
```

### Mapping

```text
send_text_message
  -> WeChatTransport.send_text()

send_image_message
  -> WeChatTransport.send_image()

generate_image_message
  -> ImageGenerationProvider.generate()
  -> WeChatTransport.send_image()

send_sticker
  -> AssetLibrary.select()
  -> WeChatTransport.send_image_or_sticker()
```

### Tool exposure rules

普通聊天默认：

```text
send_text_message
finish_turn
get_time
get_weather
```

用户明确请求图片时：

```text
send_text_message
send_image_message
generate_image_message
finish_turn
```

主动消息：

```text
send_text_message
skip_initiative
finish_turn
```

主动消息永远不暴露：

```text
send_image_message
generate_image_message
send_sticker
```

### Migration requirement

旧 `wechat_reply` 不保留长期兼容模块。

大型迁移期间可以短暂存在 adapter，但必须在同一大版本计划内删除。

### Tool categories

工具应该按作用分类，而不是按渠道分类。

```text
Output tools
- send_text_message
- send_image_message
- generate_image_message
- send_sticker

Turn control tools
- finish_turn
- skip_initiative

State tools
- schedule_user_reminder
- mark_reminder_delivered
- update_next_wake

Information tools
- get_time
- get_weather
- search_memory
```

模型每轮看到的工具集合必须由 `ToolExposurePolicy` 生成。

不能因为某个工具在代码里存在，就默认让模型每轮都能用。

例如：

- 普通聊天不一定需要 `skip_initiative`。
- 主动消息不应该暴露图片生成。
- 用户没有请求图片时，不应该暴露 `generate_image_message`。
- 强制开口阶段不应该暴露 `skip_initiative`。

### Tool execution rules

工具执行前必须做校验：

```text
模型请求调用工具
-> 检查工具是否在本轮 exposed tools 内
-> 检查 arguments 是否符合 schema
-> 检查当前 turn state 是否允许调用
-> 通过后才执行
-> 执行结果写入 CurrentTurnLocalState
```

如果模型调用了未暴露工具，系统不应该帮它执行，也不应该猜它想干什么。

应该记录：

```text
tool_call_rejected
- reason: tool_not_exposed
- requested_tool
- exposed_tools
```

然后按重试策略让模型修正，或安全结束本轮。

---

## 7. 大型版本迁移策略

### Decision

采用大型版本迁移，而不是长期兼容旧架构。

开发节奏可以按天实施，但目标版本完成后必须：

- 系统整洁。
- 无死代码。
- 无无效测试。
- 无长期兼容模块。
- 无旧 prompt 残留。
- 无旧工具名残留。
- 无未解释的 fallback path。
- 无“下一阶段再补”的核心架构债。

### Allowed during migration

按天实施时，允许临时 adapter，但必须满足：

- 文档写明删除日期或删除阶段。
- 不跨越最终版本完成线。
- 有测试证明新旧路径行为等价。

### Forbidden

- 永久保留 `wechat_reply` 兼容名。
- 永久保留旧 ContextBuilder。
- 永久保留 prompt 双来源。
- 永久保留旧 scheduler task payload 猜测逻辑。
- 永久保留无效测试。
- 把“暂时能跑”当成完成。

### Completion definition

大型版本完成时必须满足：

1. 所有模型输入都经过 ContextBuilder。
2. 所有工具暴露都经过 ToolExposurePolicy。
3. 输出工具完成去渠道化命名。
4. `main.py` 只负责启动和挂载。
5. TurnEngine 不负责上下文构建。
6. MemorySystem 按关系型记忆策略写入和读取。
7. SentContentMemory 防重复生效。
8. 主动消息支持 skip 和 forced speak after skip。
9. 多模态输入进入统一 InboundMessageBatch。
10. 主动消息不暴露图片输出工具。
11. context snapshot 可查看每轮关键输入。
12. 旧工具、旧 prompt、旧兼容 adapter 被删除。
13. 全量测试通过。

---

## 8. 需要写入长期计划的改动

长期计划必须根据本文重写，至少补充这些开发阶段：

1. ProviderGateway 多模态能力抽象。
2. InboundMessageBatch 和媒体 item 数据结构。
3. ContextBuilder 唯一入口。
4. ToolExposurePolicy 去渠道化工具命名。
5. 主动消息 skip / forced speak 状态。
6. Turn-local sent message block。
7. SentContentMemory 与 duplicate guard。
8. 多模态直接输入 + 摘要 snapshot。
9. 输出图片工具。
10. 删除 `wechat_reply` 旧工具名。
11. 大型版本最终清理日。

---

## 9. 最终运行逻辑验收标准

这份设计继续推进后，最终系统应该满足下面这些运行逻辑。

### 9.1 外部输入进入系统

```text
微信 / 其他渠道收到事件
-> InboundAdapter 解析事件
-> 生成 InboundMessageBatch
-> TurnClassifier 判断 turn_type
-> TurnEngine 创建 turn_id
```

渠道只存在于 adapter 层。

进入核心系统后，不应该再到处出现 `wechat_reply` 这种渠道命名。

### 9.2 每次调用模型前

```text
TurnEngine 准备调用模型
-> 读取当前 turn state
-> 读取 recent history
-> 读取需要的 memory blocks
-> ToolExposurePolicy 计算 exposed tools
-> ContextBuilder 生成 BuiltContext
-> ContextSnapshotStore 记录本次输入快照
-> ProviderGateway 调用模型
```

模型看到什么，系统必须能查。

模型能用什么工具，系统必须能查。

### 9.3 模型输出

```text
模型返回结构化 JSON / native tool calls
-> ProviderGateway 做结构校验
-> TurnEngine 做业务校验
-> ToolExecutor 执行被允许的工具
-> 工具结果进入 CurrentTurnLocalState
```

模型不能用自然语言绕过工具。

例如模型不能只说：

```text
我决定跳过，45 分钟后再来。
```

它必须调用对应工具：

```text
skip_initiative
finish_turn(next_wake_minutes=45)
```

### 9.4 一轮如何结束

每个 turn type 都必须定义终态。

普通聊天结束时至少要知道：

```text
- 本轮是否发送了消息
- 本轮发送了几条
- 哪些工具被调用
- 本轮是否正常 finish
- 是否触发预算上限
```

主动消息结束时还要知道：

```text
- 本轮是 spoke 还是 skipped
- 下次允许唤醒的时间
- 是否因为上次 skip 导致本轮不能 skip
```

定时任务结束时还要知道：

```text
- 哪个 reminder 被领取
- 是否成功发送
- 是否标记 delivered
- 失败是否需要重试
```

这些都不能靠读聊天文本猜。

必须来自结构化工具结果和 turn state。

### 9.5 本轮结束后

```text
TurnEngine 到达终态
-> 成功发送的消息写入真实历史
-> 本轮 sent content 写入 SentContentMemory
-> MemorySystem 生成记忆候选
-> 根据记忆策略决定是否长期保存
-> InitiativeState / ReminderState 更新
-> ContextSnapshot 固化
-> 释放本轮锁
```

注意：记忆写入发生在本轮结束后。

模型不能在一句回复里直接决定“这条一定进长期记忆”。它最多可以提供候选或理由，最终由 MemorySystem 按策略处理。

### 9.6 系统不应该出现的行为

最终系统不应该出现这些情况：

- 某条路径绕过 ContextBuilder 自己拼 prompt。
- prompt 说有某工具，但 native schema 没暴露。
- native schema 暴露了某工具，但 ToolExecutor 不支持。
- 模型输出自然语言 JSON，系统用字符串猜测动作。
- 本轮刚发送的消息同时出现在 recent history 和 tool result。
- 主动消息连续 skip，因为第二次仍暴露了 skip 工具。
- 定时任务和主动冷启动抢同一个时机，没有领取机制。
- `wechat_reply` 长期残留在核心 runtime。
- 旧 prompt、旧工具、旧 adapter 靠兼容逻辑长期存在。
- 测试只验证“能跑”，不验证工具暴露、终态和状态更新。

---

## 10. 已确认设计决策

1. 长期尽量使用一个支持多模态的主模型。
2. 图片生成可以是单独 provider，但 runtime 通过工具抽象。
3. Provider adapter 必须保留。
4. ContextBuilder 是所有模型输入的唯一入口。
5. 这是当前系统的最佳方案，因为它解决审计、缓存、工具一致性和多模态统一。
6. 主动消息先由系统 gate，再由模型决定说话或跳过。
7. 模型跳过主动时必须给下一轮唤醒时间。
8. 下一轮唤醒时间由系统给范围，模型在范围内选择。
9. 如果上次主动没有开口，下一次主动不允许继续跳过。
10. 保留一轮多条消息机制。
11. 同一轮内刚发的话要以 turn-local state 传给模型，不混入普通 recent history。
12. 多模态输入直接传给主模型。
13. 同时生成媒体摘要用于 snapshot 和记忆候选。
14. 输出工具不再使用 `wechat_reply` 命名。
15. 新输出工具使用渠道无关命名，如 `send_text_message`。
16. 采用大型版本迁移，最终不保留死代码、无效测试、兼容模块和旧工具残留。
17. 模型输出必须通过 provider 结构化能力约束，不能只靠 prompt 要求 JSON。
18. 每种 turn type 都必须有明确终态工具和终态状态。
19. `finish_turn` 是通用终态工具；主动消息的下一次唤醒时间由终态参数写入。
20. `skip_initiative` 只表达本次主动不说话，不负责单独完成整轮状态收尾。
21. 如果上一轮主动 skip，下一轮 ToolExposurePolicy 不暴露 `skip_initiative`。
22. 外部渠道命名只允许存在于 adapter 层，不进入核心工具协议。
