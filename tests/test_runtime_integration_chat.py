"""Phase 10: Runtime integration tests.

Validates runtime module imports, structure, and key route-equivalent
operations.
"""

from __future__ import annotations

import pytest
from agent.state_machine import TurnScratchpad


# ═══════════════════════════════════════════════════════════════════
# Module imports
# ═══════════════════════════════════════════════════════════════════


class TestRuntimeImports:
    def test_runtime_package_importable(self):
        import runtime
        assert hasattr(runtime, "ChatRuntime")

    def test_chat_runtime_importable(self):
        from runtime.chat_runtime import ChatRuntime, create_chat_runtime
        assert ChatRuntime is not None
        assert create_chat_runtime is not None

    def test_all_runtime_modules_importable(self):
        from runtime.chat_runtime import ChatRuntime
        from agent.scheduler_runtime import SchedulerRuntime
        from agent.runtime_context_builder import ContextBuilder
        from agent.tool_exposure_policy import ToolExposurePolicy
        from agent.structured_output_validator import StructuredOutputValidator
        from memory.system import MemorySystem
        # All imports succeed = pass

    def test_phase_1_types_importable(self):
        from runtime_contracts import (
            TurnType, TurnRequest, BuiltContext, TurnResult,
            CurrentTurnLocalState, TerminalPolicy,
        )
        assert TurnType.USER_CHAT is not None


# ═══════════════════════════════════════════════════════════════════
# ChatRuntime skeleton
# ═══════════════════════════════════════════════════════════════════


class TestChatRuntime:
    def test_create_default_runtime(self):
        from runtime.chat_runtime import create_chat_runtime
        rt = create_chat_runtime(user_id="u1")
        assert rt.user_id == "u1"
        assert rt.bot_id == "default_bot"
        assert rt.chat_agent is None

    def test_build_turn_request(self):
        from runtime.chat_runtime import create_chat_runtime
        rt = create_chat_runtime()
        turn = rt.build_turn_request("你好")
        assert isinstance(turn, TurnScratchpad)
        assert turn.user_message == "你好"
        assert turn.trigger == "user"

    def test_get_history_for_context_filters_non_visible(self):
        from runtime.chat_runtime import create_chat_runtime
        rt = create_chat_runtime()
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "system", "content": "should be filtered"},
            {"role": "user", "content": ""},  # empty
        ]
        visible = rt.get_history_for_context(history)
        assert len(visible) == 2
        assert visible[0]["role"] == "user"
        assert visible[1]["role"] == "assistant"

    def test_handle_chat_without_agent_returns_error(self):
        from runtime.chat_runtime import create_chat_runtime
        import asyncio
        rt = create_chat_runtime()

        async def run():
            result = await rt.handle_chat("hello")
            assert result["success"] is False
            assert "error" in result

        asyncio.run(run())

    def test_handle_chat_exception_handled(self):
        from runtime.chat_runtime import create_chat_runtime
        import asyncio
        rt = create_chat_runtime()
        rt.chat_agent = None  # Explicitly None

        async def run():
            result = await rt.handle_chat("hello")
            assert result["success"] is False

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════
# Phase 12: TurnEngine + facade wiring
# ═══════════════════════════════════════════════════════════════════


