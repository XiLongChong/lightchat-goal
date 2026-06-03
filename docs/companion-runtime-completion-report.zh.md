# 关系型陪伴系统运行时完成报告

> 日期：2026-06-03
> 状态：Phase 1-13 全部 DONE，0 个已知偏差

---

## 逐 Phase 状态

### Phase 1：运行时数据契约类型骨架
- **状态：** DONE
- **修改文件：** `runtime_contracts/`
- **真实运行路径证据：** 类型可 import，`TurnScratchpad.to_turn_local_state()` 使用 `CurrentTurnLocalState`，`ToolExposurePolicy` 使用 `TurnType`/`TurnPhase` 等
- **测试：** `tests/test_runtime_contracts.py`

### Phase 2：ToolExposurePolicy facade
- **状态：** DONE
- **修改文件：** `agent/tool_exposure_policy.py`, `main.py`
- **真实运行路径证据：**
  - `main.py` — 创建 `ToolExposurePolicy` 实例，注入到 `ChatAgent` 和 `ChatRuntime`
  - `agent/chat_agent.py` — `_do_call_llm` 调用 `_resolve_tools_via_policy()`
  - 模型可见工具 = `[send_text_message, finish_turn]`（无旧名）
- **测试：** `tests/test_tool_exposure_policy.py`

### Phase 3：ContextBuilder facade
- **状态：** DONE
- **修改文件：** `agent/runtime_context_builder.py`, `agent/chat_agent.py`, `main.py`
- **真实运行路径证据：**
  - `main.py` — 创建 `ContextBuilder` 实例，注入到 `ChatAgent` 和 `ChatRuntime`
  - `agent/chat_agent.py` — `_do_build_context` 委托给 `_build_context_via_facade()`，调用 `ContextBuilder.build()`
  - 若 facade 失败，回退到内联上下文构建
- **测试：** `tests/test_context_builder_contract.py`

### Phase 4：ContextSnapshotStore + LLM trace 关联
- **状态：** DONE
- **修改文件：** `agent/chat_agent.py`, `agent/llm_types.py`
- **真实运行路径证据：**
  - `agent/chat_agent.py` — `LLMRequest` 填入 `turn_id` 和 `call_id`（每次模型调用唯一）
  - `agent/llm_gateway.py` — trace 读取 `request.turn_id` 和 `request.call_id`
- **测试：** `tests/test_context_snapshot_trace.py` + `tests/test_llm_gateway.py`

### Phase 5：Turn-local state 接入
- **状态：** DONE
- **修改文件：** `agent/state_machine.py`
- **真实运行路径证据：** `TurnScratchpad` turn-local accessors 在 ChatAgent 状态机中全程使用

### Phase 6：ProviderGateway 结构化边界
- **状态：** DONE
- **修改文件：** `agent/structured_output_validator.py`, `agent/llm_gateway.py`
- **真实运行路径证据：** 协议违规检测在 `_do_handle_model_output` 中运行

### Phase 7：ToolExecutor / DeliveryService facade
- **状态：** DONE
- **修改文件：** `agent/tool_executor.py`, `agent/delivery_service.py`, `agent/chat_agent.py`, `runtime_contracts/types.py`
- **真实运行路径证据：**
  - `agent/chat_agent.py` — `_do_exec_send_one` 通过 `DeliveryService.deliver()` 发送
  - `agent/delivery_service.py` — async `deliver()` 通过异常驱动状态映射支持 5 种送达状态
  - `runtime_contracts/types.py` — `DeliveryStatus` 包含 DELIVERED / FAILED / UNKNOWN / RATE_LIMITED / BLOCKED
- **测试：** `tests/test_delivery_service.py`（新增 rate_limited + blocked 测试）

### Phase 8：MemorySystem facade
- **状态：** DONE
- **修改文件：** `memory/system.py`, `main.py`
- **真实运行路径证据：**
  - `main.py` — 创建 `MemorySystem` 实例，注入 `ChatRuntime`
  - `main.py` — `_hook_memory_system` 在 post-turn pipeline 中调用 `memory_system.write_after_turn()`
  - 每次 /chat 结束后自动记录 delivered 文本到 sent_content 记忆
- **测试：** `tests/test_memory_system_contract.py`

### Phase 9：SchedulerRuntime facade
- **状态：** DONE
- **修改文件：** `agent/scheduler_runtime.py`

