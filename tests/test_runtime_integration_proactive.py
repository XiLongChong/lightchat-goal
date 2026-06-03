"""Phase 12: Runtime integration tests for proactive turn flow.

Validates TurnEngine + ToolExposurePolicy wiring for relationship_proactive
turns, including phase transitions (skippable → forced_speak → after_first_message).

Spec: docs/runtime-specs/03-turn-engine-spec.zh.md § 5.3
Test matrix: docs/runtime-specs/10-test-matrix.zh.md INT-004 to INT-010
"""

from __future__ import annotations

import pytest


# ═══════════════════════════════════════════════════════════════════
# INT-004: Proactive skippable phase tool exposure
# ═══════════════════════════════════════════════════════════════════


class TestProactiveSkippablePhase:
    """Tool exposure for relationship_proactive when last decision was 'spoke' or first trigger."""

    def test_proactive_skippable_exposes_skip_initiative(self):
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)

        req = TurnEngineRequest(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            source="PROACTIVE_IMPULSE",
            last_decision="spoke",
            consecutive_skip_count=0,
        )
        tools = engine._resolve_proactive_tools(req)
        assert "skip_initiative" in tools
        assert "send_text_message" in tools
        assert "finish_turn" in tools

    def test_proactive_first_trigger_exposes_skip(self):
        """First proactive trigger (no prior decision) — skippable."""
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
        assert "skip_initiative" in tools

    def test_skippable_does_not_expose_image_tools(self):
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)

        req = TurnEngineRequest(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            source="PROACTIVE_IMPULSE",
        )
        tools = engine._resolve_proactive_tools(req)
        assert "generate_image_message" not in tools
        assert "send_image_message" not in tools
        assert "send_sticker" not in tools


# ═══════════════════════════════════════════════════════════════════
# INT-005: Proactive forced_speak phase (skip 后下一轮)
# ═══════════════════════════════════════════════════════════════════


class TestProactiveForcedSpeakPhase:
    """REG-007: After a skip, next proactive turn must NOT expose skip_initiative."""

    def test_forced_speak_hides_skip_initiative(self):
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
        assert "skip_initiative" not in tools, "REG-007: skip_initiative must NOT be exposed after a skip"
        assert "send_text_message" in tools
        assert "finish_turn" in tools

    def test_forced_speak_with_consecutive_skip_2(self):
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)

        req = TurnEngineRequest(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            source="PROACTIVE_IMPULSE",
            last_decision="skipped",
            consecutive_skip_count=2,
        )
        tools = engine._resolve_proactive_tools(req)
        assert "skip_initiative" not in tools


# ═══════════════════════════════════════════════════════════════════
# INT-006: Proactive after_first_message phase
# ═══════════════════════════════════════════════════════════════════


class TestProactiveAfterFirstMessage:
    """After first delivery, skip_initiative is hidden, finish_turn is exposed."""

    def test_after_first_message_no_skip(self):
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)

        req = TurnEngineRequest(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            source="PROACTIVE_IMPULSE",
            last_decision="spoke",
            consecutive_skip_count=0,
        )
        # Simulate after_first_message by using policy directly with delivered_count > 0
        from agent.tool_exposure_policy import ToolExposureRequest
        from runtime_contracts import TurnType as RTTurnType, UserIntent

        policy_req = ToolExposureRequest(
            turn_type=RTTurnType.RELATIONSHIP_PROACTIVE,
            trigger_source="scheduler",
            user_intent=UserIntent.CHAT,
            delivered_message_count=1,
        )
        exposure = policy.resolve(policy_req)
        assert "skip_initiative" not in exposure.exposed_tool_names
        assert "send_text_message" in exposure.exposed_tool_names
        assert "finish_turn" in exposure.exposed_tool_names


# ═══════════════════════════════════════════════════════════════════
# INT-007: Tool exposure phase consistency
# ═══════════════════════════════════════════════════════════════════


class TestToolExposurePhaseConsistency:
    """prompt-visible tools must equal native schema tools (REG-005)."""

    def test_prompt_equals_schema_skippable(self):
        from agent.tool_exposure_policy import ToolExposurePolicy, ToolExposureRequest
        from runtime_contracts import TurnType, UserIntent

        policy = ToolExposurePolicy()
        req = ToolExposureRequest(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            trigger_source="scheduler",
            user_intent=UserIntent.CHAT,
        )
        exposure = policy.resolve(req)

        schema_names = {s.name for s in exposure.native_tool_schemas}
        exposed_set = set(exposure.exposed_tool_names)
        assert schema_names == exposed_set, (
            f"REG-005: prompt/schema divergence: "
            f"exposed={exposed_set}, schema={schema_names}"
        )

    def test_prompt_equals_schema_forced_speak(self):
        from agent.tool_exposure_policy import ToolExposurePolicy, ToolExposureRequest
        from runtime_contracts import TurnType, UserIntent

        policy = ToolExposurePolicy()
        req = ToolExposureRequest(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            trigger_source="scheduler",
            user_intent=UserIntent.CHAT,
            last_decision="skipped",
            consecutive_skip_count=1,
        )
        exposure = policy.resolve(req)

        schema_names = {s.name for s in exposure.native_tool_schemas}
        exposed_set = set(exposure.exposed_tool_names)
        assert schema_names == exposed_set, "REG-005 violation in forced_speak"

    def test_contract_block_mentions_all_exposed(self):
        from agent.tool_exposure_policy import ToolExposurePolicy, ToolExposureRequest
        from runtime_contracts import TurnType, UserIntent

        policy = ToolExposurePolicy()
        req = ToolExposureRequest(
            turn_type=TurnType.USER_CHAT,
            trigger_source="user",
            user_intent=UserIntent.CHAT,
        )
        exposure = policy.resolve(req)
        for name in exposure.exposed_tool_names:
            assert name in exposure.tool_contract_block, (
                f"REG-005: {name} missing from contract block"
            )


