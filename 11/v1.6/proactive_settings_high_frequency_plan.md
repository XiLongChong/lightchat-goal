# Goal Chat Agent — 主动消息设置与高频触发计划方案 v1.0

> 生成日期：2026-05-31  
> 目标：让主动消息更容易触发，并通过控制台可配置频率、定时任务、勿扰时间和每日上限。  
> 核心原则：主动消息不是系统通知，而是 bot 在微信聊天里像人一样“想起你了”，自然地接一句、补一句、继续一句。  
> 状态：设计确认稿，等待实施计划拆解。

---

## 1. 背景与问题

当前主动消息链路已经可以通过测试台 dry-run / real-send 验证，但实际触发频率偏低，常见表现是：

```text
eligibility pass
impulse_generation empty
impulse_selection empty
```

也就是：系统允许主动发，但没有生成主动理由候选。

用户当前目标不是先防骚扰，而是先把主动消息频率调高，自己体验是否太烦，再逐步调低。因此本方案优先解决：

1. 主动消息开关与设置页。
2. Bot 自主消息频率档位。
3. 最迟触发机制。
4. 用户定时消息。
5. 勿扰时间。
6. 每日主动消息上限。
7. 定时任务工具。
8. 主动上下文构造，使消息像自然微信聊天，而不是通知。

---

## 2. 核心产品原则

### 2.1 主动消息表达的不是“我有信息要告诉你”

错误方向：

```text
我有一条通知要发给你。
检测到你很久没回复。
根据设置我现在来提醒你。
```

正确方向：

```text
我想起了你。
我还在这儿。
我顺着刚才的话又想说一句。
你没回，我就轻轻补一句。
```

### 2.2 主动消息在用户眼里应是普通聊天

对用户来说，不应该感知到：

```text
主动消息
调度器
触发器
频率策略
系统判断
定时任务机制
```

用户应该只感觉到：

```text
这个 bot 像一个会自己找我说话的人。
```

### 2.3 内部必须知道触发来源

虽然对用户表现自然，但模型内部必须知道：

```text
这次是主动开启一轮微信聊天，不是回复用户刚刚发来的消息。
```

否则模型可能编造“你刚刚说……”或错误接话。

因此原则是：

```text
内部知道来源。
外部表现自然。
```

---

## 3. 设置结构

控制台新增页面：

```text
主动消息设置
```

页面包含 5 个区域：

```text
1. 主动消息
2. Bot 自主消息
3. 用户定时消息
4. 勿扰时间
5. 安全限制
```

---

## 4. 主动消息总开关

### 4.1 UI

```text
主动消息：开 / 关
```

### 4.2 语义

这是总开关。

关闭时，所有非用户即时触发的消息都不得发出。

包括：

```text
Bot 自主消息
用户定时消息
08:00 问好
23:00 总结
用户通过微信设置的提醒
```

### 4.3 规则

| 主动消息总开关 | 行为 |
|---|---|
| 开 | 允许 Bot 自主消息和用户定时消息继续走各自子开关 |
| 关 | 所有主动类触发全部禁止 |

### 4.4 工具行为

如果用户在微信里说：

```text
10 分钟后提醒我喝水
```

但主动消息总开关为关，则工具返回失败，模型需要告诉用户：

```text
我现在还不能帮你定时提醒，因为主动消息是关着的。你去控制台打开后，我就能做这个。
```

不得假装创建成功。

---

## 5. Bot 自主消息

### 5.1 UI

```text
Bot 自主消息：开 / 关
```

### 5.2 语义

控制 bot 自己判断是否主动找用户说话。

覆盖类型包括：

```text
PRESENCE：久未互动后的自然出现
ECHO：回应用户之前的情绪
TOPIC_CONTINUE：继续未结束话题
REQUIRED_PRESENCE：达到最迟触发后的强制尝试
```

关闭后：

```text
Bot 不会自己冒泡。
```

但如果满足以下条件，用户定时消息仍可执行：

```text
主动消息总开关 = 开
用户定时消息 = 开
不在勿扰时间或允许顺延
未超过每日主动消息上限
```

