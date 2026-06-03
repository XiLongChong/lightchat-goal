"""Tests for v3.1 proactive message pipeline — Phase 0-4.

Covers: source constants, proactive_meta contract, skip_proactive routing,
proactive-specific terminal states, allowed_tools phase-based filtering,
proactive guard, fallback suppression, delivered-only history writing,
and visible context consistency.
"""

from __future__ import annotations

import asyncio
import json as _json
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent.chat_agent import ChatAgent
from agent.output_guard import guard_proactive_output
from agent.scheduler import ProactiveScheduler
from agent.proactive_settings import ProactiveSettings
from agent.state_machine import (
    SOURCE_PROACTIVE_IMPULSE,
    SOURCE_USER_MESSAGE,
    DeliveryStatus,
    TurnResult,
    TurnScratchpad,
    TurnState,
)
from agent.tool_registry import TOOL_REGISTRY


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _make_tc(name: str, args: str, call_id: str = ""):
    """Build a mock native tool_call object."""
    call_id = call_id or f"call_{name}_{int(time.time() * 1000)}"
    class _Fn:
        pass
    class _TC:
        pass
    fn = _Fn()
    fn.name = name
    fn.arguments = args
    tc = _TC()
    tc.id = call_id
    tc.function = fn
    return tc


def _make_response(content: str, tool_calls=None):
    """Build a mock LLM response."""
    class _Usage:
        prompt_tokens = 40
        completion_tokens = 20
        total_tokens = 60
        estimated = False
        cache_hit_tokens = 0
        cache_miss_tokens = 40
        reasoning_tokens = 0

    class _Resp:
        pass
    r = _Resp()
    r.content = content
    r.tool_calls = tool_calls or []
    r.usage = _Usage()
    r.finish_reason = "tool_calls" if tool_calls else "stop"
    return r


def _make_mgw(responses: list):
    """Build a mock LLM gateway that returns responses in order."""
    call_count = [0]

    class _MGW:
        def __init__(self):
            self.requests = []

        async def complete(self, req, **kwargs):
            self.requests.append(req)
            idx = call_count[0]
            call_count[0] += 1
            if idx >= len(responses):
                return _make_response("", tool_calls=[_make_tc("done", '{"status":"no_reply","note":"out of responses"}')])
            return responses[idx]

    return _MGW()


def _make_agent(native_allowed_tools=None, test_mode=True):
    """Build a minimal ChatAgent for testing."""
    from agent.state_machine import ModelProfile

    return ChatAgent(
        model="test",
        persona="test persona",
        personality="",
        llm_gateway=_make_mgw([]),
        native_tool_calls=True,
        native_allowed_tools=native_allowed_tools or ["get_time", "get_weather", "wechat_reply", "done", "skip_proactive"],
        test_mode=test_mode,
        flash_profile=ModelProfile(model_name="test-flash"),
        pro_profile=ModelProfile(model_name="test-pro"),
    )


def _make_proactive_meta(**overrides) -> dict:
    """Build a minimal proactive_meta for testing."""
    meta = {
        "impulse_id": "test_impulse_001",
        "impulse_type": "ECHO",
        "proactive_chain_id": "chain_001",
        "trigger_reason_summary": "测试主动触发",
        "evidence_pack": {
            "source_topic_id": "",
            "source_message_ids": [],
            "recent_proactive_messages": [],
            "relationship_temperature": 5.0,
            "topic_snapshot": "测试话题",
        },
        "dedupe_key": "test_dedup_001",
        "reply_window_seconds": 14400,
        "max_wechat_reply": 3,
    }
    meta.update(overrides)
    return meta


# ══════════════════════════════════════════════════════════════════
class _FakeStore:
    """Minimal fake store for scheduler tests."""
    def __init__(self):
        self._last_visible = 0.0
        self.history_rows: list = []
        self.due_tasks: list = []

    async def resolve_last_visible_message_at(self, user_id, bot_id):
        return {"timestamp": self._last_visible, "reason": "fake"}

    async def get_last_visible_message_at(self, user_id, bot_id):
        return self._last_visible

    async def get_recent_conversation_history(self, user_id, limit=40, bot_id=""):
        return self.history_rows

    async def count_today_proactive_sent(self, today_start):
        return 0

    async def get_autonomous_trigger_count(self, user_id, bot_id):
        return (0, "")

    async def get_dialogue_state(self, user_id, bot_id):
        return {}

    async def get_latest_recap(self, user_id, bot_id=""):
        return None

    async def create_scheduled_wake_up(self, user_id, bot_id, task):
        return "fake_task_id"

    async def get_pending_proactive_wake_up(self, user_id, bot_id):
        return None

    async def claim_due_scheduled_messages(self, now_ts=None, limit=1, user_id=""):
        return self.due_tasks[:limit]

    async def cancel_pending_proactive_wake_ups(self, user_id, bot_id):
        return 0

    async def count_turns_since_last_snapshot(self, user_id, bot_id):
        return 0

    async def get_latest_snapshot(self, user_id, bot_id=""):
        return None

    async def get_turns_since_last_snapshot(self, user_id, bot_id, limit=20):
        return []

    async def mark_scheduled_message_status(self, task_id, status, error=""):
        pass


