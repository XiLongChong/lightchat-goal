# 关系型陪伴系统运行时实施计划

> 日期：2026-06-03
> 状态：AI 开发执行手册
> 上游文档：
> - `docs/companion-architecture-requirements.zh.md`
> - `docs/companion-architecture-design-decisions.zh.md`
> - `docs/companion-runtime-module-contracts.zh.md`
> - `docs/companion-runtime-data-contracts.zh.md`
> - `docs/companion-runtime-state-machines.zh.md`
> - `docs/companion-runtime-gap-analysis.zh.md`
> - `docs/companion-runtime-test-plan.zh.md`
> - `docs/runtime-specs/*.zh.md`
>
> 目的：让后续 AI agent 可以按批次开发，而不是边读边猜、边做边改架构。

---

## 0. 这份文档怎么用

这份文档是派工入口。

后续开发 agent 不应只收到一句“阅读文档后开始开发”。每个 agent 必须收到一个具体 phase 的任务，并且严格遵守：

```text
只做当前 phase。
只改允许的文件。
必须读当前 phase 指定文档。
必须新增或更新当前 phase 指定测试。
完成后必须报告测试命令和结果。
没有通过审查，不进入下一 phase。
```

如果 agent 在实现中发现规格冲突，必须停止并报告冲突，不允许自行选择一个方向继续实现。

---

## 1. 全局实施原则

### 1.1 单批次原则

每次只执行一个 phase。

禁止：

- 一个 agent 同时做数据契约、ContextBuilder、ToolExposurePolicy 和 ChatAgent 瘦身。
- 为了让测试快点过，顺手修改无关模块。
- 在没有规格的情况下补业务判断。

### 1.2 先规格，后代码

运行时代码开发前，必须先确认 `docs/runtime-specs` 已冻结。

冻结不是指文档不能再改，而是指当前 phase 依赖的规格没有冲突。

### 1.3 旧兼容只能短期存在

迁移期间可以有 adapter，例如：

```text
send_text_message -> wechat_reply
finish_turn -> done
skip_initiative -> skip_proactive
```

但 adapter 必须：

- 标明删除 phase。
- 不进入最终完成线。
- 不在 prompt/native schema 中长期暴露旧名。

### 1.4 测试是完成定义的一部分

任何 phase 没有对应测试，不算完成。

“手测正常”“看起来没问题”“现有测试没跑”都不算完成。

---

## 2. 开发前规格冻结审查

在进入 Phase 1 前，必须先做一次只读审查。

### 必读

- `docs/runtime-specs/01-context-builder-spec.zh.md`
- `docs/runtime-specs/02-tool-exposure-policy-spec.zh.md`
- `docs/runtime-specs/03-turn-engine-spec.zh.md`
- `docs/runtime-specs/04-memory-system-spec.zh.md`
- `docs/runtime-specs/05-scheduler-runtime-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`

### 审查点

必须确认：

- `SystemTimeBlock` 的顺序表、伪代码、示例、测试矩阵一致。
- `get_time` 默认不暴露，只有规格允许的场景暴露。
- `get_weather` 是否默认暴露已有明确规则，并与测试矩阵一致。
- `skip_initiative` 和 `finish_turn` 是否同一次调用的规则一致。
- `consecutive_skip_count` 最大值和 forced speak 规则一致。
- 当前版本是否暴露 `search_memory` 已明确。
- 每个 turn type 都有终态。
- 每个 phase 都有测试覆盖。

### 如果发现冲突

停止开发，输出：

```text
发现的冲突：
涉及文档和行号：
建议修订方向：
不能继续开发的原因：
```

---

## 3. Agent 输出模板


---

## 4. Phase 1：运行时数据契约类型骨架

### 目标

新增运行时数据结构，不接入业务流程。

### 必读

- `docs/runtime-specs/01-context-builder-spec.zh.md`
- `docs/runtime-specs/02-tool-exposure-policy-spec.zh.md`
- `docs/runtime-specs/03-turn-engine-spec.zh.md`
- `docs/runtime-specs/04-memory-system-spec.zh.md`
- `docs/runtime-specs/05-scheduler-runtime-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`

### 允许修改

- 新增 `runtime_contracts/` 或等价低耦合包。
- 新增 `tests/test_runtime_contracts.py`。

