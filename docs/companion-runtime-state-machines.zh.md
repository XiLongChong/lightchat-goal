# 关系型陪伴系统运行时状态机设计

> 日期：2026-06-03
> 状态：可实现设计草案
> 上游文档：
> - `docs/companion-runtime-module-contracts.zh.md`
> - `docs/companion-runtime-data-contracts.zh.md`
> 目的：定义 turn 的流转、终态、失败收尾和不同 turn type 的差异。

---

## 0. 总原则

每个 turn 都必须满足：

```text
有开始。
有状态。
有预算。
有终态。
有持久化收尾。
有可审计证据。
```

不能出现：

- 模型说一句自然语言就算结束。
- 工具执行失败后状态不明。
- 发送失败但写入历史。
- 本轮已发送内容混入历史导致上下文污染。
- 达到预算上限后继续调用模型。

---

## 1. 通用 turn 状态机

```text
TURN_CREATED
-> LOAD_RUNTIME_STATE
-> CLASSIFY_TURN
-> RESOLVE_TOOL_EXPOSURE
-> SELECT_MEMORY
-> BUILD_CONTEXT
-> SAVE_CONTEXT_SNAPSHOT
-> CALL_MODEL
-> VALIDATE_MODEL_OUTPUT
-> VALIDATE_TOOL_CALL
-> EXECUTE_TOOL
-> UPDATE_TURN_LOCAL_STATE
-> CHECK_TERMINAL
-> if not terminal: BUILD_CONTEXT
-> PERSIST_TURN_RESULT
-> POST_TURN_MEMORY
-> RELEASE_LOCK
```

### 状态解释

`TURN_CREATED`：创建 turn_id，记录来源。

`LOAD_RUNTIME_STATE`：加载用户状态、关系状态、主动状态、提醒状态。

`CLASSIFY_TURN`：判断 turn_type。

`RESOLVE_TOOL_EXPOSURE`：确定本次模型可见工具。

`SELECT_MEMORY`：选择本轮可能需要的记忆。

`BUILD_CONTEXT`：构建模型输入。

`SAVE_CONTEXT_SNAPSHOT`：保存摘要级输入快照。

`CALL_MODEL`：调用 ProviderGateway。

`VALIDATE_MODEL_OUTPUT`：校验结构化输出。

`VALIDATE_TOOL_CALL`：校验工具是否暴露、参数是否合法、阶段是否允许。

`EXECUTE_TOOL`：执行工具。

`UPDATE_TURN_LOCAL_STATE`：记录工具结果、送达状态、计数。

`CHECK_TERMINAL`：判断是否满足终态。

`PERSIST_TURN_RESULT`：写历史、写 outbound、写 turn diagnostics。

`POST_TURN_MEMORY`：处理记忆候选。

`RELEASE_LOCK`：释放用户 turn 锁。

---

## 2. 普通聊天 turn

### 触发

用户发送文字、图片、语音或组合输入。

### 工具暴露

默认：

```text
send_text_message
finish_turn
get_time
get_weather
```

用户明确要求图片时额外暴露：

```text
generate_image_message
send_image_message
send_sticker
```

### 终态

```text
finish_turn
```

### 流程

```text
user input
-> build context with recent history and current input
-> model calls send_text_message or information tool
-> if sent: add delivered message to CurrentTurnLocalState
-> model may continue
-> model calls finish_turn
-> delivered messages are persisted to history
```

### 允许行为

- 可以不回复，但必须 `finish_turn(status=no_reply)`。
- 可以多条消息，但受消息上限控制。
- 可以先查时间/天气，再回复。

### 禁止行为

- 不允许调用主动专用 `skip_initiative`。
- 不允许在用户未请求图片时主动发图片。
- 不允许把工具结果当作用户消息写入历史。

---

## 3. 用户提醒请求 turn

### 触发

用户说：

```text
十分钟后提醒我喝水。
晚上九点叫我睡觉。
明天提醒我交材料。
```

### 工具暴露

```text
send_text_message
schedule_user_reminder
finish_turn
get_time
```

### 终态

```text
schedule_user_reminder
-> send_text_message
-> finish_turn
```

也允许先发确认，再安排提醒，但最终必须能判断是否创建提醒成功。

### 失败路径

- 时间无法解析：模型应追问或 `finish_turn(status=needs_clarification)`。
- schedule 工具失败：不能假装已经安排，应告诉用户失败或进入安全结束。

---

## 4. 用户提醒到期 turn

### 触发

