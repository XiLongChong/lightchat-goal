# 关系型陪伴系统架构需求确认

> 日期：2026-06-02
> 状态：需求确认草案
> 目的：先确认目标系统和底层原则，再重写长期重构计划。

***

## 0. 这份文档解决什么问题

这份文档不是实现计划，也不是 bug 修复列表。

它要先回答一个更基础的问题：

> 我们到底要构建一个什么样的系统？

当前系统的问题不是单个 bug，而是架构目标没有被稳定写下来，导致每次修复都像是在现有代码上继续补洞。现在需要先确认产品哲学、运行时边界、主动消息原则、上下文原则、记忆原则、多模态原则，然后再重写长期开发计划。

后续所有重构计划都必须服从这份需求文档。

***

## 1. 核心产品定位

### Decision

系统的核心身份是：

> 一个长期关系型陪伴对象，更接近网恋对象，而不是客服、效率助手、普通聊天机器人或任务管理工具。

### Implications

这意味着：

- 它的第一目标是建立和维持长期关系连续性。
- 它应该能主动关心用户。
- 它应该记得用户长期偏好、边界、情绪习惯、相处方式。
- 它说话应该像一个熟悉用户的人，而不是像工具。
- 工具、提醒、图片、语音、记忆都服务于“关系型陪伴”，而不是反过来让系统变成任务工具。

### Rejected Alternatives

- 纯客服型助手：拒绝。
- 纯效率型任务助理：拒绝。
- 只响应用户、不主动开口的被动聊天机器人：拒绝。
- 只靠 prompt 模拟关系，而没有长期记忆和主动机制的角色扮演 bot：拒绝。

***

## 2. 主动消息是核心能力

### Decision

主动消息是核心能力，不是插件。

系统必须能在合适的时候主动开口，包括：

- 用户长时间没说话时的关系型主动关心。
- 用户明确要求后的提醒。
- 固定仪式型互动，例如早安、晚安、睡前关心。

### Implications

主动能力不能继续作为边缘 scheduler 功能挂在系统旁边。它应该进入主运行时架构。

架构上应有统一的主动发起层：

```text
OutboundInitiativeRuntime
  - RelationshipProactiveFlow
  - UserReminderFlow
  - ScheduledRitualFlow
```

外层统一处理“系统主动发起输出”，内层按行为类型拆分规则。

### Non-negotiable

主动消息不能再只靠模型自由发挥。系统必须先判断能不能开口，再让模型决定怎么说或是否沉默。

***

## 3. 主动行为分类

### Decision

主动行为分成三类，底层分开，表层统一。

### 3.1 RelationshipProactiveFlow

关系型主动开口。

例子：

- 用户很久没说话，系统想关心一下。
- 用户前面说很累，过一会儿系统想轻轻问一句。
- 系统根据长期关系状态主动找用户。

规则：

- 系统先做 gate，判断是否允许打扰。
- 模型可以选择沉默。
- 必须看最近已发送内容，防止重复。
- 必须看关系状态和用户边界。
- 不应该机械完成任务。
- 主动输出默认只能是文字。

### 3.2 UserReminderFlow

用户明确授权的提醒。

例子：

- “十分钟后提醒我喝水。”
- “晚上九点叫我睡觉。”

规则：

- 这是用户授权过的承诺。
- 到点应触发，不应被 autonomous proactive 规则随意拦截。
- 是否受勿扰时间影响，需要看用户表达和配置。
- 模型不应随便 skip。
- 上下文重点是任务内容和关系语气，不需要完整冷场判断。

### 3.3 ScheduledRitualFlow

固定仪式型主动行为。

例子：

- 早安。
- 晚安。
- 睡前关心。
- 周期性陪伴仪式。

规则：

- 有固定时间或规律。
- 内容不能机械重复。
- 可以看长期记忆、关系状态、最近互动。
- 用户烦、拒绝、要求安静时应暂停或降频。
- 默认只发文字。

### Why split them

这三类都表现为“bot 主动说话”，但底层决策完全不同。

如果继续混在一个 `scheduled_messages` 或一个 proactive 概念里，会继续出现：

- 提醒被当成冷场消息跳过。
- 冷场消息被当成提醒强制发送。
- 定时仪式机械重复。
- 任务类型靠 payload 猜，状态混乱。