### 禁止修改

- `main.py`
- `agent/chat_agent.py`
- `agent/scheduler.py`
- `agent/tool_registry.py`
- `agent/tools.py`
- `memory/store.py`
- `/chat` 行为

### 必须覆盖的类型

- `InboundMessageBatch`
- `InboundMessageItem`
- `MediaAttachment`
- `TurnRequest`
- `RuntimeTrigger`
- `TurnClassification`
- `CurrentTurnLocalState`
- `ConversationHistoryMessage`
- `ToolExposure`
- `BuiltContext`
- `ContextBlock`
- `ContextSnapshot`
- `ModelCallResult`
- `ToolExecutionResult`
- `DeliveryReceipt`
- `TerminalPolicy`
- `MemorySelection`
- `TurnResult`

### 必须测试

- 枚举值和必填字段。
- failed / unknown delivery 不能构造成 assistant history。
- tool result 不能构造成 user history。
- media summary 不能构造成 user text。
- `TerminalPolicy` 主动可跳过和 forced speak 配置正确。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_contracts.py -q
```

### 完成定义

- 新类型可 import。
- 无运行时副作用。
- 不改变任何现有业务行为。

---

## 5. Phase 2：ToolExposurePolicy facade

### 目标

建立单一工具暴露入口，先不删除旧工具。

### 必读

- `docs/runtime-specs/02-tool-exposure-policy-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `agent/tool_registry.py`
- `agent/tool_classifier.py`
- `agent/chat_agent.py` 中现有 allowed_tools / proactive phase 逻辑
- `main.py` 中 allowed_tools 相关逻辑

### 允许修改

- 新增 `agent/tool_exposure_policy.py`
- 新增 `tests/test_tool_exposure_policy.py`
- 如确有必要，可对现有 allowed_tools 构造点做最小接入

### 禁止修改

- 删除旧工具名。
- 大改 `ChatAgent` 状态机。
- 改 `/chat` 外部响应格式。
- 改 scheduler 行为。

### 实现要求

- 目标语义使用 `send_text_message`, `finish_turn`, `skip_initiative`。
- 迁移期可映射到 `wechat_reply`, `done`, `skip_proactive`。
- `get_time` 默认不暴露。
- `get_weather` 按规格决定是否默认暴露，不允许测试矩阵和实现不一致。
- prompt-visible tools 必须等于 native schema tools。
- schema 中每个工具必须有 executor。

### 必须测试

按 `10-test-matrix.zh.md` 的 ToolExposurePolicy 部分执行。

重点：

- 普通聊天默认工具集。
- 用户请求图片工具集。
- 用户提醒请求工具集。
- 用户提醒到期工具集。
- 主动 skippable。
- 主动 forced speak。
- 主动 after-first-message。
- 非主动回合不暴露 `skip_initiative`。
- `get_time` 三类暴露条件。
- prompt/schema/executor 一致。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tool_exposure_policy.py tests/test_tool_registry.py tests/test_native_tool_calls.py -q
```

### 完成定义

- ToolExposurePolicy 可独立测试。
- 旧工具名只作为 adapter 或现有 runtime 兼容存在。
- 不引入新的 prompt/schema 不一致路径。

---

## 6. Phase 3：ContextBuilder facade

### 目标

建立目标意义上的 ContextBuilder，先包装现有上下文，不一次性重写模型行为。

### 必读

- `docs/runtime-specs/01-context-builder-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `agent/context_builder.py`
- `agent/temporal_context.py`
- `main.py` 中 `build_enriched_system_prompt()` 和 history 组装逻辑
- `agent/chat_agent.py` 中 `_do_build_context` 相关逻辑

### 允许修改

- 新增 `agent/runtime_context_builder.py` 或扩展 `agent/context_builder.py`
- 新增 `tests/test_context_builder_contract.py`
- 新增 snapshot 纯内存结构或临时测试结构

### 禁止修改

- 改实际 `/chat` 行为。
- 删除 `TemporalContextBuilder`。
- 删除旧 prompt。
- 改工具执行行为。

### 实现要求

