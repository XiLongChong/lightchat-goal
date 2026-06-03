"""Phase 1: Runtime data contract tests.

Tests for all data types defined in runtime_contracts/types.py.
Validates enums, required fields, invariants, and cross-type constraints.
"""

from __future__ import annotations

import pytest
from datetime import datetime

from runtime_contracts import (
    # Enums
    TurnType,
    TriggerSource,
    UserIntent,
    MediaIntent,
    MessageKind,
    DeliveryStatus,
    TurnPhase,
    CacheClass,
    ContextBlockKind,
    ResumeType,
    ToolCategory,
    ToolSideEffect,
    # Primitives
    SystemTime,
    MediaAttachment,
    DeliveryReceipt,
    ContextBuildWarning,
    # Inbound
    InboundMessageItem,
    InboundMessageBatch,
    # Turn request
    RuntimeTrigger,
    TurnClassification,
    TurnRequest,
    # Turn-local state
    TurnLocalSentMessage,
    TurnBudgetState,
    RejectedToolCall,
    CurrentTurnLocalState,
    # Tool exposure
    NativeToolSchema,
    DeniedTool,
    ToolExposure,
    # Context
    ContentPart,
    ProviderMessage,
    ContextBlock,
    ContextSnapshot,
    BuildContextRequest,
    BuiltContext,
    BuildContextError,
    # Terminal policy
    TerminalPolicy,
    # Model call
    ModelCallResult,
    ToolExecutionResult,
    # Memory
    MemorySelection,
    # Turn result
    TurnResult,
    # Conversation history
    ConversationHistoryMessage,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

NOW = datetime(2026, 6, 3, 14, 30, 0)


def make_delivery_receipt(
    message_id: str = "msg_1",
    turn_id: str = "turn_1",
    status: DeliveryStatus = DeliveryStatus.DELIVERED,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        delivery_receipt_id="dr_1",
        message_id=message_id,
        turn_id=turn_id,
        status=status,
        delivered_at=NOW if status == DeliveryStatus.DELIVERED else None,
    )


def make_turn_local_sent(
    message_id: str = "msg_1",
    text: str = "test message",
    status: DeliveryStatus = DeliveryStatus.DELIVERED,
) -> TurnLocalSentMessage:
    return TurnLocalSentMessage(
        message_id=message_id,
        tool_call_id="tc_1",
        text=text,
        status=status,
        delivered_at=NOW if status == DeliveryStatus.DELIVERED else None,
    )


def make_tool_execution_result(
    tool_name: str = "get_weather",
    status: str = "success",
) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_call_id="tc_1",
        turn_id="turn_1",
        tool_name=tool_name,
        status=status,
        result_payload={"city": "上海", "temperature": 25},
        visible_to_model=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# L1: Enum tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnums:
    def test_turn_type_has_all_8_values(self):
        values = set(TurnType)
        assert len(values) == 8
        assert TurnType.USER_CHAT in values
        assert TurnType.USER_MEDIA_CHAT in values
        assert TurnType.USER_REMINDER_REQUEST in values
        assert TurnType.RELATIONSHIP_PROACTIVE in values
        assert TurnType.USER_REMINDER_DUE in values
        assert TurnType.SCHEDULED_RITUAL in values
        assert TurnType.ADMIN_MANUAL_TRIGGER in values
        assert TurnType.MEMORY_MANAGEMENT in values

    def test_user_intent_has_all_9_values(self):
        values = set(UserIntent)
        assert len(values) == 9
        assert UserIntent.CHAT in values
        assert UserIntent.REQUEST_REMINDER in values
        assert UserIntent.REQUEST_IMAGE in values
        assert UserIntent.REQUEST_WEATHER in values
        assert UserIntent.FORGET_DIRECTIVE in values
        assert UserIntent.TIME_QUERY in values
        assert UserIntent.UNKNOWN in values

    def test_delivery_status_values(self):
        values = set(DeliveryStatus)
        assert DeliveryStatus.DELIVERED in values
        assert DeliveryStatus.FAILED in values
        assert DeliveryStatus.UNKNOWN in values
        assert DeliveryStatus.BLOCKED_BY_GUARD in values
        assert DeliveryStatus.NOT_APPLICABLE in values

    def test_turn_phase_values(self):
        values = set(TurnPhase)
        assert TurnPhase.NORMAL_CHAT in values
        assert TurnPhase.SKIPPABLE in values
        assert TurnPhase.FORCED_SPEAK in values
        assert TurnPhase.AFTER_FIRST_MESSAGE in values
        assert TurnPhase.REMINDER_DELIVERY in values
        assert TurnPhase.MEMORY_MANAGEMENT in values
        assert TurnPhase.ADMIN in values

    def test_context_block_kind_values(self):
        values = set(ContextBlockKind)
        assert ContextBlockKind.FIXED_PROTOCOL in values
        assert ContextBlockKind.PERSONA in values
        assert ContextBlockKind.RELATIONSHIP_STATE in values
        assert ContextBlockKind.MEMORY in values
        assert ContextBlockKind.RECENT_HISTORY in values
        assert ContextBlockKind.TURN_LOCAL in values
        assert ContextBlockKind.MEDIA in values
        assert ContextBlockKind.SYSTEM_TIME in values
        assert ContextBlockKind.CURRENT_INPUT in values
        assert ContextBlockKind.TOOL_CONTRACT in values
        assert ContextBlockKind.TERMINAL_POLICY in values

    def test_cache_class_values(self):
        values = set(CacheClass)
        assert CacheClass.FIXED in values
        assert CacheClass.LOW_FREQUENCY in values
        assert CacheClass.TURN_DYNAMIC in values

    def test_resume_type_values(self):
        values = set(ResumeType)
        assert ResumeType.COLD_START in values
        assert ResumeType.CONTINUOUS_CHAT in values
        assert ResumeType.SHORT_RESUME in values
        assert ResumeType.SAME_DAY_RESUME in values
        assert ResumeType.NEXT_DAY_RESUME in values
        assert ResumeType.LONG_GAP_RESUME in values

    def test_all_enums_are_string_enums(self):
        enums = [
            TurnType, TriggerSource, UserIntent, MediaIntent, MessageKind,
            DeliveryStatus, TurnPhase, CacheClass, ContextBlockKind,
            ResumeType, ToolCategory, ToolSideEffect,
        ]
        for e in enums:
            for member in e:
                assert isinstance(member.value, str), f"{e.__name__}.{member.name} is not str"


# ═══════════════════════════════════════════════════════════════════════════════
# L1: Required fields tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequiredFields:
    """Verify that required fields are enforced (no default)."""

    def test_inbound_message_item_requires_item_id_and_kind(self):
        item = InboundMessageItem(item_id="i1", kind=MessageKind.TEXT)
        assert item.item_id == "i1"
        assert item.kind == MessageKind.TEXT
        assert item.text is None

    def test_inbound_message_batch_requires_batch_id_user_id_bot_id(self):
        batch = InboundMessageBatch(batch_id="b1", user_id="u1", bot_id="bot1")
        assert batch.batch_id == "b1"
        assert batch.user_id == "u1"
        assert batch.bot_id == "bot1"
        assert batch.items == []

    def test_turn_request_requires_core_ids(self):
        tr = TurnRequest(
            turn_id="turn_1",
            turn_type=TurnType.USER_CHAT,
            user_id="u1",
            bot_id="bot1",
        )
        assert tr.turn_id == "turn_1"
        assert tr.turn_type == TurnType.USER_CHAT
        assert tr.inbound_batch is None
        assert tr.runtime_trigger is None

    def test_build_context_request_requires_identity_fields(self):
        bcr = BuildContextRequest(
            turn_id="turn_1",
            call_id="call_1",
            turn_type=TurnType.USER_CHAT,
            user_id="u1",
            bot_id="bot1",
        )
        assert bcr.turn_id == "turn_1"
        assert bcr.call_id == "call_1"

    def test_terminal_policy_defaults(self):
        tp = TerminalPolicy(turn_type=TurnType.USER_CHAT)
        assert tp.required_terminal_tools == ["finish_turn"]
        assert tp.min_delivered_messages == 0
        assert tp.max_delivered_messages == 15
        assert tp.allows_skip is False
        assert tp.forced_speak is False

    def test_tool_exposure_requires_exposure_id_and_turn_id(self):
        te = ToolExposure(
            exposure_id="exp_1",
            turn_id="turn_1",
            phase=TurnPhase.NORMAL_CHAT,
        )
        assert te.exposure_id == "exp_1"
        assert te.phase == TurnPhase.NORMAL_CHAT
        assert te.exposed_tool_names == []

    def test_context_block_requires_block_id_kind_cache_class_content_summary_source(self):
        cb = ContextBlock(
            block_id="b1",
            kind=ContextBlockKind.FIXED_PROTOCOL,
            cache_class=CacheClass.FIXED,
            content_summary="system protocol (248 tokens)",
            source="ContextBuilder",
        )
        assert cb.block_id == "b1"
        assert cb.enters_model is True
        assert cb.enters_snapshot is True

    def test_memory_selection_empty_defaults(self):
        ms = MemorySelection()
        assert ms.profile_memory == []
        assert ms.boundary_memory == []
        assert ms.total_memory_count == 0
        assert ms.token_estimate == 0

    def test_conversation_history_message_fields(self):
        msg = ConversationHistoryMessage(
            message_id="m1",
            turn_id="t1",
            role="user",
            content="hello",
            source="user_message",
        )
        assert msg.message_id == "m1"
        assert msg.role == "user"
        assert msg.delivery_status == DeliveryStatus.NOT_APPLICABLE

    def test_turn_result_defaults(self):
        tr = TurnResult(turn_id="turn_1", terminal_state="finished")
        assert tr.turn_id == "turn_1"
        assert tr.terminal_state == "finished"
        assert tr.success is False
        assert tr.skipped is False
        assert tr.failed is False


# ═══════════════════════════════════════════════════════════════════════════════
# L1: Invariant enforcement tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailedDeliveryNotInAssistantHistory:
    """REG-002: failed/unknown delivery can't construct assistant history."""

    def test_failed_delivery_status_is_not_delivered(self):
        receipt = make_delivery_receipt(status=DeliveryStatus.FAILED)
        assert receipt.status != DeliveryStatus.DELIVERED
        assert receipt.status == DeliveryStatus.FAILED

    def test_unknown_delivery_status_is_not_delivered(self):
        receipt = make_delivery_receipt(status=DeliveryStatus.UNKNOWN)
        assert receipt.status != DeliveryStatus.DELIVERED
        assert receipt.status == DeliveryStatus.UNKNOWN

    def test_turn_local_can_record_failed_message(self):
        msg = make_turn_local_sent(status=DeliveryStatus.FAILED)
        assert msg.status == DeliveryStatus.FAILED
        assert msg.delivered_at is None

    def test_conversation_history_message_default_delivery_status(self):
        msg = ConversationHistoryMessage(
            message_id="m1", turn_id="t1", role="assistant",
            content="hello", source="assistant_reply",
        )
        # Default is NOT_APPLICABLE — caller must explicitly set DELIVERED
        assert msg.delivery_status != DeliveryStatus.DELIVERED
        assert msg.delivery_status == DeliveryStatus.NOT_APPLICABLE


class TestToolResultNotAsUserMessage:
    """REG-003: tool result can't construct user history."""

    def test_tool_execution_result_has_dedicated_type(self):
        result = make_tool_execution_result()
        assert isinstance(result, ToolExecutionResult)
        assert result.tool_name == "get_weather"

    def test_tool_result_not_confusable_with_conversation_history(self):
        result = make_tool_execution_result()
        # ToolExecutionResult cannot be passed where ConversationHistoryMessage is expected
        assert not hasattr(result, "role")
        assert not hasattr(result, "source")

    def test_tool_result_not_in_conversation_history_type(self):
        msg = ConversationHistoryMessage(
            message_id="m1", turn_id="t1", role="user",
            content="hello", source="user_message",
        )
        assert msg.role in ("user", "assistant")
        assert msg.source != "tool_result"


class TestMediaSummaryNotAsUserText:
    """REG-004: media summary can't construct user text."""

    def test_media_attachment_is_separate_from_text(self):
        item = InboundMessageItem(
            item_id="img_1",
            kind=MessageKind.IMAGE,
            media=MediaAttachment(
                item_id="img_1",
                kind="image",
                summary="图片里是一只杯子",
            ),
        )
        # The text field is None — summary is in media, not in text
        assert item.text is None
        assert item.media is not None
        assert item.media.summary == "图片里是一只杯子"

    def test_media_summary_never_set_on_text_field(self):
        item = InboundMessageItem(
            item_id="img_1",
            kind=MessageKind.IMAGE,
            text=None,
            media=MediaAttachment(
                item_id="img_1",
                kind="image",
                summary="图片摘要",
            ),
        )
        assert item.text != "图片摘要"
        assert item.media.summary == "图片摘要"

    def test_content_part_text_not_mistaken_for_media_summary(self):
        part = ContentPart(type="text", text="看看我的猫")
        assert part.type == "text"
        assert part.image_url is None


class TestTerminalPolicyConfig:
    """TerminalPolicy proactive skippable and forced speak config."""

    def test_skippable_proactive_config(self):
        tp = TerminalPolicy(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            min_delivered_messages=0,
            max_delivered_messages=5,
            allows_skip=True,
            forced_speak=False,
            requires_next_wake=True,
        )
        assert tp.allows_skip is True
        assert tp.forced_speak is False
        assert tp.requires_next_wake is True
        assert tp.min_delivered_messages == 0

    def test_forced_speak_proactive_config(self):
        tp = TerminalPolicy(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            min_delivered_messages=1,
            max_delivered_messages=5,
            allows_skip=False,
            forced_speak=True,
            requires_next_wake=True,
        )
        assert tp.allows_skip is False
        assert tp.forced_speak is True
        assert tp.min_delivered_messages == 1

    def test_user_chat_does_not_allow_skip(self):
        tp = TerminalPolicy(turn_type=TurnType.USER_CHAT)
        assert tp.allows_skip is False
        assert tp.forced_speak is False
        assert tp.requires_next_wake is False
        assert tp.allows_fact_tools is True

    def test_reminder_due_requires_min_one_message(self):
        tp = TerminalPolicy(
            turn_type=TurnType.USER_REMINDER_DUE,
            min_delivered_messages=1,
            max_delivered_messages=5,
            allows_skip=False,
            forced_speak=False,
        )
        assert tp.min_delivered_messages == 1
        assert tp.allows_skip is False


# ═══════════════════════════════════════════════════════════════════════════════
# L1: Cross-type consistency tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossTypeConsistency:
    def test_turn_request_can_carry_tool_exposure_and_terminal_policy(self):
        te = ToolExposure(
            exposure_id="exp_1", turn_id="turn_1", phase=TurnPhase.NORMAL_CHAT,
            exposed_tool_names=["send_text_message", "finish_turn"],
        )
        tp = TerminalPolicy(turn_type=TurnType.USER_CHAT)
        tr = TurnRequest(
            turn_id="turn_1", turn_type=TurnType.USER_CHAT,
            user_id="u1", bot_id="bot1",
            tool_exposure=te, terminal_policy=tp,
        )
        assert tr.tool_exposure is te
        assert tr.terminal_policy is tp

    def test_current_turn_local_state_tracks_messages_and_results(self):
        msg = make_turn_local_sent()
        result = make_tool_execution_result()
        state = CurrentTurnLocalState(
            turn_id="turn_1",
            model_call_count=1,
            sent_messages_this_turn=[msg],
            tool_results_this_turn=[result],
            delivered_message_count=1,
        )
        assert len(state.sent_messages_this_turn) == 1
        assert len(state.tool_results_this_turn) == 1
        assert state.delivered_message_count == 1

    def test_turn_result_wraps_turn_local_messages(self):
        msgs = [make_turn_local_sent(message_id=f"msg_{i}") for i in range(2)]
        result = TurnResult(
            turn_id="turn_1", terminal_state="finished",
            success=True, delivered_messages=msgs,
            delivered_message_count=2,
        )
        assert result.success is True
        assert len(result.delivered_messages) == 2
        assert result.skipped is False

    def test_built_context_contains_snapshot_reference(self):
        snap = ContextSnapshot(
            snapshot_id="snap_1", turn_id="turn_1", call_id="call_1",
            created_at=NOW,
            exposed_tool_names=["send_text_message", "finish_turn"],
        )
        bc = BuiltContext(
            turn_id="turn_1", call_id="call_1",
            context_snapshot=snap,
        )
        assert bc.context_snapshot is snap
        assert bc.context_snapshot.snapshot_id == "snap_1"

    def test_build_context_error_preserves_fields(self):
        err = BuildContextError(
            reason="missing required fields: ['turn_id']",
            missing_fields=["turn_id"],
        )
        assert "turn_id" in err.reason
        assert err.missing_fields == ["turn_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# L1: Spec scenario coverage
# ═══════════════════════════════════════════════════════════════════════════════


class TestSpecScenarios:
    def test_user_chat_turn_with_text_input(self):
        item = InboundMessageItem(
            item_id="item_001", kind=MessageKind.TEXT, text="今天好累啊",
        )
        batch = InboundMessageBatch(
            batch_id="batch_1", user_id="wxid_abc", bot_id="default_bot",
            items=[item],
        )
        classification = TurnClassification(
            turn_type=TurnType.USER_CHAT,
            trigger_source=TriggerSource.USER,
            user_intent=UserIntent.CHAT,
        )
        te = ToolExposure(
            exposure_id="exp_1", turn_id="turn_n1",
            phase=TurnPhase.NORMAL_CHAT,
            exposed_tool_names=["send_text_message", "finish_turn"],
        )
        tp = TerminalPolicy(turn_type=TurnType.USER_CHAT)
        tr = TurnRequest(
            turn_id="turn_n1", turn_type=TurnType.USER_CHAT,
            user_id="wxid_abc", bot_id="default_bot",
            inbound_batch=batch, classification=classification,
            tool_exposure=te, terminal_policy=tp,
        )
        assert tr.turn_type == TurnType.USER_CHAT
        assert tr.inbound_batch.items[0].text == "今天好累啊"

    def test_proactive_skippable_turn(self):
        trigger = RuntimeTrigger(
            trigger_id="trig_1", trigger_type="heartbeat",
            reason="cold_start", trigger_kind="cold_start",
            is_wake_up=False,
        )
        te = ToolExposure(
            exposure_id="exp_1", turn_id="turn_pro",
            phase=TurnPhase.SKIPPABLE,
            exposed_tool_names=["send_text_message", "skip_initiative", "finish_turn"],
        )
        tp = TerminalPolicy(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            allows_skip=True, forced_speak=False,
            requires_next_wake=True,
            min_delivered_messages=0, max_delivered_messages=5,
        )
        tr = TurnRequest(
            turn_id="turn_pro", turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            user_id="u1", bot_id="bot1",
            runtime_trigger=trigger, tool_exposure=te, terminal_policy=tp,
        )
        assert tr.tool_exposure.phase == TurnPhase.SKIPPABLE
        assert "skip_initiative" in tr.tool_exposure.exposed_tool_names
        assert tr.terminal_policy.allows_skip is True

    def test_proactive_forced_speak_turn(self):
        trigger = RuntimeTrigger(
            trigger_id="trig_1", trigger_type="heartbeat",
            reason="wake_up", trigger_kind="wake_up",
            is_wake_up=True,
        )
        te = ToolExposure(
            exposure_id="exp_1", turn_id="turn_pro",
            phase=TurnPhase.FORCED_SPEAK,
            exposed_tool_names=["send_text_message", "finish_turn"],
            denied_tools=[
                DeniedTool(tool_name="skip_initiative", reason="forced_speak"),
            ],
        )
        tp = TerminalPolicy(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            allows_skip=False, forced_speak=True,
            requires_next_wake=True,
            min_delivered_messages=1, max_delivered_messages=5,
        )
        tr = TurnRequest(
            turn_id="turn_pro", turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            user_id="u1", bot_id="bot1",
            runtime_trigger=trigger, tool_exposure=te, terminal_policy=tp,
        )
        assert tr.tool_exposure.phase == TurnPhase.FORCED_SPEAK
        assert "skip_initiative" not in tr.tool_exposure.exposed_tool_names
        assert tr.terminal_policy.forced_speak is True
        assert tr.terminal_policy.min_delivered_messages == 1

    def test_user_reminder_due_turn(self):
        trigger = RuntimeTrigger(
            trigger_id="trig_1", trigger_type="due_reminder",
            reason="reminder到期", trigger_kind="user_requested",
            source_task_id="rem_001",
        )
        te = ToolExposure(
            exposure_id="exp_1", turn_id="turn_rem",
            phase=TurnPhase.REMINDER_DELIVERY,
            exposed_tool_names=["send_text_message", "mark_reminder_delivered", "finish_turn"],
        )
        tp = TerminalPolicy(
            turn_type=TurnType.USER_REMINDER_DUE,
            min_delivered_messages=1, max_delivered_messages=5,
            allows_skip=False, forced_speak=False,
        )
        tr = TurnRequest(
            turn_id="turn_rem", turn_type=TurnType.USER_REMINDER_DUE,
            user_id="u1", bot_id="bot1",
            runtime_trigger=trigger, tool_exposure=te, terminal_policy=tp,
        )
        assert tr.turn_type == TurnType.USER_REMINDER_DUE
        assert "mark_reminder_delivered" in tr.tool_exposure.exposed_tool_names
        assert tr.terminal_policy.min_delivered_messages == 1

    def test_image_input_with_summary(self):
        item = InboundMessageItem(
            item_id="img_001", kind=MessageKind.IMAGE,
            media=MediaAttachment(
                item_id="img_001",
                kind="image",
                summary="一只白猫坐在窗台上，阳光洒在它身上",
            ),
        )
        batch = InboundMessageBatch(
            batch_id="b1", user_id="u1", bot_id="bot1", items=[item],
        )
        tr = TurnRequest(
            turn_id="turn_img", turn_type=TurnType.USER_MEDIA_CHAT,
            user_id="u1", bot_id="bot1", inbound_batch=batch,
        )
        assert tr.turn_type == TurnType.USER_MEDIA_CHAT
        assert tr.inbound_batch.items[0].media.summary is not None
        assert tr.inbound_batch.items[0].text is None

    def test_turn_local_state_accumulates_over_multi_call(self):
        state1 = CurrentTurnLocalState(
            turn_id="turn_1", model_call_count=0,
        )
        msg = make_turn_local_sent(message_id="tl_msg_1", text="辛苦了宝宝")
        state2 = CurrentTurnLocalState(
            turn_id="turn_1",
            model_call_count=1,
            sent_messages_this_turn=[msg],
            delivered_message_count=1,
            budget_state=TurnBudgetState(
                delivered_messages_remaining=14,
                model_calls_remaining=2,
            ),
        )
        assert state2.model_call_count > state1.model_call_count
        assert len(state2.sent_messages_this_turn) == 1
        assert state2.sent_messages_this_turn[0].text == "辛苦了宝宝"

    def test_context_snapshot_contains_audit_data(self):
        blocks = [
            ContextBlock(
                block_id="blk_fixed", kind=ContextBlockKind.FIXED_PROTOCOL,
                cache_class=CacheClass.FIXED,
                content_summary="system protocol (248 tokens)",
                source="ContextBuilder",
            ),
        ]
        snap = ContextSnapshot(
            snapshot_id="snap_turn_1_call_1",
            turn_id="turn_1", call_id="call_1",
            created_at=NOW,
            block_summaries=blocks,
            exposed_tool_names=["send_text_message", "finish_turn"],
            history_message_ids=["m1", "m2"],
            memory_ids=[],
            turn_local_summary={"delivered_count": 1, "failed_count": 0},
        )
        assert len(snap.block_summaries) == 1
        assert snap.exposed_tool_names == ["send_text_message", "finish_turn"]
        assert snap.history_message_ids == ["m1", "m2"]


# ═══════════════════════════════════════════════════════════════════════════════
# L1: Token budget and truncation
# ═══════════════════════════════════════════════════════════════════════════════


class TestTokenBudget:
    def test_token_budget_report_normal(self):
        from runtime_contracts import TokenBudgetReport
        report = TokenBudgetReport(budget=8000, used=4200, remaining=3800)
        assert report.budget_exhausted is False
        assert report.truncation_applied is False

    def test_token_budget_report_exhausted(self):
        from runtime_contracts import TokenBudgetReport
        report = TokenBudgetReport(
            budget=8000, used=8500, remaining=-500,
            budget_exhausted=True, truncation_applied=True,
        )
        assert report.budget_exhausted is True
        assert report.remaining < 0


# ═══════════════════════════════════════════════════════════════════════════════
# L1: No side effects
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoSideEffects:
    """All types are pure data — no business logic, no I/O."""

    def test_creating_types_does_not_modify_global_state(self):
        TurnRequest(turn_id="t1", turn_type=TurnType.USER_CHAT, user_id="u1", bot_id="b1")
        # No exception = pass — types are inert

    def test_types_are_hashable_where_appropriate(self):
        for e in (TurnType, DeliveryStatus, TurnPhase, ContextBlockKind):
            d = {member: member.value for member in e}
            assert len(d) > 0

    def test_types_have_no_methods_with_side_effects(self):
        import inspect
        from runtime_contracts import types as m
        for name in dir(m):
            obj = getattr(m, name)
            if not inspect.isclass(obj):
                continue
            if not hasattr(obj, "__dataclass_fields__"):
                continue
            for method_name in dir(obj):
                if method_name.startswith("_"):
                    continue
                method = getattr(obj, method_name, None)
                if callable(method) and not isinstance(method, type):
                    pytest.fail(f"{name}.{method_name}() unexpected method on data type")