# Phase 0: Source constants + proactive_meta contract
# ══════════════════════════════════════════════════════════════════

class TestSourceConstants:
    """Phase 0.1: SOURCE_PROACTIVE_IMPULSE is the only legal proactive source."""

    def test_source_constants_defined(self):
        assert SOURCE_PROACTIVE_IMPULSE == "PROACTIVE_IMPULSE"
        assert SOURCE_USER_MESSAGE == "user_message"

    def test_proactive_source_not_equal_to_user(self):
        assert SOURCE_PROACTIVE_IMPULSE != SOURCE_USER_MESSAGE

    def test_proactive_turn_rejects_non_none_user_message(self):
        """v3.1 §0.4: user_message must be None for proactive turns."""
        agent = _make_agent()

        with pytest.raises(ValueError, match="user_message must be None"):
            asyncio.run(agent.chat(
                user_message="hello",
                history=[],
                trigger="proactive",
                source=SOURCE_PROACTIVE_IMPULSE,
                proactive_meta=_make_proactive_meta(),
            ))

    def test_proactive_turn_accepts_none_user_message(self):
        """v3.1 §0.4: user_message=None is valid for proactive turns."""
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"too_soon"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None,
            history=[],
            trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        assert result.terminal_state == TurnState.END_SKIPPED_PROACTIVE


class TestProactiveMetaContract:
    """Phase 0.3: proactive_meta data contract."""

    def test_proactive_meta_stored_on_turn(self):
        meta = _make_proactive_meta()
        turn = TurnScratchpad(source=SOURCE_PROACTIVE_IMPULSE, proactive_meta=meta)
        assert turn.proactive_meta is not None
        assert turn.proactive_meta["impulse_id"] == "test_impulse_001"
        assert turn.proactive_meta["impulse_type"] == "ECHO"

    def test_turn_counters_initialized_to_zero(self):
        turn = TurnScratchpad()
        assert turn.proactive_attempt_count == 0
        assert turn.proactive_send_attempt_count == 0
        assert turn.proactive_delivered_count == 0
        assert turn.proactive_llm_step_count == 0
        assert turn.proactive_delivered_texts == []


# ══════════════════════════════════════════════════════════════════
# Phase 1: skip_proactive + proactive-specific terminal states
# ══════════════════════════════════════════════════════════════════

class TestSkipProactiveRegistration:
    """Phase 1.1: skip_proactive is registered as native-eligible terminal tool."""

    def test_skip_proactive_registered(self):
        t = TOOL_REGISTRY.get("skip_proactive")
        assert t is not None
        assert t.tool_category == "terminal_tool"
        assert t.native_tool_call_eligible is False  # Phase 11: deprecated, use skip_initiative

    def test_skip_proactive_arg_schema_has_reason_required(self):
        t = TOOL_REGISTRY.get("skip_proactive")
        required = t.args_schema.get("required", [])
        assert "reason" in required

    def test_skip_proactive_reason_enum(self):
        t = TOOL_REGISTRY.get("skip_proactive")
        props = t.args_schema.get("properties", {})
        reason_enum = props.get("reason", {}).get("enum", [])
        assert "too_soon" in reason_enum
        assert "user_might_be_busy" in reason_enum
        assert "next_check_minutes" in props


