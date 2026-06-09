"""Unit tests for ContextBuilder — covers system prompt assembly, history conversion,
channel wrapping, and edge cases (empty history, missing persona, etc.)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from agent.runtime_context_builder import ContextBuilder
from runtime_contracts import (
    BuildContextRequest,
    BuiltContext,
    ContextBuildWarning,
    ConversationHistoryMessage,
    DeliveryStatus,
    NativeToolSchema,
    SystemTime,
    ToolExposure,
    TurnPhase,
    TurnType,
)


# ── helpers ──────────────────────────────────────────────────────────

def _make_request(**overrides) -> BuildContextRequest:
    defaults = {
        "turn_id": "turn_1",
        "call_id": "call_1",
        "turn_type": TurnType.USER_CHAT,
        "user_id": "user_1",
        "bot_id": "default",
        "persona": "test persona",
        "fixed_system_protocol": "test protocol",
        "system_time": SystemTime(
            dt=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
            timezone="UTC",
            weekday_cn="周二",
        ),
    }
    defaults.update(overrides)
    return BuildContextRequest(**defaults)


def _make_history_message(role, content, delivery_status="delivered"):
    return ConversationHistoryMessage(
        message_id=f"msg_{role}",
        turn_id="turn_0",
        role=role,
        content=content,
        source="protocol",
        delivery_status=delivery_status,
    )


# ── System prompt assembly ────────────────────────────────────────────

class TestSystemPromptAssembly:
    def test_includes_fixed_protocol_persona_and_time(self):
        built = ContextBuilder().build(_make_request())

        texts = [part["text"] for part in built.provider_system]
        assert texts[0] == "test protocol"
        assert texts[1] == "test persona"
        assert texts[2] == "Current time: 2026-06-09 12:00:00 周二 (UTC)."

    def test_persona_fallback_when_request_persona_empty(self):
        builder = ContextBuilder(persona_text="fallback persona")
        built = builder.build(_make_request(persona=""))

        texts = [part["text"] for part in built.provider_system]
        assert "fallback persona" in texts

    def test_persona_fallback_not_used_when_request_persona_set(self):
        builder = ContextBuilder(persona_text="fallback persona")
        built = builder.build(_make_request(persona="explicit persona"))

        texts = [part["text"] for part in built.provider_system]
        assert "explicit persona" in texts
        assert "fallback persona" not in texts

    def test_skips_empty_blocks(self):
        built = ContextBuilder().build(_make_request(
            persona="",
            fixed_system_protocol="",
        ))

        # Only time block should remain (persona empty, protocol empty)
        texts = [part["text"] for part in built.provider_system]
        assert len(texts) == 1
        assert "Current time:" in texts[0]

    def test_time_block_absent_when_system_time_none(self):
        built = ContextBuilder().build(_make_request(system_time=None))

        texts = [part["text"] for part in built.provider_system]
        assert not any("Current time:" in t for t in texts)


# ── History conversion ─────────────────────────────────────────────────

class TestHistoryConversion:
    def test_preserves_user_and_assistant_roles(self):
        built = ContextBuilder().build(_make_request(
            recent_history=[
                _make_history_message("user", "hello"),
                _make_history_message("assistant", "hi there"),
            ]
        ))

        assert len(built.provider_messages) == 2
        assert built.provider_messages[0].role == "user"
        assert built.provider_messages[0].content == "hello"
        assert built.provider_messages[1].role == "assistant"
        assert built.provider_messages[1].content == "hi there"

    def test_filters_system_role(self):
        built = ContextBuilder().build(_make_request(
            recent_history=[
                _make_history_message("system", "internal"),
                _make_history_message("user", "hello"),
            ]
        ))

        assert len(built.provider_messages) == 1
        assert built.provider_messages[0].role == "user"

    def test_filters_empty_content(self):
        built = ContextBuilder().build(_make_request(
            recent_history=[
                _make_history_message("user", "hello"),
                _make_history_message("user", ""),
                _make_history_message("user", None),
            ]
        ))

        assert len(built.provider_messages) == 1

    def test_filters_undelivered_assistant_messages(self):
        built = ContextBuilder().build(_make_request(
            recent_history=[
                _make_history_message("user", "hello"),
                _make_history_message("assistant", "reply", delivery_status="failed"),
                _make_history_message("assistant", "reply2", delivery_status="sent_confirmed"),
            ]
        ))

        assert len(built.provider_messages) == 2
        # failed delivery → filtered out
        # sent_confirmed → kept (matches "delivered" set)
        assert built.provider_messages[1].content == "reply2"

    def test_empty_history_adds_warning(self):
        built = ContextBuilder().build(_make_request(recent_history=[]))

        assert len(built.warnings) == 1
        assert built.warnings[0].source == "recent_history"
        assert built.warnings[0].severity == "info"

    def test_preserves_structured_content(self):
        structured = [
            {"type": "thinking", "thinking": "consider"},
            {"type": "tool_use", "id": "t1", "name": "get_time", "input": {}},
        ]
        built = ContextBuilder().build(_make_request(
            recent_history=[
                _make_history_message("assistant", structured),
            ]
        ))

        assert built.provider_messages[0].content == structured


# ── Tool exposure passthrough ──────────────────────────────────────────

class TestToolExposure:
    def test_passes_tool_schemas(self):
        exposure = ToolExposure(
            exposure_id="exp_1",
            turn_id="turn_1",
            phase=TurnPhase.NORMAL_CHAT,
            exposed_tool_names=["get_time"],
            native_tool_schemas=[
                NativeToolSchema(
                    name="get_time",
                    description="Get current time",
                    parameters_schema={"type": "object", "properties": {}},
                ),
            ],
            tool_contract_block="Use tools wisely.",
        )
        built = ContextBuilder().build(_make_request(tool_exposure=exposure))

        assert len(built.exposed_tool_schemas) == 1
        assert built.exposed_tool_schemas[0].name == "get_time"

    def test_tool_contract_in_system(self):
        exposure = ToolExposure(
            exposure_id="exp_1",
            turn_id="turn_1",
            phase=TurnPhase.NORMAL_CHAT,
            tool_contract_block="Use tools wisely.",
            native_tool_schemas=[],
        )
        built = ContextBuilder().build(_make_request(tool_exposure=exposure))

        texts = [part["text"] for part in built.provider_system]
        assert "Use tools wisely." in texts

    def test_no_tool_exposure_no_contract_block(self):
        built = ContextBuilder().build(_make_request(tool_exposure=None))

        texts = [part["text"] for part in built.provider_system]
        assert not any("Use tools" in t for t in texts)


# ── Validation ──────────────────────────────────────────────────────────

class TestValidation:
    def test_missing_turn_id(self):
        from runtime_contracts import BuildContextError

        with pytest.raises(BuildContextError, match="turn_id"):
            ContextBuilder().build(_make_request(turn_id=""))

    def test_missing_call_id(self):
        from runtime_contracts import BuildContextError

        with pytest.raises(BuildContextError, match="call_id"):
            ContextBuilder().build(_make_request(call_id=""))


# ── Regression anchors (same input → same output) ──────────────────────

class TestRegressionAnchors:
    """These tests define the contract that must hold before and after refactoring."""

    def test_full_build_output_structure(self):
        """Verify the output shape of a complete build call."""
        request = BuildContextRequest(
            turn_id="turn_1",
            call_id="call_1",
            turn_type=TurnType.USER_CHAT,
            user_id="user_x",
            bot_id="bot_y",
            persona="You are helpful.",
            fixed_system_protocol="PROTOCOL v1",
            system_time=SystemTime(
                dt=datetime(2026, 1, 15, 8, 30, tzinfo=timezone.utc),
                timezone="Asia/Shanghai",
                weekday_cn="周四",
            ),
            recent_history=[
                ConversationHistoryMessage(
                    message_id="m1", turn_id="t0", role="user",
                    content="hi", source="protocol",
                    delivery_status="delivered",
                ),
                ConversationHistoryMessage(
                    message_id="m2", turn_id="t0", role="assistant",
                    content="hello!", source="protocol",
                    delivery_status="delivered",
                ),
            ],
        )

        built = ContextBuilder().build(request)

        # System parts
        system_texts = [p["text"] for p in built.provider_system]
        assert system_texts == [
            "PROTOCOL v1",
            "You are helpful.",
            "Current time: 2026-01-15 08:30:00 周四 (Asia/Shanghai).",
        ]

        # Messages (history only, current user message NOT added by builder)
        assert len(built.provider_messages) == 2
        assert built.provider_messages[0].role == "user"
        assert built.provider_messages[0].content == "hi"
        assert built.provider_messages[1].role == "assistant"
        assert built.provider_messages[1].content == "hello!"

    def test_system_prompt_order_is_stable(self):
        """System prompt blocks must appear in a fixed, documented order."""
        exposure = ToolExposure(
            exposure_id="e1", turn_id="t1", phase=TurnPhase.NORMAL_CHAT,
            tool_contract_block="TOOL_CONTRACT",
            native_tool_schemas=[],
        )
        built = ContextBuilder().build(_make_request(
            fixed_output_protocol="OUTPUT_PROTOCOL",
            fixed_tool_protocol="TOOL_PROTOCOL",
            tool_exposure=exposure,
        ))

        texts = [p["text"] for p in built.provider_system]
        # Order: protocol → output → tool → persona → time → tool_contract
        assert texts == [
            "test protocol",       # fixed_system_protocol
            "OUTPUT_PROTOCOL",     # fixed_output_protocol
            "TOOL_PROTOCOL",       # fixed_tool_protocol
            "test persona",        # persona
            "Current time: 2026-06-09 12:00:00 周二 (UTC).",  # time_block
            "TOOL_CONTRACT",       # tool_contract_block
        ]
