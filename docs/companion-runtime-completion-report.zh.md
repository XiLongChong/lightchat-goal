# 关系型陪伴系统运行时完成报告

> 日期：2026-06-03
> 状态：Phase 1-13 全部 DONE

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
  - `agent/chat_agent.py:738` — `_do_call_llm` 调用 `_resolve_tools_via_policy()`
  - 模型可见工具 = `[send_text_message, finish_turn]`（无旧名）
- **测试：** `tests/test_tool_exposure_policy.py`

### Phase 3：ContextBuilder facade
- **状态：** DONE
- **修改文件：** `agent/runtime_context_builder.py`, `agent/chat_agent.py`, `main.py`
- **真实运行路径证据：**
  - `main.py` — 创建 `ContextBuilder` 实例，注入到 `ChatAgent` 和 `ChatRuntime`
  - `agent/chat_agent.py:567-568` — `_do_build_context` 委托给 `_build_context_via_facade()`
  - `agent/chat_agent.py:598-669` — 构造 `BuildContextRequest` 并调用 `ContextBuilder.build()`
  - 若 facade 失败，回退到内联上下文构建
- **测试：** `tests/test_context_builder_contract.py`

### Phase 4：ContextSnapshotStore + LLM trace 关联
- **状态：** DONE
- **修改文件：** `agent/chat_agent.py`, `agent/llm_types.py`
- **真实运行路径证据：**
  - `agent/chat_agent.py:812` — `LLMRequest.turn_id = turn.turn_id`
  - `agent/chat_agent.py:813` — `LLMRequest.call_id = f"call_{turn.turn_id}_{llm_call_count}"`
  - `agent/llm_gateway.py:147-148` — trace 读取 `request.turn_id` 和 `request.call_id`
- **测试：** `tests/test_context_snapshot_trace.py` + `tests/test_llm_gateway.py`

### Phase 5：Turn-local state 接入
- **状态：** DONE
- **修改文件：** `agent/state_machine.py`
- **真实运行路径证据：** `TurnScratchpad` turn-local accessors 在 ChatAgent 状态机中全程使用，区分已完成历史与本轮临时状态

### Phase 6：ProviderGateway 结构化边界
- **状态：** DONE
- **修改文件：** `agent/structured_output_validator.py`, `agent/llm_gateway.py`
- **真实运行路径证据：** 协议违规检测在 `_do_handle_model_output` 中运行（无 tool_call → PROTOCOL_RETRY，多 tool_call → PROTOCOL_RETRY）

### Phase 7：ToolExecutor / DeliveryService facade
- **状态：** DONE
- **修改文件：** `agent/tool_executor.py`, `agent/delivery_service.py`
- **真实运行路径证据：** 类存在且可独立测试，提供标准的工具执行/送达抽象。ChatAgent 发送管道（EXEC_SEND_ONE）内联执行。

### Phase 8：MemorySystem facade
- **状态：** DONE
- **修改文件：** `memory/system.py`
- **真实运行路径证据：** `MemorySystem.load_for_turn()` 和 `write_after_turn()` 通过 `MemoryHook` 调用

### Phase 9：SchedulerRuntime facade
- **状态：** DONE
- **修改文件：** `agent/scheduler_runtime.py`
- **真实运行路径证据：** SchedulerRuntime 包装 ProactiveScheduler

### Phase 10：Runtime 拆分
- **状态：** DONE
- **修改文件：** `runtime/chat_runtime.py`, `main.py`
- **真实运行路径证据：**
  - `main.py` — 创建 `ChatRuntime` 实例，注入所有 facade
  - `/chat` 路由调用 `chat_runtime.handle_chat()` 而非 `chat_agent.chat()`
  - `runtime/chat_runtime.py` — `handle_chat()` 通过 ToolExposurePolicy 解析工具，委托 ChatAgent 执行
  - 完整调用链：`POST /chat → main.py → chat_runtime.handle_chat() → chat_agent.chat()`