---

## 6. 自主消息强度

### 6.1 UI 名称

四档统一两个字：

```text
安静 / 日常 / 关心 / 陪伴
```

不要使用：

```text
冷漠 / 粘人
```

原因：

1. “冷漠”负面，不适合作为用户设置。
2. “粘人”容易让模型误以为要改变人格或语气。
3. 本设置控制的是触发频率，不是 bot 人格。

### 6.2 频率档位

| 档位 | 每日自主触发上限 | 开始判断 | 最迟触发 |
|---|---:|---:|---:|
| 安静 | 2 次 | 距离最后一条可见消息 6 小时 | 8 小时内必须触发一次 |
| 日常 | 5 次 | 距离最后一条可见消息 2 小时 | 3 小时内必须触发一次 |
| 关心 | 10 次 | 距离最后一条可见消息 30 分钟 | 60 分钟内必须触发一次 |
| 陪伴 | 20 次 | 距离最后一条可见消息 5 分钟 | 20 分钟内必须触发一次 |

### 6.3 触发上限不是消息条数上限

```text
每日自主触发上限 = 主动回合触发次数上限
```

一次主动触发后，模型可能发 1 条，也可能发多条。

因此必须区分：

| 名称 | 统计对象 | 作用 |
|---|---|---|
| 每日自主触发上限 | Bot 自主主动回合次数 | 控制触发频率 |
| 每日主动消息上限 | 所有主动类真实发出的消息条数 | 防止异常刷屏 |

### 6.4 频率设置影响范围

强度档位不只是影响最小间隔。

必须同时影响：

```text
1. eligibility：是否允许进入主动判断
2. impulse_generation：多容易生成 PRESENCE / REQUIRED_PRESENCE
3. impulse_scoring：候选通过阈值
4. required_trigger：最迟触发时间
5. daily_autonomous_trigger_cap：每日自主触发上限
```

否则会出现：

```text
eligibility pass
impulse_generation empty
```

用户仍然觉得主动消息太冷清。

---

## 7. 时间判断基准

### 7.1 触发计时用 last_visible_message_at

主动频率判断基于：

```text
last_visible_message_at
```

含义：

```text
用户眼里最后一条可见对话消息的时间。
```

不区分：

```text
用户发的消息
bot 普通回复
bot 主动消息
定时消息
```

只要用户在微信里能看到，就算最后一条可见消息。

### 7.2 语义上下文用 last_user_message + assistant_messages_after_last_user

主动消息生成时，模型需要看到：

```text
last_user_message:
  用户最后一次发的话

assistant_messages_after_last_user:
  bot 在这之后已经发过的所有可见消息

silence_duration:
  距离最后一条可见消息过去多久

trigger_mode:
  optional / required

trigger_reason:
  presence_followup / echo_followup / topic_followup / scheduled
```

这样可以实现类似：

```text
用户最后说了一句话
bot 回了几句
用户没再回
过一会儿 bot 像人一样继续补一句
```

而不是系统通知式主动。

---

## 8. Optional Trigger 与 Required Trigger

### 8.1 Optional Trigger：可选触发

开始判断时间到了，但还没有到最迟触发时间。

例如“陪伴”档位：

```text
距离最后一条可见消息 5 分钟后
→ 可以开始判断
```

流程：

```text
后端资格检查
→ 生成 impulse
→ 调用模型
→ 模型可以 wechat_reply
→ 模型也可以 skip_proactive
→ 后端 guard
→ 合适则发送
```

模型可以判断这次不适合发。

### 8.2 Required Trigger：最迟触发

超过最迟触发时间后，必须进入主动回合。

例如“陪伴”档位：

```text
距离最后一条可见消息 20 分钟后
→ 必须触发一次主动回合
```

流程：

```text
后端资格检查
→ 生成 REQUIRED_PRESENCE / required presence follow-up
→ 调用模型
→ allowed_tools 不包含 skip_proactive
→ 模型必须至少调用一次 wechat_reply
→ 后端 guard
→ guard 通过则发送
```

### 8.3 Required Trigger 不使用固定内容兜底