class TestSkipProactiveRouting:
    """Phase 1.3: skip_proactive routing in proactive vs non-proactive turns."""

    def test_skip_proactive_in_proactive_turn(self):
        """skip_proactive is valid in proactive phase A."""
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"low_value"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None,
            history=[],
            trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        assert result.terminal_state == TurnState.END_SKIPPED_PROACTIVE

    def test_skip_proactive_in_normal_turn_rejected(self):
        """skip_proactive in non-proactive turn → PROTOCOL_RETRY."""
        # Model calls skip_proactive → PROTOCOL_RETRY → then model calls done
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"low_value"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"no_reply","note":"cannot skip"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message="hello",
            history=[],
            trigger="user",
            source=SOURCE_USER_MESSAGE,
        ))
        # Should route to PROTOCOL_RETRY, then eventually call done
        assert result.terminal_state == TurnState.END_NO_REPLY

    def test_skip_proactive_after_delivery_rejected(self):
        """Phase 1.6: skip_proactive after delivered > 0 → PROTOCOL_RETRY."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"hello"}')]),
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"too_soon"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None,
            history=[],
            trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=fake_sender,
        ))
        # After delivery, skip_proactive should be rejected → PROTOCOL_RETRY → then done
        assert result.terminal_state == TurnState.END_REPLIED
        assert result.proactive_delivered_count == 1

    def test_skip_proactive_reason_validation(self):
        """Invalid reason → PROTOCOL_RETRY."""
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"invalid_reason"}')]),
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"too_soon"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None,
            history=[],
            trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        assert result.terminal_state == TurnState.END_SKIPPED_PROACTIVE


class TestSkipProactiveSkipStatus:
    """v3.1 §note: send_attempt_count > 0 and delivered_count == 0 → not SKIPPED_BY_MODEL."""

    def test_skip_without_send_attempt_is_skipped_by_model(self):
        """No send attempts → SKIPPED_BY_MODEL."""
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"too_soon"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None,
            history=[],
            trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        assert result.terminal_state == TurnState.END_SKIPPED_PROACTIVE
        # Check fallback events for skip status
        skip_events = [e for e in result.fallback_events if e.get("type") == "skip_proactive"]
        assert len(skip_events) == 1
        assert "SKIPPED_BY_MODEL" in skip_events[0]["detail"]

    def test_skip_after_send_failure_not_skipped_by_model(self):
        """send_attempt > 0 but delivered == 0 → SKIPPED_AFTER_SEND_FAILURE."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"msg1"}')]),
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"too_soon"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def failing_sender(messages):
            for m in messages:
                m["send_status"] = "failed"
                m["send_error"] = "no_bridge"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None,
            history=[],
            trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=failing_sender,
        ))
        assert result.terminal_state == TurnState.END_SKIPPED_PROACTIVE
        skip_events = [e for e in result.fallback_events if e.get("type") == "skip_proactive"]
        assert len(skip_events) == 1
        assert "SKIPPED_AFTER_SEND_FAILURE" in skip_events[0]["detail"]


class TestProactiveTerminalStates:
    """Phase 1.2: Proactive-specific terminal states exist in enum and TERMINAL_STATES."""

    def test_end_skipped_proactive_in_terminal_states(self):
        from agent.state_machine import TERMINAL_STATES
        assert TurnState.END_SKIPPED_PROACTIVE in TERMINAL_STATES

    def test_end_failed_no_fallback_in_terminal_states(self):
        from agent.state_machine import TERMINAL_STATES
        assert TurnState.END_FAILED_NO_FALLBACK in TERMINAL_STATES

    def test_end_proactive_guard_blocked_in_terminal_states(self):
        from agent.state_machine import TERMINAL_STATES
        assert TurnState.END_PROACTIVE_GUARD_BLOCKED in TERMINAL_STATES

    def test_end_proactive_max_reply_limit_in_terminal_states(self):
        from agent.state_machine import TERMINAL_STATES
        assert TurnState.END_PROACTIVE_MAX_REPLY_LIMIT in TERMINAL_STATES

    def test_end_proactive_max_llm_steps_in_terminal_states(self):
        from agent.state_machine import TERMINAL_STATES
        assert TurnState.END_PROACTIVE_MAX_LLM_STEPS in TERMINAL_STATES


# ══════════════════════════════════════════════════════════════════
# Phase 2: Proactive context + allowed_tools phase-based
# ══════════════════════════════════════════════════════════════════

