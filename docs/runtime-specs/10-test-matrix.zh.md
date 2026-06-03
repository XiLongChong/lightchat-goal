# 关系型陪伴系统运行时重构 -- 聚合测试矩阵

> 日期：2026-06-03
> 状态：测试矩阵草案
> 上游文档：
> - `docs/companion-runtime-test-plan.zh.md`（测试设计主要参考）
> - `docs/companion-architecture-requirements.zh.md`
> - `docs/companion-architecture-design-decisions.zh.md`
> - `docs/companion-runtime-module-contracts.zh.md`
> - `docs/companion-runtime-data-contracts.zh.md`
> - `docs/companion-runtime-state-machines.zh.md`
> - `docs/companion-runtime-gap-analysis.zh.md`
> - `docs/companion-runtime-implementation-plan.zh.md`
> 关联 spec（并行起草中，当前尚未落盘）：
> - `docs/runtime-specs/01-context-builder-spec.zh.md`
> - `docs/runtime-specs/02-tool-exposure-policy-spec.zh.md`
> - `docs/runtime-specs/03-turn-engine-spec.zh.md`
> - `docs/runtime-specs/04-memory-system-spec.zh.md`
> - `docs/runtime-specs/05-scheduler-runtime-spec.zh.md`
> 注意：以上 5 份模块 spec 正在并行起草中，本测试矩阵基于源设计文档（contracts/data-contracts/state-machines）编写，待 spec 落盘后需要做最终对齐。

---

## 1. 文档目的

本测试矩阵是运行时重构的**唯一测试真相源**。

任何实现批次不得在对应测试段落通过之前宣称"完成"。

- 每个 Phase 有对应的测试套件和最低通过标准。
- 任何实现 PR 必须引用本矩阵中对应 Test ID，并展示通过证据。
- "手测正常"不能替代本矩阵中定义的结构化断言。

本矩阵交叉引用 5 份模块 spec，覆盖：
1. 哪些测试是强制通过门（completion gates）
2. 哪些测试覆盖跨模块集成
3. 哪些现有测试必须更新
4. 测试套件的执行顺序

---

## 2. 测试分层

四层测试体系：

```text
L1 结构测试（Structure/Data Contract tests）
  - 数据对象字段合法性、枚举值约束、互斥/必填规则
  - 禁止混用清单验证
  - 不涉及任何运行时行为

L2 模块测试（Module tests）
  - 单个模块输入输出正确性
  - Mock 所有下游依赖
  - 覆盖模块契约中声明的所有接口和边界情况

L3 状态机测试（State machine tests）
  - Turn 类型流转和终态正确
  - 工具调用序列、送达状态机、中断恢复
  - 不一定真正调用 provider

L4 集成测试（Integration tests）
  - /chat、scheduler、memory、tool、delivery 端到端串联
  - 验证上下文污染防线在全链路生效
  - 验证跨模块数据一致性
```

| 层 | 覆盖什么 | 不覆盖什么 |
|----|---------|-----------|
| L1 结构 | dataclass 字段、枚举值、序列化/反序列化 | 模块行为 |
| L2 模块 | 单模块输入输出、错误处理、边界值 | 跨模块数据流 |
| L3 状态机 | Turn 流转、终态判定、预算耗尽、中断 | Provider 网络调用、真实渠道发送 |
| L4 集成 | 端到端数据流、跨模块锁/状态一致性 | Provider 认证、真实微信 iLink |

---

## 3. ContextBuilder 测试矩阵

目标：验证 ContextBuilder 作为唯一模型输入入口的所有不变式。
至少 25 个测试用例。

**Context block 固定顺序（来自 module contracts 8.）**：

```text
1. fixed_system_protocol
2. fixed_output_protocol
3. fixed_tool_protocol
4. persona
5. relationship_state
6. system_time_block
7. boundary_memory
8. selected_profile_memory
9. selected_episode_memory
10. selected_open_threads
11. recent_conversation_history
12. current_turn_local_state
13. media_context
14. current_input
15. exposed_tools_contract
16. terminal_policy
```

### 3.1 Context Block 顺序验证

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| CB-001 | context_block_ordering_matches_spec | L1 结构 | turn_type=user_chat, 全量 context 构建 | built_context.context_blocks 按 16 个 block 顺序排列，kind 依次为 fixed_system_protocol 到 terminal_policy | P0 | missing | Phase 3 |
| CB-002 | fixed_blocks_use_max_cache_class | L2 模块 | turn_type=user_chat, 完整 context | fixed_protocol/fixed_output_protocol/fixed_tool_protocol 三个 block 的 cache_class=fixed | P0 | missing | Phase 3 |
| CB-003 | persona_block_cache_class_low_frequency | L2 模块 | persona prompt 存在 | persona block 的 cache_class=low_frequency | P1 | missing | Phase 3 |
| CB-004 | dynamic_blocks_cache_class_turn_dynamic | L2 模块 | current_input block | current_input/turn_local/terminal_policy block 的 cache_class=turn_dynamic | P0 | missing | Phase 3 |
| CB-005 | empty_turn_local_block_when_no_sent_messages | L2 模块 | turn_local 无已发送消息 | current_turn_local_state block 存在但内容为空或 N/A | P1 | missing | Phase 5 |

### 3.2 Turn-local 与 Recent History 分离

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| CB-006 | same_assistant_msg_not_in_both_history_and_turn_local | L2 模块 | 本轮已 delivery 一条 assistant 消息"我在。" | "我在。"出现在 turn_local block，不出现在 recent_conversation_history block | P0 | missing | Phase 5 |
| CB-007 | assistant_msg_in_turn_local_before_turn_end | L2 模块 | send_text_message 执行成功，turn 未 finish | 该消息在 CurrentTurnLocalState.sent_messages_this_turn 中，不在 RecentConversationHistory.messages 中 | P0 | missing | Phase 5 |
| CB-008 | assistant_msg_enters_history_after_turn_end | L3 状态机 | turn 正常 finish，消息 status=delivered | turn 结束后该消息进入 ConversationHistory，role=assistant，delivery_status=delivered | P0 | missing | Phase 5 |
| CB-009 | failed_delivery_not_in_assistant_history | L3 状态机 | send_text_message 返回 status=failed | failed 消息不出现在 assistant history，出现在 turn_local failed 列表 | P0 | missing | Phase 5 |
| CB-010 | tool_result_not_as_user_message | L2 模块 | get_weather 返回"上海 25 度" | tool result 作为 tool_result block 或内联 tool call result，roles 不是 user | P0 | missing | Phase 4 |

### 3.3 多模态上下文验证

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| CB-011 | media_summary_not_as_user_text | L2 模块 | InboundMessageItem.kind=image, media.summary="图片里是一只杯子" | media_context block 包含 summary，current_input block 不含 summary | P0 | missing | Phase 3 |
| CB-012 | media_summary_enters_snapshot_only | L2 模块 | debug_mode=false, 有图片输入 | snapshot 包含 media_context block 摘要，provider_messages 按 provider 能力决定是否含原始媒体引用 | P1 | missing | Phase 3 |
| CB-013 | SystemTimeBlock_injected_for_all_turn_types | L2 模块 | 任意 turn_type, system_time="2026-06-03T14:00:00+08:00" | context_blocks 包含 system_time block，位置在 relationship_state 之后、boundary_memory 之前（第 6 位），cache_class=low_frequency | P1 | missing | Phase 3 |
| CB-014 | SystemTimeBlock_format_consistent | L2 模块 | 多次构建 context | system_time block 使用固定格式，包含时区 | P2 | missing | Phase 3 |

### 3.4 工具暴露一致性

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| CB-015 | prompt_visible_tools_equal_native_schema_tools | L2 模块 | ToolExposure 提供 exposed_tool_names=[send_text_message, finish_turn] | tool_contract_block 提到的工具名 = provider_messages 中的 native_tool_schemas 工具名 | P0 | missing | Phase 3 |
| CB-016 | get_time_not_exposed_by_default | L2 模块 | turn_type=user_chat, runtime_config 未开启 get_time | exposed_tool_names 中不含 get_time | P1 | missing | Phase 2 |
| CB-017 | get_time_exposure_per_decision_table | L2 模块 | 遍历 get_time 决策表 T1-T11 所有行 | 每行的 expose 决策与 ToolExposure 输出一致。特别验证：T1(N), T2(Y), T3(Y), T4(Y), T5(N), T6(Y), T7a(Y), T7b(N), T8a(N), T8b(N), T9a(N), T9b(N) | P0 | missing | Phase 2 |

