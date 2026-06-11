# 时间提醒被误解为当前消息已发送 22 分钟

## 现象

冷场超过 10 分钟后，系统在当前用户消息后注入时间提醒：

```text
<system-reminder>
Current time: 2026-06-11 21:00:45 Thursday. The previous conversation was 22 minutes ago. This is a system reminder. Use this information when relevant; otherwise ignore it.
</system-reminder>
```

模型将其理解为当前消息“嗯 还早”是 22 分钟前发送的，并在思考中写出：

```text
用户说"嗯 还早"，22分钟前的事了。现在晚上9点。
```

实际情况是：当前消息刚刚收到，22 分钟指当前消息与上一条用户消息之间的空闲间隔。

## 证据

- 上一条用户消息“算了 有点累”记录于 `2026-06-11 20:38:10`。
- 当前用户消息“嗯 还早”记录于 `2026-06-11 21:00:43`，请求于 `21:00:45` 进入聊天处理。
- 两条用户消息之间相隔约 22 分钟，因此间隔计算正确。
- 捕获的模型请求中，当前 `<channel>` 块与 `<system-reminder>` 块同属一个 `role:user` 的内容数组，且提醒紧跟在当前消息之后。
- 模型思考明确把 “22 minutes ago” 绑定到了当前用户消息。

## 已确认原因

这是提示词语义歧义，不是时间计算或历史查询错误。

`The previous conversation was 22 minutes ago.` 中的 `previous conversation` 没有明确指向“上一条用户消息”，也没有说明当前 `<channel>` 消息是刚收到的。提醒又紧跟当前消息，模型可能把 “22 minutes ago” 错误地修饰当前话语或本次交互。

## 影响

- 模型可能错误判断当前消息已经过时。
- 模型可能跳过本应立即回应的内容，或按“恢复旧话题”的方式作答。
- 冷场时间越长，错误时间归因对回复策略的影响越明显。

## 期望行为

提醒必须同时明确两个时间事实：

1. 最新 `<channel>` 消息是刚刚收到的。
2. 间隔描述的是上一条用户消息与最新消息之间的空闲时间。

模型不应把该间隔解释为最新消息的消息年龄。

## 可行方案

保留当前按需注入的用户消息标签方案，改为明确描述两个事件。建议文案：

```text
<system-reminder>
Current time: 2026-06-11 21:00:45 Thursday.
The latest <channel> message was received just now, after 22 minutes of inactivity since the previous user message.
Use this idle gap only as conversational context when relevant; otherwise ignore it.
</system-reminder>
```

同时建议在协议说明中补充：

```text
A time reminder describes the idle gap before the latest <channel> message. It never means the latest message itself is old.
```

主要修复应放在每次注入的提醒文案中，不能只依赖全局协议说明。

## 验收条件

- 最新消息被明确标记为 `received just now`。
- 时间差被明确标记为上一条用户消息到最新 `<channel>` 消息之间的空闲间隔。
- 不再使用 `The previous conversation was ... ago.`。
- 提醒不包含地点。
- 未达到冷场阈值时不注入提醒。
- 增加回归测试，验证生成文本的时间指代，并保留 10 分钟阈值行为。

## 状态

已确认，待修复。严重程度建议为中等：不会导致请求失败，但会直接影响模型对当前对话时间关系的理解和回复选择。