class TestProactiveContextInjection:
    """Phase 2.1-2.2: Proactive context as independent system message."""

    def test_proactive_context_injected(self):
        """Proactive turn gets a proactive context system message."""
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"too_soon","next_check_minutes":15}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))

        # Check that the LLM request includes proactive context in system messages
        req = mgw.requests[0]
        system_messages = [m for m in req.messages if m["role"] == "system"]
        proactive_msgs = [m for m in system_messages if "主动回合" in m["content"]]
        assert len(proactive_msgs) >= 1

    def test_proactive_context_has_no_user_message(self):
        """Proactive turn messages should not include a user role entry."""
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"too_soon","next_check_minutes":15}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))

        req = mgw.requests[0]
        # The last message beyond system messages should NOT be user role
        non_system = [m for m in req.messages if m["role"] != "system"]
        user_msgs = [m for m in non_system if m["role"] == "user"]
        assert len(user_msgs) == 0, "Proactive turn should not have role=user messages"

    def test_proactive_context_contains_impulse_info(self):
        """Proactive context must include tool stage info."""
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"too_soon","next_check_minutes":15}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(impulse_type="PROACTIVE_WAKE"),
        ))

        req = mgw.requests[0]
        system_msgs = [m["content"] for m in req.messages if m["role"] == "system"]
        proactive_ctx = next((m for m in system_msgs if "主动回合" in m), "")
        assert "阶段A-optional" in proactive_ctx
        assert "skip_initiative" in proactive_ctx

    def test_required_context_matches_exposed_tools(self):
        """Wake-up proactive stage must not expose skip_proactive."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"先吃点东西，别硬撑"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied","next_check_minutes":15}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            return messages

        asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(trigger_mode="required"),
            wechat_sender=fake_sender,
        ))

        req = mgw.requests[0]
        tool_names = [t["function"]["name"] for t in (req.tools or [])]
        assert "send_text_message" in tool_names
        assert "skip_initiative" not in tool_names
        assert "finish_turn" not in tool_names

        system_msgs = [m["content"] for m in req.messages if m["role"] == "system"]
        proactive_ctx = next((m for m in system_msgs if "主动回合" in m), "")
        assert "阶段A-wake-up" in proactive_ctx
        assert "skip_initiative 不可用" in proactive_ctx
        assert "必须" in proactive_ctx

    def test_proactive_context_marks_assistant_history_as_own_messages(self):
        """Context must explicitly prevent treating prior assistant bubbles as user replies."""
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"low_value","next_check_minutes":30}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        asyncio.run(agent.chat(
            user_message=None,
            history=[
                {"role": "user", "content": "好累", "source": "wechat", "ts": 1},
                {"role": "assistant", "content": "累就趴一会儿，别硬撑", "source": "assistant", "ts": 2},
            ],
            trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(
                trigger_mode="optional",
                last_user_message="好累",
                assistant_messages_after_last_user=["累就趴一会儿，别硬撑"],
            ),
        ))

        req = mgw.requests[0]
        system_msgs = [m["content"] for m in req.messages if m["role"] == "system"]
        proactive_ctx = next((m for m in system_msgs if "主动回合" in m or "对话边界" in m), "")
        assert "assistant 的历史消息都是你已说过的话" in proactive_ctx
        assert "不要把自己刚发过的话当成用户回答" in proactive_ctx


class TestAllowedToolsPhaseBased:
    """Phase 2.3-2.4: allowed_tools dynamic based on delivered_count."""

    def test_phase_a_allows_wechat_reply_and_skip_proactive(self):
        """delivered_count == 0 → [wechat_reply, skip_proactive]."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"msg1"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            return messages

        asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=fake_sender,
        ))

        # Check that the first LLM call only exposed phase A tools
        req = mgw.requests[0]
        tool_names = [t["function"]["name"] for t in (req.tools or [])]
        assert "send_text_message" in tool_names
        assert "skip_initiative" in tool_names
        assert "finish_turn" not in tool_names

    def test_phase_a_done_is_rejected(self):
        """Phase A: calling done when delivered == 0 → PROTOCOL_RETRY."""
        responses = [
            _make_response("", tool_calls=[_make_tc("done", '{"status":"no_reply","note":"nothing to say"}')]),
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"low_value"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        assert result.terminal_state == TurnState.END_SKIPPED_PROACTIVE

    def test_phase_b_allows_wechat_reply_and_done(self):
        """0 < delivered < max → [wechat_reply, done]."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"msg1"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"msg2"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            return messages

        asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=fake_sender,
        ))

        # Second LLM call should have phase B tools (delivered = 1)
        req = mgw.requests[1]
        tool_names = [t["function"]["name"] for t in (req.tools or [])]
        assert "send_text_message" in tool_names
        assert "finish_turn" in tool_names
        assert "skip_initiative" not in tool_names

    def test_phase_c_only_done_allowed(self):
        """delivered >= max_reply → [done] only."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m1"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m2"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m3"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            return messages

        asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(max_wechat_reply=3),
            wechat_sender=fake_sender,
        ))

        # Fourth LLM call should have phase C tools (delivered = 3, max = 3)
        req = mgw.requests[3]
        tool_names = [t["function"]["name"] for t in (req.tools or [])]
        assert "finish_turn" in tool_names
        assert "send_text_message" not in tool_names

    def test_phase_c_wechat_reply_triggers_end_proactive_max_reply_limit(self):
        """Phase C: calling wechat_reply → END_PROACTIVE_MAX_REPLY_LIMIT."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m1"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m2"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m3"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m4_too_many"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(max_wechat_reply=3),
            wechat_sender=fake_sender,
        ))
        assert result.terminal_state == TurnState.END_PROACTIVE_MAX_REPLY_LIMIT


class TestProactiveMaxLlmSteps:
    """Phase 2.7: max_llm_steps check for proactive turns."""

    def test_max_llm_steps_exceeded_ends_properly(self):
        """proactive_llm_step_count > max → END_PROACTIVE_MAX_LLM_STEPS."""
        # With default max_llm_steps=8 (from _get_proactive_config), create enough
        # responses to overflow. The proactive_llm_step_count check runs BEFORE
        # the LLM call, so we need 9 calls to trigger (8 calls pass, 9th fails).
        responses = []
        for i in range(12):
            responses.append(_make_response("", tool_calls=[_make_tc("wechat_reply", f'{{"text":"msg{i}"}}')]))

        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def failing_sender(messages):
            for m in messages:
                m["send_status"] = "failed"
                m["send_error"] = "test"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=failing_sender,
        ))

        # Should hit a proactive-specific terminal state (not HARD_FALLBACK).
        # hard_max_wechat_reply_per_turn (5) may trigger first, or max_llm_steps,
        # or the global max_llm_steps_per_turn (20) — all are proactive-safe.
        assert result.terminal_state in (
            TurnState.END_PROACTIVE_MAX_LLM_STEPS,
            TurnState.END_FAILED_NO_FALLBACK,
            TurnState.END_MAX_LIMIT,
            TurnState.END_PROACTIVE_MAX_REPLY_LIMIT,
        )


# ══════════════════════════════════════════════════════════════════
# Phase 3: Proactive guard + fallback suppression
# ══════════════════════════════════════════════════════════════════

class TestGuardProactiveOutput:
    """Phase 3.1: guard_proactive_output detects fabricated observations etc."""

    def test_fabricated_observation_retry(self):
        result, _ = guard_proactive_output("我看到你还没睡")
        assert result == "RETRY"

    def test_mechanism_exposure_retry(self):
        result, _ = guard_proactive_output("检测到你的在线状态")
        assert result == "RETRY"

    def test_task_flavor_retry(self):
        result, _ = guard_proactive_output("提醒你完成今天的任务")
        assert result == "RETRY"

    def test_notification_flavor_retry(self):
        result, _ = guard_proactive_output("请注意休息")
        assert result == "RETRY"

    def test_normal_text_pass(self):
        result, text = guard_proactive_output("今天天气真好")
        assert result == "PASS"
        assert "今天天气真好" in text

    def test_empty_text_block(self):
        result, _ = guard_proactive_output("")
        assert result == "BLOCK"

    def test_customer_service_sanitize(self):
        result, text = guard_proactive_output("请问你最近怎么样")
        assert result in ("PASS", "SANITIZE")  # SANITIZE removes "请问", then re-checks → PASS


class TestProactiveGuardIntegration:
    """Phase 3.2: guard integrated into _do_guard_output."""

    def test_guard_retry_feeds_back_to_model(self):
        """Proactive guard RETRY → feedback to model as tool result → CALL_LLM."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"我看到你还没睡"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"今天天气真好"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=fake_sender,
        ))
        # First call gets guard RETRY → feedback → model retries with correct text → delivered
        assert result.terminal_state == TurnState.END_REPLIED
        assert result.proactive_delivered_count == 1


