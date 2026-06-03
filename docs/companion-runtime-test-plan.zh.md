# 关系型陪伴系统运行时测试设计

> 日期：2026-06-03
> 状态：测试设计草案
> 上游文档：
> - `companion-runtime-module-contracts.zh.md`
> - `companion-runtime-data-contracts.zh.md`
> - `companion-runtime-state-machines.zh.md`
> - `companion-runtime-implementation-plan.zh.md`
> 目的：定义开发批次的测试门槛，防止只验证“能跑”，没有验证上下文、工具、终态和状态更新。

---

## 0. 测试原则

测试必须覆盖四层：

```text
结构测试：数据对象和字段合法。
模块测试：单个模块输入输出正确。
状态机测试：turn 流转和终态正确。
集成测试：/chat、scheduler、memory、tool、delivery 串起来正确。
```

测试不能只断言最终回复文本。

必须断言：

- 模型看到什么。
- 工具暴露什么。
- 工具拒绝什么。
- 哪些消息写入历史。
- 哪些消息只在 turn-local。
- 哪些状态被更新。
- 哪些 trace 可以查。

---

## 1. 数据契约测试

### 文件建议

```text
tests/test_runtime_contracts.py
```

### 覆盖

- `InboundMessageBatch` 必填字段。
- `InboundMessageItem` 文本和媒体互斥/必填。
- `ConversationHistoryMessage.role` 只能是 user/assistant。
- `DeliveryReceipt.status` 枚举。
- `TerminalPolicy` 主动可跳过/强制开口配置。

### 关键断言

- 媒体摘要不能写入 `InboundMessageItem.text` 伪装成用户原话。
- failed delivery 不能构造成 assistant history。
- tool result 不能构造成 user history。

---

## 2. ToolExposurePolicy 测试

### 文件建议

```text
tests/test_tool_exposure_policy.py
```

### 用例

普通聊天：

```text
turn_type=user_chat
-> send_text_message
-> finish_turn
-> get_time/get_weather if enabled
```

用户请求图片：

```text
user_intent=request_image
-> generate_image_message exposed
```

主动可跳过：

```text
turn_type=relationship_proactive
previous_decision=spoke
-> send_text_message
-> skip_initiative
-> finish_turn
```

主动强制开口：

```text
turn_type=relationship_proactive
previous_decision=skipped
-> send_text_message
-> finish_turn
-> skip_initiative not exposed
```

用户提醒到期：

```text
turn_type=user_reminder_due
-> send_text_message
-> mark_reminder_delivered
-> finish_turn
```

### 关键断言

- prompt contract 工具名等于 native schema 工具名。
- schema 中每个工具都有 executor。
- 主动消息永远不暴露图片输出工具。
- 工具存在于注册表不等于本轮暴露。

---

## 3. ContextBuilder 测试

### 文件建议

```text
tests/test_context_builder_contract.py
```

### 上下文顺序测试

断言 block 顺序：

```text
fixed_protocol
persona
relationship_state
memory
recent_history
turn_local
media
current_input
tool_contract
terminal_policy
```

### 上下文污染测试

用例 1：本轮已发送消息

```text
recent_history:
  user: "你在吗"

turn_local:
  delivered assistant: "我在。"

expected:
  "我在。" 只出现在 turn_local block
  不出现在 recent_history block
```

用例 2：发送失败

```text
turn_local:
  failed assistant: "我在。"

expected:
  failed 状态可见
  不进入 recent_history
```

用例 3：工具结果

```text
tool_result:
  get_weather -> "上海 25 度"

expected:
  作为 tool result block
  不作为 user message
```

用例 4：媒体摘要

```text
image summary: "图片里是一只杯子"

expected:
  作为 media_context
  不作为 user text
```

### 快照测试

- snapshot 包含 block ids。
- snapshot 包含 exposed tool names。
- snapshot 包含 history message ids。
- snapshot 不包含 API key。

---

## 4. ProviderGateway 测试

### 文件建议

```text
tests/test_provider_gateway_structured_output.py
```

### 覆盖

- valid tool call 被解析。
- invalid JSON 被标记 invalid。
- missing tool call 不执行 side effect。
- provider error 归一化。
- raw request/response ref 记录。

### 关键断言

- 非法输出不会进入 ToolExecutor。
- repair retry 有上限。
- call_id 与 turn_id 关联。

---

## 5. ToolExecutor / DeliveryService 测试

### 文件建议

```text
tests/test_tool_executor.py
tests/test_delivery_service.py
```

### ToolExecutor

用例：

- 调用暴露工具成功。
- 调用未暴露工具被拒绝。
- 参数缺失被拒绝。
- 主动消息阶段非法工具被拒绝。