### 3.5 Token Budget 与截断

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| CB-018 | token_budget_truncation_preserves_fixed_blocks | L2 模块 | 大量 history 导致超 budget | fixed blocks 不被截断，dynamic blocks 从最旧开始截断 | P0 | missing | Phase 3 |
| CB-019 | token_budget_report_included_in_built_context | L2 模块 | 正常构建 | BuiltContext.token_budget_report 包含 estimated_total, block_breakdown | P1 | missing | Phase 3 |
| CB-020 | token_budget_zero_messages_means_no_truncation | L2 模块 | recent_history 为空，budget 充裕 | 无截断发生，token_budget_report 显示 used < budget | P2 | missing | Phase 3 |

### 3.6 Snapshot 完整性

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| CB-021 | snapshot_contains_all_block_summaries | L2 模块 | 完整 context 构建 | ContextSnapshot.block_summaries 包含所有 block，每个 block 有 kind/content_summary/cache_class | P0 | missing | Phase 4 |
| CB-022 | snapshot_contains_exposed_tool_names | L2 模块 | ToolExposure 暴露 [send_text_message, finish_turn] | snapshot.exposed_tool_names = [send_text_message, finish_turn] | P0 | missing | Phase 4 |
| CB-023 | snapshot_contains_history_message_ids | L2 模块 | recent_history 有 msg_001, msg_002 | snapshot.history_message_ids = [msg_001, msg_002] | P1 | missing | Phase 4 |
| CB-024 | snapshot_no_api_key_leak | L2 模块 | 构建完成 | snapshot 及其所有子结构中不包含 API key 字符串 | P0 | missing | Phase 4 |
| CB-025 | snapshot_queryable_by_turn_id | L3 状态机 | turn 完成，turn_id=tn_001 | 可通过 turn_id 检索到完整的 ContextSnapshot | P0 | missing | Phase 4 |

### 3.7 ContextBuilder Spec 示例场景覆盖

| Test ID | 测试名称 | 分类 | 对应场景 | 预期断言 | 优先级 | 当前覆盖 |
|---------|---------|------|---------|---------|--------|---------|
| CB-S01 | user_chat_scenario_basic_text | L4 集成 | 用户发"你好" | recent_history + current_input 正确拼接，当前输入不在 history 中 | P0 | missing |
| CB-S02 | multi_turn_accumulation | L4 集成 | 3 轮对话后发"还记得吗" | recent_history 包含前 3 轮，turn_local 为空 | P1 | missing |
| CB-S03 | proactive_cold_start_no_user_input | L3 状态机 | proactive cold start | current_input 为空或为 trigger reason，无 role=user 的新消息 | P0 | existing |
| CB-S04 | image_input_with_summary | L3 状态机 | 用户发图片 | media_context 包含 summary，不污染 user text | P1 | missing |
| CB-S05 | tool_result_not_mixed_with_user | L3 状态机 | get_weather 后继续对话 | tool result 不在 recent_history，在 turn_local | P0 | missing |
| CB-S06 | send_then_continue_in_same_turn | L3 状态机 | 发一条后模型继续 | 已发送消息在 turn_local，不在 recent_history | P0 | missing |
| CB-S07 | failed_send_then_retry_context | L3 状态机 | 发送失败后重试 | failed 消息在 turn_local 带状态标记 | P1 | missing |
| CB-S08 | boundary_memory_first_in_memory_blocks | L2 模块 | boundary memory 存在 | boundary_memory block 排在所有其他 memory block 之前 | P0 | missing |
| CB-S09 | empty_memory_produces_empty_blocks | L2 模块 | MemorySelection 全部为空 | memory block 存在但标记为 empty | P2 | missing |
| CB-S10 | forced_speak_terminal_policy_block | L3 状态机 | 上一轮 skip，本轮 forced_speak | terminal_policy block 中 allows_skip=false, forced_speak=true, min_delivered_messages=1 | P0 | missing |

---

## 4. ToolExposurePolicy 测试矩阵

目标：验证 ToolExposurePolicy 作为唯一工具暴露入口的所有规则。
至少 20 个测试用例。

### 4.1 各 Turn Type 工具集合

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| TE-001 | user_chat_default_tool_set | L2 模块 | turn_type=user_chat, 无特殊 intent, system_time_available=true | exposed_tool_names = [send_text_message, finish_turn]，不含 get_time（当前时间由 SystemTimeBlock 注入） | P0 | partial | Phase 2 |
| TE-002 | user_chat_with_weather_intent_exposes_get_weather | L2 模块 | turn_type=user_chat, user_intent=request_weather（用户问"今天天气怎么样"） | exposed_tool_names 包含 get_weather | P1 | missing | Phase 2 |
| TE-002b | user_chat_without_weather_intent_no_get_weather | L2 模块 | turn_type=user_chat, user_intent=chat, system_time_available=true | exposed_tool_names 不含 get_weather（get_weather 非默认工具） | P0 | missing | Phase 2 |
| TE-003 | user_request_image_exposes_image_tools | L2 模块 | turn_type=user_chat, user_intent=request_image | exposed_tool_names 包含 generate_image_message 或 send_image_message | P0 | missing | Phase 2 |
| TE-004 | user_request_image_does_not_expose_in_non_user_turns | L2 模块 | turn_type=relationship_proactive, user_intent=request_image（不应存在） | exposed_tool_names 不含 generate_image_message/send_image_message | P0 | missing | Phase 2 |
| TE-005 | proactive_skippable_tool_set | L2 模块 | turn_type=relationship_proactive, consecutive_skip_count=0 | exposed_tool_names = [send_text_message, skip_initiative, finish_turn] | P0 | partial | Phase 2 |
| TE-006 | proactive_forced_speak_tool_set | L2 模块 | turn_type=relationship_proactive, consecutive_skip_count>=1 | exposed_tool_names = [send_text_message, finish_turn]，不含 skip_initiative | P0 | missing | Phase 2 |
| TE-007 | proactive_never_exposes_image_tools | L2 模块 | turn_type=relationship_proactive, 任意状态 | denied_tools 包含 send_image_message/generate_image_message/send_sticker | P0 | missing | Phase 2 |
| TE-008 | user_reminder_due_tool_set | L2 模块 | turn_type=user_reminder_due, reminder claimed | exposed_tool_names = [send_text_message, mark_reminder_delivered, finish_turn] | P0 | missing | Phase 2 |
| TE-009 | user_reminder_request_tool_set | L2 模块 | turn_type=user_reminder_request, system_time_available=true | exposed_tool_names = [send_text_message, schedule_user_reminder, finish_turn, get_time]（get_time 仅在 reminder 回合因需要绝对时间校验而暴露，见 get_time 决策表 T6） | P0 | missing | Phase 2 |
| TE-010 | scheduled_ritual_tool_set | L2 模块 | turn_type=scheduled_ritual | exposed_tool_names = [send_text_message, finish_turn]，不含 skip_initiative（默认） | P1 | missing | Phase 2 |
| TE-011 | admin_manual_trigger_dry_run_tool_set | L2 模块 | turn_type=admin_manual_trigger, dry_run=true | exposed_tool_names 不含任何发送工具 | P1 | missing | Phase 2 |
| TE-012 | memory_management_tool_set | L2 模块 | turn_type=memory_management, forget_directive 存在 | exposed_tool_names = [apply_forget_directive, send_text_message, finish_turn] | P1 | missing | Phase 2 |

### 4.2 Prompt-Schema-Executor 一致性

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| TE-013 | prompt_contract_equals_native_schema_tools | L2 模块 | ToolExposure.resolve() 完成 | tool_contract_block 中提到的每个工具名在 native_tool_schemas 中存在 | P0 | missing | Phase 2 |
| TE-014 | native_schema_has_executor | L2 模块 | 每个 exposed tool | ToolExecutor 中存在对应的 executor 函数 | P0 | missing | Phase 7 |
| TE-015 | tool_in_registry_not_equals_exposed | L2 模块 | generate_image_message 在 registry 但不在 exposed | ToolRegistry.get("generate_image_message") 存在，但 ToolExposure.exposed_tool_names 中不含它 | P1 | missing | Phase 2 |
| TE-016 | denied_tools_recorded_with_reason | L2 模块 | skip 不暴露 | denied_tools 包含 {tool_name: "skip_initiative", reason: "forced_speak_required"} | P1 | missing | Phase 2 |

### 4.3 ToolExposurePolicy Spec 示例场景覆盖

| Test ID | 测试名称 | 分类 | 对应场景 | 预期断言 | 优先级 | 当前覆盖 |
|---------|---------|------|---------|---------|--------|---------|
| TE-S01 | normal_chat_tool_set_exact | L2 模块 | 普通聊天场景 | 工具集合精确匹配默认配置 | P0 | partial |
| TE-S02 | image_request_exposes_generate | L2 模块 | 用户说"画个图" | generate_image_message 暴露，skip_initiative 不暴露 | P0 | missing |
| TE-S03 | proactive_skip_tool_present | L2 模块 | 首次主动冷启动 | skip_initiative 可见 | P0 | existing |
| TE-S04 | proactive_forced_speak_skip_absent | L2 模块 | 上次 skip 后的主动回合 | skip_initiative 不在 exposed 中，在 denied 中 | P0 | missing |
| TE-S05 | reminder_due_has_mark_delivered | L2 模块 | 用户提醒到期 | mark_reminder_delivered 暴露 | P0 | missing |
| TE-S06 | no_image_tools_in_proactive | L2 模块 | proactive turn | generate_image_message/send_image_message/send_sticker 全在 denied | P0 | missing |