不允许常规使用固定兜底文案，例如：

```text
我来冒个泡。
```

原因：

1. 固定兜底容易变得机械。
2. 模型多次失败时，用户会频繁看到同一句。
3. 这会破坏“想起你”的自然感。

正确规则：

```text
Required Trigger 保证必须认真尝试生成自然主动消息。
但不保证后端一定硬塞一条固定句子。
```

### 8.4 Required Trigger 失败处理

如果模型输出被 guard 拦截：

```text
允许模型重试 1-2 次。
```

如果仍失败：

```text
本次不发送。
记录 required_trigger_failed_guard。
不写可见历史。
不使用固定内容兜底。
```

如果模型协议失败 / API 失败：

```text
记录 required_trigger_failed_model。
不发送。
不写可见历史。
```

---

## 9. 模型工具限制

### 9.1 Optional Trigger allowed_tools

```text
wechat_reply
skip_proactive
done
get_time / get_weather（如当前上下文允许）
```

### 9.2 Required Trigger allowed_tools

第一阶段：

```text
wechat_reply
done
get_time / get_weather（如当前上下文允许）
```

不提供：

```text
skip_proactive
```

并且状态机需要限制：

```text
required trigger 阶段，在 delivered_count = 0 前，模型不得直接 done。
```

模型必须至少尝试一次 `wechat_reply`。

---

## 10. 主动上下文 Prompt 约束

主动回合给模型的 context 必须表达：

```text
你正在主动开启一轮微信聊天。
这不是通知，不是任务播报，也不是系统提醒。
请像普通微信聊天一样自然发起一句。
只能基于给定事实，不要编造用户正在做什么。
```

### 10.1 必须禁止

```text
不要说“检测到你很久没回复”
不要说“根据设置我来找你”
不要说“现在触发主动消息”
不要像通知
不要像客服
不要像系统提醒
不要编造用户醒了、睡了、在学习、在忙
```

### 10.2 鼓励

```text
短一点
轻一点
像微信里等不到回复后又补一句
可以有一点自言自语感
可以顺着上一段话自然延伸
可以轻轻冒泡
```

### 10.3 示例风格

可接受：

```text
你又不见啦。
那我先在这儿待会儿。
刚才那个我还在想。
```

不可接受：

```text
检测到你已经 20 分钟没有回复，我来主动问候你。
根据你的陪伴频率设置，我现在来找你聊天。
你今天学习辛苦了吧。（无证据时）
```

---

## 11. 用户定时消息

### 11.1 UI

```text
用户定时消息：开 / 关
```

### 11.2 默认任务

```text
08:00 问好：开 / 关
23:00 总结：开 / 关
```

### 11.3 自定义任务

第一版只做选项，不开放自由文本。

```text
+ 添加任务
  时间：HH:mm
  任务类型：
    问好
    提醒休息
    总结
    喝水
    睡前
```

### 11.4 任务类型映射

| UI 类型 | trigger_source |
|---|---|
| 问好 | SCHEDULED_GREETING |
| 提醒休息 | SCHEDULED_BREAK |
| 总结 | SCHEDULED_SUMMARY |
| 喝水 | SCHEDULED_WATER |
| 睡前 | SCHEDULED_SLEEP |

### 11.5 定时消息也必须进入同一主动回路

到点后不得直接发固定文本。

必须走：

```text
ScheduledTrigger
→ ProactiveScheduler / TriggerDispatcher
→ ChatAgent 主动回合
→ guard
→ wechat_sender
→ delivered-only history write
```

原因：

1. 保持输出守卫一致。
2. 保证发送失败不写历史。
3. 保证模型知道自己发过。
4. 保证和 Bot 自主消息一样可诊断、可追踪。

---

## 12. 23:00 总结的特殊约束

23:00 总结不能编造一天发生的事情。

### 12.1 有足够聊天内容时

可以总结真实聊过的内容：

```text
今天你一直在调主动消息测试台，确实挺折腾的。
```

### 12.2 没有足够聊天内容时

降级为轻量晚间问候：

```text
今天也快结束了，早点歇会儿。
```

