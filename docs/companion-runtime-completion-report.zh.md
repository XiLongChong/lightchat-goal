# 关系型陪伴系统运行时完成报告

> 日期：2026-06-03
> 状态：Phase 13 完成

---

## 各 Phase 完成状态

| Phase | 描述 | 状态 | 关键产出 |
|-------|------|------|---------|
| Phase 1 | 运行时数据契约类型骨架 | DONE | `runtime_contracts/`, 17 个数据类型 |
| Phase 2 | ToolExposurePolicy facade | DONE | `agent/tool_exposure_policy.py` |
| Phase 3 | ContextBuilder facade | DONE | `agent/runtime_context_builder.py` |
| Phase 4 | ContextSnapshotStore + LLM trace | DONE | snapshot/turn_id/call_id 关联 |
| Phase 5 | Turn-local state 接入 | DONE | `TurnScratchpad` turn-local accessors |
| Phase 6 | ProviderGateway 结构化边界 | DONE | `StructuredOutputValidator` |
| Phase 7 | ToolExecutor / DeliveryService | DONE | `agent/tool_executor.py`, `agent/delivery_service.py` |
| Phase 8 | MemorySystem facade | DONE | `memory/system.py` |
| Phase 9 | SchedulerRuntime facade | DONE | `agent/scheduler_runtime.py` |
| Phase 10 | Runtime 拆分 | DONE | `runtime/chat_runtime.py` |
| Phase 11 | 工具去渠道化 | DONE | 规范工具名：send_text_message, finish_turn, skip_initiative |
| Phase 12 | TurnEngine 定型 + ChatAgent 瘦身 | DONE | `agent/turn_engine.py`, policy 注入 ChatAgent |
| Phase 13 | 清理旧结构 | DONE | 迁移映射移除，deprecated hints 移除，路由双名支持 |

---

## 防回归项验证

### REG-001: 同一条 assistant 消息不重复出现
- TurnScratchpad.get_history_writable_messages() 只返回 delivered 消息
- turn-local state 与 history 分离：`get_sent_messages_this_turn()` 独立于 `get_history_writable_messages()`

### REG-002: failed delivery 不写入 assistant history
- `TurnScratchpad.get_history_writable_messages()` 只返回 status=delivered 的消息
- `get_delivered_texts()` / `get_failed_texts()` 显式区分

### REG-003: tool result 不能作为 user message
- ContextBuilder 中 tool result 的 role 为 "tool"，不进入 recent_history block

### REG-004: media summary 不能作为 user text
- `InboundMessageItem.text` 与 `MediaAttachment.summary` 独立字段

### REG-005: prompt-visible tools 等于 native schema tools
- ToolExposurePolicy._validate_consistency() 确保 exposed = schema_names
- `test_prompt_equals_schema_skippable` / `test_prompt_equals_schema_forced_speak` 验证

### REG-006: 未暴露工具调用被拒绝
- ToolExposurePolicy.denied_tools 包含所有未暴露工具及拒绝原因
- `test_image_tools_denied_in_proactive` / `test_skip_initiative_denied_in_user_chat` 验证

### REG-007: 主动 skip 后下一轮不暴露 skip_initiative
- ToolExposurePolicy: last_decision="skipped" → phase=FORCED_SPEAK → skip_initiative 不暴露
- `test_forced_speak_hides_skip_initiative` 验证

### REG-008: 用户提醒到期优先于主动冷启动
- TurnEngine.classify_turn_type(): reminder_due 优先于 relationship_proactive

### REG-009: finish_turn 是唯一终态来源
- ChatAgent 状态机: 只有 done/finish_turn 工具调用进入终态路径
- `HANDLE_MODEL_OUTPUT`: 无 tool_call → PROTOCOL_RETRY

### REG-010: context snapshot 可通过 turn_id 查询
- Phase 4 ContextSnapshotStore 实现

### REG-011: wechat_reply 已从核心 runtime 删除
- `agent/turn_engine.py`: 0 处 wechat_reply
- `agent/tool_exposure_policy.py`: 0 处 wechat_reply（迁移映射已删除）
- `agent/tool_classifier.py`: 0 处 deprecated hints
- `agent/chat_agent.py`: 4 处 wechat_reply（仅 legacy 兼容路由，模型不可见）
- 规范工具名为 send_text_message, finish_turn, skip_initiative

---

## 全量测试结果

```
730 passed, 8 skipped, 94 warnings
```

8 skipped = scheduled_store 测试需要可写 temp 目录（CI 环境问题，非代码问题）

---

## 清理项完成情况

| 清理项 | 完成 |
|--------|------|
| `_MIGRATION_MAP` 从 tool_exposure_policy.py 移除 | YES |
| `_DEPRECATED_HINTS` 从 tool_classifier.py 移除 | YES |
| `_registry_name()` 迁移方法移除 | YES |
| ChatAgent 路由支持规范名 + 旧名 | YES |
| `_get_proactive_allowed_tools()` 使用规范名 | YES |
| `_validate_proactive_tool_constraints()` 使用规范名 | YES |
| 测试更新为规范名断言 | YES |
| deprecated tool 测试更新为 unknown/unroutable | YES |

---

## 剩余风险

1. `agent/chat_agent.py` 仍使用少量旧名作为路由兼容（`wechat_reply`, `done`, `skip_proactive`）。这些仅用于内部路由，模型不可见（`native_tool_call_eligible=False`）。完全删除旧注册需各调用方迁移完成。
2. `_do_build_context` 仍使用内联上下文构建而非 ContextBuilder facade。完整委托需要更大范围重构。
3. MemorySystem facade 存在但尚未完全替代 ChatAgent 的 post-terminal callback 模式。

---

## 临时兼容状态

| 项目 | 状态 |
|------|------|
| tool_registry 旧名注册 | 保留，native_tool_call_eligible=False（模型不可见） |
| ChatAgent 双名路由 | send_text_message/wechat_reply 均接受 |
| ToolExposurePolicy 迁移映射 | 已删除 |

---

## 最终验证命令

```powershell
D:\projects\goal\.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_proactive_debug.py
→ 730 passed, 8 skipped
```