# ═══════════════════════════════════════════════════════════════════
# INT-008: Unregistered/denied tools
# ═══════════════════════════════════════════════════════════════════


class TestDeniedTools:
    """REG-006: unexposed tools are properly denied."""

    def test_image_tools_denied_in_proactive(self):
        from agent.tool_exposure_policy import ToolExposurePolicy, ToolExposureRequest
        from runtime_contracts import TurnType, UserIntent

        policy = ToolExposurePolicy()
        req = ToolExposureRequest(
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            trigger_source="scheduler",
            user_intent=UserIntent.CHAT,
        )
        exposure = policy.resolve(req)
        denied_names = {d.tool_name for d in exposure.denied_tools}
        assert "generate_image_message" in denied_names
        assert "send_image_message" in denied_names

    def test_skip_initiative_denied_in_user_chat(self):
        from agent.tool_exposure_policy import ToolExposurePolicy, ToolExposureRequest
        from runtime_contracts import TurnType, UserIntent

        policy = ToolExposurePolicy()
        req = ToolExposureRequest(
            turn_type=TurnType.USER_CHAT,
            trigger_source="user",
            user_intent=UserIntent.CHAT,
        )
        exposure = policy.resolve(req)
        denied_names = {d.tool_name for d in exposure.denied_tools}
        assert "skip_initiative" in denied_names

    def test_schedule_reminder_denied_in_normal_chat(self):
        from agent.tool_exposure_policy import ToolExposurePolicy, ToolExposureRequest
        from runtime_contracts import TurnType, UserIntent

        policy = ToolExposurePolicy()
        req = ToolExposureRequest(
            turn_type=TurnType.USER_CHAT,
            trigger_source="user",
            user_intent=UserIntent.CHAT,
        )
        exposure = policy.resolve(req)
        denied_names = {d.tool_name for d in exposure.denied_tools}
        assert "schedule_user_reminder" in denied_names


# ═══════════════════════════════════════════════════════════════════
# INT-009: TurnEngine proactive run_turn (structural)
# ═══════════════════════════════════════════════════════════════════


class TestTurnEngineProactiveRun:
    """TurnEngine.run_turn() with proactive requests (structural wiring)."""

    def test_run_turn_proactive_without_agent(self):
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType

        engine = create_turn_engine(chat_agent=None)
        req = TurnEngineRequest(
            source="PROACTIVE_IMPULSE",
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
        )
        # Without a chat_agent, run_turn delegates to None.chat() → AttributeError
        # This tests structural wiring — the policy path is exercised
        import asyncio

        async def run():
            with pytest.raises(AttributeError):
                await engine.run_turn(req)

        asyncio.run(run())

    def test_run_turn_proactive_resolves_tools_before_delegate(self):
        from agent.turn_engine import TurnEngineRequest, create_turn_engine, TurnType
        from agent.tool_exposure_policy import ToolExposurePolicy

        policy = ToolExposurePolicy()
        engine = create_turn_engine(chat_agent=None, tool_exposure_policy=policy)
        req = TurnEngineRequest(
            source="PROACTIVE_IMPULSE",
            turn_type=TurnType.RELATIONSHIP_PROACTIVE,
            last_decision=None,
            consecutive_skip_count=0,
        )
        # Verify tools are resolved (will crash on actual agent.run, but resolution works)
        tools = engine._resolve_proactive_tools(req)
        assert "send_text_message" in tools
        assert "finish_turn" in tools
        assert "skip_initiative" in tools


# ═══════════════════════════════════════════════════════════════════
# INT-010: ChatAgent policy-driven tool exposure in proactive flow
# ═══════════════════════════════════════════════════════════════════


class TestChatAgentProactivePolicy:
    """ChatAgent._resolve_tools_via_policy for proactive turns."""

    def test_proactive_skippable_via_chat_agent(self):
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
            source="PROACTIVE_IMPULSE",
        )
        tools = agent._resolve_tools_via_policy(turn)
        assert "send_text_message" in tools
        assert "finish_turn" in tools
        assert "skip_initiative" in tools

    def test_proactive_after_delivery_via_chat_agent(self):
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
            source="PROACTIVE_IMPULSE",
        )
        turn.proactive_delivered_count = 1
        tools = agent._resolve_tools_via_policy(turn)
        assert "send_text_message" in tools
        assert "finish_turn" in tools
        # skip_initiative NOT exposed after delivery
        assert "skip_initiative" not in tools

    def test_chat_agent_policy_fallback_on_error(self):
        """When policy raises, fallback to inline logic without crashing."""
        from agent.chat_agent import ChatAgent
        from agent.state_machine import TurnScratchpad

        # Policy that will raise on resolve
        class BrokenPolicy:
            def resolve(self, request):
                raise RuntimeError("simulated policy failure")

        agent = ChatAgent(
            model="test",
            persona="test",
            personality="test",
            llm_gateway=None,
            native_allowed_tools=["wechat_reply", "done"],
            tool_exposure_policy=BrokenPolicy(),
        )
        turn = TurnScratchpad(
            user_message="hello",
            source="user_message",
        )
        tools = agent._resolve_tools_via_policy(turn)
        # Falls back to instance native_allowed_tools
        assert tools == ["wechat_reply", "done"]