SchedulerRuntime claim 到一个到期的用户授权提醒。

### 工具暴露

```text
send_text_message
mark_reminder_delivered
finish_turn
```

### 终态

```text
send_text_message
-> mark_reminder_delivered
-> finish_turn
```

### 规则

- 用户授权提醒不使用主动冷启动的 skip 规则。
- 如果发送失败，不能标记 delivered。
- 如果 mark 失败，必须记录 diagnostics，避免下次重复发送无法解释。

---

## 5. 关系型主动 turn

### 触发

SchedulerRuntime 判断用户长时间没说话，且主动 gate 允许。

### 可跳过阶段工具

```text
send_text_message
skip_initiative
finish_turn
```

### 强制开口阶段工具

```text
send_text_message
finish_turn
```

### 可跳过阶段终态

```text
skip_initiative(reason)
-> finish_turn(next_wake_minutes)
```

或：

```text
send_text_message
-> finish_turn(next_wake_minutes)
```

### 强制开口阶段终态

```text
send_text_message
-> finish_turn(next_wake_minutes)
```

### 状态更新

主动 turn 结束后必须更新：

```text
InitiativeState
- last_trigger_at
- last_spoke_at or last_skipped_at
- consecutive_skip_count
- next_wake_after
- last_decision
```

### 规则

- 如果上一轮主动 skipped，下一轮不暴露 `skip_initiative`。
- 主动消息禁止图片、语音和表情工具。
- 如果没有新鲜话题，允许 skip，但不能连续 skip。
- 每次结束必须给出下一次唤醒窗口。

---

## 6. 固定仪式 turn

### 触发

例如早安、晚安、睡前关心。

### 工具暴露

```text
send_text_message
finish_turn
```

### 规则

- 不应该机械重复。
- 应看最近发送内容和关系状态。
- 用户处于勿扰或关闭仪式时不触发。
- 默认不暴露 skip，除非系统 gate 允许“本次仪式可跳过”。

---

## 7. 管理台手动触发 turn

### 触发

管理员手动测试或触发主动消息。

### 规则

- 必须标记 `trigger_source=admin`。
- 可以选择 dry-run，不真正发送。
- dry-run 仍应生成 context snapshot。
- 不应污染真实用户历史，除非明确选择 real-send。

---

## 8. 记忆管理 turn

### 触发

用户说：

```text
别记这个。
忘了刚才那些。
不要再提这件事。
```

### 工具暴露

```text
apply_forget_directive
send_text_message
finish_turn
```

### 规则

- 不追问原因。
- 执行忘记/屏蔽优先于记忆抽取。
- 前后窗口禁止记忆。
- 回复语气由 persona 控制，不固定死文案。

---

## 9. 结构化输出失败

### 触发

模型返回：

- 无 tool call。
- 非法 JSON。
- tool arguments 无法解析。
- 同时调用多个互斥工具。

### 流程

```text
invalid model output
-> do not execute side-effect tools
-> record invalid_output
-> if retry_budget remains: call model with repair instruction
-> else safe_finish_turn
```

### 规则

- 无论模型自然语言说得多合理，都不能绕过结构校验。
- 修复重试使用同一 context snapshot 的引用，不重新加入污染内容。

---

## 10. 工具执行失败

### 分类

```text
tool_not_exposed
invalid_arguments
invalid_phase
executor_missing
delivery_failed
state_write_failed
external_timeout
```

### 处理

- `tool_not_exposed`：拒绝，要求模型改用可用工具或结束。
- `invalid_arguments`：允许结构修复重试。
- `executor_missing`：系统配置错误，安全结束。
- `delivery_failed`：记录失败，不写 assistant history。
- `state_write_failed`：不伪造状态成功。

---

## 11. 预算上限

每轮 turn 必须有预算：

```text
max_model_calls
max_tool_calls
max_delivered_messages
max_send_attempts
max_elapsed_seconds
```

达到预算后：

```text
stop new model calls
stop external side effects
record budget_exhausted
persist diagnostics
safe terminal
```

主动消息和普通聊天都允许最多 15 条已送达消息，但这只是硬上限，不是推荐数量。

---

## 12. 持久化收尾

本轮结束后按固定顺序：

```text
1. persist delivered outbound messages
2. append delivered messages to conversation history
3. persist turn diagnostics
4. persist context snapshots
5. update initiative/reminder state
6. run memory write policy
7. release lock
```

### 约束

- 未送达消息不写 history。
- 记忆写入在 turn 结束后。
- snapshot 不是下一轮历史。