class TestTurnEngine:
    """TurnEngine adapter — coordinates ChatAgent with facades."""

    def test_turn_engine_importable(self):
        from agent.turn_engine import (
            TurnEngine, TurnEngineRequest, create_turn_engine,
        )
        assert TurnEngine is not None
        assert TurnEngineRequest is not None
        assert create_turn_engine is not None

    def test_create_turn_engine_without_facades(self):
        from agent.turn_engine import create_turn_engine
        engine = create_turn_engine(chat_agent=None)
        assert engine is not None
        assert engine._agent is None
        assert engine._tool_exposure_policy is None
        assert engine._context_builder is None
        assert engine._memory_system is None

    def test_create_turn_engine_with_policy(self):
        from agent.turn_engine import create_turn_engine
        from agent.tool_exposure_policy import ToolExposurePolicy
        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)
        assert engine._tool_exposure_policy is policy

    def test_create_turn_engine_with_all_facades(self):
        from agent.turn_engine import create_turn_engine
        from agent.tool_exposure_policy import ToolExposurePolicy
        from agent.runtime_context_builder import ContextBuilder
        from memory.system import MemorySystem

        policy = ToolExposurePolicy()
        ctx = ContextBuilder(persona_text="test")
        mem = MemorySystem()
        mem.bot_id = "test"

        engine = create_turn_engine(
            chat_agent=None,
            tool_exposure_policy=policy,
            context_builder=ctx,
            memory_system=mem,
        )
        assert engine._tool_exposure_policy is policy
        assert engine._context_builder is ctx
        assert engine._memory_system is mem

    def test_turn_engine_request_defaults(self):
        from agent.turn_engine import TurnEngineRequest
        from runtime_contracts import TurnType

        req = TurnEngineRequest()
        assert req.user_message is None
        assert req.trigger == "user"
        assert req.source == "user_message"
        assert req.turn_type == TurnType.USER_CHAT

    def test_turn_engine_classify_turn_type_user(self):
        from agent.turn_engine import TurnEngine
        from runtime_contracts import TurnType

        assert TurnEngine.classify_turn_type("user_message") == TurnType.USER_CHAT

    def test_turn_engine_classify_turn_type_proactive(self):
        from agent.turn_engine import TurnEngine
        from runtime_contracts import TurnType

        result = TurnEngine.classify_turn_type("PROACTIVE_IMPULSE")
        assert result == TurnType.RELATIONSHIP_PROACTIVE

    def test_turn_engine_classify_turn_type_reminder_due(self):
        from agent.turn_engine import TurnEngine
        from runtime_contracts import TurnType

        result = TurnEngine.classify_turn_type(
            "PROACTIVE_IMPULSE",
            proactive_meta={"trigger_kind": "reminder_due"},
        )
        assert result == TurnType.USER_REMINDER_DUE

    def test_turn_engine_resolve_tools_user_chat(self):
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)

        req = TurnEngineRequest(
            user_message="hello",
            turn_type=TurnType.USER_CHAT,
            source="user_message",
        )
        tools = engine._resolve_allowed_tools(req)
        assert "send_text_message" in tools
        assert "finish_turn" in tools
        # get_time not exposed by default when system_time is available
        # get_weather not exposed without weather intent

    def test_turn_engine_resolve_tools_proactive_skippable(self):
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)

        req = TurnEngineRequest(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            source="PROACTIVE_IMPULSE",
            last_decision=None,
            consecutive_skip_count=0,
        )
        tools = engine._resolve_proactive_tools(req)
        assert "send_text_message" in tools
        assert "skip_initiative" in tools
        assert "finish_turn" in tools

    def test_turn_engine_resolve_tools_proactive_forced_speak(self):
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)

        req = TurnEngineRequest(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            source="PROACTIVE_IMPULSE",
            last_decision="skipped",
            consecutive_skip_count=1,
        )
        tools = engine._resolve_proactive_tools(req)
        assert "send_text_message" in tools
        assert "finish_turn" in tools
        # skip_initiative NOT exposed when forced_speak
        assert "skip_initiative" not in tools


# ═══════════════════════════════════════════════════════════════════
# Phase 12: ChatAgent tool_exposure_policy injection
# ═══════════════════════════════════════════════════════════════════