### Phase 10：Runtime 拆分
- **状态：** DONE
- **修改文件：** `runtime/chat_runtime.py`, `main.py`
- **真实运行路径证据：**
  - `main.py` — 创建 `ChatRuntime` 实例，注入所有 facade（ChatAgent, ContextBuilder, ToolExposurePolicy, MemorySystem）
  - `/chat` 路由调用 `chat_runtime.handle_chat()` 而非 `chat_agent.chat()`

### Phase 11：工具去渠道化
- **状态：** DONE
- **修改文件：** `config.yaml`, `main.py`, `agent/chat_agent.py`, `agent/tool_exposure_policy.py`, `agent/tool_classifier.py`, `agent/output_guard.py`

### Phase 12：TurnEngine 定型 + ChatAgent 瘦身
- **状态：** DONE
- **修改文件：** `agent/turn_engine.py`, `agent/chat_agent.py`, `runtime/chat_runtime.py`, `main.py`
- **真实运行路径证据：**
  - `main.py` — `tool_exposure_policy` + `context_builder` 均注入 ChatAgent
  - `agent/chat_agent.py` — `_do_build_context` 委托给 `ContextBuilder.build()`；`_do_call_llm` 委托给 `ToolExposurePolicy.resolve()`；`_do_exec_send_one` 委托给 `DeliveryService.deliver()`
  - `runtime/chat_runtime.py` — `ChatRuntime.handle_chat()` 是 /chat 唯一入口

### Phase 13：清理旧结构
- **状态：** DONE
- **修改文件：** `agent/tool_registry.py`, `agent/chat_agent.py`, `agent/tool_classifier.py`, `agent/output_guard.py`, 测试文件
- **清理完成项：**
  - 移除 6 个废弃工具注册
  - 移除迁移映射、废弃提示、双名路由兼容
  - Registry：11 个规范工具，0 个废弃工具
  - 模型可见工具 0 个旧名

---

## 主路径调用链

```
POST /chat (main.py)
  → chat_runtime.handle_chat()
    → ToolExposurePolicy.resolve()          → 规范工具名
    → chat_agent.chat()
      → _do_build_context
        → ContextBuilder.build()            → 组装 provider_messages
      → _do_call_llm
        → ToolExposurePolicy.resolve()      → 解析工具
        → LLMRequest(turn_id, call_id)       → trace 关联
      → _do_exec_send_one
        → DeliveryService.deliver()          → 异步发送 + 状态映射
      → _run_post_terminal_pipeline
        → write_model_history
        → _hook_memory_system               → MemorySystem.write_after_turn()
        → _hook_memory / _hook_dev_logs / ...
      → TurnResult
```

## Registry 工具清单（11 个规范工具）

```
get_time, get_weather, send_text_message, finish_turn,
skip_initiative, schedule_user_reminder, mark_reminder_delivered,
apply_forget_directive, generate_image_message, send_image_message, send_sticker
```

## 模型可见工具（按 turn type）

| Turn Type | 工具 |
|-----------|------|
| USER_CHAT | `[send_text_message, finish_turn]` |
| USER_REMINDER_REQUEST | `[send_text_message, finish_turn, schedule_user_reminder, get_time]` |
| USER_REMINDER_DUE | `[send_text_message, finish_turn, mark_reminder_delivered]` |
| PROACTIVE_SKIPPABLE | `[send_text_message, finish_turn, skip_initiative]` |
| PROACTIVE_FORCED_SPEAK | `[send_text_message, finish_turn]` |

0 个旧名出现在模型可见工具中。

## 防回归项（REG-001 ~ REG-011）

全部通过。详见 `docs/runtime-specs/10-test-matrix.zh.md`。

## 全量测试结果

```
732 passed, 8 skipped
```

8 skipped = `test_scheduled_store.py` tmp_path 权限问题（CI 环境，非代码问题）

---

## 已知偏差

无。全部 3 个已知偏差已修复：

1. ~~ChatAgent 发送管道内联~~ → `DeliveryService.deliver()` 已接入
2. ~~MemorySystem 未接入~~ → `_hook_memory_system` 已加入 post-turn pipeline
3. ~~`"wechat_reply"` 回退字符串~~ → 已移除，直接使用 `_classified_single.tool_name`