***

## 4. 开口控制原则

### Decision

系统先决定能不能开口，模型再决定说什么或是否沉默。

```text
System Gate:
  Is this initiative allowed now?

Model Choice:
  If allowed, what should be said?
  Or should the model choose silence?
```

### System Gate considers

- 用户是否刚刚拒绝或表达烦躁。
- 是否处于勿扰时间。
- 最近是否已经主动说过类似内容。
- 今天主动次数是否过多。
- 当前是否有未完成的用户消息回合。
- 当前主动类型是否是用户授权提醒。
- 用户长期边界和偏好。

### Model Choice considers

- 关系语气。
- 当前上下文。
- 是否有新鲜角度。
- 是否应该轻轻开口。
- 是否应该沉默。
- 是否需要调用工具。

### Important

模型有沉默权，但沉默权不能只是形式上的 `skip_proactive`。上下文必须告诉模型：

- 什么情况下应该说。
- 什么情况下应该跳过。
- 跳过不是失败。
- 重复、没新角度、用户明显不想被打扰时应跳过。

***

## 5. 多条消息原则

### Decision

允许一轮发多条消息。

当前模式可以保留：

```text
LLM call
-> model calls wechat_reply once
-> system sends one message
-> model sees send result and updated turn state
-> LLM call again
-> model decides continue or done
```

### Required safeguards

每一条消息都必须经过：

- 输出安全检查。
- 重复内容检查。
- 当前 turn 上限检查。
- 中断检查。
- 发送结果记录。

每发一条后，下一次 LLM 调用必须看到：

- 刚才发了什么。
- 是否送达。
- 当前已经发了几条。
- 还能不能继续发。

### Why

多条消息让聊天更像真人，尤其是微信气泡式对话。但如果没有 per-message guard 和 sent-content memory，就会变成连续重复发送。

***

## 6. 记忆系统目标

### Decision

系统需要长期甚至接近永久的关系型记忆。

记忆的目标不是“存很多文本”，而是让系统长期了解用户，并且在当前轮用对记忆。

### Memory categories

#### 6.1 Profile Memory

用户长期画像。

例子：

- 用户喜欢什么。
- 用户讨厌什么。
- 用户作息。
- 用户长期压力来源。
- 用户说话习惯。

#### 6.2 Relationship Memory

你们之间的关系状态。

例子：

- 用户喜欢被怎么哄。
- 用户讨厌什么语气。
- 哪些调侃可以，哪些不可以。
- 当前亲密度、边界、互动风格。

#### 6.3 Episode Memory

近期发生的重要事件。

例子：

- 今天用户很累。
- 用户刚说要去做某件事。
- 用户今天心情不好。

#### 6.4 Open Thread Memory

未完成的上下文线索。

例子：

- 用户说要去做任务，但还没反馈完成。
- 用户说晚点再聊。
- 用户发了图片，后续可能追问。

#### 6.5 Boundary Memory

用户明确表达过的边界。

例子：

- “别烦我。”
- “别这样说话。”
- “不要主动找我。”
- “别记这个。”

#### 6.6 Sent Content Memory

系统最近已经发过的内容。

用途：

- 防止主动消息重复。
- 防止同一轮多条消息重复。
- 让模型知道自己刚才已经说过什么。

### Non-negotiable

记忆不能作为一大段泛文本随便塞进 prompt。

每类记忆必须有：

- 类型。
- 来源。
- 更新时间。
- 是否进入长期记忆。
- 是否进入本轮上下文。
- 为什么进入。

***

## 7. Context Snapshot

### Explanation

Context snapshot 是“本轮模型实际看到内容”的记录。

它不是给模型看的，而是给开发者和管理台 debug 用的。

### Decision

每轮至少保存摘要级 snapshot。调试模式可以保存完整 snapshot。

### Snapshot should include

- turn id。
- turn type。
- system gate 结果。
- 暴露工具列表。
- context block 列表。
- 每个 block 的类型和 cache class。
- 使用了哪些长期记忆。
- 使用了哪些近期事件。
- 使用了哪些已发送内容。
- 最近历史包含哪些 turn。
- 当前用户输入或主动触发原因。
- token/字符数估算。

### Full content persistence