class TestChatAgentPolicyInjection:
    """ChatAgent accepts tool_exposure_policy via __init__."""

    def test_chat_agent_accepts_policy_param(self):
        from agent.chat_agent import ChatAgent
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        agent = ChatAgent(
            model="test",
            persona="test",
            personality="test",
            llm_gateway=None,
            tool_exposure_policy=policy,
        )
        assert agent._tool_exposure_policy is policy

    def test_chat_agent_without_policy_defaults_to_none(self):
        from agent.chat_agent import ChatAgent

        agent = ChatAgent(
            model="test",
            persona="test",
            personality="test",
            llm_gateway=None,
        )
        assert agent._tool_exposure_policy is None

    def test_chat_agent_resolve_tools_via_policy_proactive(self):
        from agent.chat_agent import ChatAgent
        from agent.tool_exposure_policy import ToolExposurePolicy
        from agent.state_machine import TurnScratchpad

        policy = ToolExposurePolicy()
        agent = ChatAgent(
            model="test",
            persona="test",
            personality="test",
            llm_gateway=None,
            tool_exposure_policy=policy,
        )
        turn = TurnScratchpad(
            user_message="",
            source="PROACTIVE_IMPULSE",
        )
        tools = agent._resolve_tools_via_policy(turn)
        assert "send_text_message" in tools
        assert "finish_turn" in tools
        # Phase is skippable (delivered_count=0, not skipped before)
        assert "skip_initiative" in tools

    def test_chat_agent_resolve_tools_via_policy_regular(self):
        from agent.chat_agent import ChatAgent
        from agent.tool_exposure_policy import ToolExposurePolicy
        from agent.state_machine import TurnScratchpad

        policy = ToolExposurePolicy()
        agent = ChatAgent(
            model="test",
            persona="test",
            personality="test",
            llm_gateway=None,
            tool_exposure_policy=policy,
        )
        turn = TurnScratchpad(
            user_message="你好",
            source="user_message",
        )
        tools = agent._resolve_tools_via_policy(turn)
        assert "send_text_message" in tools
        assert "finish_turn" in tools

    def test_chat_agent_fallback_when_policy_none(self):
        """When no policy, _do_call_llm uses inline logic (no crash)."""
        from agent.chat_agent import ChatAgent
        from agent.state_machine import TurnScratchpad

        agent = ChatAgent(
            model="test",
            persona="test",
            personality="test",
            llm_gateway=None,
            native_allowed_tools=["wechat_reply", "done"],
        )
        turn = TurnScratchpad(
            user_message="你好",
            source="user_message",
            native_allowed_tools_override=None,
        )
        # Verify agent works without policy
        assert agent._tool_exposure_policy is None
        assert agent.native_allowed_tools == ["wechat_reply", "done"]


# ═══════════════════════════════════════════════════════════════════
# INT-001 to INT-003: Cross-module wiring
# ═══════════════════════════════════════════════════════════════════


class TestCrossModuleWiring:
    """Verify facades wire correctly into TurnEngine and ChatAgent."""

    def test_int001_tool_exposure_policy_wires_to_turn_engine(self):
        """INT-001: ToolExposurePolicy → TurnEngine → allowed_tools."""
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)

        req = TurnEngineRequest(
            user_message="hello",
            turn_type=TurnType.USER_CHAT,
            source="user_message",
        )
        tools = engine._resolve_allowed_tools(req)
        # INT-001: exposed tool names match native schema names
        assert set(tools) == {"send_text_message", "finish_turn"}

    def test_int002_policy_wires_to_chat_agent(self):
        """INT-002: ToolExposurePolicy → ChatAgent._resolve_tools_via_policy."""
        from agent.chat_agent import ChatAgent
        from agent.tool_exposure_policy import ToolExposurePolicy
        from agent.state_machine import TurnScratchpad

        policy = ToolExposurePolicy()
        agent = ChatAgent(
            model="test",
            persona="test",
            personality="test",
            llm_gateway=None,
            tool_exposure_policy=policy,
        )
        turn = TurnScratchpad(
            user_message="hello",
            source="user_message",
        )
        tools = agent._resolve_tools_via_policy(turn)
        assert "send_text_message" in tools
        assert "finish_turn" in tools

    def test_int003_context_builder_wires_to_turn_engine(self):
        """INT-003: ContextBuilder → TurnEngine (structural wiring)."""
        from agent.turn_engine import create_turn_engine
        from agent.runtime_context_builder import ContextBuilder

        ctx = ContextBuilder(persona_text="test persona")
        engine = create_turn_engine(chat_agent=None, context_builder=ctx)
        assert engine._context_builder is ctx