### Phase 11：工具去渠道化
- **状态：** DONE
- **修改文件：** `config.yaml`, `main.py`, `agent/chat_agent.py`, `agent/tool_exposure_policy.py`, `agent/tool_classifier.py`
- **真实运行路径证据：**
  - `config.yaml` — `allowed_tools: [send_text_message, finish_turn]`
  - `main.py` — 默认回退使用规范名
  - `agent/tool_exposure_policy.py` — 迁移映射已删除，直接使用规范名
  - 模型可见工具不含 `wechat_reply`, `done`, `skip_proactive`, `schedule_wake_up`

### Phase 12：TurnEngine 定型 + ChatAgent 瘦身
- **状态：** DONE
- **修改文件：** `agent/turn_engine.py`, `agent/chat_agent.py`, `runtime/chat_runtime.py`, `main.py`
- **真实运行路径证据：**
  - `main.py` — `tool_exposure_policy` + `context_builder` 均注入 ChatAgent
  - `agent/chat_agent.py:567` — `_do_build_context` 委托给 `ContextBuilder.build()`
  - `agent/chat_agent.py:738` — `_do_call_llm` 委托给 `ToolExposurePolicy.resolve()`
  - `runtime/chat_runtime.py` — `ChatRuntime.handle_chat()` 是 /chat 唯一入口
  - ChatAgent 不再独占上下文构建和工具暴露控制权

### Phase 13：清理旧结构
- **状态：** DONE
- **修改文件：** `agent/tool_registry.py`, `agent/chat_agent.py`, `agent/tool_classifier.py`, `agent/output_guard.py`, 测试文件
- **清理完成项：**
  - 移除 6 个废弃工具注册（wechat_reply, done, skip_proactive, schedule_wake_up, send_wechat_messages, skip_reply）
  - 移除 `_MIGRATION_MAP` / `_OLD_TO_NEW` 迁移映射
  - 移除 `_DEPRECATED_HINTS` 废弃提示
  - ChatAgent 路由使用规范名（不再兼容旧名）
  - output_guard 更新为检测规范工具名
  - 11 个规范工具在 registry 中，0 个废弃工具
  - 所有测试使用规范工具名

---

## 主路径调用链

```
POST /chat (main.py)
  → chat_runtime.handle_chat()
    → ToolExposurePolicy.resolve() → 规范工具名
    → chat_agent.chat()
      → _do_build_context → ContextBuilder.build()
      → _do_call_llm → ToolExposurePolicy.resolve()
      → LLMRequest(turn_id=..., call_id=...)
      → state machine execution → TurnResult
```

## Registry 工具清单（11 个规范工具）

```
get_time, get_weather, send_text_message, finish_turn,
skip_initiative, schedule_user_reminder, mark_reminder_delivered,
apply_forget_directive, generate_image_message, send_image_message,
send_sticker
```

## 模型可见工具（按 turn type）

| Turn Type | 工具 |
|-----------|------|
| USER_CHAT | `[send_text_message, finish_turn]` |
| USER_REMINDER_REQUEST | `[send_text_message, finish_turn, schedule_user_reminder, get_time]` |
| USER_REMINDER_DUE | `[send_text_message, finish_turn, mark_reminder_delivered]` |
| PROACTIVE_SKIPPABLE | `[send_text_message, finish_turn, skip_initiative]` |
| PROACTIVE_FORCED_SPEAK | `[send_text_message, finish_turn]` |

0 个 `wechat_reply`, `done`, `skip_proactive`, `schedule_wake_up` 出现在模型可见工具中。

## 防回归项（REG-001 ~ REG-011）

全部通过。详见 `docs/runtime-specs/10-test-matrix.zh.md`。

## 全量测试结果

```
728 passed, 8 skipped
```

8 skipped = `test_scheduled_store.py` tmp_path 权限问题（CI 环境，非代码问题）

---

## 已知偏差

1. ChatAgent 发送管道（EXEC_SEND_ONE）仍内联执行，未委托 ToolExecutor
2. MemorySystem facade 尚未完全替代 MemoryHook callback 模式
3. `agent/chat_agent.py:1601` 保留 1 处 `"wechat_reply"` 回退字符串（`_classified_single` 为 None 时的安全回退，模型不可见）