### 12.3 禁止

```text
你今天学习了一整天辛苦了。
你今天肯定很忙吧。
你今天做得很好。
```

除非聊天历史里有明确证据。

---

## 13. 定时任务工具

### 13.1 必须新增工具

当用户在微信里说：

```text
10 分钟后提醒我喝水
```

模型需要能创建定时任务。

工具建议名称：

```text
create_scheduled_trigger
```

### 13.2 工具分类

```text
STATE_TOOL
```

原因：

1. 它不是 fact tool，不是查询事实。
2. 它不是 terminal tool，执行后对话还要继续。
3. 它不是 wechat_reply action tool，不直接发送微信消息。
4. 它改变系统状态，创建一个未来触发器。

### 13.3 工具参数

第一版参数：

```json
{
  "trigger_at": "2026-05-31T18:30:00+08:00",
  "task_type": "REMINDER",
  "task_label": "喝水",
  "repeat": "once"
}
```

### 13.4 task_type 枚举

```text
REMINDER
GREETING
SUMMARY
BREAK
WATER
SLEEP
```

第一版不接受任意自由类型。

### 13.5 工具执行流程

```text
用户：10 分钟后提醒我喝水
→ 模型调用 create_scheduled_trigger
→ 后端检查主动消息总开关
→ 后端检查用户定时消息开关
→ 后端创建 scheduled trigger
→ 工具结果回灌模型
→ 模型调用 wechat_reply：好，10 分钟后提醒你喝水。
→ done
```

### 13.6 开关关闭时

如果主动消息总开关关闭：

```json
{
  "ok": false,
  "error_code": "PROACTIVE_DISABLED",
  "message": "主动消息未开启，无法创建定时提醒"
}
```

如果用户定时消息开关关闭：

```json
{
  "ok": false,
  "error_code": "SCHEDULED_MESSAGES_DISABLED",
  "message": "用户定时消息未开启，无法创建定时提醒"
}
```

模型必须根据工具结果诚实回复，不得假装创建成功。

---

## 14. 勿扰时间

### 14.1 UI

```text
勿扰时间：
  开始 HH:mm
  结束 HH:mm
```

### 14.2 语义

勿扰时间内，不发送任何主动类消息。

包括：

```text
Bot 自主消息
用户定时消息
08:00 问好
23:00 总结
自定义定时任务
```

### 14.3 勿扰内到点处理

第一版规则：

| 类型 | 勿扰内到点 |
|---|---|
| Bot 自主消息 | 跳过 |
| 用户提醒 | 顺延到勿扰结束 |
| 08:00 问好 | 顺延到勿扰结束 |
| 23:00 总结 | 跳过 |
| 其他定时任务 | 顺延到勿扰结束 |

### 14.4 顺延限制

顺延任务需要记录：

```text
original_trigger_at
deferred_trigger_at
defer_reason = quiet_hours
```

防止重复顺延或无限循环。

---

## 15. 每日主动消息上限

### 15.1 UI

```text
每日主动消息上限：50 条
```

### 15.2 语义

用于防止异常情况下主动消息过多。

统计对象：

```text
所有主动类真实发出的消息条数
```

包括：

```text
Bot 自主消息发出的消息
用户定时消息发出的消息
08:00 问好
23:00 总结
自定义定时任务
```

不包括：

```text
用户主动发消息后的普通回复
dry-run 模拟消息
发送失败消息
guard 拦截消息
```

### 15.3 与自主触发上限的区别

| 名称 | 统计对象 | 默认来源 |
|---|---|---|
| 每日自主触发上限 | Bot 自主主动回合次数 | 强度档位 |
| 每日主动消息上限 | 主动类真实发出的消息条数 | 用户设置，默认 50 |

---

## 16. 配置结构建议

建议存储到 runtime settings 或数据库配置表，前端通过接口读写。

示例结构：

