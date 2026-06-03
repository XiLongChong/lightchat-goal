"""Phase 6: ProviderGateway structured output validation tests.

Validates that illegal model output is intercepted before tool execution.

Spec: docs/runtime-specs/03-turn-engine-spec.zh.md INV-02 to INV-05, INV-12, INV-13
Test matrix: TN-004 to TN-008
"""

from __future__ import annotations

import pytest

from agent.structured_output_validator import (
    StructuredOutputValidator,
    ValidationResultKind,
)


def make_tc(name: str, args: str = "{}", tc_id: str = "call_1") -> dict:
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


# ═══════════════════════════════════════════════════════════════════
# TN-004: No tool calls → no side effects
# ═══════════════════════════════════════════════════════════════════


class TestNoToolCallsRejected:
    def test_empty_tool_calls_and_content_is_no_tool_calls(self):
        v = StructuredOutputValidator()
        result = v.validate([], "")
        assert result.kind == ValidationResultKind.NO_TOOL_CALLS
        assert not result.tool_calls

    def test_empty_tool_calls_with_content_is_assistant_content_only(self):
        v = StructuredOutputValidator()
        result = v.validate([], "我觉得应该回复你好")
        assert result.kind == ValidationResultKind.ASSISTANT_CONTENT_ONLY
        assert result.assistant_content == "我觉得应该回复你好"

    def test_assistant_content_only_is_retryable(self):
        v = StructuredOutputValidator()
        result = v.validate([], "some text")
        assert result.retryable is True

    def test_no_tool_calls_does_not_execute_side_effect(self):
        """Validation result is not VALID → no tool execution."""
        v = StructuredOutputValidator()
        result = v.validate([], "")
        assert result.kind != ValidationResultKind.VALID


# ═══════════════════════════════════════════════════════════════════
# TN-004 (cont.): Pure assistant.content not sent to user
# ═══════════════════════════════════════════════════════════════════


class TestAssistantContentNotSent:
    def test_text_only_output_never_valid(self):
        v = StructuredOutputValidator()
        result = v.validate([], "你好，今天天气不错")
        assert result.kind != ValidationResultKind.VALID

    def test_old_json_actions_pattern_blocked(self):
        v = StructuredOutputValidator()
        result = v.validate([], '{"actions": [{"wechat_reply": "hello"}]}')
        assert result.kind == ValidationResultKind.ASSISTANT_CONTENT_ONLY

    def test_empty_string_assistant_content_no_tool_calls(self):
        v = StructuredOutputValidator()
        result = v.validate([], "")
        assert result.kind == ValidationResultKind.NO_TOOL_CALLS


# ═══════════════════════════════════════════════════════════════════
# TN-008: Invalid args → don't execute tools
# ═══════════════════════════════════════════════════════════════════


class TestInvalidArgsRejected:
    def test_invalid_json_args_rejected(self):
        v = StructuredOutputValidator()
        tc = make_tc("send_text_message", '{text: "bad json}')
        result = v.validate([tc])
        assert result.kind == ValidationResultKind.INVALID_ARGS

    def test_empty_args_accepted(self):
        v = StructuredOutputValidator()
        tc = make_tc("finish_turn", "{}")
        result = v.validate([tc])
        assert result.kind == ValidationResultKind.VALID

    def test_valid_json_args_accepted(self):
        v = StructuredOutputValidator()
        tc = make_tc("send_text_message", '{"text": "hello"}')
        result = v.validate([tc])
        assert result.kind == ValidationResultKind.VALID

    def test_nonexistent_tool_with_valid_args_rejected_if_exposed_set(self):
        v = StructuredOutputValidator(
            exposed_tool_names=["send_text_message", "finish_turn"],
        )
        tc = make_tc("generate_image_message", '{"prompt": "cat"}')
        result = v.validate([tc])
        assert result.kind == ValidationResultKind.TOOL_NOT_EXPOSED

    def test_tool_not_exposed_without_exposed_set_allowed(self):
        """When exposed set is empty, don't check exposure (legacy compat)."""
        v = StructuredOutputValidator()
        tc = make_tc("any_tool", "{}")
        result = v.validate([tc])
        assert result.kind == ValidationResultKind.VALID


# ═══════════════════════════════════════════════════════════════════
# Retry limit
# ═══════════════════════════════════════════════════════════════════


class TestRetryLimit:
    def test_default_max_retries_is_one(self):
        v = StructuredOutputValidator()
        assert v.max_retries == 1

    def test_first_retry_allowed(self):
        v = StructuredOutputValidator(max_retries=2)
        assert v.record_retry() is True
        assert v.retry_count == 1
        assert not v.retries_exhausted

    def test_second_retry_allowed_with_max_2(self):
        v = StructuredOutputValidator(max_retries=2)
        assert v.record_retry() is True
        assert v.record_retry() is True
        assert v.retry_count == 2
        assert not v.retries_exhausted

    def test_third_retry_exhausted_with_max_2(self):
        v = StructuredOutputValidator(max_retries=2)
        v.record_retry()
        v.record_retry()
        assert v.record_retry() is False
        assert v.retries_exhausted

    def test_retries_exhausted_after_max(self):
        v = StructuredOutputValidator(max_retries=1)
        assert v.record_retry() is True
        assert v.record_retry() is False
        assert v.retries_exhausted