---

## 5. TurnEngine 测试矩阵

目标：验证 TurnEngine 状态机、15-state 流转、所有 turn type、送达状态机和终态处理。
至少 25 个测试用例。

### 5.1 通用状态机 15 态流转

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| TN-001 | full_15_state_flow_user_chat | L3 状态机 | turn_type=user_chat, model 返回 send_text_message -> finish_turn | 状态序列包含 TURN_CREATED -> ... -> RELEASE_LOCK，terminal_state=finished | P0 | existing | Phase 1 |
| TN-002 | lifecycle_states_all_visited | L3 状态机 | 正常 turn | TurnScratchpad.state_history 包含所有预期状态 | P0 | existing | Phase 1 |
| TN-003 | turn_has_interrupt_token | L3 状态机 | turn 创建 | interrupt_token 格式为 it_{turn_id}_{timestamp_ms} | P0 | existing | Phase 1 |

### 5.2 模型输出校验

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| TN-004 | no_tool_calls_triggers_retry | L3 状态机 | assistant.content 有内容但 tool_calls=[] | VALIDATE_MODEL_OUTPUT 返回 invalid，进入 PROTOCOL_RETRY | P0 | existing | Phase 6 |
| TN-005 | old_json_actions_detected_as_protocol_error | L3 状态机 | assistant.content 含旧 JSON actions | 检测为 protocol_error，不执行任何工具 | P0 | existing | Phase 6 |
| TN-006 | multi_terminal_tool_calls_rejected | L3 状态机 | 模型同时调用 finish_turn 和 send_text_message（若禁止同时） | 根据 TerminalPolicy 判定，若禁止则拒绝并重试 | P0 | partial | Phase 6 |
| TN-007 | parallel_fact_tool_calls_accepted | L3 状态机 | turn_type=user_chat, user asked about timezone（get_time 已暴露）, 模型同时调用 get_time 和 get_weather | 两个 fact tool 并行执行(gather)，结果均回灌模型 | P0 | existing | Phase 6 |
| TN-008 | unexposed_tool_call_rejected | L3 状态机 | 模型调用 generate_image_message（未暴露） | ToolExecutor 拒绝，reason=tool_not_exposed | P0 | missing | Phase 7 |

### 5.3 9 种 Turn Type 状态机

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| TN-009 | user_chat_turn_send_then_finish | L3 状态机 | model -> send_text_message -> finish_turn | terminal_state=finished, delivered_message_count=1 | P0 | existing | Phase 12 |
| TN-010 | user_chat_multi_message_send_continue_send_finish | L3 状态机 | model1 -> send_text_message("A"), model2 -> send_text_message("B"), model3 -> finish_turn | A 发送后在 turn_local，B 发送后 A+B 都在 turn_local，turn 结束后 A+B 进入 history | P0 | partial | Phase 5 |
| TN-011 | user_reminder_request_turn | L3 状态机 | user says "十分钟后提醒我喝水" | model -> schedule_user_reminder -> send_text_message -> finish_turn，reminder created | P0 | missing | Phase 12 |
| TN-012 | user_reminder_due_turn | L3 状态机 | claimed reminder | model -> send_text_message -> mark_reminder_delivered -> finish_turn，reminder status=delivered | P0 | missing | Phase 12 |
| TN-013 | proactive_skip_turn | L3 状态机 | skippable proactive | model call 1 → skip_initiative(reason="no_news"), model call 2（同一 turn）→ finish_turn(next_wake_minutes=45) | terminal_state=finished, skipped=true, InitiativeState.last_skipped_at 已更新。两次模型调用，不是同一 tool_calls | P0 | existing | Phase 12 |
| TN-014 | proactive_forced_speak_skip_rejected | L3 状态机 | forced_speak, model tries skip_initiative | 工具调用 rejected (tool_not_exposed)，模型必须用 send_text_message | P0 | missing | Phase 12 |
| TN-015 | scheduled_ritual_turn | L3 状态机 | morning ritual triggered | model -> send_text_message -> finish_turn, source=scheduled_ritual | P1 | missing | Phase 12 |
| TN-016 | admin_dry_run_turn | L3 状态机 | admin trigger, dry_run=true | model 正常执行但无真实发送，delivered_message_count=0, turn 记录 dry_run=true | P1 | missing | Phase 12 |
| TN-017 | memory_management_forget_turn | L3 状态机 | user says "别记这个" | model -> apply_forget_directive -> send_text_message -> finish_turn，forget_window 已记录 | P1 | missing | Phase 12 |

### 5.4 Delivery Status 状态机（六态）

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| TN-018 | delivery_status_sent_confirmed_writes_history | L3 状态机 | send_text_message -> DeliveryReceipt.status=delivered | delivery_status=SENT_CONFIRMED，消息写入 assistant history | P0 | existing | Phase 7 |
| TN-019 | delivery_status_failed_before_send_retryable | L3 状态机 | send 前即失败 | delivery_status=FAILED_BEFORE_SEND, retryable=true, 不写 history | P0 | partial | Phase 7 |
| TN-020 | delivery_status_unknown_not_treated_as_delivered | L3 状态机 | send 后无确认 | delivery_status=UNKNOWN_AFTER_ATTEMPT, retryable=false, 不写 history | P0 | partial | Phase 7 |
| TN-021 | delivery_status_failed_confirmed_retryable | L3 状态机 | 发送后确认失败 | delivery_status=FAILED_CONFIRMED, retryable=true，可进入 retry | P1 | missing | Phase 7 |
| TN-022 | delivery_status_blocked_by_guard_not_retryable | L3 状态机 | guard 拦截 | delivery_status=BLOCKED_BY_GUARD, retryable=false | P1 | existing | Phase 7 |

### 5.5 TurnEngine Spec 示例场景覆盖

| Test ID | 测试名称 | 分类 | 对应场景 | 预期断言 | 优先级 | 当前覆盖 |
|---------|---------|------|---------|---------|--------|---------|
| TN-S01 | interrupt_check_at_all_6_points | L3 状态机 | 6 处中断检查覆盖 | 每处创建 check: CREATE_TURN后, CALL_LLM后, EXEC_FACT后, GUARD_OUTPUT前, EXEC_SEND前, FALLBACK前 | P0 | existing |
| TN-S02 | protocol_retry_exceeded_goes_to_hard_fallback | L3 状态机 | 连续 2 次协议违规 | 进入 HARD_FALLBACK，使用固定文案 | P0 | existing |
| TN-S03 | hard_fallback_uses_fixed_text_not_model_output | L3 状态机 | HARD_FALLBACK 触发 | 输出不是模型原文，是 fixed fallback text | P0 | existing |
| TN-S04 | partial_delivery_end_partial_sent | L3 状态机 | 3 条消息，1 delivered + 2 failed | terminal_state=END_PARTIAL_SENT, 仅 delivered 消息写 history | P0 | partial |
| TN-S05 | post_terminal_hook_failure_does_not_block_turn | L3 状态机 | POST_TURN_HOOKS 中某 hook 抛异常 | 异常被捕获，不影响其他 hook 执行，不影响 terminal_state | P0 | existing |
| TN-S06 | budget_model_calls_exhausted_safe_finish | L3 状态机 | model_call_count 达到 max | 不再调用模型，safe_finish_turn，记录 budget_exhausted | P0 | existing |
| TN-S07 | budget_messages_exhausted_truncation | L3 状态机 | 已发送 15 条 | 第 16 条被拒绝，turn 强制结束 | P0 | partial |
| TN-S08 | finish_turn_is_only_terminal_source | L3 状态机 | 任意 turn | 终态判定来自 finish_turn 工具调用，不来自自然语言文本 | P0 | partial |
| TN-S09 | after_proactive_skip_next_turn_no_skip_initiative | L3 状态机 | 上一轮 skip，本轮 proactive | ToolExposure 不含 skip_initiative, consecutive_skip_count 检查 | P0 | missing |

---

## 6. MemorySystem 测试矩阵

目标：验证 MemorySystem 的记忆选择、写入、forget 指令和边界规则。
至少 20 个测试用例。