```yaml
proactive:
  enabled: true

  autonomous:
    enabled: true
    intensity: companion   # quiet / daily / caring / companion

    profiles:
      quiet:
        daily_trigger_cap: 2
        optional_after_minutes: 360
        required_after_minutes: 480
      daily:
        daily_trigger_cap: 5
        optional_after_minutes: 120
        required_after_minutes: 180
      caring:
        daily_trigger_cap: 10
        optional_after_minutes: 30
        required_after_minutes: 60
      companion:
        daily_trigger_cap: 20
        optional_after_minutes: 5
        required_after_minutes: 20

  scheduled:
    enabled: true

    defaults:
      morning_greeting:
        enabled: true
        time: "08:00"
        type: "SCHEDULED_GREETING"

      night_summary:
        enabled: true
        time: "23:00"
        type: "SCHEDULED_SUMMARY"

    custom_tasks:
      - id: "task_001"
        enabled: true
        time: "18:30"
        task_type: "WATER"
        repeat: "daily"

  quiet_hours:
    enabled: true
    start: "00:00"
    end: "08:00"

  safety:
    daily_active_message_cap: 50
```

### 16.1 UI 名称与内部枚举

| UI | 内部枚举 |
|---|---|
| 安静 | quiet |
| 日常 | daily |
| 关心 | caring |
| 陪伴 | companion |

---

## 17. 后端模块建议

### 17.1 新增/扩展模块

```text
agent/proactive_settings.py
agent/scheduled_triggers.py
agent/scheduled_trigger_store.py（或放入 memory/store.py）
```

### 17.2 职责划分

| 模块 | 职责 |
|---|---|
| proactive_settings.py | 读取/保存主动消息设置，提供当前强度参数 |
| scheduler.py | 根据设置决定是否进入 optional / required 触发 |
| scheduled_triggers.py | 管理用户定时任务到点触发 |
| tool_registry.py | 注册 create_scheduled_trigger |
| tools.py 或 dedicated module | 执行 create_scheduled_trigger |
| chat_agent.py | 支持 STATE_TOOL 流转 |
| output_guard.py | 继续负责主动消息输出守卫 |

---

## 18. 状态机工具分类

### 18.1 新增 STATE_TOOL

`create_scheduled_trigger` 是状态变更工具。

执行后：

```text
工具结果回灌模型
模型继续调用 wechat_reply 告诉用户结果
最后 done
```

### 18.2 STATE_TOOL 流转

```text
CLASSIFY_ONE_TOOL_CALL
→ EXEC_STATE_TOOL
→ APPEND_TOOL_RESULT
→ CALL_LLM
```

不要把它当 terminal。

不要把它当 fact tool。

它不是查询外部事实，而是修改系统状态。

---

## 19. 调度流程

### 19.1 Bot 自主消息 heartbeat

```text
heartbeat
→ 读取 proactive settings
→ 如果 proactive.enabled=false：跳过
→ 如果 autonomous.enabled=false：跳过
→ 如果处于勿扰时间：跳过
→ 如果每日主动消息上限已达：跳过
→ 如果每日自主触发上限已达：跳过
→ 读取 last_visible_message_at
→ 计算 elapsed
→ 如果 elapsed < optional_after：跳过
→ 如果 optional_after <= elapsed < required_after：optional trigger
→ 如果 elapsed >= required_after：required trigger
→ 构造主动上下文
→ 进入 ChatAgent 主动回合
```

### 19.2 用户定时消息 trigger

```text
scheduled heartbeat
→ 读取 proactive settings
→ 如果 proactive.enabled=false：跳过
→ 如果 scheduled.enabled=false：跳过
→ 找到 due scheduled triggers
→ 如果处于勿扰时间：
    Bot 自主消息：跳过
    提醒/问好/其他：顺延到勿扰结束
    23:00 总结：跳过
→ 如果每日主动消息上限已达：跳过
→ 构造 scheduled impulse
→ 进入 ChatAgent 主动回合
```

---

## 20. 主动回合上下文

### 20.1 必须包含