- 严格按规格顺序构建 context blocks。
- `SystemTimeBlock` 位置必须和规格表、伪代码、测试矩阵一致。
- `recent_history` 只含已完成历史。
- `current_turn_local_state` 包含本轮刚发送消息、工具结果、送达状态。
- 本轮 delivered 消息在 turn 结束前不进入 recent_history。
- failed / unknown 不进入 assistant history。
- tool result 不伪装成 user message。
- media summary 不伪装成 user text。
- snapshot 不回灌模型。

### 必须测试

- block 顺序。
- SystemTimeBlock 注入。
- turn-local 与 history 分离。
- failed delivery 不进 history。
- tool result role 不是 user。
- media summary role 不是 user text。
- snapshot 包含 exposed tool names 和 history ids。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_context_builder_contract.py tests/test_temporal_context.py -q
```

### 完成定义

- ContextBuilder 可独立输出 `BuiltContext` 和 `ContextSnapshot`。
- 还未全面接入 runtime 也可以完成本 phase。

---

## 7. Phase 4：ContextSnapshotStore 与 LLM trace 关联

### 目标

每次模型调用可通过 `turn_id/call_id` 查到输入摘要和原始模型 I/O 引用。

### 必读

- `docs/runtime-specs/01-context-builder-spec.zh.md`
- `docs/runtime-specs/03-turn-engine-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `agent/llm_gateway.py`
- `agent/llm_types.py`
- `memory/store.py` 中 trace/diagnostics 相关方法
- `main.py` 中 `_on_llm_trace`

### 允许修改

- `agent/llm_gateway.py`
- `agent/llm_types.py`
- `memory/store.py` 中 trace/snapshot 相关最小新增
- 新增 `tests/test_context_snapshot_trace.py`

### 禁止修改

- 改模型 provider 行为。
- 改工具执行行为。
- 改 ChatAgent 主状态机。

### 必须测试

- snapshot 不保存 API key。
- turn_id 可查 snapshot。
- call_id 可关联 raw request/response 引用。
- exposed tool names 进入 snapshot。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_context_snapshot_trace.py tests/test_llm_gateway.py -q
```

---

## 8. Phase 5：Turn-local state 接入

### 目标

显式区分已完成历史、本轮已发送消息、工具结果、送达状态。

### 必读

- `docs/runtime-specs/01-context-builder-spec.zh.md`
- `docs/runtime-specs/03-turn-engine-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `agent/state_machine.py`
- `agent/chat_agent.py`
- `main.py` 中 history 写入回调

### 允许修改

- `agent/state_machine.py`
- `agent/chat_agent.py` 中最小 turn-local 接入
- `main.py` 中 history 写入回调的最小调整
- 新增/更新状态机测试

### 禁止修改

- 工具去渠道化改名。
- Runtime 大拆分。
- MemoryStore 大迁移。

### 必须测试

- 同一轮发送两条消息，第二次模型调用能看到第一条 delivered。
- 第一条在 turn 结束前不进入 recent history。
- 发送失败不写 assistant history。
- tool result 不进入 recent history。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_chat_agent.py tests/test_context_builder_contract.py -q
```

---

## 9. Phase 6：ProviderGateway 结构化边界

### 目标

强化结构化输出校验，让非法模型输出不进入工具执行。

### 必读

- `docs/runtime-specs/03-turn-engine-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `agent/llm_gateway.py`
- `agent/llm_types.py`
- `tests/test_native_tool_calls.py`

### 允许修改

- `agent/llm_gateway.py`
- `agent/llm_types.py`
- `agent/chat_agent.py` 中模型输出校验边界
- `tests/test_native_tool_calls.py`
- 新增 `tests/test_provider_gateway_structured_output.py`

### 禁止修改

- 改外部工具行为。
- 改记忆系统。
- 改 scheduler。

### 必须测试

- 无 tool call 不执行 side effect。
- 纯 assistant.content 不发送给用户。
- 非法 args 不执行工具。
- repair retry 有上限。
- 同一次调用中终态工具组合规则符合规格。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_native_tool_calls.py tests/test_provider_gateway_structured_output.py tests/test_chat_agent.py -q
```

---

## 10. Phase 7：ToolExecutor / DeliveryService facade

### 目标

把工具执行和真实发送抽象成统一接口，先内部调用旧发送链路。

### 必读

- `docs/runtime-specs/03-turn-engine-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `agent/tools.py`
- `agent/chat_agent.py` 中 EXEC_SEND_ONE / fact tool 执行
- `main.py` 中 `wechat_sender`