默认不长期保存完整模型输入全文，尤其是图片/语音相关内容。可以配置为 debug 模式临时保存。

### Why needed

当模型输出奇怪、重复、越界、忘记上下文时，必须能回答：

- 它到底有没有看到历史？
- 它有没有看到刚刚发过的内容？
- 它有没有看到用户边界？
- 它当时能用哪些工具？
- 它为什么被允许主动开口？

没有 snapshot，系统就只能猜。

***

## 8. 系统规则与人设

### Decision

系统规则和工具约定统一使用英文。人设保留中文。

### Desired structure

```text
System identity:
  You are a long-term romantic companion for the user.

Runtime rules:
  English protocol, output rules, memory rules, tool rules.

Persona:
  中文人设、语气、关系风格。
```

### Why system rules affect tone

模型不会严格把“系统规则只当规则，人设只当性格”分开。所有上文都会影响输出。

如果 system 写成“你是 AI 助手”，人设再写网恋对象也会被削弱。

所以系统规则应该固定身份：

> 你是长期关系型陪伴对象，但必须遵守运行协议。

这样系统规则服务于身份，而不是覆盖人设。

***

## 9. 多模态需求

### Decision

系统需要支持多模态输入和部分多模态输出。

### 9.1 用户发图片

用户可以发图片给 bot。

要求：

- bot 先自然回应，而不是输出图片分析报告。
- 图片理解结果作为上下文摘要进入本轮。
- 默认不长期保存原图。
- 默认不把隐私图片写入长期记忆。
- 可短期缓存原图，用于当轮和短期追问。

推荐行为：

```text
User sends image
-> MediaAdapter extracts image item
-> ImageUnderstanding creates summary
-> ContextBuilder adds image summary block
-> Model replies naturally as companion
```

### 9.2 用户发语音

用户可以发语音给 bot。

要求：

- 系统需要听懂语音内容。
- 不只做 STT，还要理解语气和情绪。
- 语气 metadata 可以进入上下文。
- 暂不要求 bot 发语音。

语音处理结果：

```text
VoiceUnderstanding
- transcript
- tone
- emotion_hint
- confidence
- duration
```

### 9.3 bot 发图片

bot 未来可以发图片。

支持两类：

- 生成图片。
- 发送已有素材、图片、表情包。

但必须受系统控制：

- 只能在用户回合中按需发。
- 主动消息默认禁止发图片。
- 必须通过工具调用，例如 `generate_image`、`send_image`、`send_sticker`。
- 必须进入 ToolExposurePolicy。
- 必须记录 sent content summary。

### 9.4 bot 发语音

暂不做。

保留未来扩展，但不进入当前长期重构范围。

### 9.5 主动消息多模态限制

主动消息禁止主动发图片或语音。

原因：

- 网恋对象主动发文字是自然的。
- 主动发图片/语音更打扰，也更不可控。
- 多模态主动输出需要更强的用户授权和边界系统。

### 9.6 原始媒体保存策略

默认策略：

- 不长期保存原图。
- 不长期保存原始语音。
- 保存文字摘要、语音转写和语气 metadata。
- 可配置短期缓存原始媒体，例如 24 小时。
- context snapshot 默认只保存摘要。

优点：

- 隐私风险低。
- 数据库和备份更轻。
- 上下文可审计。
- 足够支持自然回应。

缺点：

- 不能长期回看原图。
- 如果第一次摘要错了，后续可能基于错误摘要。
- 对长时间后的细节追问支持弱。

当前默认接受这些缺点。

***

## 10. 多模态 API 接入原则

### Decision

即使接入支持多模态的模型 API，也不能把媒体直接塞进旧 `ChatAgent`。

多模态必须先进入统一消息结构和 ContextBuilder。

### Required runtime path

```text
iLink getupdates
-> MediaAdapter
-> InboundMessageBatch
-> MediaUnderstanding
-> Preprocessor
-> MemorySystem
-> ContextBuilder
-> ToolExposurePolicy
-> TurnEngine
```

### Why

支持多模态的 API 只是 provider 能力，不等于系统能力。

系统仍然需要：

- 解析 iLink 媒体 item。
- 下载或读取媒体。
- 控制原始媒体保存策略。
- 生成摘要。
- 决定摘要是否进上下文。
- 决定是否写入长期记忆。
- 在 snapshot 中记录媒体摘要。
- 控制 bot 是否可以发图。