### 6.1 六类记忆选择

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| MM-001 | all_6_memory_categories_in_selection | L2 模块 | MemorySelectionRequest 完整 | MemorySelection 包含 profile_memory, relationship_memory, boundary_memory, episode_memory, open_thread_memory, sent_content_memory | P0 | missing | Phase 8 |
| MM-002 | boundary_memory_highest_priority | L2 模块 | boundary_memory 有内容 | boundary_memory block 在 context 中出现在所有其他 memory block 之前 | P0 | missing | Phase 8 |
| MM-003 | hidden_memory_not_in_context | L2 模块 | memory_id=mm_hidden, visibility=hidden_by_user | MemorySelection 中不含该记忆 | P0 | missing | Phase 8 |
| MM-004 | blocked_memory_not_in_context | L2 模块 | memory_id=mm_blocked, visibility=blocked_by_forget_directive | MemorySelection 中不含该记忆 | P0 | missing | Phase 8 |
| MM-005 | do_not_surface_memory_not_in_model_input | L2 模块 | memory.privacy_level=do_not_surface | memory 在 snapshot 中但不出现在 provider_messages | P0 | missing | Phase 8 |

### 6.2 Forget Directive

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| MM-006 | forget_directive_plus_minus_5_window | L2 模块 | turn_id=tn_010 检测到 forget_directive | turn_id tn_005 到 tn_015 之间的记忆候选被阻断 | P0 | missing | Phase 8 |
| MM-007 | forget_directive_triggers_topic_block_after_repeat | L2 模块 | 同一话题第 3 次被 forget | 该话题加入 permanent_blocked_topics | P1 | missing | Phase 8 |
| MM-008 | permanent_blocked_topic_memory_not_surface | L2 模块 | blocked_topic="前女友" | 该话题相关记忆不出现在 MemorySelection 中 | P1 | missing | Phase 8 |
| MM-009 | forget_directive_no_question_asked | L3 状态机 | user says "别记这个" | 系统不追问"为什么"，不追问"那该记住什么" | P1 | missing | Phase 8 |

### 6.3 记忆衰减与生命周期

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| MM-010 | negative_emotion_memory_decays_after_6h | L2 模块 | episode_memory 创建，emotional_weight=negative, 6h 后 | memory.retention_policy=decays, 不再出现在默认 MemorySelection | P0 | missing | Phase 8 |
| MM-011 | short_term_episode_expires_after_12h | L2 模块 | episode_memory 创建，12h 后 | memory.retention_policy=short_term, 不再出现在 MemorySelection | P0 | missing | Phase 8 |
| MM-012 | negative_emotion_not_referenced_unless_user_brings_up | L2 模块 | 负面记忆已 decay，新 turn 用户未提及 | 不主动引入负面记忆 | P1 | missing | Phase 8 |
| MM-013 | user_revisits_negative_emotion_renew_decay | L2 模块 | 用户再次提起旧负面记忆 | 衰减倒计时重置 | P2 | missing | Phase 8 |

### 6.4 长期记忆写入严格性

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| MM-014 | fight_words_filtered_from_long_term | L2 模块 | user says "我不想理你了" | 不写入长期记忆，write_decision=rejected, reason 包含"conflict_expression" | P0 | missing | Phase 8 |
| MM-015 | anniversary_date_promoted_to_long_term | L2 模块 | assistant accurately identifies "我们第一次聊天是 3 月 15" | write_decision=write_long_term, category=relationship | P1 | missing | Phase 8 |
| MM-016 | long_term_write_strictness_model_candidate_vs_system_decision | L2 模块 | model 建议写入某条记忆 | MemorySystem 独立判定 write_decision，可以否决 model 建议 | P0 | missing | Phase 8 |
| MM-017 | sent_content_dedup_prevent_repeat_proactive | L2 模块 | SentContentMemory 已有"今晚早点睡" | 新 proactive candidate "今晚早点睡" 被标记为 duplicate | P0 | missing | Phase 8 |

### 6.5 Bot 隔离与隐私

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| MM-018 | bot_isolation_memory_query | L2 模块 | bot_id=bot_a, user_id=wxid_me, 存在 bot_b 的记忆 | MemorySelection 只包含 bot_a 的记忆 | P0 | missing | Phase 8 |
| MM-019 | relationship_reset_clears_relationship_memory | L2 模块 | 执行 relationship_reset | relationship_stage 相关记忆 visibility=archived_by_relationship_reset | P1 | missing | Phase 8 |
| MM-020 | privacy_level_filtering_normal_vs_sensitive | L2 模块 | memory with privacy_level=sensitive | only surfaces when relationship intimacy_level >= configured threshold | P2 | missing | Phase 8 |

### 6.6 MemorySystem Spec 示例场景覆盖

| Test ID | 测试名称 | 分类 | 对应场景 | 预期断言 | 优先级 | 当前覆盖 |
|---------|---------|------|---------|---------|--------|---------|
| MM-S01 | sent_content_retrieved_for_active_turn | L2 模块 | proactive turn, 之前已发送过内容 | sent_content_memory 中包含最近已发送消息，用于去重 | P0 | missing |
| MM-S02 | forget_directive_blocks_extraction | L3 状态机 | "别记这个"后 turn 结束 | MemorySystem.write_after_turn 跳过 forget_window 内的内容 | P0 | missing |
| MM-S03 | negative_emotion_short_term_only | L3 状态机 | 用户生气，turn 结束 | 情绪相关记忆 retention_policy=short_term, expires 12h | P1 | missing |
| MM-S04 | model_candidate_overridden_by_system | L2 模块 | model 建议写入"吵架原文" | MemorySystem 拒绝写入长期记忆，记录 reason | P0 | missing |

---

## 7. SchedulerRuntime 测试矩阵

目标：验证心跳、定时任务领取、主动冷启动、频率控制和熔断。
至少 20 个测试用例。

### 7.1 心跳与开关

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| SR-001 | heartbeat_with_global_switch_off_no_turn | L2 模块 | features.proactive=false | heartbeat tick 后无 turn 创建，无模型调用 | P0 | existing | Phase 9 |
| SR-002 | heartbeat_with_switch_on_checks_eligibility | L2 模块 | features.proactive=true, no due tasks | heartbeat 进入 eligibility check | P0 | existing | Phase 9 |
| SR-003 | heartbeat_does_not_generate_text_directly | L2 模块 | heartbeat tick | heartbeat 本身不产生文案，只决定是否创建 turn | P0 | existing | Phase 9 |

### 7.2 提醒优先于主动

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| SR-004 | reminder_priority_over_proactive_cold_start | L3 状态机 | due reminder exists AND cold start eligible | claim due reminder 成功，创建 user_reminder_due turn，不创建 relationship_proactive turn | P0 | missing | Phase 9 |
| SR-005 | atomic_claim_prevents_double_execution | L2 模块 | due reminder, 2 concurrent heartbeat ticks | 只有一个 heartbeat claim 成功，另一个返回 None | P0 | missing | Phase 9 |
| SR-006 | unclaimed_task_no_model_call | L2 模块 | claim_due_task returns None | 无 turn 创建，无 LLM 调用 | P0 | missing | Phase 9 |

### 7.3 频率与时间控制

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| SR-007 | quiet_hours_block_proactive | L2 模块 | current_time inside quiet_hours | relationship_proactive eligibility=false, reason contains quiet_hours | P0 | existing | Phase 9 |
| SR-008 | frequency_tier_caps_enforced | L2 模块 | intensity_profile=quiet, 2 proactive turns today | 第 3 次 heartbeat 不触发 | P0 | missing | Phase 9 |
| SR-009 | consecutive_skip_forces_next_speak | L3 状态机 | last_decision=skipped, consecutive_skip_count=1 | 下一轮 ToolExposure 不暴露 skip_initiative（forced_speak=true）。consecutive_skip_count 保持为 1（最大值，因为不能连续 skip），forced speak 成功后归零 | P0 | missing | Phase 9 |
| SR-010 | daily_count_reset_at_midnight | L2 模块 | daily_trigger_count=5, clock crosses 00:00 | daily_trigger_count 重置为 0 | P1 | missing | Phase 9 |
| SR-011 | next_wake_range_enforced | L2 模块 | finish_turn(next_wake_minutes=10), range=[30,90] | next_wake_after 被 clamp 到 30，或者调用 invalid | P1 | missing | Phase 9 |

### 7.4 技术熔断

| Test ID | 测试名称 | 分类 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|------|---------|---------|--------|---------|-------|
| SR-012 | duplicate_claim_prevention | L2 模块 | same task_id claimed twice | 第二次 claim 返回 None | P0 | missing | Phase 9 |
| SR-013 | technical_circuit_breaker_max_per_hour | L2 模块 | 1h 内触发 100 次 proactive（模拟 bug） | circuit_breaker 打开，停止触发 | P0 | missing | Phase 9 |
| SR-014 | circuit_breaker_resets_after_cooldown | L2 模块 | circuit_breaker open, cooldown period passes | 下一 heartbeat 正常评估 | P1 | missing | Phase 9 |

### 7.5 SchedulerRuntime Spec 示例场景覆盖