### DeliveryService

用例：

- send_text_message 成功返回 delivered。
- 发送失败返回 failed。
- unknown 不写入 history。

### 关键断言

- rejected tool call 有 reason。
- failed delivery 有 error。
- delivered receipt 才能持久化为 assistant history。

---

## 6. TurnEngine 状态机测试

### 文件建议

```text
tests/test_turn_engine_state_machine.py
```

### 普通聊天

```text
model -> send_text_message
tool -> delivered
model -> finish_turn
expected:
  terminal_state=finished
  delivered_message_count=1
  history append after finish
```

### 多条消息

```text
model1 -> send_text_message("A")
model2 sees turn_local delivered "A"
model2 -> send_text_message("B")
model3 -> finish_turn
expected:
  A/B both delivered
  A not in recent_history during model2 call
```

### 主动 skip

```text
model -> skip_initiative
model -> finish_turn(next_wake_minutes=45)
expected:
  skipped=true
  delivered_message_count=0
  next_wake set
```

### 主动 forced speak

```text
previous_decision=skipped
tool exposure excludes skip
model attempts skip_initiative
expected:
  rejected tool_not_exposed
```

### 用户提醒到期

```text
claim reminder
model -> send_text_message
tool -> delivered
model -> mark_reminder_delivered
model -> finish_turn
expected:
  reminder status delivered
```

---

## 7. MemorySystem 测试

### 文件建议

```text
tests/test_memory_system_contract.py
```

### 选择测试

- boundary memory 必须优先进入上下文。
- sent content memory 可被 proactive turn 读取。
- 隐藏记忆不进入上下文。
- do_not_surface 不进入模型。

### 写入测试

- turn 结束后才生成记忆候选。
- “别记这个”阻断前后窗口记忆。
- 负面情绪默认短期记忆。
- sent content 单独写入，不当作 relationship memory。

---

## 8. SchedulerRuntime 测试

### 文件建议

```text
tests/test_scheduler_runtime_contract.py
```

### 用例

主动关闭：

```text
features.proactive=false
expected:
  heartbeat no turn created
```

用户提醒优先：

```text
due reminder exists
cold start also eligible
expected:
  claim reminder
  no relationship proactive turn
```

未 claim 不执行：

```text
claim_due_task returns None
expected:
  no model call
```

主动冷启动：

```text
no due task
eligibility allowed
expected:
  relationship_proactive turn created
```

---

## 9. 集成测试

### 文件建议

```text
tests/test_runtime_integration_chat.py
tests/test_runtime_integration_proactive.py
tests/test_runtime_integration_reminder.py
```

### `/chat` 普通文本

验证：

- request 转成 InboundMessageBatch。
- TurnClassifier 输出 user_chat。
- ContextBuilder 生成 snapshot。
- ToolExposurePolicy 暴露普通工具。
- delivered 后写 history。

### `/chat` 图片

验证：

- image item 不伪装成 text。
- media summary 进入 media_context。
- 原始媒体不进长期 snapshot。

### 主动回合

验证：

- heartbeat 不直接生成文案。
- proactive turn 有 context snapshot。
- skip 后下一轮不暴露 skip。

### 用户提醒

验证：

- reminder request 创建任务。
- due reminder claim 后发送。
- delivered 后 mark delivered。

---

## 10. 回归测试清单

每个大批次至少跑：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tool_registry.py tests/test_native_tool_calls.py tests/test_chat_agent.py -q
```

涉及 scheduler 时加：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_proactive.py tests/test_scheduled_store.py -q
```

涉及 memory 时加：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_memory.py tests/test_temporal_context.py -q
```

完成阶段必须跑：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

---

## 11. 必须新增的防回归测试

这些测试是完成线，不是可选项：

1. 同一条 assistant 消息不能同时出现在 recent history 和 turn-local。
2. failed delivery 不能写入 assistant history。
3. tool result 不能作为 user message。
4. media summary 不能作为 user text。
5. prompt-visible tools 等于 native schema tools。
6. 未暴露工具调用被拒绝。
7. 主动 skip 后下一轮不暴露 skip。
8. 用户提醒到期优先于主动冷启动。
9. finish_turn 是终态来源，不靠自然语言结束。
10. context snapshot 可通过 turn_id 查询。
11. `wechat_reply` 从核心目标 runtime 删除后，有测试防止重新引入。

---

## 12. 测试完成定义

测试设计落地后，任何实现批次都不能只说“手测正常”。

完成必须包含：

- 本批次新增测试。
- 相关旧测试更新。
- 目标回归测试命令。
- 测试结果。
- 未覆盖风险说明。

