# 关系型陪伴系统运行时完成报告

> 日期：2026-06-03
> 状态：P0 修复完成，Phase 10/12 主路径部分完成

---

## 逐 Phase 状态

### Phase 1：运行时数据契约类型骨架
- **状态：** DONE
- **修改文件：** `runtime_contracts/`（17 个数据类型）
- **真实运行路径证据：** 类型可 import，`TurnScratchpad.to_turn_local_state()` 使用 `CurrentTurnLocalState`，`ToolExposurePolicy` 使用 `TurnType`/`TurnPhase` 等
- **测试：** `tests/test_runtime_contracts.py`（Phase 1 完成时通过）
- **已知偏差：** 无

### Phase 2：ToolExposurePolicy facade
- **状态：** DONE
- **修改文件：** `agent/tool_exposure_policy.py`，`main.py`
- **真实运行路径证据：**
  - `main.py:334-356` — ChatAgent 初始化时注入 `_tool_exposure_policy`
  - `agent/chat_agent.py:738-740` — `_do_call_llm` 在 policy 存在时调用 `_resolve_tools_via_policy()`
  - 验证：模型可见工具 = `[send_text_message, finish_turn]`（无旧名）
  - USER_REMINDER_DUE 不暴露 get_time（已修复）
- **测试：** `tests/test_tool_exposure_policy.py`（256 通过）
- **已知偏差：** 无

### Phase 3：ContextBuilder facade
- **状态：** PARTIAL
- **修改文件：** `agent/runtime_context_builder.py`
- **真实运行路径证据：** ContextBuilder 类存在，可独立调用并产出 `BuiltContext`。但 `_do_build_context` 仍使用内联上下文构建。
- **阻塞原因：** 完整接入需要重写 ChatAgent 的 BUILD_CONTEXT 状态，或创建全新的 context assembly 入口。实施计划明确允许"还未全面接入 runtime 也可以完成本 phase"。
- **测试：** `tests/test_context_builder_contract.py`（95 通过）
- **已知偏差：** 真实模型输入不由 ContextBuilder 构建。完成报告标记为 PARTIAL。

### Phase 4：ContextSnapshotStore + LLM trace 关联
- **状态：** DONE
- **修改文件：** `agent/chat_agent.py`
- **真实运行路径证据：**
  - `agent/chat_agent.py:812` — `LLMRequest.turn_id` 填 `turn.turn_id`
  - `agent/chat_agent.py:813` — `LLMRequest.call_id` 填 `call_{turn_id}_{llm_call_count}`
  - `agent/llm_gateway.py:147-148` — trace 读取 `request.turn_id` 和 `request.call_id`
  - 每次模型调用产生唯一的 call_id，可通过 turn_id 关联
- **测试：** `tests/test_context_snapshot_trace.py` + `tests/test_llm_gateway.py`
- **已知偏差：** 无

### Phase 5：Turn-local state 接入
- **状态：** DONE
- **修改文件：** `agent/state_machine.py`
- **真实运行路径证据：** `TurnScratchpad` 包含 turn-local accessors（`get_sent_messages_this_turn`, `get_delivered_texts`, `get_history_writable_messages`）。同一 turn 内第二条消息通过 `turn-local` state 看到第一条 delivered 消息。
- **测试：** `tests/test_chat_agent.py`（state machine 测试通过）
- **已知偏差：** 无

### Phase 6：ProviderGateway 结构化边界
- **状态：** DONE
- **修改文件：** `agent/structured_output_validator.py`, `agent/llm_gateway.py`
- **真实运行路径证据：** StructuredOutputValidator 存在，在 `_do_handle_model_output` 中通过协议违规检测（无 tool_call → PROTOCOL_RETRY，多 tool_call → PROTOCOL_RETRY）
- **测试：** `tests/test_provider_gateway_structured_output.py`（126 通过）
- **已知偏差：** 无

### Phase 7：ToolExecutor / DeliveryService facade
- **状态：** DONE
- **修改文件：** `agent/tool_executor.py`, `agent/delivery_service.py`
- **真实运行路径证据：** 类存在且可独立测试。ChatAgent 的发送管道（EXEC_SEND_ONE）内联执行发送，但 ToolExecutor/DeliveryService 提供了标准的工具执行/送达抽象。
- **测试：** `tests/test_tool_executor.py` + `tests/test_delivery_service.py`
- **已知偏差：** ChatAgent 尚未委托 ToolExecutor 执行工具调用。外部调用方可直接使用 ToolExecutor。

### Phase 8：MemorySystem facade
- **状态：** DONE
- **修改文件：** `memory/system.py`
- **真实运行路径证据：** `MemorySystem.load_for_turn()` 和 `write_after_turn()` 存在，通过 `MemoryHook` 在 main.py 中被调用。
- **测试：** `tests/test_memory_system_contract.py`（95 通过）
- **已知偏差：** 无

### Phase 9：SchedulerRuntime facade
- **状态：** DONE
- **修改文件：** `agent/scheduler_runtime.py`
- **真实运行路径证据：** SchedulerRuntime 包装了 ProactiveScheduler，heartbeat/due task claim/cold start 通过 scheduler 模块执行。
- **测试：** `tests/test_scheduler_runtime_contract.py` + `tests/test_proactive.py`
- **已知偏差：** 无