| Test ID | 测试名称 | 分类 | 对应场景 | 预期断言 | 优先级 | 当前覆盖 |
|---------|---------|------|---------|---------|--------|---------|
| SR-S01 | cold_start_after_user_silence | L3 状态机 | last_user_message 超过 cold_start_threshold | eligibility=true, turn_type=relationship_proactive 创建 | P0 | existing |
| SR-S02 | cold_start_not_triggered_too_early | L2 模块 | last_user_message 在 threshold 内 | eligibility=false | P0 | existing |
| SR-S03 | scheduler_rejects_concurrent_run | L2 模块 | run_proactive_pipeline 已在进行中 | 第二个并发调用被拒绝 | P0 | existing |
| SR-S04 | sleep_hours_eligibility_false | L2 模块 | 当前在 sleep_hours 范围内 | eligibility=false，reason 包含 sleep_hours | P0 | existing |
| SR-S05 | skip_then_forced_speak_sequence | L3 状态机 | 连续 2 轮 proactive: 第一轮 skip，第二轮 forced | 第一轮 state=skipped, 第二轮 ToolExposure 无 skip_initiative | P0 | missing |
| SR-S06 | reminder_claim_sets_due_task_state | L2 模块 | due reminder claimed | task status 变为 claimed，prevent re-claim | P0 | missing |

---

## 8. 跨模块集成测试矩阵

目标：验证跨模块数据流一致性、上下文污染防线全链路生效、多模块协作锁/状态正确。
至少 15 个测试用例。

| Test ID | 测试名称 | 覆盖模块 | 输入条件 | 预期断言 | 优先级 | 当前覆盖 | Phase |
|---------|---------|---------|---------|---------|--------|---------|-------|
| INT-001 | full_chat_flow_text | InboundAdapter -> TurnClassifier -> TurnEngine -> ContextBuilder -> ToolExposurePolicy -> ProviderGateway -> ToolExecutor -> DeliveryService -> MemorySystem | POST /chat, user says "你好" | turn_id 生成, context_snapshot 可查, tool 执行正确, 回复送达, history 写入 | P0 | existing | Phase 12 |
| INT-002 | full_proactive_flow | SchedulerRuntime -> TurnEngine -> ContextBuilder -> ToolExposurePolicy -> ProviderGateway -> DeliveryService -> MemorySystem -> InitiativeState update | heartbeat -> eligibility -> proactive turn | turn 创建, snapshot 可查, 发送或 skip 正确, InitiativeState 更新 | P0 | partial | Phase 12 |
| INT-003 | full_reminder_flow | SchedulerRuntime claim -> TurnEngine -> ToolExecutor -> mark_reminder_delivered -> state update | due reminder claimed, turn completes | reminder status=delivered, turn 记录包含 reminder_id | P0 | missing | Phase 12 |
| INT-004 | context_pollution_prevention_full_turn | ContextBuilder + TurnEngine + MemorySystem | 同一轮：发送 2 条消息 + 1 次 fact tool | 已发送消息不在 recent_history，tool result 不在 recent_history，turn 结束后仅 delivered 消息进 history | P0 | missing | Phase 12 |
| INT-005 | multi_turn_memory_accumulation | TurnEngine + MemorySystem + ContextBuilder | 3 轮 user_chat，每轮有新信息 | round 3 context 包含 round 1-2 的 short-term episode，round 1 的 sent content 出现在 MemorySelection | P0 | missing | Phase 12 |
| INT-006 | snapshot_audit_trail_completeness | TurnEngine + ContextBuilder + ContextSnapshotStore | 1 轮含 3 次 LLM call | 每次 call 有独立 snapshot，snapshot 包含 call_id 和 turn_id | P0 | missing | Phase 12 |
| INT-007 | tool_exposure_consistency_across_calls | TurnEngine + ToolExposurePolicy + ProviderGateway | 同一轮 3 次 LLM call | 每次 call 的 exposed_tool_names 一致（或按 phase 合法变化） | P0 | missing | Phase 12 |
| INT-008 | proactive_skip_then_followup_user_chat | SchedulerRuntime + TurnEngine + ContextBuilder | proactive skip 后 5 分钟用户发消息 | InitiativeState.last_skipped_at 已记录，用户回复正常，context 不含 skip 相关 prompt | P1 | missing | Phase 12 |
| INT-009 | concurrent_turn_prevention | TurnEngine + RuntimeStore | 同一 user_id 同时 2 个 turn | 第二个 turn 被锁拒绝或排队 | P0 | missing | Phase 12 |
| INT-010 | delivery_failed_across_modules | ToolExecutor + DeliveryService + ContextBuilder + MemorySystem | send_text_message 后 DeliveryReceipt=failed | turn_local 标记 failed，不写 history，不写 sent_content_memory，code 可查 reason | P0 | missing | Phase 12 |
| INT-011 | image_input_full_flow | InboundAdapter -> MediaAdapter -> ContextBuilder -> ToolExposurePolicy -> TurnEngine | POST /chat with image item | media summary in context, no raw media in snapshot, tool exposure 按需暴露图片工具 | P0 | missing | Phase 12 |
| INT-012 | forget_directive_full_flow | TurnEngine + MemorySystem + ContextBuilder | user says "别记这个" -> turn ends | forget_window 记录，后续 memory extraction 跳过，下一轮 context 不含被 forget 的记忆 | P0 | missing | Phase 12 |
| INT-013 | wechat_reply_not_in_core_runtime | TurnEngine + ToolExposurePolicy + ToolRegistry | 全量代码检查 | 核心 runtime 路径中不出现 wechat_reply 字符串（adapter 层除外） | P0 | missing | Phase 11 |
| INT-014 | proactive_disabled_scheduler_never_triggers | SchedulerRuntime + TurnEngine | features.proactive=false, 运行 10 个 heartbeat | 0 个 turn 创建，0 个 LLM 调用 | P0 | existing | Phase 9 |
| INT-015 | multi_bot_memory_isolation_full | InboundAdapter -> TurnClassifier -> ContextBuilder -> MemorySystem | bot_a turn -> bot_b turn -> bot_a turn | bot_a 的 context 不含 bot_b 的记忆和历史 | P0 | missing | Phase 12 |

---

## 9. 现有测试迁移清单

本节列出每个现有 test 文件的处理方案。对每个文件声明：哪些保留不改、哪些需要修改（含具体改法）、哪些需要删除（含原因）、哪些需要移到新文件。

### 9.1 tests/test_chat_agent.py（2450 lines）

**可以保留不改的测试：**

| 测试函数 | 原因 |
|---------|------|
| test_extract_json_* / test_safe_parse_* (7 个) | JSON 解析工具函数，与协议变迁无关 |
| test_context_sample_in_chat_result / test_context_sample_no_raw_json | snapshot 基本验证，语义不变 |
| test_conversation_history_same_ts_ordering | 历史排序逻辑，不变 |
| test_*_interrupt_* (约 6 个) | 中断检查机制不变 |
| test_*_protocol_retry_* (约 4 个) | 协议重试逻辑保留 |
| test_*_hard_fallback_* (约 3 个) | 硬兜底逻辑保留 |
| test_*_lifecycle_states_* | 状态生命周期不变 |
| test_*_guard_blocks_* (约 6 个) | guard 行为不变 |
| test_*_history_* (约 4 个) | 历史格式化不变 |
| test_*_model_attempts_* | 模型尝试记录不变 |

**需要修改的测试：**

| 测试函数 | 修改内容 |
|---------|---------|
| test_single_wechat_reply_then_done | wechat_reply -> send_text_message, done -> finish_turn |
| test_three_wechat_reply_then_done | 同上 |
| test_done_no_reply_without_send | finish_turn(status=no_reply) |
| test_fact_tool_then_wechat_reply_then_done | 工具名替换 + 验证 tool result 不在 history |
| test_no_tool_calls_fallback | 不变（逻辑一致） |
| test_wechat_reply_sends_one_bubble_then_done | 工具名替换 |
| test_assistant_content_triggers_tool_retry | assistant.content no tool_calls 逻辑不变 |
| test_ten_messages_allowed_by_schema / test_twenty_messages_truncated_by_guard | 不变但验证 sent_content_memory |
| test_fact_tool_success_returns_to_llm | 添加 tool result not as user message 断言 |
| test_production_missing_checker_raises | 添加 ContextBuilder 唯一入口检查 |
| test_optional_preprocess_injects_time | 添加 SystemTimeBlock 断言 |
| test_*_allowed_tools_* | 工具名替换 + ToolExposurePolicy 引入 |
| test_*_deprecated_* (约 3 个) | 在 Phase 11 后删除 |

**需要删除的测试（Phase 11 后）：**