```json
{
  "trigger_kind": "autonomous",
  "trigger_mode": "optional",
  "trigger_source": "PRESENCE",
  "last_visible_message_at": "...",
  "elapsed_since_last_visible_message_seconds": 1234,
  "last_user_message": "...",
  "assistant_messages_after_last_user": [
    "..."
  ],
  "allowed_facts": [
    "用户有一段时间没有继续回复",
    "你之后已经发过若干条可见消息"
  ],
  "forbidden_claims": [
    "不要说检测到用户很久没回复",
    "不要声称用户正在做什么",
    "不要暴露触发机制"
  ],
  "style": "像普通微信聊天一样自然补一句"
}
```

### 20.2 Required Trigger 示例

```json
{
  "trigger_kind": "autonomous",
  "trigger_mode": "required",
  "trigger_source": "PRESENCE",
  "reason": "超过当前强度的最迟触发时间",
  "skip_allowed": false
}
```

### 20.3 Scheduled 示例

```json
{
  "trigger_kind": "scheduled",
  "trigger_source": "SCHEDULED_WATER",
  "task_label": "喝水",
  "user_created": true,
  "style": "像自然微信提醒，不要像系统通知"
}
```

---

## 21. API 计划

### 21.1 设置读取

```http
GET /admin/proactive-test/settings
```

返回当前设置。

### 21.2 设置保存

```http
POST /admin/proactive-test/settings
```

保存：

```json
{
  "enabled": true,
  "autonomous": {
    "enabled": true,
    "intensity": "companion"
  },
  "scheduled": {
    "enabled": true,
    "morning_greeting": {
      "enabled": true,
      "time": "08:00"
    },
    "night_summary": {
      "enabled": true,
      "time": "23:00"
    },
    "custom_tasks": []
  },
  "quiet_hours": {
    "enabled": true,
    "start": "00:00",
    "end": "08:00"
  },
  "safety": {
    "daily_active_message_cap": 50
  }
}
```

### 21.3 定时任务 CRUD

第一版可通过同一个 settings 接口保存 custom_tasks。

如果复杂度上升，再拆：

```http
POST /admin/proactive-test/scheduled-tasks
PATCH /admin/proactive-test/scheduled-tasks/{id}
DELETE /admin/proactive-test/scheduled-tasks/{id}
```

---

## 22. 前端设置页

### 22.1 页面结构

在测试台新增 Tab：

```text
主动消息设置
```

区域：

```text
1. 主动消息总开关
2. Bot 自主消息开关 + 强度选择
3. 用户定时消息开关 + 默认任务 + 自定义任务
4. 勿扰时间
5. 每日主动消息上限
```

### 22.2 强度 UI

```text
自主消息强度：
[安静] [日常] [关心] [陪伴]
```

每个档位下方展示人话说明：

```text
安静：每天最多 2 次触发；6 小时后开始判断；8 小时内必须触发。
日常：每天最多 5 次触发；2 小时后开始判断；3 小时内必须触发。
关心：每天最多 10 次触发；30 分钟后开始判断；60 分钟内必须触发。
陪伴：每天最多 20 次触发；5 分钟后开始判断；20 分钟内必须触发。
```

---

## 23. 测试计划

### 23.1 设置读取/保存

```text
test_get_proactive_settings_default
test_update_proactive_settings
test_master_switch_off_blocks_all_active_triggers
test_autonomous_switch_off_blocks_autonomous_only
test_scheduled_switch_off_blocks_scheduled_only
```

### 23.2 强度档位

```text
test_intensity_quiet_profile
test_intensity_daily_profile
test_intensity_caring_profile
test_intensity_companion_profile
test_optional_trigger_after_threshold
test_required_trigger_after_threshold
```

### 23.3 Required Trigger

```text
test_required_trigger_disallows_skip_proactive
test_required_trigger_requires_first_wechat_reply
test_required_trigger_guard_block_no_fixed_fallback
test_required_trigger_failure_records_reason
```

### 23.4 last_visible_message_at

```text
test_trigger_uses_last_visible_message_not_last_user_only
test_context_includes_last_user_message_and_assistant_messages_after_last_user
```

### 23.5 定时任务

```text
test_create_scheduled_trigger_tool_success
test_create_scheduled_trigger_fails_when_proactive_disabled
test_create_scheduled_trigger_fails_when_scheduled_disabled
test_due_scheduled_trigger_enters_proactive_flow
test_scheduled_trigger_does_not_direct_send
```