### Provider strategy

允许两种实现：

#### Option A: Native multimodal chat model

聊天模型本身支持图片/音频输入。

优点：

- 上下文更完整。
- 模型可以直接看图。
- 用户体验更强。

缺点：

- 成本可能更高。
- provider 适配更复杂。
- snapshot 不能保存原始媒体时，复现能力有限。

#### Option B: Dedicated media understanding

先用独立 vision/STT 模块把媒体转成摘要，再交给聊天模型。

优点：

- 聊天模型可以继续用文本模型。
- 更容易缓存和审计。
- 隐私控制更清楚。

缺点：

- 图片细节可能损失。
- 语音语气理解依赖预处理质量。

### Recommended initial choice

先做 Option B 的架构，再允许 provider 支持时升级到 Option A。

原因：

- 这符合“只保存摘要”的隐私目标。
- 更容易接入当前 ContextBuilder。
- 更容易调试。

***

## 11. 输入消息模型

### Decision

未来 `/chat` 不应继续只接受 `messages: list[str]`。

需要升级为：

```text
InboundMessageBatch
- user_id
- bot_id
- context_token
- items[]
- input_boundary

InboundMessageItem
- id
- kind: text | image | voice | file | video
- text
- media
- received_at
- raw_source

MediaAttachment
- media_id
- kind
- mime_type
- local_cache_path
- remote_url
- size
- duration
- width
- height
- summary
- transcript
- tone
- metadata
```

### Compatibility

旧 `messages: list[str]` 可以保留一段时间，但应在 ChatRuntime 入口转换成 `InboundMessageBatch`。

***

## 12. 输出消息模型

### Decision

未来模型输出不能只等于文本。

需要支持：

```text
OutboundMessage
- kind: text | image | sticker
- text
- media_ref
- generated_asset_id
- delivery_policy
- summary_for_memory
```

### Rules

- 普通聊天可以发文本。
- 普通聊天在工具允许时可以发图片或表情。
- 主动消息只能发文本。
- 提醒默认只能发文本。
- bot 发图必须通过工具，并经过系统 gate。

***

## 13. 关系边界、主动频率和记忆策略

本节记录已经确认的关系型陪伴需求。这些需求会直接影响状态机、记忆系统、主动消息 gate、输出 guard 和管理台设计。

***

### 13.1 亲密边界

### Decision

bot 可以非常暧昧，可以像热恋情侣一样拉扯、吃醋、撒娇、顶嘴、生气、认错、冷处理，也可以在必要时反击。

允许的关系行为包括：

- 强烈暧昧。
- 热恋情侣式表达。
- 拉扯。
- 吃醋。
- 撒娇。
- 顶嘴。
- 生气。
- 认错。
- 冷处理。
- 必要时反击。
- 适度骂人或攻击性调侃。

### Implications

这意味着系统不能被设计成过度礼貌的客服式助手。它需要支持关系张力。

但是这种张力必须被边界系统约束：

- 用户明确讨厌的称呼、话题、语气必须记入 Boundary Memory。
- 反击不能升级成无限争吵循环。
- 冷处理是关系行为，不是系统失效。
- 生气、顶嘴、认错都应该作为可控情绪策略，而不是模型随机失控。

### Architecture requirement

需要在 ContextBuilder 中加入关系状态 block：

```text
RelationshipState
- stage
- intimacy_level
- current_mood_between_them
- allowed_teasing_level
- jealousy_allowed
- conflict_mode
- active_boundaries
```

输出 guard 不应该简单禁止所有强情绪表达，而应该区分：

- 关系内允许的拉扯。
- 用户明确禁止的雷区。
- 失控重复、辱骂升级、机械攻击。

***

### 13.2 主动频率

### Decision

主动频率按配置档位控制。这里的“一次”指触发一次主动回合，不是发一条消息。

当前档位需求来自管理台截图：

```text
安静：每天最多 2 次，间隔 6h / 8h
日常：每天最多 5 次，间隔 2h / 3h
关心：每天最多 10 次，间隔 30min / 1h
陪伴：每天最多 20 次，间隔 5min / 20min
```