| 测试函数 | 原因 |
|---------|------|
| test_skip_reply_deprecated_rejected | deprecated tool 路径已删除 |
| test_deprecated_batch_tool_rejected | deprecated send_wechat_messages 已删除 |
| test_old_json_actions_detected_as_protocol_error | 保留（这是检测，不是执行），但需确认旧名 `wechat_reply` 替换为 `send_text_message` |
| 所有使用 `wechat_reply` 作为 tool_name 的测试 | 在 Phase 11 工具去渠道化后重命名为 send_text_message |

**需要移到新文件的测试：**

| 测试函数 | 目标文件 | 原因 |
|---------|---------|------|
| 所有 ContextBuilder 相关测试（约 8 个） | tests/test_context_builder.py | 模块拆分 |
| 所有 ToolExposurePolicy 相关测试（约 5 个） | tests/test_tool_exposure_policy.py | 模块拆分 |
| 所有 DeliveryService 相关测试（约 4 个） | tests/test_delivery_service.py | 模块拆分 |

### 9.2 tests/test_tool_registry.py（259 lines）

**可以保留不改的测试：**

| 测试函数 | 原因 |
|---------|------|
| test_every_def_has_required_fields | 定义完整性检查通用 |
| test_registry_has_native_eligible_tools | 注册表存在性 |
| test_send_email_not_registered / test_search_memory_not_registered | 注册表边界 |
| test_native_specs_* (约 4 个) | native schema 生成逻辑 |

**需要修改的测试：**

| 测试函数 | 修改内容 |
|---------|---------|
| test_action_registry_is_empty_in_v0_2 | 确认 ActionRegistry 在 runtime-refactor 后仍然为空或移除 |
| test_wechat_reply_is_in_tool_registry | Phase 11 后改为 test_send_text_message_is_in_tool_registry |
| test_contains_external_tools | 更新工具列表：send_text_message, finish_turn, skip_initiative, get_time, get_weather ... |
| test_wechat_reply_is_registered | Phase 11 后改为对应的新工具名 |
| test_skip_action_not_registered | Phase 11 后确认旧 skip 不在 |
| test_send_wechat_messages_is_terminal | Phase 11 后改为 test_finish_turn_is_terminal |
| test_skip_reply_is_terminal | Phase 11 后删除 |
| test_get_time_is_fact_tool / test_get_weather_is_fact_tool | 保留，fact tool 分类不变 |

**需要删除的测试（Phase 13 清理后）：**

| 测试函数 | 原因 |
|---------|------|
| test_wechat_reply_is_in_tool_registry | 旧工具名已删除 |
| test_contains_external_tools 中对 deprecated tools 的断言 | deprecated tools 已删除 |

**需要移到新文件的测试：**

| 测试函数 | 目标文件 | 原因 |
|---------|---------|------|
| 无 | -- | 当前 test_tool_registry.py 是 registry 单元测试，保留在此 |

### 9.3 tests/test_native_tool_calls.py（458 lines）

**可以保留不改的测试：**

| 测试函数 | 原因 |
|---------|------|
| test_disabled_returns_no_tools / test_empty_allowed_returns_no_tools | 工具过滤通用 |
| test_specs_are_openai_compatible | schema 格式验证 |
| test_get_time_spec_* / test_get_weather_spec_* | schema 细节 |
| test_*_executes_successfully (约 4 个) | 工具执行验证，rename 参数 |
| test_*_returns_error (约 4 个) | 工具错误处理 |
| test_*_tool_choice_* | LLM request 构造，不变 |
| test_no_strict_true_in_payload / test_no_beta_base_url / test_thinking_explicitly_disabled | provider payload 细节 |

**需要修改的测试：**

| 测试函数 | 修改内容 |
|---------|---------|
| test_wechat_reply_in_specs | 重命名为 test_send_text_message_in_specs，assert send_text_message |
| test_wechat_reply_is_action_tool | 重命名为 send_text_message 的相关测试 |
| test_old_send_wechat_messages_rejected | Phase 11 后可能改为 deprecated tool 不存在 |
| test_fact_tool_then_wechat_reply_native | wechat_reply -> send_text_message |

**需要删除的测试（Phase 11 后）：**

| 测试函数 | 原因 |
|---------|------|
| test_send_wechat_messages_is_terminal | 旧工具已删除 |
| test_skip_reply_is_terminal | 旧工具已删除 |

### 9.4 tests/test_proactive.py（1145 lines）

**可以保留不改的测试：**

| 测试函数 | 原因 |
|---------|------|
| test_source_constants_defined | 源常量不变 |
| test_proactive_source_not_equal_to_user | 源区分不变 |
| test_proactive_turn_rejects_non_none_user_message | 输入校验不变 |
| test_proactive_meta_stored_on_turn | metadata 基本语义不变 |
| test_*_guard_* (约 8 个) | guard 行为保留 |
| test_hard_fallback_returns_end_failed_no_fallback | fallback 抑制不变 |
| test_proactive_delivered_count_* (约 3 个) | delivered 计数逻辑保留 |
| test_*_eligibility_* (约 4 个) | eligibility 检查保留 |
| test_scheduler_initial_state / test_scheduler_rejects_concurrent | scheduler 基础行为不变 |

**需要修改的测试：**

| 测试函数 | 修改内容 |
|---------|---------|
| test_skip_proactive_registered | 改为 test_skip_initiative_registered，检查 skip_initiative |
| test_skip_proactive_arg_schema_has_reason_required | 改为 skip_initiative |
| test_skip_proactive_reason_enum | skip_initiative |
| test_skip_proactive_in_proactive_turn | skip_initiative, 添加 next_wake 参数断言 |
| test_skip_proactive_in_normal_turn_rejected | skip_initiative not in exposed |
| test_phase_a_allows_wechat_reply_and_skip_proactive | send_text_message + skip_initiative |
| test_phase_a_done_is_rejected | 改为 finish_turn 被拒绝 |
| test_phase_b_allows_wechat_reply_and_done | send_text_message + finish_turn |
| test_phase_c_only_done_allowed | finish_turn |
| test_phase_c_wechat_reply_triggers_end_proactive_max_reply_limit | send_text_message 超过 limit |
| test_max_llm_steps_exceeded_ends_properly | 添加 budget_exhausted 记录 |
| test_proactive_context_injected | 添加 context_blocks 验证 |
| test_proactive_context_has_no_user_message | 保持不变 |
| test_write_history_only_delivered | 添加 snapshot 验证 |

**需要移到新文件的测试：**

| 测试函数 | 目标文件 | 原因 |
|---------|---------|------|
| 所有 scheduler/eligibility 测试（约 8 个） | tests/test_scheduler_runtime.py | 模块拆分 |
| 所有 proactive guard 测试（约 8 个） | tests/test_output_guard.py | guard 统一 |

### 9.5 tests/test_output_guard.py（76 lines）

**可以保留不改：** 全部。

当前 12 个测试全部是关于协议泄露检测和消息 guard，功能不变。Guard 规则迁移后在 guard_messages 中补充对 send_text_message 参数格式的验证。

### 9.6 tests/test_memory.py（67 lines）

**可以保留不改：** 全部。

当前 7 个测试是关于 CJK 二元组 Jaccard 去重和 importance 计算，算法不变。

**需要添加（新文件 tests/test_memory_system.py）：**

- 六类记忆选择测试
- Forget directive 测试
- 记忆衰减/生命周期测试
- 长期记忆写入严格性测试
- 隐私级别过滤测试
- Bot 隔离测试

### 9.7 tests/test_temporal_context.py（386 lines）

**可以保留不改：** 全部。

当前约 30+ 测试覆盖时间感知上下文层（TemporalContextBuilder），该层功能保留。但在 Phase 3 ContextBuilder 统一后，需要验证 temporal context 是否作为 ContextBuilder 的一个 block 而不是独立入口。

**可能需要添加：**

- 验证 temporal context block 出现在 ContextBuilder 输出中的位置和格式

### 9.8 tests/test_scheduled_store.py（202 lines）

**可以保留不改：** 全部。

当前 8 个测试覆盖 MemoryStore 兼容性（并发初始化、contact state、scheduled wake up、legacy tables）。这些是存储层测试，功能不变。但在 Phase 8 MemorySystem facade 引入后，需要确认这些底层方法仍可通过 RuntimeStore 访问。

### 9.9 tests/test_llm_gateway.py（805 lines）

**可以保留不改：** 全部。

ProviderGateway 相关测试，provider adapter 测试，不变。Phase 6 需要添加结构化输出校验边界测试。

### 9.10 其他测试文件

| 文件 | 策略 |
|------|------|
| test_admin_events.py | 保留，admin 行为不变 |
| test_channel_guard.py | 保留，频道 guard 不变 |
| test_chat_security.py | 保留，安全检查不变 |
| test_message_actions.py | Phase 11 后更新工具名 |
| test_proactive_debug.py | 保留，debug 功能保持 |
| test_prompt_externalization.py | 保留，prompt 分包验证不变 |
| test_tools.py | Phase 11 后更新工具名 |
| test_wechat_bridge.py | 保留，bridge 层不变 |