### 23.6 勿扰时间

```text
test_quiet_hours_blocks_autonomous
test_quiet_hours_defers_reminder
test_quiet_hours_defers_morning_greeting
test_quiet_hours_skips_night_summary
```

### 23.7 每日主动消息上限

```text
test_daily_active_message_cap_blocks_after_limit
test_dry_run_not_counted_in_daily_active_message_cap
test_failed_send_not_counted_in_daily_active_message_cap
test_user_normal_reply_not_counted_in_daily_active_message_cap
```

---

## 24. 分阶段实施建议

### Phase 1：设置页 + 设置持久化

做：

```text
主动消息总开关
Bot 自主消息开关
强度档位
用户定时消息开关
勿扰时间
每日主动消息上限
GET/POST settings API
```

不做：

```text
定时任务工具
真正 scheduled trigger 执行
```

### Phase 2：Scheduler 读取设置 + 高频自主触发

做：

```text
强度档位影响 optional_after / required_after / daily_trigger_cap
last_visible_message_at 计时
optional trigger
required trigger
required trigger 禁止 skip
主动上下文带 last_user_message + assistant_messages_after_last_user
```

### Phase 3：默认定时任务

做：

```text
08:00 问好
23:00 总结
勿扰处理
进入同一主动回合
```

### Phase 4：自定义定时任务 + create_scheduled_trigger 工具

做：

```text
create_scheduled_trigger STATE_TOOL
用户微信里创建提醒
到点后进入同一主动回合
```

### Phase 5：测试台联动

做：

```text
测试台显示当前设置
测试台可模拟强度档位
测试台可模拟 required trigger
测试台报告展示 trigger_mode optional/required/scheduled
```

---

## 25. 本期不做

```text
不开放任意自由文本定时任务
不做复杂 cron 表达式
不做人设/情绪/好感度联动
不让模型看到“陪伴模式”等人格暗示
不在 required trigger 中使用固定文案兜底
不让定时任务绕过 ChatAgent 直接发微信
不把普通聊天回复计入每日主动消息上限
```

---

## 26. 验收标准

### 26.1 设置页验收

```text
能开关主动消息总开关
能开关 Bot 自主消息
能选择安静/日常/关心/陪伴
能开关用户定时消息
能开关 08:00 问好
能开关 23:00 总结
能设置勿扰时间
能设置每日主动消息上限
设置刷新后仍保留
```

### 26.2 高频触发验收

以“陪伴”为例：

```text
距离最后一条可见消息 5 分钟后，系统允许进入 optional trigger。
距离最后一条可见消息 20 分钟后，系统进入 required trigger。
required trigger 不允许模型调用 skip_proactive。
required trigger 上下文包含 last_user_message 和 assistant_messages_after_last_user。
```

### 26.3 自然感验收

主动消息不应出现：

```text
检测到你很久没回复
根据设置我来找你
主动消息触发
系统提醒
```

主动消息应更像：

```text
你又不见啦。
我刚才还在想那个事。
那我先自己待会儿。
```

### 26.4 安全验收

```text
主动消息总开关关闭后，所有主动类消息都不能发。
勿扰时间内，Bot 自主消息必须跳过。
用户定时提醒落入勿扰时间，顺延到勿扰结束。
23:00 总结落入勿扰时间，跳过。
每日主动消息达到上限后，不再发送主动类消息。
发送失败不写历史。
guard 拦截不写历史。
```

---

## 27. 给实施者的关键提醒

1. 高频只影响触发，不影响人格。
2. 模型不要看到“陪伴模式”这种会改变人格的设置词。
3. 最迟触发不是固定兜底，不能滥用固定文案。
4. Required trigger 禁止 skip_proactive，但 guard 仍然必须保留。
5. 定时任务必须进入同一 ChatAgent / guard / history 回路。
6. 主动消息应像“想起你”，不是“通知你”。
7. 触发计时基于 last_visible_message_at。
8. 语义接话基于 last_user_message + assistant_messages_after_last_user。