# ═══════════════════════════════════════════════════════════════════
# INV-12 / INV-13: Terminal tool combination rules
# ═══════════════════════════════════════════════════════════════════


class TestTerminalToolCombinations:
    def test_finish_turn_alone_is_valid(self):
        v = StructuredOutputValidator()
        result = v.validate([make_tc("finish_turn", "{}")])
        assert result.kind == ValidationResultKind.VALID

    def test_send_text_message_alone_is_valid(self):
        v = StructuredOutputValidator()
        result = v.validate([make_tc("send_text_message", '{"text": "hi"}')])
        assert result.kind == ValidationResultKind.VALID

    def test_finish_turn_with_send_text_is_valid(self):
        v = StructuredOutputValidator()
        result = v.validate([
            make_tc("send_text_message", '{"text": "bye"}', "call_1"),
            make_tc("finish_turn", "{}", "call_2"),
        ])
        assert result.kind == ValidationResultKind.VALID

    def test_finish_turn_with_get_weather_rejected(self):
        v = StructuredOutputValidator()
        result = v.validate([
            make_tc("finish_turn", "{}", "call_1"),
            make_tc("get_weather", '{"city": "上海"}', "call_2"),
        ])
        assert result.kind == ValidationResultKind.TERMINAL_COMBINATION_INVALID

    def test_skip_initiative_with_finish_turn_rejected(self):
        """INV-12: skip + finish in same call → rejected."""
        v = StructuredOutputValidator()
        result = v.validate([
            make_tc("skip_initiative", '{"reason": "too_soon"}', "call_1"),
            make_tc("finish_turn", "{}", "call_2"),
        ])
        assert result.kind == ValidationResultKind.TERMINAL_COMBINATION_INVALID

    def test_skip_initiative_alone_is_valid(self):
        v = StructuredOutputValidator()
        result = v.validate([
            make_tc("skip_initiative", '{"reason": "too_soon", "next_check_minutes": 60}'),
        ])
        assert result.kind == ValidationResultKind.VALID

    def test_skip_initiative_with_any_other_tool_rejected(self):
        v = StructuredOutputValidator()
        result = v.validate([
            make_tc("skip_initiative", '{"reason": "too_soon"}', "call_1"),
            make_tc("send_text_message", '{"text": "hi"}', "call_2"),
        ])
        assert result.kind == ValidationResultKind.TERMINAL_COMBINATION_INVALID

    def test_done_with_get_time_rejected(self):
        """Old name done + fact tool → same terminal rule."""
        v = StructuredOutputValidator()
        result = v.validate([
            make_tc("done", '{"status": "replied"}', "call_1"),
            make_tc("get_time", "{}", "call_2"),
        ])
        assert result.kind == ValidationResultKind.TERMINAL_COMBINATION_INVALID

    def test_wechat_reply_alone_is_valid(self):
        v = StructuredOutputValidator()
        result = v.validate([make_tc("wechat_reply", '{"text": "你好"}')])
        assert result.kind == ValidationResultKind.VALID


# ═══════════════════════════════════════════════════════════════════
# Parallel fact tools (allowed per INV-12a)
# ═══════════════════════════════════════════════════════════════════


class TestParallelFactTools:
    def test_two_fact_tools_parallel_is_valid(self):
        v = StructuredOutputValidator()
        result = v.validate([
            make_tc("get_time", "{}", "call_1"),
            make_tc("get_weather", '{"city": "上海"}', "call_2"),
        ])
        assert result.kind == ValidationResultKind.VALID

    def test_fact_tool_with_send_text_is_valid(self):
        v = StructuredOutputValidator()
        result = v.validate([
            make_tc("get_weather", '{"city": "北京"}', "call_1"),
            make_tc("send_text_message", '{"text": "今天北京很冷"}', "call_2"),
        ])
        assert result.kind == ValidationResultKind.VALID

    def test_multiple_identical_fact_tools_rejected_as_multiple(self):
        """Multiple get_time calls are not terminal/send → must pass terminal checks.
        But they're both fact tools → should be valid."""
        v = StructuredOutputValidator()
        result = v.validate([
            make_tc("get_time", '{"timezone": "Asia/Shanghai"}', "call_1"),
            make_tc("get_time", '{"timezone": "America/New_York"}', "call_2"),
        ])
        assert result.kind == ValidationResultKind.VALID