勿扰时间内不发送主动消息。睡觉时间等同于勿扰时间。

用户如果觉得打扰，可以手动关闭主动消息或调整档位。系统不因为一次对话停止就自动暂停主动能力。

### No forced stop rule

不设置“用户不回复几次后必须停”的产品规则。

但是必须保留技术级熔断：

- 防止 bug 导致无限触发。
- 防止同一任务重复 claim。
- 防止同一内容连续发送。
- 防止同一主动回合超过消息上限。

技术熔断不是关系上的“暂停联系”，而是系统安全保护。

### Architecture requirement

主动频率 gate 应输出：

```text
InitiativeGateDecision
- allowed
- reason
- intensity_profile
- daily_trigger_count
- min_interval
- max_interval
- quiet_hours_blocked
- technical_circuit_breaker
```

***

### 13.3 多条消息上限

### Decision

普通聊天和主动消息都允许一轮最多 15 条消息。

失败重试不计入 15 条消息上限。

### Clarification

这里的 15 条是兜底上限，不是鼓励模型每轮发满。

系统还需要软约束：

- 普通聊天通常 1-5 条更自然。
- 主动消息通常应更克制。
- 关系冲突时可以多条，但不能重复刷屏。

### Architecture requirement

Turn state 必须区分：

```text
delivered_message_count
send_attempt_count
failed_send_attempt_count
model_retry_count
duplicate_block_count
```

其中：

- `delivered_message_count` 受 15 条上限控制。
- 失败重试不计入 delivered 上限。
- duplicate retry 也不计入 delivered 上限，但受单独 retry 上限控制。

***

### 13.4 网恋场景的记忆原则

### Decision

网恋场景中，情感真实性高于信息完整性。

记忆系统应该模拟真实人类恋爱关系中的“选择性记忆”和“适度遗忘”，避免显得机械、全知、监控式。

### Long-term memory should be strict

长期记忆只写入对情感维系至关重要的内容。

允许写入长期记忆：

- 专属昵称与爱称。
- 双方特殊称呼方式。
- 确定关系的日期。
- 第一次聊天、第一次语音、第一次视频等核心情感节点。
- 重要纪念日。
- 核心喜好。
- 核心雷区。
- 长期约定与承诺。
- 重要人生事件，例如生日、考试、升职、家人重要事情。

禁止写入长期记忆：

- 吵架时的气话。
- 伤人的话。
- “我不想理你了”“我们分手吧”这类临时冲突表达。
- 过于私密的身体细节。
- 亲密对话原文。
- 对方的负面情绪和脆弱时刻，除非用户明确希望记住并安慰。
- 临时小矛盾。
- 临时误会。

### Short-term memory

短期事件默认有效期为 12 小时。

短期事件包括：

- 日常闲聊。
- 临时情绪。
- 当天发生的小事。
- 未标记为长期记忆的普通上下文。

会话结束后，当前会话中未被标记为长期记忆的内容应被清理或降级为不可主动引用的短期摘要。

### Emotion decay

负面情绪记忆应更快衰减：

- 生气、难过、烦躁等负面状态 6 小时后自动衰减。
- 下次聊天不主动提及。
- 只有用户再次提起时才承接。

### Architecture requirement

MemorySystem 不能只做“抽取事实”。它必须支持：

```text
MemoryCandidate
- content
- category
- emotional_weight
- relationship_importance
- privacy_level
- retention_policy
- user_forgettable
- write_decision
- reason
```

***

### 13.5 “别记这个”指令

### Decision

用户说“别记这个”时，系统应立即温柔执行，不追问原因。

推荐回复风格：

```text
好呀宝宝，我听你的，刚才那些我不记了。
```

具体文案由 persona 控制，不固定死。

### Required behavior

- 立即执行。
- 不追问为什么。
- 不追问应该记住什么。
- 自动转移话题。
- 指令前后各 5 轮对话标记为禁止记忆。
- 如果用户多次要求不要记某类话题，该话题加入永久屏蔽列表。
- 永久屏蔽话题不再主动提及。

### Architecture requirement

需要支持：

```text
ForgetDirective
- detected_at_turn_id
- scope: local_window | topic | relationship_reset
- window_before: 5
- window_after: 5
- permanent_topic_block
- response_style
```