---

## 10. 防回归清单

来自测试计划文档第 11 节的 11 个必须新增的防回归测试，此处展开为精确断言。

### REG-001：同一条 assistant 消息不能同时出现在 recent history 和 turn-local

```text
GIVEN turn 内 send_text_message 已 delivery "我在。"
AND assistant "我在。" 已在 turn_local block
WHEN 构建下一轮 context（或本轮第二次 LLM call）
THEN recent_history block 中不包含 "我在。"
AND turn_local block 中有且仅有一次 "我在。"
```

### REG-002：failed delivery 不能写入 assistant history

```text
GIVEN send_text_message 返回 DeliveryReceipt(status=failed)
WHEN turn 结束，执行 PERSIST_TURN_RESULT
THEN ConversationHistory.messages 中不含该消息
AND 该消息仅存在于 CurrentTurnLocalState.sent_messages_this_turn (status=failed)
```

### REG-003：tool result 不能作为 user message

```text
GIVEN get_weather 返回 "上海 25 度"
WHEN 构建下一轮 context
THEN tool result 的 role 不为 user
AND tool result 不在 recent_history block
AND tool result 在 turn_local.tool_results_this_turn
```

### REG-004：media summary 不能作为 user text

```text
GIVEN InboundMessageItem(kind=image, media.summary="图片里是一只杯子")
WHEN 构建 context
THEN current_input block 的 text 字段不为 "图片里是一只杯子"
AND "图片里是一只杯子" 仅出现在 media_context block
AND InboundMessageItem.text 为 null
```

### REG-005：prompt-visible tools 等于 native schema tools

```text
GIVEN ToolExposurePolicy.resolve() 返回 exposed_tool_names=[send_text_message, finish_turn]
AND ContextBuilder.build() 完成
THEN tool_contract_block 中提及的每个工具名在 native_tool_schemas 中都有对应 schema
AND native_tool_schemas 中的每个工具名在 tool_contract_block 中都被提及
AND 没有 tool_contract_block 提到但 native_tool_schemas 缺失的工具
```

### REG-006：未暴露工具调用被拒绝

```text
GIVEN ToolExposure.exposed_tool_names=[send_text_message, finish_turn]
AND 模型调用 generate_image_message(arguments=...)
WHEN TurnEngine 执行 VALIDATE_TOOL_CALL
THEN 工具被拒绝，reason=tool_not_exposed
AND ToolExecutionResult.status=rejected
AND 不实际执行 generate_image
```

### REG-007：主动 skip 后下一轮不暴露 skip_initiative

```text
GIVEN 上一轮 relationship_proactive 结束，last_decision=skipped
AND InitiativeState.consecutive_skip_count=1
WHEN SchedulerRuntime 触发下一轮 relationship_proactive
AND ToolExposurePolicy.resolve() 执行
THEN exposed_tool_names 不含 skip_initiative
AND denied_tools 包含 {tool_name: "skip_initiative", reason: "forced_speak"}
AND terminal_policy.allows_skip=false
AND terminal_policy.forced_speak=true
```

### REG-008：用户提醒到期优先于主动冷启动

```text
GIVEN due_user_reminder 存在（due_at <= now）
AND relationship_proactive cold_start also eligible
WHEN SchedulerRuntime heartbeat tick 执行
THEN claim_due_task 返回 user_reminder task
AND create user_reminder_due turn
AND 不创建 relationship_proactive turn
```

### REG-009：finish_turn 是唯一终态来源

```text
GIVEN 任意 turn_type
WHEN turn 结束
THEN terminal_state 被设定为 finished / finished_no_reply / finished_skipped 等
AND 终态判定来自 model 调用了 finish_turn 工具
AND 终态判定不来自 model content 文本中的自然语言
AND 不来自无工具调用的"停止"推断
```

### REG-010：context snapshot 可通过 turn_id 查询

```text
GIVEN turn_id=tn_test_001, turn 已完成
AND ContextSnapshotStore 保存了 2 个 snapshot（2 次 LLM call）
WHEN 通过 turn_id=tn_test_001 查询 snapshot
THEN 返回 2 个 ContextSnapshot
AND 每个 snapshot.snapshot_id 非空
AND 每个 snapshot.call_id 关联到对应的 model_call
AND 每个 snapshot.block_summaries 非空
AND snapshot 中不包含 API key
```

### REG-011：wechat_reply 已从核心 runtime 删除，测试防止重新引入

```text
GIVEN 核心 runtime 代码（TurnEngine, ToolExposurePolicy, ContextBuilder, ToolExecutor）
WHEN 搜索 "wechat_reply" 字符串
THEN 仅在以下位置出现：
  - adapter/bridge 层（WeChat transport）
  - migration/legacy 兼容模块（若有，且有删除计划）
  - 旧测试（等待更新）
  - 文档（历史记录）
AND 不在任何运行时核心模块中出现
AND 不在 ToolRegistry.native_tool_names 中出现
AND 不在 BuildContext 的 tool_contract_block 中出现
AND 不在任何 context block 的 kind 中出现
```

---

## 11. 测试执行顺序

测试套件按 Phase 分阶段执行，依赖关系明确。

```text
Phase 0: 设计文档冻结
  └─ 无代码测试

Phase 1: 数据结构骨架
  └─ tests/test_runtime_contracts.py          (L1, 新增)
  └─ 依赖：无

Phase 2: ToolExposurePolicy facade
  ├─ tests/test_tool_exposure_policy.py        (L2, 新增)
  ├─ tests/test_tool_registry.py               (L2, 更新)
  └─ 依赖：Phase 1 数据类型

Phase 3: ContextBuilder facade
  ├─ tests/test_context_builder.py             (L2, 新增)
  ├─ tests/test_temporal_context.py            (L2, 保留 + 3 tests added)
  └─ 依赖：Phase 2 ToolExposurePolicy 接口

Phase 4: ContextSnapshotStore + LLM trace 关联
  ├─ tests/test_context_snapshot_store.py      (L2, 新增)
  └─ 依赖：Phase 3 ContextBuilder 输出 BuiltContext

Phase 5: Turn-local state 接入
  ├─ tests/test_turn_engine_state_machine.py   (L3, 新增)
  └─ 依赖：Phase 3 ContextBuilder + Phase 4 snapshot

Phase 6: ProviderGateway 结构化边界
  ├─ tests/test_provider_gateway_structured_output.py (L2, 新增)
  ├─ tests/test_llm_gateway.py                 (L2, 保留 + 5 tests added)
  ├─ tests/test_native_tool_calls.py           (L2, 更新)
  └─ 依赖：Phase 5 Turn-local state 接口

Phase 7: ToolExecutor / DeliveryService facade
  ├─ tests/test_tool_executor.py               (L2, 新增)
  ├─ tests/test_delivery_service.py            (L2, 新增)
  └─ 依赖：Phase 6 ProviderGateway

Phase 8: MemorySystem facade
  ├─ tests/test_memory_system.py               (L2, 新增)
  ├─ tests/test_memory.py                      (L2, 保留)
  └─ 依赖：Phase 3 ContextBuilder + Phase 5 Turn-local state

Phase 9: SchedulerRuntime facade
  ├─ tests/test_scheduler_runtime.py           (L2, 新增)
  └─ 依赖：Phase 8 MemorySystem

Phase 10: Runtime 拆分
  ├─ tests/test_chat_runtime.py                (L4, 新增)
  ├─ tests/test_runtime_integration_chat.py    (L4, 新增)
  └─ 依赖：Phase 1-9 全部模块

Phase 11: 工具去渠道化
  ├─ tests/test_tool_registry.py               (L2, 最终更新)
  ├─ tests/test_chat_agent.py                  (L3, 更新)
  ├─ tests/test_proactive.py                   (L3, 更新)
  ├─ tests/test_native_tool_calls.py           (L2, 更新)
  └─ 依赖：Phase 10 模块定型

Phase 12: TurnEngine 定型 + ChatAgent 瘦身
  ├─ tests/test_runtime_integration_chat.py    (L4, 完成)
  ├─ tests/test_runtime_integration_proactive.py (L4, 新增)
  ├─ tests/test_runtime_integration_reminder.py  (L4, 新增)
  ├─ tests/test_chat_agent.py                  (L3, 最终更新)
  └─ 依赖：Phase 11 完成

Phase 13: 清理旧结构
  ├─ 全量回归：所有 Test Runner 命令
  └─ 依赖：Phase 12 完成
```

### 可并行执行的套件

以下套件无相互依赖，可并行开发/运行：

