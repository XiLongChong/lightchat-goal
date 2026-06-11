# 达到 15 条回复上限后持久化孤儿 tool_use（2026-06-11）

## 现象

单次用户交互达到 `MAX_REPLY_MESSAGES_PER_TURN = 15` 后，当前 turn 以
`end_max_limit` 结束。下一次对话调用 LLM 时，Provider 返回 HTTP 400：

```text
messages.122: `tool_use` ids were found without `tool_result` blocks immediately after:
call_00_YsnHYVK8OURZnh9G4xd09000
```

重启服务后同样会复现，因为不完整的工具轨迹已经写入数据库，并会在重建
`ConversationContext` 时重新载入。

## 复现证据

- 发生时间：2026-06-11 21:31:33 至 21:35:06（Asia/Shanghai）。
- 触发 turn：`turn_ef3885f78dff`。
- 触发 turn 最终记录：

```text
terminal=end_max_limit delivered=15 tool_results=15 iterations=15
```

- 数据库 `raw_events` 中该 turn 的最后两条事件为：

```text
seq=28 role=user      type=tool_result
seq=29 role=assistant type=tool_use
       id=call_00_YsnHYVK8OURZnh9G4xd09000
       name=send_text_message
```

- seq 29 后没有对应的 user `tool_result` 事件。
- 下一次用户消息“我得慢慢消化会”创建 `turn_18abb1790dc3` 后，第一次 LLM
  调用即因同一个 `call_00_YsnHYVK8OURZnh9G4xd09000` 缺少结果而返回 400。

## 已确认原因

这是达到回复上限时的控制流和事件持久化顺序错误。

当前执行顺序为：

1. 模型返回包含 `send_text_message` 的 assistant `tool_use`。
2. assistant message 立即追加到 `turn.messages`，并写入 `raw_events`。
3. `_send_text()` 实际执行工具并返回 `sent`。
4. 工具结果只先加入局部变量 `result_parts` 和 `turn.tool_results`。
5. `sent_message_count` 达到 15 后，代码立即设置 `END_MAX_LIMIT` 并跳出循环。
6. 用于追加和持久化 `tool_result_message` 的代码位于上限判断之后，因此没有执行。

所以孤儿调用不是“模型尝试发送第 16 条”造成的。实际情况是：**第 15 条工具
调用已经执行并被计数，但它的 `tool_result` 尚未写入历史，程序就暴力终止了
turn。**

该不完整轨迹同时存在于：

- 当前进程中的 `ConversationContext`：所以下一次对话不重启也会出错。
- 数据库 `raw_events`：所以重启并重建上下文后仍会出错。

## 影响

- 达到 15 条上限后，后续普通聊天全部被 Provider 以协议错误拒绝。
- 重启不能恢复，因为损坏状态已经持久化。
- 用户必须清除或修复历史才能继续聊天。
- `tool_results=15` 的 turn 日志会掩盖持久化历史中实际只有 14 个配对结果的事实。
- 任何在工具执行后、写入 `tool_result` 前终止 turn 的分支都有同类风险。

## 期望行为

- 每个已追加或持久化的 assistant `tool_use` 都必须紧跟对应的 user
  `tool_result`。
- 达到回复上限时，应先完整追加并持久化第 15 条工具结果，再终止 turn。
- 更稳妥的边界是在调用模型前或执行下一次发送工具前检查上限，向模型返回明确的
  `limit_reached` 结果，但无论采用哪种策略，都不能留下半条工具轨迹。
- 上下文重建应增加协议完整性校验，避免历史中的孤儿 `tool_use` 直接进入 Provider
  请求。
- 需要覆盖“同进程下一轮”和“重启后下一轮”两个回归测试。

## 状态

已确认，待修复。严重程度为高：达到可正常触发的回复上限后，会持续阻断该用户的
后续所有对话。