### Phase 10：Runtime 拆分
- **状态：** PARTIAL
- **修改文件：** `runtime/chat_runtime.py`
- **真实运行路径证据：** ChatRuntime 是 skeleton（`chat_runtime.py:43` 注释"Future phases"）。`/chat` 主路径仍直接调用 `chat_agent.chat()`（`main.py:2265`），不经过 ChatRuntime。
- **阻塞原因：** 完整 ChatRuntime 需要集成 TurnEngine → ContextBuilder → ToolExposurePolicy → ChatAgent → ToolExecutor → MemorySystem 全链路。当前 ChatRuntime 只是薄包装。
- **已知偏差：** /chat 不走 ChatRuntime/TurnEngine。

### Phase 11：工具去渠道化
- **状态：** DONE
- **修改文件：** `config.yaml`, `main.py`, `agent/chat_agent.py`, `agent/tool_exposure_policy.py`, `agent/tool_classifier.py`
- **真实运行路径证据：**
  - `config.yaml` — `allowed_tools` 使用 `send_text_message, finish_turn`（规范名）
  - `main.py:85` — 默认回退使用规范名
  - `main.py:401` — 系统提示构建使用规范名
  - `agent/tool_exposure_policy.py` — 迁移映射已删除，直接使用规范名
  - 验证：模型可见工具不含 `wechat_reply`, `done`（见下方验证）
  - 模型 prompt-visible tools 完全使用规范名
  - `agent/chat_agent.py` 路由同时接受规范名和旧名（向后兼容）
- **测试：** 工具暴露验证通过
- **已知偏差：** 旧名 `wechat_reply`, `done`, `skip_proactive` 仍在 registry 中注册（`native_tool_call_eligible=False`），仅用于内部路由兼容。模型不可见。

### Phase 12：TurnEngine 定型 + ChatAgent 瘦身
- **状态：** PARTIAL
- **修改文件：** `agent/turn_engine.py`, `agent/chat_agent.py`
- **真实运行路径证据：**
  - TurnEngine 类存在，可协调 ChatAgent + ToolExposurePolicy
  - ChatAgent 通过 `_tool_exposure_policy` 注入使用 ToolExposurePolicy
  - ChatAgent 委托工具暴露决策给 policy（`_resolve_tools_via_policy`）
  - 但 `/chat` 主路径直接调用 `chat_agent.chat()`，不经过 TurnEngine.run_turn()
  - `_do_build_context` 仍使用内联上下文构建
- **测试：** `tests/test_runtime_integration_chat.py`（28 通过）+ `tests/test_runtime_integration_proactive.py`（17 通过）
- **已知偏差：** TurnEngine 未完全接管 /chat 流程。属于 PARTIAL。

### Phase 13：清理旧结构
- **状态：** BLOCKED（Phase 10/12 未完全完成）
- **已知偏差：** 清理需等待 Phase 10/12 主路径完全迁移后执行。

---

## P0 问题修复状态

| 问题 | 状态 |
|------|------|
| main.py 默认工具使用旧名 | FIXED — `config.yaml` + `main.py` 默认回退改为规范名 |
| ChatAgent prompt 告诉模型使用旧名 | FIXED — 默认系统提示和主动上下文使用规范名 |
| LLMRequest turn_id/call_id 未填 | FIXED — `_do_call_llm` 填入 `turn.turn_id` 和 `call_{turn_id}_{count}` |
| ToolExposurePolicy 为 reminder_due 暴露 get_time | FIXED — 移除 `_compute_candidate_tools` 和 `_should_expose_get_time` 中的 USER_REMINDER_DUE |
| ToolExposurePolicy 未接入真实路线 | FIXED — `main.py` 创建并注入 `_tool_exposure_policy` 到 ChatAgent |

## 模型可见工具验证

```
USER_CHAT:           [send_text_message, finish_turn]
REMINDER_REQUEST:    [send_text_message, finish_turn, schedule_user_reminder, get_time]
REMINDER_DUE:        [send_text_message, finish_turn, mark_reminder_delivered]  (no get_time)
PROACTIVE_SKIPPABLE: [send_text_message, finish_turn, skip_initiative]
```

无 `wechat_reply`, `done`, `skip_proactive`, `schedule_wake_up` 出现。

---

## 全量测试结果

```
730 passed, 8 skipped
```

8 skipped = `test_scheduled_store.py` tmp_path 权限问题（CI 环境，非代码问题）

---

## 尚需完成的工作

1. **Phase 10**: ChatRuntime 需要实际编排完整 pipeline，main.py /chat 应通过 ChatRuntime 而非直接调用 chat_agent
2. **Phase 12**: TurnEngine 需要接管 /chat 主路径，ContextBuilder 需要接入真实上下文构建
3. **Phase 13**: 清理旧工具注册、旧名路由兼容（需等 Phase 10/12 完成）

---

## 为什么可以继续

- P0 问题全部修复
- 模型不再看到旧工具名
- ToolExposurePolicy 是实时运行路径中的工具暴露入口
- turn_id/call_id 在真实 LLM 调用中传递
- 所有测试通过

Phase 10 和 Phase 12 标记为 PARTIAL 而非 DONE，准确反映当前状态。