- Phase 1 tests/test_runtime_contracts.py (独立)
- Phase 2 + Phase 3 可部分并行（ContextBuilder 的接口依赖 ToolExposurePolicy 的数据类型，但测试 mock 可隔离）
- Phase 6 tests/test_native_tool_calls.py + tests/test_llm_gateway.py (可以并行)
- Phase 8 tests/test_memory.py + tests/test_memory_system.py (可并行)
- Phase 11 所有旧测试更新 (可并行执行)

---

## 12. 测试基础设施要求

### 12.1 Mock/Stub 清单

| 模块 | 需要 Mock | Mock 要求 |
|------|----------|----------|
| ProviderGateway | 是 | 必须模拟：valid tool calls（含 tool_call_id + name + arguments）、invalid output（无 tool call / 非法 JSON / 缺少必填字段）、provider error（timeout / 500 / 限流） |
| DeliveryService | 是 | 必须模拟：delivered（含 delivery_receipt_id + delivered_at）、failed（含 error_code）、unknown |
| ToolExecutor（fact tools） | 部分 | get_time 可用真实实现；weather 需 mock HTTP |
| MemoryStore | 否 | 使用 fixture tmp_path + SQLite |
| WeChatTransport | 是 | 必须模拟 send_text / send_image 返回，不出现在 L2/L3 测试中 |
| SchedulerRuntime clock | 是 | 时间模拟器：可设置 freeze_time、advance_time、reset_midnight |
| MediaAdapter | 是 | 模拟 image/voice item 解析，不真实下载 |

### 12.2 关键 Test Fixture

| Fixture 名称 | 作用 | 范围 |
|-------------|------|------|
| empty_memory_store | 空 SQLite 数据库在 tmp_path | session |
| populated_memory_store | 预填充：3 turns history + 5 memories + 2 reminders | function |
| active_initiative_state | InitiativeState(last_spoke_at, last_skipped_at, consecutive_skip_count=0) | function |
| skipped_initiative_state | InitiativeState(consecutive_skip_count=1, last_decision=skipped) | function |
| sample_inbound_batch_text | InboundMessageBatch with 1 text item | function |
| sample_inbound_batch_image | InboundMessageBatch with 1 image item + summary | function |
| mock_provider_gateway_ok | returns valid tool_call of specified name | function |
| mock_provider_gateway_error | returns provider error (timeout) | function |
| mock_delivery_service_ok | always returns delivered | function |
| mock_delivery_service_fail | always returns failed | function |
| freeze_clock_2026_06_03_14_00 | 固定时间 2026-06-03T14:00:00+08:00 | function |
| advance_clock_30m | 时间前进 30 分钟 | function |

### 12.3 ProviderGateway Mock 详细要求

```text
MockProviderGateway
  - 构造函数接收 responses: list[ModelCallResult]
  - 按顺序返回预置 response
  - 记录每次 call 的 request（用于断言 context 内容）
  - 支持 fail_mode: "no_tool_call" | "invalid_json" | "provider_error" | "timeout"
  - 支持 on_call callback（用于在 LLM call 之间注入 side effect，例如更新 turn_local）
```

### 12.4 SchedulerRuntime 时间模拟要求

```text
MockClock
  - freeze_time(dt: datetime)
  - advance(minutes: int)
  - reset_to_midnight()
  - set_quiet_hours_mode(enabled: bool)
  - get_current_time() -> datetime
```

---

## 13. 完成定义

### 13.1 每个 Phase 的完成定义

| Phase | 最低通过标准 | 覆盖率阈值 |
|-------|-------------|-----------|
| Phase 1 | tests/test_runtime_contracts.py 所有测试通过 | 100% 数据结构覆盖 |
| Phase 2 | tests/test_tool_exposure_policy.py TE-001 到 TE-016 通过 | 所有 turn_type 工具集 + 一致性检查 |
| Phase 3 | tests/test_context_builder.py CB-001 到 CB-025 通过 | 所有 block 顺序 + 所有污染防线 |
| Phase 4 | tests/test_context_snapshot_store.py 通过，CB-021 到 CB-025 通过 | 所有 snapshot 字段 + 可查询性 |
| Phase 5 | TN-006/TN-007/TN-010 通过，CB-006 到 CB-009 通过 | turn-local 与 history 分离 |
| Phase 6 | tests/test_provider_gateway_structured_output.py 通过 | 所有非法输出被拦截 |
| Phase 7 | TE-014 通过，TN-018 到 TN-022 通过 | 所有 delivery 状态机 + executor 校验 |
| Phase 8 | MM-001 到 MM-020 通过 | 六类记忆 + forget + 衰减 + bot 隔离 |
| Phase 9 | SR-001 到 SR-014 通过 | 心跳 + 优先级 + 频率 + 熔断 |
| Phase 10 | INT-001 到 INT-002 通过 | 全链路 chat + proactive |
| Phase 11 | REG-011 通过（wechat_reply 已从核心删除） | 全局搜索 zero match 在核心 |
| Phase 12 | INT-001 到 INT-015 全部通过 | 所有集成测试绿色 |
| Phase 13 | 全量回归通过 | 现有 350+ tests 通过 + 新增 150+ tests 通过 |

### 13.2 "所有测试通过" 的全局定义

1. 所有 L1 结构测试通过。
2. 所有 L2 模块测试（每个模块的 spec 覆盖）通过。
3. 所有 L3 状态机测试通过。
4. 所有 L4 集成测试通过。
5. 11 条防回归测试全部通过。
6. 现有全量测试回归通过（更新后的）。
7. 无 known skipped test 属于本矩阵的 P0 范围。

### 13.3 关于 Skipped Tests

- 允许 skip 的测试：仅限外部依赖不可控的测试（例如真实微信 iLink 连接、真实 weather API 调用）。
- 不允许 skip 的测试：任何可以使用 mock 的 P0/P1 测试。
- 如果 P0 测试因环境原因 skip，该 Phase 不算完成。必须先解决 skip 原因（例如补齐 mock、修复 fixture）。
- P2 测试可以 skip 并标注原因，不阻塞 Phase 完成。

### 13.4 最终验收命令

```powershell
# Phase 1-7 验证
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_contracts.py tests/test_tool_exposure_policy.py tests/test_context_builder.py tests/test_context_snapshot_store.py tests/test_turn_engine_state_machine.py tests/test_provider_gateway_structured_output.py tests/test_tool_executor.py tests/test_delivery_service.py -v

# Phase 8-9 验证（加 memory + scheduler）
.\.venv\Scripts\python.exe -m pytest tests/test_memory_system.py tests/test_memory.py tests/test_scheduler_runtime.py -v

# Phase 10-12 集成验证
.\.venv\Scripts\python.exe -m pytest tests/test_runtime_integration_chat.py tests/test_runtime_integration_proactive.py tests/test_runtime_integration_reminder.py tests/test_chat_runtime.py -v

# Phase 13 全量回归
.\.venv\Scripts\python.exe -m pytest -q
```

---

## 附录 A：测试 ID 索引

| ID 前缀 | 模块 | 测试数量 | 优先级分布 |
|---------|------|---------|-----------|
| CB- | ContextBuilder | 25 + 10 场景 | P0:18, P1:14, P2:3 |
| TE- | ToolExposurePolicy | 16 + 6 场景 | P0:14, P1:8 |
| TN- | TurnEngine | 22 + 9 场景 | P0:20, P1:11 |
| MM- | MemorySystem | 20 + 4 场景 | P0:14, P1:8, P2:2 |
| SR- | SchedulerRuntime | 14 + 6 场景 | P0:13, P1:7 |
| INT- | 跨模块集成 | 15 | P0:13, P1:2 |
| REG- | 防回归 | 11 | P0:11 |
| **总计** | | **178** | P0:103, P1:63, P2:5 |

---

## 附录 B：与 5 份模块 Spec 的交叉引用

| Spec 文件 | 对应测试段落 | 关键不变式 |
|-----------|------------|-----------|
| 01-context-builder-spec | CB-001 ~ CB-S10 (第 3 节) | block 顺序、turn-local 分离、污染防线、snapshot 完整性 |
| 02-tool-exposure-policy-spec | TE-001 ~ TE-S06 (第 4 节) | turn_type 工具集、prompt-schema 一致、proactive 图片限制 |
| 03-turn-engine-spec | TN-001 ~ TN-S09 (第 5 节) | 15 态流转、9 种 turn type、送达六态、中断 6 点、hard fallback |
| 04-memory-system-spec | MM-001 ~ MM-S04 (第 6 节) | 六类记忆、边界优先、forget 窗口、衰减、写入严格性 |
| 05-scheduler-runtime-spec | SR-001 ~ SR-S06 (第 7 节) | 心跳、提醒优先、claim 原子性、频率、熔断 |

---

> 本测试矩阵将在 5 份模块 spec 落盘后做最终对齐。当前所有测试用例基于源设计文档（contracts/data-contracts/state-machines）编写，测试 ID 将作为 spec 中"验证"段落的交叉引用目标。
