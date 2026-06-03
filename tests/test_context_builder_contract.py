"""Phase 3: ContextBuilder contract tests.

Validates context block ordering, turn-local/history separation,
failed delivery exclusion, tool result isolation, media handling,
and snapshot integrity.

Spec: docs/runtime-specs/01-context-builder-spec.zh.md
Test matrix: CB-001 to CB-025 + CB-S01 to CB-S10
"""

from __future__ import annotations

import pytest
from datetime import datetime

from agent.runtime_context_builder import ContextBuilder
from runtime_contracts import (
    BuildContextError,
    BuildContextRequest,
    BuiltContext,
    CacheClass,
    ContextBlock,
    ContextBlockKind,
    ContextBuildWarning,
    ContextSnapshot,
    ConversationHistoryMessage,
    CurrentTurnLocalState,
    DeliveryReceipt,
    DeliveryStatus,
    InboundMessageBatch,
    InboundMessageItem,
    MediaAttachment,
    MediaContextItem,
    MemorySelection,
    MessageKind,
    NativeToolSchema,
    ProviderMessage,
    SystemTime,
    TerminalPolicy,
    ToolExposure,
    ToolExecutionResult,
    TurnLocalSentMessage,
    TurnPhase,
    TurnType,
)

NOW = datetime(2026, 6, 3, 14, 30, 0)

# ── Helpers ───────────────────────────────────────────────────────


def make_bcr(**overrides) -> BuildContextRequest:
    defaults = dict(
        turn_id="turn_1",
        call_id="call_1",
        turn_type=TurnType.USER_CHAT,
        user_id="u1",
        bot_id="bot1",
        fixed_system_protocol="[SYSTEM PROTOCOL]",
        fixed_output_protocol="[OUTPUT PROTOCOL]",
        fixed_tool_protocol="[TOOL PROTOCOL]",
        persona="你是一个温柔的助手。",
        system_time=SystemTime(dt=NOW, timezone="Asia/Shanghai", weekday_cn="周三"),
        tool_exposure=ToolExposure(
            exposure_id="exp_1", turn_id="turn_1",
            phase=TurnPhase.NORMAL_CHAT,
            exposed_tool_names=["send_text_message", "finish_turn"],
            native_tool_schemas=[
                NativeToolSchema(name="send_text_message", description="发送消息", parameters_schema={"type": "object", "properties": {"text": {"type": "string"}}}),
                NativeToolSchema(name="finish_turn", description="结束回合", parameters_schema={"type": "object", "properties": {}}),
            ],
            tool_contract_block="可用工具: send_text_message, finish_turn",
        ),
        terminal_policy=TerminalPolicy(turn_type=TurnType.USER_CHAT),
        current_turn_local_state=CurrentTurnLocalState(turn_id="turn_1"),
        memory_selection=MemorySelection(),
        recent_history=[],
        media_context=[],
    )
    defaults.update(overrides)
    return BuildContextRequest(**defaults)


@pytest.fixture
def builder() -> ContextBuilder:
    return ContextBuilder()


# ═══════════════════════════════════════════════════════════════════
# CB-001: Block ordering
# ═══════════════════════════════════════════════════════════════════

EXPECTED_BLOCK_ORDER = [
    ContextBlockKind.FIXED_PROTOCOL,   # 1. fixed_system_protocol
    ContextBlockKind.FIXED_PROTOCOL,   # 2. fixed_output_protocol
    ContextBlockKind.FIXED_PROTOCOL,   # 3. fixed_tool_protocol
    ContextBlockKind.PERSONA,          # 4. persona
    # 5. relationship_state (skipped if None)
    ContextBlockKind.SYSTEM_TIME,      # 6. system_time_block
    # 7-10. memory blocks (skipped if empty)
    ContextBlockKind.RECENT_HISTORY,   # 11. recent_conversation_history
    ContextBlockKind.TURN_LOCAL,       # 12. current_turn_local_state
    # 13. media_context (skipped if empty)
    ContextBlockKind.CURRENT_INPUT,    # 14. current_input
    ContextBlockKind.TOOL_CONTRACT,    # 15. exposed_tools_contract
    ContextBlockKind.TERMINAL_POLICY,  # 16. terminal_policy
]