### 允许修改

- 新增 `agent/tool_executor.py`
- 新增 `agent/delivery_service.py`
- 新增 `tests/test_tool_executor.py`
- 新增 `tests/test_delivery_service.py`
- 对 ChatAgent 做最小接入

### 禁止修改

- 改工具目标命名。
- 删除旧发送回调。
- 大拆 main.py。

### 必须测试

- 未暴露工具拒绝执行。
- 参数非法拒绝执行。
- delivered 返回 receipt。
- failed / unknown 不写 history。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tool_executor.py tests/test_delivery_service.py tests/test_chat_agent.py -q
```

---

## 11. Phase 8：MemorySystem facade

### 目标

建立 `MemorySystem.load_for_turn()` 和 `write_after_turn()`，先复用现有 MemoryStore。

### 必读

- `docs/runtime-specs/04-memory-system-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `memory/store.py`
- `agent/memory_worker.py`
- `agent/memory_hook.py`
- `tests/test_memory.py`

### 允许修改

- 新增 `memory/system.py`
- 新增/扩展 memory 相关测试
- 对 ContextBuilder 使用 MemorySelection 的最小接入

### 禁止修改

- 大规模数据库重构。
- 改 scheduler。
- 改 ChatAgent 状态机。

### 必须测试

- boundary memory 优先。
- hidden/do_not_surface 不入上下文。
- “别记这个”阻断写入。
- sent content memory 独立于 relationship memory。
- bot_id 隔离。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_memory.py tests/test_memory_system_contract.py -q
```

---

## 12. Phase 9：SchedulerRuntime facade

### 目标

把 heartbeat、due task claim、主动 cold start 顺序做成显式 runtime。

### 必读

- `docs/runtime-specs/05-scheduler-runtime-spec.zh.md`
- `docs/runtime-specs/02-tool-exposure-policy-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `agent/scheduler.py`
- `agent/proactive_settings.py`
- `memory/store.py` scheduled 相关方法
- `main.py` heartbeat loop

### 允许修改

- 新增 `runtime/scheduler_runtime.py` 或等价 facade
- 对 `agent/scheduler.py` 做包装或小步迁移
- 新增 `tests/test_scheduler_runtime_contract.py`

### 禁止修改

- 改 ChatAgent 主状态机。
- 改工具命名。
- 改 admin UI。

### 必须测试

- 主动关闭不创建 turn。
- due reminder 优先于 relationship proactive。
- claim 失败不调用模型。
- skip 后下一轮 forced speak。
- quiet hours。
- 技术熔断。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler_runtime_contract.py tests/test_proactive.py tests/test_scheduled_store.py -q
```

---

## 13. Phase 10：Runtime 拆分

### 目标

逐步把 `main.py` 变成启动、依赖构造和路由挂载。

### 必读

- `docs/runtime-specs/03-turn-engine-spec.zh.md`
- `docs/runtime-specs/05-scheduler-runtime-spec.zh.md`
- `docs/companion-runtime-gap-analysis.zh.md`
- `main.py`

### 子批次

必须按顺序做，不建议并行：

1. `ChatRuntime.handle_chat()`
2. `SchedulerRuntime` / `ProactiveRuntime`
3. `MemoryAdminRuntime`
4. `WechatRuntime`
5. routes 拆分

### 允许修改

- 新增 `runtime/`
- 新增 route 模块
- 小步迁移 main.py 逻辑
- 新增 runtime 集成测试

### 禁止修改

- 同时做工具去渠道化。
- 同时重写 ChatAgent。
- 同时做 MemoryStore 大迁移。

### 必须测试

- `/chat` 行为不回归。
- admin/proactive 行为不回归。
- health/admin/wechat 关键路由仍可导入。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_chat_security.py tests/test_chat_agent.py tests/test_proactive.py -q
```

---

## 14. Phase 11：工具去渠道化

### 目标

从目标语义工具名替换旧工具名。

### 必读

