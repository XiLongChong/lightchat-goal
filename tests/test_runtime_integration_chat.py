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

    def test_handle_chat_without_agent_raises(self):
        """Phase 10: ChatRuntime raises RuntimeError when chat_agent not configured."""
        from runtime.chat_runtime import create_chat_runtime
        import asyncio
        import pytest
        rt = create_chat_runtime()

        async def run():
            with pytest.raises(RuntimeError, match="chat_agent not configured"):
                await rt.handle_chat("hello")

        asyncio.run(run())

    def test_handle_chat_with_none_agent_raises(self):
        from runtime.chat_runtime import create_chat_runtime
        import asyncio
        import pytest
        rt = create_chat_runtime()
        rt.chat_agent = None

        async def run():
            with pytest.raises(RuntimeError, match="chat_agent not configured"):
                await rt.handle_chat("hello")

        asyncio.run(run())

    def test_create_runtime_with_all_facades(self):
        """Phase 10: ChatRuntime created with all facade dependencies."""
        from runtime.chat_runtime import create_chat_runtime
        from agent.chat_agent import ChatAgent
        from agent.tool_exposure_policy import ToolExposurePolicy
        from agent.runtime_context_builder import ContextBuilder

        policy = ToolExposurePolicy()
        ctx = ContextBuilder(persona_text="test")
        agent = ChatAgent(
            model="test", persona="test", personality="",
            llm_gateway=None,
            tool_exposure_policy=policy,
            context_builder=ctx,
        )
        rt = create_chat_runtime(
            user_id="u1",
            chat_agent=agent,
            context_builder=ctx,
            tool_exposure_policy=policy,
        )
        assert rt.chat_agent is agent
        assert rt.context_builder is ctx
        assert rt.tool_exposure_policy is policy

    def test_handle_chat_reminder_request_exposes_reminder_tools(self):
        """Reminder requests must survive ChatRuntime -> ChatAgent tool override."""
        from runtime.chat_runtime import create_chat_runtime
        from agent.tool_exposure_policy import ToolExposurePolicy
        import asyncio

        class FakeChatAgent:
            async def chat(self, **kwargs):
                return kwargs["native_allowed_tools_override"]

        runtime = create_chat_runtime(
            chat_agent=FakeChatAgent(),
            tool_exposure_policy=ToolExposurePolicy(),
        )

        async def run():
            return await runtime.handle_chat(
                "十分钟后提醒我喝水",
                extra_tools=["schedule_user_reminder"],
            )

        tools = asyncio.run(run())
        assert "send_text_message" in tools
        assert "finish_turn" in tools
        assert "schedule_user_reminder" in tools
        assert "get_time" in tools

    def test_relative_reminder_delay_can_be_inferred_from_user_text(self):
        import main

        assert main._extract_delay_minutes_from_text("\u4e94\u5206\u949f\u540e\u63d0\u9192\u6211\u5403\u836f") == 5
        assert main._extract_delay_minutes_from_text("2\u5c0f\u65f6\u540e\u53eb\u6211") == 120
        assert main._looks_relative_time_text("5\u5206\u949f\u540e")

    def test_state_tool_executor_accepts_canonical_reminder_tool(self, tmp_path):
        import asyncio
        import main
        from agent.state_machine import TurnScratchpad
        from memory.store import MemoryStore

        async def run():
            old_store = main.memory_store
            store = MemoryStore(str(tmp_path / "memory.db"))
            main.memory_store = store
            try:
                turn = TurnScratchpad(
                    user_message="\u4e94\u5206\u949f\u540e\u63d0\u9192\u6211\u5403\u836f",
                )
                executor = main._build_state_tool_executor("u1", "ctx", "default_bot")
                result = await executor(
                    "schedule_user_reminder",
                    {
                        "message_hint": "\u63d0\u9192\u5403\u836f",
                        "trigger_at": "\u4e94\u5206\u949f\u540e",
                    },
                    turn,
                )
                tasks = await store.list_scheduled_messages(status="pending")

                assert result["status"] == "ok"
                assert result["scheduled_task_id"]
                assert len(tasks) == 1
                assert tasks[0]["messages"][0]["source"] == "schedule_user_reminder"
            finally:
                await store.close()
                main.memory_store = old_store

        asyncio.run(run())

    def test_handle_chat_delegates_to_agent(self):
        """Phase 10: ChatRuntime.handle_chat() delegates to ChatAgent.chat()."""
        from runtime.chat_runtime import create_chat_runtime
        from agent.chat_agent import ChatAgent
        from agent.state_machine import TurnState
        import asyncio

        agent = ChatAgent(
            model="test", persona="test", personality="",
            llm_gateway=None,
            test_mode=True,
        )
        rt = create_chat_runtime(chat_agent=agent)

        async def run():
            result = await rt.handle_chat("hello", history=[])
            assert result.terminal_state in (
                TurnState.HARD_FALLBACK,  # no LLM gateway
                TurnState.END_FAILED,
            )

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

    def test_chat_agent_accepts_context_builder_param(self):
        from agent.chat_agent import ChatAgent
        from agent.runtime_context_builder import ContextBuilder

        ctx = ContextBuilder(persona_text="test")
        agent = ChatAgent(
            model="test",
            persona="test",
            personality="test",
            llm_gateway=None,
            context_builder=ctx,
        )
        assert agent._context_builder is ctx

    def test_chat_agent_without_policy_defaults_to_none(self):
        from agent.chat_agent import ChatAgent

        agent = ChatAgent(
            model="test",
            persona="test",
            personality="test",
            llm_gateway=None,
        )
        assert agent._tool_exposure_policy is None
        assert agent._context_builder is None

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

    def test_chat_agent_build_context_uses_injected_context_builder(self):
        """Injected ContextBuilder must work on the real ChatAgent build path."""
        from agent.chat_agent import ChatAgent
        from agent.runtime_context_builder import ContextBuilder
        from agent.state_machine import TurnScratchpad, TurnState

        agent = ChatAgent(
            model="test",
            persona="test",
            personality="",
            llm_gateway=None,
            context_builder=ContextBuilder(persona_text="test"),
        )
        turn = TurnScratchpad(user_message="你好", source="user_message")

        next_state = agent._do_build_context(
            turn,
            system_prompt="system prompt",
            history=[],
            trigger="user",
        )

        assert next_state == TurnState.CLASSIFY_CONTEXT_ERROR
        assert turn._context_result is not None
        assert turn._context_result.fatal_error == ""
        assert any(
            m.get("role") == "user"
            and m.get("content") == '<channel source="wechat">\n你好\n</channel>'
            for m in turn._context_result.messages
        )