class TestBlockOrdering:
    def test_core_blocks_appear_in_spec_order(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        kinds = [b.kind for b in result.context_blocks]
        # Check relative ordering of the always-present blocks
        assert kinds.index(ContextBlockKind.PERSONA) < kinds.index(ContextBlockKind.SYSTEM_TIME)
        assert kinds.index(ContextBlockKind.SYSTEM_TIME) < kinds.index(ContextBlockKind.TURN_LOCAL)
        assert kinds.index(ContextBlockKind.TURN_LOCAL) < kinds.index(ContextBlockKind.CURRENT_INPUT)
        assert kinds.index(ContextBlockKind.CURRENT_INPUT) < kinds.index(ContextBlockKind.TOOL_CONTRACT)
        assert kinds.index(ContextBlockKind.TOOL_CONTRACT) < kinds.index(ContextBlockKind.TERMINAL_POLICY)

    def test_all_fixed_blocks_have_fixed_or_low_frequency_cache(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        for blk in result.context_blocks:
            if blk.kind == ContextBlockKind.FIXED_PROTOCOL:
                assert blk.cache_class == CacheClass.FIXED, f"{blk.block_id} not fixed"
            if blk.kind == ContextBlockKind.PERSONA:
                assert blk.cache_class in (CacheClass.FIXED, CacheClass.LOW_FREQUENCY)

    def test_dynamic_blocks_have_turn_dynamic_cache(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        for blk in result.context_blocks:
            if blk.kind in (
                ContextBlockKind.TURN_LOCAL, ContextBlockKind.CURRENT_INPUT,
                ContextBlockKind.TOOL_CONTRACT, ContextBlockKind.TERMINAL_POLICY,
            ):
                assert blk.cache_class == CacheClass.TURN_DYNAMIC, f"{blk.block_id} not turn_dynamic"


# ═══════════════════════════════════════════════════════════════════
# CB-013: SystemTimeBlock injection
# ═══════════════════════════════════════════════════════════════════


class TestSystemTimeBlock:
    def test_system_time_block_present(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        time_blocks = [b for b in result.context_blocks if b.kind == ContextBlockKind.SYSTEM_TIME]
        assert len(time_blocks) == 1
        assert time_blocks[0].block_id == "system_time_block"

    def test_system_time_block_after_relationship_state_before_memory(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        time_idx = next(i for i, b in enumerate(result.context_blocks) if b.kind == ContextBlockKind.SYSTEM_TIME)
        turn_idx = next(i for i, b in enumerate(result.context_blocks) if b.kind == ContextBlockKind.TURN_LOCAL)
        assert time_idx < turn_idx  # comes before turn_local (and any memory blocks)

    def test_system_time_contains_correct_time(self, builder):
        bcr = make_bcr(system_time=SystemTime(dt=NOW, timezone="Asia/Shanghai", weekday_cn="周三"))
        result = builder.build(bcr)
        provider_texts = [
            m.content for m in result.provider_messages
            if isinstance(m.content, str)
        ]
        time_text = next((t for t in provider_texts if "当前时间" in t and "2026" in t), "")
        assert "2026" in time_text
        assert "周三" in time_text

    def test_degraded_mode_when_system_time_none(self, builder):
        bcr = make_bcr(system_time=None)
        result = builder.build(bcr)
        degraded = any(
            isinstance(m.content, str) and "系统时间不可用" in m.content
            for m in result.provider_messages
        )
        assert degraded
        warns = [w for w in result.warnings if w.source == "system_time_unavailable"]
        assert len(warns) == 1


# ═══════════════════════════════════════════════════════════════════
# I-4: Turn-local vs history separation
# ═══════════════════════════════════════════════════════════════════


class TestTurnLocalHistorySeparation:
    def test_delivered_msg_in_turn_local_not_in_recent_history(self, builder):
        sent_msg = TurnLocalSentMessage(
            message_id="tl_1", tool_call_id="tc_1",
            text="辛苦了宝宝", status=DeliveryStatus.DELIVERED,
        )
        tls = CurrentTurnLocalState(
            turn_id="turn_1", model_call_count=1,
            sent_messages_this_turn=[sent_msg], delivered_message_count=1,
        )
        bcr = make_bcr(current_turn_local_state=tls)
        result = builder.build(bcr)
        turn_local_texts = [
            m.content for m in result.provider_messages
            if isinstance(m.content, str) and "辛苦了宝宝" in m.content
        ]
        assert len(turn_local_texts) == 1
        # No assistant message in provider_messages with "辛苦了宝宝" outside turn_local
        assistant_msgs = [
            m for m in result.provider_messages
            if m.role == "assistant" and "辛苦了宝宝" in str(m.content)
        ]
        assert len(assistant_msgs) == 0

    def test_multiple_messages_accumulate_in_turn_local(self, builder):
        msgs = [
            TurnLocalSentMessage(message_id="tl_1", tool_call_id="tc_1", text="你好", status=DeliveryStatus.DELIVERED),
            TurnLocalSentMessage(message_id="tl_2", tool_call_id="tc_2", text="在吗", status=DeliveryStatus.DELIVERED),
        ]
        tls = CurrentTurnLocalState(turn_id="turn_1", model_call_count=1, sent_messages_this_turn=msgs, delivered_message_count=2)
        bcr = make_bcr(current_turn_local_state=tls)
        result = builder.build(bcr)
        texts = [m.content for m in result.provider_messages if isinstance(m.content, str)]
        combined = " ".join(texts)
        assert "你好" in combined
        assert "在吗" in combined


# ═══════════════════════════════════════════════════════════════════
# I-5: Failed delivery not in history
# ═══════════════════════════════════════════════════════════════════


class TestFailedDeliveryExclusion:
    def test_failed_message_not_in_assistant_history(self, builder):
        history = [
            ConversationHistoryMessage(
                message_id="h1", turn_id="t_old", role="user",
                content="hello", source="user_message",
            ),
            ConversationHistoryMessage(
                message_id="h2", turn_id="t_old", role="assistant",
                content="hi back", source="assistant_reply",
                delivery_status=DeliveryStatus.DELIVERED,
            ),
            ConversationHistoryMessage(
                message_id="h3", turn_id="t_old2", role="assistant",
                content="this failed", source="assistant_reply",
                delivery_status=DeliveryStatus.FAILED,
            ),
        ]
        bcr = make_bcr(recent_history=history)
        result = builder.build(bcr)
        assistant_contents = [
            m.content for m in result.provider_messages
            if m.role == "assistant"
        ]
        assert "hi back" in assistant_contents
        assert "this failed" not in assistant_contents

    def test_unknown_delivery_not_in_assistant_history(self, builder):
        history = [
            ConversationHistoryMessage(
                message_id="h1", turn_id="t_old", role="assistant",
                content="unknown status", source="assistant_reply",
                delivery_status=DeliveryStatus.UNKNOWN,
            ),
        ]
        bcr = make_bcr(recent_history=history)
        result = builder.build(bcr)
        assistant_contents = [
            m.content for m in result.provider_messages
            if m.role == "assistant"
        ]
        assert "unknown status" not in assistant_contents

    def test_failed_in_turn_local_marked_appropriately(self, builder):
        sent_msg = TurnLocalSentMessage(
            message_id="tl_1", tool_call_id="tc_1",
            text="失败了", status=DeliveryStatus.FAILED,
        )
        tls = CurrentTurnLocalState(
            turn_id="turn_1", model_call_count=1,
            sent_messages_this_turn=[sent_msg], failed_send_attempt_count=1,
        )
        bcr = make_bcr(current_turn_local_state=tls)
        result = builder.build(bcr)
        texts = [m.content for m in result.provider_messages if isinstance(m.content, str)]
        combined = " ".join(texts)
        assert "failed" in combined.lower() or "失败" in combined


# ═══════════════════════════════════════════════════════════════════
# I-6: Tool result not as user message
# ═══════════════════════════════════════════════════════════════════


class TestToolResultNotAsUser:
    def test_tool_results_in_turn_local_not_as_user_role(self, builder):
        tool_result = ToolExecutionResult(
            tool_call_id="tc_1", turn_id="turn_1",
            tool_name="get_weather", status="success",
            result_payload={"city": "上海", "temperature": 25},
        )
        tls = CurrentTurnLocalState(
            turn_id="turn_1", model_call_count=1,
            tool_results_this_turn=[tool_result],
        )
        bcr = make_bcr(current_turn_local_state=tls)
        result = builder.build(bcr)
        # No user-role message contains the tool result
        user_texts = [
            m.content for m in result.provider_messages
            if m.role == "user" and isinstance(m.content, str)
        ]
        for text in user_texts:
            assert "25" not in text or "上海" not in text.split("temperature")[0]


# ═══════════════════════════════════════════════════════════════════
# I-7: Media summary not as user text
# ═══════════════════════════════════════════════════════════════════


class TestMediaSummaryNotAsUserText:
    def test_media_context_is_system_role(self, builder):
        bcr = make_bcr(
            media_context=[
                MediaContextItem(item_id="img_1", kind="image", summary="一只白猫坐在窗台上"),
            ],
        )
        result = builder.build(bcr)
        media_msgs = [
            m for m in result.provider_messages
            if isinstance(m.content, str) and "白猫" in m.content
        ]
        assert len(media_msgs) == 1
        assert media_msgs[0].role == "system"

    def test_media_summary_not_in_user_input(self, builder):
        item = InboundMessageItem(item_id="txt_1", kind=MessageKind.TEXT, text="看看我的猫")
        batch = InboundMessageBatch(batch_id="b1", user_id="u1", bot_id="bot1", items=[item])
        bcr = make_bcr(
            inbound_batch=batch,
            media_context=[
                MediaContextItem(item_id="img_1", kind="image", summary="一只白猫"),
            ],
        )
        result = builder.build(bcr)
        user_texts = [
            m.content for m in result.provider_messages
            if m.role == "user" and isinstance(m.content, str)
        ]
        assert any("看看我的猫" in t for t in user_texts)
        for t in user_texts:
            assert "白猫" not in t


# ═══════════════════════════════════════════════════════════════════
# Snapshot tests
# ═══════════════════════════════════════════════════════════════════


class TestSnapshot:
    def test_snapshot_contains_exposed_tool_names(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        snap = result.context_snapshot
        assert snap is not None
        assert "send_text_message" in snap.exposed_tool_names
        assert "finish_turn" in snap.exposed_tool_names

    def test_snapshot_contains_history_message_ids(self, builder):
        history = [
            ConversationHistoryMessage(
                message_id="m1", turn_id="t1", role="user",
                content="hi", source="user_message",
            ),
            ConversationHistoryMessage(
                message_id="m2", turn_id="t1", role="assistant",
                content="hello", source="assistant_reply",
                delivery_status=DeliveryStatus.DELIVERED,
            ),
        ]
        bcr = make_bcr(recent_history=history)
        result = builder.build(bcr)
        assert "m1" in result.context_snapshot.history_message_ids
        assert "m2" in result.context_snapshot.history_message_ids

    def test_snapshot_has_snapshot_id(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        assert result.context_snapshot.snapshot_id.startswith("snap_")

    def test_snapshot_has_block_summaries(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        assert len(result.context_snapshot.block_summaries) > 0

    def test_snapshot_no_api_key_leak(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        snap = result.context_snapshot
        snap_str = str(snap.block_summaries) + str(snap.exposed_tool_names) + str(snap.history_message_ids)
        assert "sk-" not in snap_str
        assert "Bearer" not in snap_str


# ═══════════════════════════════════════════════════════════════════
# BuildContextError tests
# ═══════════════════════════════════════════════════════════════════


class TestBuildContextError:
    def test_missing_turn_id_raises_error(self, builder):
        with pytest.raises(BuildContextError) as exc:
            builder.build(BuildContextRequest(
                turn_id="", call_id="call_1", turn_type=TurnType.USER_CHAT,
                user_id="u1", bot_id="bot1",
            ))
        assert "turn_id" in str(exc.value.reason)

    def test_missing_user_id_raises_error(self, builder):
        with pytest.raises(BuildContextError):
            builder.build(BuildContextRequest(
                turn_id="turn_1", call_id="call_1", turn_type=TurnType.USER_CHAT,
                user_id="", bot_id="bot1",
            ))

    def test_error_contains_missing_fields(self, builder):
        with pytest.raises(BuildContextError) as exc:
            builder.build(BuildContextRequest(
                turn_id="", call_id="", turn_type=TurnType.USER_CHAT,
                user_id="u1", bot_id="bot1",
            ))
        assert "turn_id" in exc.value.missing_fields


# ═══════════════════════════════════════════════════════════════════
# Empty / cold start
# ═══════════════════════════════════════════════════════════════════


class TestColdStart:
    def test_empty_history_produces_warning(self, builder):
        bcr = make_bcr(recent_history=[])
        result = builder.build(bcr)
        empty_warn = [w for w in result.warnings if w.source == "empty_history"]
        assert len(empty_warn) == 1
        assert empty_warn[0].severity == "info"

    def test_cold_start_no_memory_produces_valid_context(self, builder):
        bcr = make_bcr(
            system_time=SystemTime(dt=NOW, timezone="Asia/Shanghai", weekday_cn="周三"),
        )
        result = builder.build(bcr)
        time_text = next(
            (m.content for m in result.provider_messages
             if isinstance(m.content, str) and "首次对话" in m.content),
            "",
        )
        assert "首次对话" in time_text


# ═══════════════════════════════════════════════════════════════════
# Token budget
# ═══════════════════════════════════════════════════════════════════


class TestTokenBudget:
    def test_token_budget_report_included(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        assert result.token_budget_report is not None
        assert result.token_budget_report.budget == 8000
        assert result.token_budget_report.used > 0

    def test_token_under_budget_not_exhausted(self, builder):
        bcr = make_bcr()
        result = builder.build(bcr)
        assert result.token_budget_report.budget_exhausted is False


# ═══════════════════════════════════════════════════════════════════
# End-to-end: full user_chat with history and memories
# ═══════════════════════════════════════════════════════════════════


class TestEndToEnd:
    def test_full_user_chat_roundtrip(self, builder):
        history = [
            ConversationHistoryMessage(
                message_id="m1", turn_id="t0", role="user",
                content="今天天气不错", source="user_message",
            ),
            ConversationHistoryMessage(
                message_id="m2", turn_id="t0", role="assistant",
                content="对呀，阳光很好～", source="assistant_reply",
                delivery_status=DeliveryStatus.DELIVERED,
            ),
        ]
        item = InboundMessageItem(item_id="i1", kind=MessageKind.TEXT, text="你好")
        batch = InboundMessageBatch(batch_id="b1", user_id="u1", bot_id="bot1", items=[item])
        bcr = make_bcr(
            turn_id="turn_1", call_id="call_1",
            inbound_batch=batch, recent_history=history,
        )
        result = builder.build(bcr)
        assert result.turn_id == "turn_1"
        assert result.call_id == "call_1"
        assert len(result.provider_messages) > 0
        assert result.context_snapshot is not None
        assert not result.warnings or all(w.severity != "error" for w in result.warnings)

    def test_multi_call_turn_local_accumulates(self, builder):
        tls1 = CurrentTurnLocalState(turn_id="turn_1", model_call_count=0)
        bcr1 = make_bcr(call_id="call_1", current_turn_local_state=tls1)
        result1 = builder.build(bcr1)

        sent = TurnLocalSentMessage(
            message_id="tl_1", tool_call_id="tc_1",
            text="我挺好的～", status=DeliveryStatus.DELIVERED,
        )
        tls2 = CurrentTurnLocalState(
            turn_id="turn_1", model_call_count=1,
            sent_messages_this_turn=[sent], delivered_message_count=1,
        )
        bcr2 = make_bcr(call_id="call_2", current_turn_local_state=tls2)
        result2 = builder.build(bcr2)

        # call_2 sees the sent message in turn_local
        texts2 = [m.content for m in result2.provider_messages if isinstance(m.content, str)]
        combined2 = " ".join(texts2)
        assert "我挺好的～" in combined2

        # call_1 does NOT have it
        texts1 = [m.content for m in result1.provider_messages if isinstance(m.content, str)]
        combined1 = " ".join(texts1)
        assert "我挺好的～" not in combined1

    def test_two_builds_produce_different_snapshots(self, builder):
        bcr1 = make_bcr(call_id="call_1")
        bcr2 = make_bcr(call_id="call_2")
        snap1 = builder.build(bcr1).context_snapshot
        snap2 = builder.build(bcr2).context_snapshot
        assert snap1.snapshot_id != snap2.snapshot_id