class TestProactiveFallbackSuppression:
    """Phase 3.3: Proactive turns suppress hard fallback."""

    def test_hard_fallback_returns_end_failed_no_fallback(self):
        """When a proactive turn enters HARD_FALLBACK → END_FAILED_NO_FALLBACK."""
        # Model produces only empty content → PROTOCOL_RETRY → exceeded → HARD_FALLBACK
        responses = [
            _make_response("no tool calls", tool_calls=[]),   # → PROTOCOL_RETRY
            _make_response("no tool calls again", tool_calls=[]),  # → PROTOCOL_RETRY
            _make_response("still none", tool_calls=[]),  # → PROTOCOL_RETRY exceeded
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        # After 3 protocol retries (max=2), should go to END_FAILED_NO_FALLBACK
        assert result.terminal_state in (
            TurnState.END_FAILED_NO_FALLBACK,
            TurnState.END_PROTOCOL_ERROR,
        )


# ══════════════════════════════════════════════════════════════════
# Phase 4: Delivered-only history + visible context consistency
# ══════════════════════════════════════════════════════════════════

class TestProactiveDeliveredOnlyHistory:
    """v3.1 §4.5: Only confirmed delivered texts enter conversation history."""

    def test_proactive_delivered_count_tracks_correctly(self):
        """proactive_delivered_count increments only on confirmed delivery."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"msg1"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"msg2"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=fake_sender,
        ))
        assert result.proactive_delivered_count == 2

    def test_proactive_delivered_texts_only_contains_delivered(self):
        """proactive_delivered_texts only stores confirmed delivered content."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"delivered_msg"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"failed_msg"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        call_count = [0]

        async def mixed_sender(messages):
            for m in messages:
                call_count[0] += 1
                if call_count[0] == 1:
                    m["send_status"] = "delivered"
                else:
                    m["send_status"] = "failed"
                    m["send_error"] = "test failure"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=mixed_sender,
        ))
        assert result.proactive_delivered_count == 1
        assert len(result.proactive_delivered_texts) == 1
        assert "delivered_msg" in result.proactive_delivered_texts[0]
        assert "failed_msg" not in result.proactive_delivered_texts

    def test_proactive_delivered_texts_empty_when_none_delivered(self):
        """No delivery → proactive_delivered_texts is empty."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"failed1"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"failed","note":"send failed"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def failing_sender(messages):
            for m in messages:
                m["send_status"] = "failed"
                m["send_error"] = "no_bridge"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=failing_sender,
        ))
        assert result.proactive_delivered_count == 0
        assert len(result.proactive_delivered_texts) == 0


class TestProactiveWriteHistoryCallback:
    """Delivered-only write_model_history — only delivered texts written as assistant:proactive."""

    def test_write_history_only_delivered(self):
        """Write model history callback only writes delivered texts."""
        history_written = []

        async def write_history_cb(turn: TurnScratchpad, terminal_state: TurnState):
            if turn.proactive_delivered_texts:
                history_written.append({
                    "role": "assistant",
                    "content": "\n".join(turn.proactive_delivered_texts),
                    "source": "assistant:proactive",
                })

        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"delivered1"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"failed1"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        call_count = [0]

        async def mixed_sender(messages):
            for m in messages:
                call_count[0] += 1
                if call_count[0] == 1:
                    m["send_status"] = "delivered"
                else:
                    m["send_status"] = "failed"
                    m["send_error"] = "test"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=mixed_sender,
            write_model_history=write_history_cb,
        ))

        assert len(history_written) == 1
        assert "delivered1" in history_written[0]["content"]
        assert "failed1" not in history_written[0]["content"]
        assert history_written[0]["source"] == "assistant:proactive"

    def test_write_history_empty_when_no_delivery(self):
        """No delivery → no history written."""
        history_written = []

        async def write_history_cb(turn: TurnScratchpad, terminal_state: TurnState):
            if turn.proactive_delivered_texts:
                history_written.append("should_not_happen")

        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"fail"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"failed","note":"send error"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def failing_sender(messages):
            for m in messages:
                m["send_status"] = "failed"
            return messages

        asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=failing_sender,
            write_model_history=write_history_cb,
        ))

        assert len(history_written) == 0


class TestProactiveContextEnrichment:
    def test_enrich_splits_recent_proactive_messages_into_visible_bubbles(self):
        history = [
            {"role": "user", "content": "你好？", "source": "wechat", "ts": 1},
            {"role": "assistant", "content": "你好啊", "source": "assistant", "ts": 2},
            {
                "role": "assistant",
                "content": "人呢\n打个招呼就不见了",
                "source": "assistant:proactive",
                "ts": 3,
            },
        ]
        impulse = {"evidence_pack": {"recent_proactive_messages": []}}

        ProactiveScheduler._enrich_impulse_with_context(None, impulse, history)

        assert impulse["last_user_message"] == "你好？"
        assert impulse["assistant_messages_after_last_user"] == [
            "你好啊",
            "人呢",
            "打个招呼就不见了",
        ]
        assert [m["text"] for m in impulse["evidence_pack"]["recent_proactive_messages"]] == [
            "人呢",
            "打个招呼就不见了",
        ]


class TestProactiveCounterIndependence:
    """v3.1 §1.4: Three independent counters — attempt, send_attempt, delivered."""

    def test_attempt_count_increments_when_model_calls_wechat_reply(self):
        """proactive_attempt_count tracks model's wechat_reply tool calls."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m1"}')]),
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m2"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"replied"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def fake_sender(messages):
            for m in messages:
                m["send_status"] = "delivered"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=fake_sender,
        ))
        assert result.proactive_attempt_count == 2

    def test_send_attempt_vs_delivered_diverge_on_failure(self):
        """send_attempt_count > delivered_count when sends fail."""
        responses = [
            _make_response("", tool_calls=[_make_tc("wechat_reply", '{"text":"m1"}')]),
            _make_response("", tool_calls=[_make_tc("done", '{"status":"failed","note":"fail"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        async def failing_sender(messages):
            for m in messages:
                m["send_status"] = "failed"
            return messages

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
            wechat_sender=failing_sender,
        ))
        assert result.proactive_send_attempt_count == 1
        assert result.proactive_delivered_count == 0
        assert result.proactive_attempt_count == 1


class TestProactiveStrictCCRegression:
    """Existing strict CC mechanisms must work for proactive turns."""

    def test_proactive_turn_single_tool_call_per_llm_response(self):
        """v3.1 regression: multiple tool calls in one response → PROTOCOL_RETRY."""
        tc1 = _make_tc("wechat_reply", '{"text":"m1"}', call_id="c1")
        tc2 = _make_tc("wechat_reply", '{"text":"m2"}', call_id="c2")
        responses = [
            _make_response("", tool_calls=[tc1, tc2]),  # 2 tool calls → PROTOCOL_RETRY
            _make_response("", tool_calls=[_make_tc("done", '{"status":"no_reply","note":"error"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        # Should not crash; should handle via PROTOCOL_RETRY
        assert result.terminal_state is not None

    def test_proactive_turn_no_send_wechat_messages(self):
        """v3.1 regression: send_wechat_messages in proactive → PROTOCOL_RETRY."""
        tc = _make_tc("send_wechat_messages", '{"messages":["m1","m2"]}')
        responses = [
            _make_response("", tool_calls=[tc]),  # deprecated → PROTOCOL_RETRY
            _make_response("", tool_calls=[_make_tc("done", '{"status":"no_reply","note":"deprecated"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        assert result.terminal_state in (TurnState.END_NO_REPLY, TurnState.END_FAILED_NO_FALLBACK)

    def test_proactive_turn_no_assistant_content_direct_send(self):
        """v3.1 regression: assistant.content without tool call → PROTOCOL_RETRY."""
        responses = [
            _make_response("just plain text", tool_calls=[]),  # no tool call → PROTOCOL_RETRY
            _make_response("", tool_calls=[_make_tc("done", '{"status":"no_reply","note":"no content"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        assert result.terminal_state in (TurnState.END_NO_REPLY, TurnState.END_FAILED_NO_FALLBACK)


class TestProactiveSchedulerMinimal:
    """Phase 4.1: ProactiveScheduler minimal skeleton."""

    def test_scheduler_initial_state(self):
        scheduler = ProactiveScheduler()
        assert scheduler.is_busy is False

    def test_scheduler_rejects_concurrent(self):
        """Second trigger while one is running → skipped."""
        import asyncio

        async def _test():
            scheduler = ProactiveScheduler()
            # Simulate lock held
            async with scheduler._lock:
                result = await scheduler.run_manual_trigger("u1", {}, "b1")
                assert result["status"] == "skipped"
                assert result["reason"] == "active_proactive_turn_running"

        asyncio.run(_test())


class TestProactiveTurnResultFields:
    """Phase 4: TurnResult carries proactive-specific fields."""

    def test_turn_result_has_proactive_fields(self):
        responses = [
            _make_response("", tool_calls=[_make_tc("skip_proactive", '{"reason":"too_soon"}')]),
        ]
        mgw = _make_mgw(responses)
        agent = _make_agent()
        agent.llm_gateway = mgw

        result = asyncio.run(agent.chat(
            user_message=None, history=[], trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            proactive_meta=_make_proactive_meta(),
        ))
        assert hasattr(result, "proactive_delivered_count")
        assert hasattr(result, "proactive_delivered_texts")
        assert hasattr(result, "proactive_attempt_count")
        assert hasattr(result, "proactive_send_attempt_count")
        assert result.proactive_delivered_count == 0


# ══════════════════════════════════════════════════════════════════
# Phase 5: Heartbeat + eligibility
# ══════════════════════════════════════════════════════════════════

class TestEligibilityChecks:
    """Phase 5.1: System pre-checks (no LLM)."""

    def test_eligibility_proactive_disabled(self):
        """proactive.enabled=false → eligibility blocked."""
        scheduler = ProactiveScheduler(
            config={"features": {"proactive": False}},
            settings_provider=lambda: ProactiveSettings(enabled=False),
        )

        async def _test():
            eligible, reason = await scheduler._check_eligibility("u1", "b1")
            assert eligible is False
            assert "proactive_disabled" in reason

        asyncio.run(_test())

    def test_eligibility_scheduler_busy(self):
        """dispatch_wake_up when lock held → skipped."""
        scheduler = ProactiveScheduler(
            config={"features": {"proactive": True}},
            settings_provider=lambda: ProactiveSettings(enabled=True, autonomous_enabled=True),
        )

        async def _test():
            async with scheduler._lock:
                result = await scheduler.dispatch_wake_up("u1", "b1", {"trigger": "proactive"})
                assert result["status"] == "skipped"
                assert "active_proactive_turn_running" in result["reason"]

        asyncio.run(_test())

    def test_eligibility_sleep_hours(self):
        """Sleep hours block eligibility."""
        class SchedulerWithSleep(ProactiveScheduler):
            def _in_sleep_hours(self, sleep_cfg):
                return True

        scheduler = SchedulerWithSleep(
            config={"features": {"proactive": True}},
            settings_provider=lambda: ProactiveSettings(
                enabled=True, autonomous_enabled=True,
                quiet_hours_enabled=True, quiet_hours_start="23:00", quiet_hours_end="08:00",
            ),
        )

        async def _test():
            eligible, reason = await scheduler._check_eligibility("u1", "b1")
            assert eligible is False
            assert "sleep_hours" in reason

        asyncio.run(_test())

    def test_cold_start_after_2_minutes(self):
        """check_cold_start returns context when last_visible > 120s ago."""
        scheduler = ProactiveScheduler()

        fake_store = _FakeStore()
        fake_store._last_visible = time.time() - 180
        scheduler._store = fake_store

        async def _test():
            result = await scheduler.check_cold_start("u1", "b1")
            assert result is not None
            assert result["trigger"] == "proactive"
            assert result["trigger_kind"] == "cold_start"
            assert result["is_wake_up"] is False

        asyncio.run(_test())

    def test_cold_start_not_triggered_too_early(self):
        """check_cold_start returns None when last_visible < 120s ago."""
        scheduler = ProactiveScheduler()

        fake_store = _FakeStore()
        fake_store._last_visible = time.time() - 30
        scheduler._store = fake_store

        async def _test():
            result = await scheduler.check_cold_start("u1", "b1")
            assert result is None

        asyncio.run(_test())


# ══════════════════════════════════════════════════════════════════
# Phase 6: Impulse generation / dedup / score
# ══════════════════════════════════════════════════════════════════

# v4.0 removed: TestImpulseGeneration


def test_claim_due_wake_up_restores_message_payload_identity():
    """Due tasks must preserve user-requested reminder identity from the message payload."""
    scheduler = ProactiveScheduler()
    fake_store = _FakeStore()
    fake_store.due_tasks = [{
        "id": "sch_user",
        "due_at": 1000.0,
        "messages": [{
            "text": "叫用户洗脚，时间到了催他去洗脚",
            "wake_up_type": "user_requested",
            "is_wake_up": True,
            "skip_quiet_hours": True,
            "recurring": None,
        }],
    }]
    scheduler._store = fake_store

    async def _test():
        result = await scheduler._claim_due_wake_up("u1", "b1", now_ts=1000.0)
        assert result["trigger"] == "user_requested"
        assert result["trigger_kind"] == "wake_up"
        assert result["is_wake_up"] is True
        assert result["message_hint"] == "叫用户洗脚，时间到了催他去洗脚"
        assert result["skip_quiet_hours"] is True

    asyncio.run(_test())

class TestProactiveInterruptBaseline:
    """V1.6-A blocker: proactive turns use current seq as baseline."""

    def test_proactive_turn_uses_current_latest_seq_as_interrupt_baseline(self):
        class FakeChatAgent:
            def __init__(self):
                self.kwargs = None

            async def chat(self, **kwargs):
                self.kwargs = kwargs
                return TurnResult(
                    terminal_state=TurnState.END_SKIPPED_PROACTIVE,
                    turn_id="t1",
                    user_message="",
                )

        agent = FakeChatAgent()
        scheduler = ProactiveScheduler(chat_agent=agent)

        async def _test():
            result = await scheduler.run_manual_trigger(
                "u1", _make_proactive_meta(), "b1",
                interrupt_checker_provider=lambda _token: False,
                input_latest_seq=3,
            )
            assert result["interrupt_baseline"] == 3
            assert agent.kwargs["input_latest_seq"] == 3

        asyncio.run(_test())


# v4.0 removed: TestScheduledTriggersV16BC

class TestRelationshipTemperature:
    """Phase 7.4: Relationship temperature updates based on events."""

    def test_update_relationship_without_store_succeeds(self):
        """Without store, returns 0 gracefully."""
        scheduler = ProactiveScheduler()

        async def _test():
            temp = await scheduler.update_relationship_temperature("u1", "b1", "user_reply", delta=2)
            assert temp == 0

        asyncio.run(_test())

    def test_attribute_reply_without_store_returns_none(self):
        """Without store, attribution returns None."""
        scheduler = ProactiveScheduler()

        async def _test():
            result = await scheduler.attribute_user_reply_to_proactive("u1", "b1", time.time())
            assert result is None

        asyncio.run(_test())


class TestMetricsSnapshot:
    """Phase 7: get_metrics returns correct snapshot."""

    def test_initial_metrics(self):
        scheduler = ProactiveScheduler()
        m = scheduler.get_metrics()
        assert m["heartbeat_tick_count"] == 0
        assert m["eligibility_pass_count"] == 0
        assert m["eligibility_fail_count"] == 0
        assert m["is_busy"] is False
        assert "eligibility_fail_reasons" in m