- `docs/runtime-specs/02-tool-exposure-policy-spec.zh.md`
- `docs/runtime-specs/03-turn-engine-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `agent/tool_registry.py`
- `agent/tool_classifier.py`
- `agent/chat_agent.py`
- `tests/test_tool_registry.py`
- `tests/test_native_tool_calls.py`

### 顺序

1. 增加新工具名，内部 alias 到旧 executor。
2. prompt/schema 改用新工具名。
3. tests 改用新工具名。
4. 删除旧工具名 native 暴露。
5. 删除旧兼容。

### 映射

```text
wechat_reply -> send_text_message
done -> finish_turn
skip_proactive -> skip_initiative
schedule_wake_up -> schedule_user_reminder / update_next_wake
```

### 禁止

- 长期同时暴露新旧工具给模型。
- prompt 中解释“send_text_message 就是微信发送”。
- adapter 超过 Phase 13 仍存在。

### 必须测试

- 核心 runtime 不再出现 `wechat_reply`。
- 新工具名 native schema 正确。
- 旧工具名在清理后 `tool_not_exposed`。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tool_registry.py tests/test_native_tool_calls.py tests/test_tool_exposure_policy.py tests/test_chat_agent.py -q
```

---

## 15. Phase 12：ChatAgent 瘦身 / TurnEngine 定型

### 目标

让 ChatAgent 或其替代物只负责状态机，不再负责上下文、工具暴露、发送细节和记忆 hook 构造。

### 必读

- `docs/runtime-specs/03-turn-engine-spec.zh.md`
- `docs/runtime-specs/10-test-matrix.zh.md`
- `agent/chat_agent.py`
- `agent/state_machine.py`

### 顺序

1. 移除上下文构建职责。
2. 移除工具暴露职责。
3. 移除发送细节。
4. 移除 memory hook 构造。
5. 拆状态 handler。

### 禁止

- 一次性重写整个 ChatAgent。
- 改变终态语义。
- 绕过已建立的 ContextBuilder / ToolExposurePolicy / ToolExecutor。

### 必须测试

- 所有 TurnEngine 状态机测试。
- 跨模块集成测试。
- 防回归清单 REG-001 到 REG-011。

### 建议测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_turn_engine_state_machine.py tests/test_chat_agent.py tests/test_runtime_integration_chat.py -q
```

---

## 16. Phase 13：清理旧结构和最终完成线

### 目标

删除长期兼容，不留下结构债。

### 清理项

- 旧 prompt 双来源。
- 旧工具名。
- deprecated tools。
- 旧 scheduled payload 猜测。
- 无效测试。
- 过时注释。
- 临时 adapter。

### 必须验证

- `main.py` 不再是业务真相中心。
- ContextBuilder 是唯一模型输入入口。
- ToolExposurePolicy 是唯一工具暴露入口。
- TurnEngine 不直接拼 prompt。
- MemorySystem 控制记忆选择和写入。
- failed / unknown delivery 不进入 assistant history。
- `wechat_reply` 不在核心 runtime 暴露给模型。

### 最终测试命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

### 完成报告

必须新增：

```text
docs/companion-runtime-completion-report.zh.md
```

报告包含：

- 每个 phase 完成状态。
- 每个防回归项证据。
- 全量测试结果。
- 剩余风险。
- 是否仍存在临时兼容。

---

## 17. 推荐 AI 开发节奏

### 最小安全节奏

每个 phase 单独一个 agent，完成后人工或审查 agent 复核。

推荐：

```text
Phase 1 -> 审查
Phase 2 -> 审查
Phase 3 -> 审查
Phase 4/5 可小范围并行，但合并前必须互审
Phase 6/7 顺序执行
Phase 8/9 可并行
Phase 10 以后必须顺序执行
Phase 11/12/13 必须顺序执行
```

### 不建议并行的阶段

- Phase 2 和 Phase 11：都碰工具名和工具暴露，不能并行。
- Phase 3 和 Phase 5：都碰上下文和 turn-local，最好先 Phase 3 再 Phase 5。
- Phase 10 和 Phase 12：都拆 runtime/ChatAgent，不能并行。

---

## 18. 每个 phase 的完成报告模板

```text
## Phase N completion

### Scope
- ...

### Files changed
- ...

### Behavior changed
- yes/no
- details:

### Tests
- command:
- result:

### Spec conformance
- referenced spec sections:
- known deviations:

### Temporary compatibility
- none / details:
- removal phase:

### Risks
- ...

### Ready for next phase
- yes/no
```