MemoryWorker 在抽取前必须检查 ForgetDirective 和 blocked topics。

***

### 13.6 记忆管理台

### Decision

管理台需要支持网恋关系专属记忆控制。

Required features:

- 一键“失忆”：回到初识状态。
- 记忆隐藏：Agent 不再参考，但用户仍可查看。
- 关系重置：例如从“热恋”回到“暧昧”，同时清理对应阶段记忆。

### Architecture requirement

Memory records need state:

```text
MemoryVisibility
- active
- hidden_by_user
- blocked_by_forget_directive
- archived_by_relationship_reset
```

RelationshipState reset must be a first-class operation, not just deleting random memory rows.

***

### 13.7 图片生成边界

### Decision

bot 只有在用户要求的情况下才发图片。

bot 不主动要求发图，不主动在关系型主动消息里发图片。

### Implications

`generate_image`、`send_image`、`send_sticker` 只应在以下条件暴露：

- 当前是用户回合。
- 用户明确请求图片、表情、画图、发图。
- 系统 gate 允许图片输出。
- 主动消息 flow 中永远不暴露图片输出工具。

### Architecture requirement

ToolExposurePolicy 必须能区分：

```text
user_requested_image_output = true | false
initiative_flow = relationship_proactive | reminder | ritual | none
```

***

### 13.8 模型供应商策略

### Decision

长期目标是使用一个支持多模态的主模型。

### Implications

这个主模型应支持：

- 文本聊天。
- 图片理解。
- 语音理解或语音转写输入。
- 工具调用。

图片生成可以作为同 provider 能力，也可以作为独立图像生成 endpoint，但对 runtime 来说仍通过工具抽象。

### Architecture requirement

即使长期使用一个多模态模型，代码仍应保留 provider adapter 抽象。

原因：

- 便于失败降级。
- 便于将图片生成和聊天模型分开。
- 便于未来替换供应商。
- 避免把多模态 provider 细节写死进 ContextBuilder 或 TurnEngine。

***

## 14. 已确认决策列表

1. 系统核心身份是长期网恋式陪伴对象。
2. 主动消息是核心能力。
3. 系统先判断能不能开口，模型再决定说什么或是否沉默。
4. 允许一轮多条消息。
5. 记忆目标是长期甚至接近永久了解用户。
6. 系统规则和工具约定用英文。
7. 人设保留中文。
8. 用户发图片时，bot 应自然回应，不做分析报告。
9. 图片默认不长期保存原图，只保存摘要。
10. 隐私图片默认不进入长期记忆。
11. 用户发语音时，需要理解内容和语气。
12. bot 未来可以发生成图片和已有素材。
13. bot 暂不发语音。
14. 主动消息禁止主动发图片或语音。
15. context snapshot 默认保存摘要，不保存原始媒体。
16. 多模态 API 接入后仍必须通过 MediaAdapter、MediaUnderstanding 和 ContextBuilder。
17. bot 可以非常暧昧，允许热恋情侣式拉扯、吃醋、撒娇、顶嘴、生气、认错、冷处理和必要反击。
18. 主动频率按档位控制，次数指主动触发次数，不是消息条数。
19. 不设置“用户不回复几次后必须停”的产品规则，但保留技术级熔断。
20. 睡觉时间等同于勿扰时间。
21. 普通聊天和主动消息每轮兜底上限均为 15 条已送达消息。
22. 失败重试不计入 15 条已送达消息上限。
23. 网恋记忆原则是情感真实性高于信息完整性。
24. 长期记忆严格限制，只记录对情感维系重要的昵称、纪念日、核心喜好雷区、长期承诺和重要人生事件。
25. 吵架气话、亲密对话原文、临时负面情绪、小矛盾默认不写入长期记忆。
26. 短期事件默认 12 小时有效。
27. 负面情绪记忆 6 小时后衰减，除非用户再次提起。
28. “别记这个”应立即温柔执行，不追问，并扩大到前后各 5 轮禁止记忆。
29. 多次要求不提的主题应进入永久屏蔽列表。
30. 管理台需要一键失忆、记忆隐藏、关系重置。
31. bot 只有在用户要求时才发图片，不主动要求发图。
32. 长期目标是使用一个支持多模态的主模型，但代码保留 provider adapter 抽象。

***

##

