"""Test scheduler — V2.1.1 due-time + intent model.

These tests verify the hard rule phase and topic/intent logic
without requiring LLM calls.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock


def test_sleep_hours_blocked():
    """During sleep hours, proactive message is blocked."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None,  # type: ignore
        llm_gateway=MagicMock(),  # type: ignore
        model="test",
        config={"sleep_start": 0, "sleep_end": 7},
    )

    result = agent._deny("sleep hours")
    assert result["should_message"] is False
    assert "sleep" in result["reason"]


def test_quiet_hours_can_be_disabled():
    """Quiet window matching remains available, but can be bypassed by config."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None,  # type: ignore
        llm_gateway=MagicMock(),  # type: ignore
        model="test",
        config={"quiet_hours_enabled": False, "sleep_start": 0, "sleep_end": 7},
    )

    assert agent._is_quiet_hour(1) is True
    assert agent.quiet_hours_enabled is False


def test_deny_returns_valid_structure():
    """All deny results have the correct fields."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
    )

    for reason in ["sleep hours", "daily cap", "ignored streak"]:
        result = agent._deny(reason)
        assert result["should_message"] is False
        assert isinstance(result["message"], str)
        assert isinstance(result["emotion"], str)
        assert result["reason"] == reason


def test_scheduler_scenarios():
    """Validate scheduler scenario expectations from fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "scheduler_scenarios.json"
    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for scenario in data["scenarios"]:
        print(f"  Scenario: {scenario['name']}")

        assert "expected" in scenario
        assert "name" in scenario

        if not scenario["expected"]:
            assert "reason_contains" in scenario, \
                f"Scenario '{scenario['name']}' missing reason_contains"


# ================================================================
# V2.1.1: Due-time + intent tests
# ================================================================


def test_random_next_due_in_range():
    """Next due time is randomized inside configured min/max range."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
        config={
            "proactive": {
                "timing": {"min_interval_minutes": 30, "max_interval_minutes": 60},
            }
        },
    )

    now = time.time()
    for _ in range(20):
        due = agent._random_next_due(now)
        delay = due - now
        assert 30 * 60 <= delay <= 60 * 60 + 1, \
            f"Delay {delay}s not in [1800, 3600] range"


def test_random_next_due_varied():
    """Consecutive due times are not identical."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
        config={
            "proactive": {
                "timing": {"min_interval_minutes": 30, "max_interval_minutes": 60},
            }
        },
    )

    now = time.time()
    dues = [agent._random_next_due(now) for _ in range(10)]
    # At least some should be different
    assert len(set(dues)) >= 2, "All due times identical"


def test_topic_eligible_when_open():
    """Open topic is eligible for follow-up."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
    )

    assert agent._topic_is_eligible({"status": "open", "followup_count": 0})
    assert agent._topic_is_eligible({"status": "waiting_user_result", "followup_count": 0})


def test_topic_ineligible_when_closed_or_stale():
    """Closed/stale/answered topics are never eligible."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
    )

    assert not agent._topic_is_eligible({"status": "closed", "followup_count": 0})
    assert not agent._topic_is_eligible({"status": "stale", "followup_count": 0})
    assert not agent._topic_is_eligible({"status": "answered", "followup_count": 0})


def test_topic_ineligible_when_followup_max_reached():
    """Topic with max followups is not eligible."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
        config={
            "proactive": {"topic": {"followup_max_count": 2}},
        },
    )

    assert agent._topic_is_eligible({"status": "open", "followup_count": 0})
    assert agent._topic_is_eligible({"status": "open", "followup_count": 1})
    assert not agent._topic_is_eligible({"status": "open", "followup_count": 2})
    assert not agent._topic_is_eligible({"status": "open", "followup_count": 3})


def test_topic_ineligible_when_expired():
    """Expired topic is not eligible."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
    )

    now = time.time()
    expired = {"status": "open", "followup_count": 0, "expires_at": now - 100}
    assert not agent._topic_is_eligible(expired)

    future = {"status": "open", "followup_count": 0, "expires_at": now + 3600}
    assert agent._topic_is_eligible(future)


def test_topic_ineligible_before_followup_after():
    """Topic with future followup_after_ts is not eligible yet."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
    )

    now = time.time()
    not_yet = {
        "status": "open",
        "followup_count": 0,
        "expires_at": now + 7200,
        "followup_after_ts": now + 1800,  # 30 min from now
    }
    assert not agent._topic_is_eligible(not_yet)

    ready = {
        "status": "open",
        "followup_count": 0,
        "expires_at": now + 7200,
        "followup_after_ts": now - 100,  # already past
    }
    assert agent._topic_is_eligible(ready)


def test_topic_none_is_ineligible():
    """None topic is never eligible."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
    )

    assert not agent._topic_is_eligible(None)


def test_recent_context_formats_recent_turns():
    """Recent proactive context is compact natural text, not raw JSON."""
    from agent.scheduler import SchedulerAgent

    turns = [
        {
            "user_messages": "我在更新一个项目 有好多 bug",
            "assistant_actions": '[{"text":"bug多卡在哪了","delay":0}]',
        },
        {
            "user_messages": "不想谈工作",
            "assistant_actions": '[{"text":"行 那不谈工作","delay":0},{"text":"现在想干点啥","delay":0}]',
        },
    ]

    text = SchedulerAgent._format_recent_context(turns)

    assert "用户: 我在更新一个项目" in text
    assert "bot: 行 那不谈工作 / 现在想干点啥" in text
    assert "assistant_actions" not in text
    assert '{"text"' not in text


def test_recent_context_keeps_proactive_assistant_message_visible():
    """A delivered proactive message remains visible as bot history."""
    from agent.scheduler import SchedulerAgent

    turns = [
        {
            "user_messages": "",
            "assistant_actions": '[{"text":"还没睡？","delay":0}]',
            "source": "proactive",
        },
        {
            "user_messages": "还没呢",
            "assistant_actions": '[{"text":"那别硬熬太久","delay":0}]',
        },
    ]

    text = SchedulerAgent._format_recent_context(turns)

    assert "bot: 还没睡？" in text
    assert "用户: 还没呢" in text
    assert "[主动消息]" not in text


def test_clingy_followup_requires_bot_ended_non_proactive_turn():
    from agent.scheduler import SchedulerAgent

    now = time.time()
    agent = SchedulerAgent(
        store=None,  # type: ignore
        llm_gateway=MagicMock(),  # type: ignore
        model="test",
        config={"proactive": {"generation": {"clingy_mode_enabled": True, "clingy_after_minutes": 20}}},
    )

    assert agent._last_turn_allows_clingy([
        {
            "user_messages": "我等会再说",
            "assistant_actions": '[{"text":"那我等你","delay":0}]',
            "source": "user_reply",
            "created_at": now - 60 * 30,
        }
    ], now) is True

    # V2.1.2: proactive-sourced turns ARE now allowed.
    # Clingy gating moved to the independent state machine
    # (awaiting_user_reply + count cap).
    assert agent._last_turn_allows_clingy([
        {
            "user_messages": "",
            "assistant_actions": '[{"text":"人呢","delay":0}]',
            "source": "proactive",
            "created_at": now - 60 * 30,
        }
    ], now) is True


def test_proactive_multi_message_normalization_and_opener_rotation():
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None,  # type: ignore
        llm_gateway=MagicMock(),  # type: ignore
        model="test",
        config={"proactive": {"generation": {"clingy_mode_enabled": True, "max_messages": 3}}},
    )

    messages = agent._normalize_generated_messages(
        [
            {"text": "人呢", "delay": 0},
            {"text": "干嘛不理我", "delay": 1},
        ],
        "clingy_followup",
        last_opener="人呢",
    )

    assert len(messages) == 2
    assert messages[0]["text"] != "人呢"
    assert messages[1]["text"] == "干嘛不理我"


def test_result_structure_v211():
    """V2.1.1 result structure has correct fields."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
    )

    result = agent._result(False, "test", stage="pre_check")
    assert result["sent"] is False
    assert result["stage"] == "pre_check"
    assert "intent" in result
    assert "candidate_id" in result

    result2 = agent._result(True, "test", stage="completed", intent="check_in")
    assert result2["sent"] is True
    assert result2["intent"] == "check_in"


def test_fallback_messages_all_types():
    """Every intent has a fallback message."""
    from agent.scheduler import FALLBACK_MESSAGES

    for intent in ["check_in", "latest_topic_followup", "clingy_followup", "self_share", "light_ping"]:
        assert intent in FALLBACK_MESSAGES
        msg = FALLBACK_MESSAGES[intent]
        assert isinstance(msg, str)
        assert len(msg) > 0


def test_intent_rules_all_types():
    """Every intent has a generation rule."""
    from agent.scheduler import INTENT_RULES

    for intent in ["check_in", "latest_topic_followup", "clingy_followup", "self_share", "light_ping"]:
        assert intent in INTENT_RULES
        rule = INTENT_RULES[intent]
        assert isinstance(rule, str)
        assert len(rule) > 10


def test_topic_format():
    """Topic formatting includes status, title, and summary."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, model="test", llm_gateway=MagicMock(),  # type: ignore
    )

    topic = {
        "status": "open",
        "title": "中午吃啥",
        "summary": "用户问中午吃什么",
    }
    formatted = agent._format_topic(topic)
    assert "open" in formatted
    assert "中午吃啥" in formatted
    assert "用户问中午吃什么" in formatted

    # Empty topic
    assert agent._format_topic(None) == ""
    assert agent._format_topic({}) == ""


def test_scheduler_message_prompt_uses_current_bot_identity():
    prompt_path = Path(__file__).parent.parent / "prompts" / "scheduler_message.md"
    text = prompt_path.read_text(encoding="utf-8")

    assert "{bot_identity}" in text
    assert "当前 bot 人设" in text
    assert "说话保持「栖」" not in text


def test_guard_accepts_plan_argument():
    from agent.scheduler import SchedulerAgent

    text, violations = SchedulerAgent._guard_proactive_message(
        "鍦ㄥ共鍢涘憿",
        {"risk": "low", "safe_basis": "inner_thought_only", "avoid": []},
    )

    assert text
    assert violations == []


@pytest.mark.asyncio
async def test_two_stage_generation_uses_guard_without_typeerror(monkeypatch):
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(store=None, model="test", llm_gateway=MagicMock())  # type: ignore[arg-type]

    async def fake_openers(limit=10):
        return []

    async def fake_plan(*args, **kwargs):
        return {
            "risk": "low",
            "style": "short",
            "safe_basis": "inner_thought_only",
            "topic_seed": "",
            "avoid": [],
        }

    async def fake_message(*args, **kwargs):
        return {"messages": [{"text": "鍦ㄥ共鍢涘憿", "delay": 0}], "emotion": "neutral"}

    monkeypatch.setattr(agent, "_load_recent_openers_detailed", fake_openers)
    monkeypatch.setattr(agent, "_generate_proactive_plan", fake_plan)
    monkeypatch.setattr(agent, "_generate_message_from_plan", fake_message)

    result = await agent._generate_proactive_message(
        now_cst=datetime.now(),
        intent="check_in",
        intent_reason="test",
        topic_text="",
        recent_context="",
        bot_identity="",
        relationship_context="",
        memory_context="",
    )

    assert result.get("messages")
    assert result["messages"][0]["text"]
    assert not result.get("skipped")


# ================================================================
# Phase 2B: Scheduler + LLMGateway integration tests
# ================================================================
from unittest.mock import MagicMock, AsyncMock

from agent.llm_types import LLMError, LLMRequest, LLMResponse, LLMUsage


def _make_gw_response(content: str, **kw):
    return LLMResponse(
        content=content,
        provider="deepseek",
        model="deepseek-v4-flash",
        usage=LLMUsage(
            prompt_tokens=kw.get("pt", 10),
            completion_tokens=kw.get("ct", 5),
            total_tokens=kw.get("tt", 15),
        ),
        latency_ms=kw.get("lat", 100),
        finish_reason=kw.get("finish_reason", "stop"),
    )


class FakeGateway:
    def __init__(self, response: LLMResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.response:
            return self.response
        return _make_gw_response('{"intent":"check_in","reason":"test"}')


# -------------------------------------------------------------------
# 1. scheduler intent uses LLMGateway
# -------------------------------------------------------------------
class TestSchedulerIntentViaGateway:
    @pytest.mark.asyncio
    async def test_intent_selection_uses_gateway(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"intent":"self_share","reason":"no topic"}'
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in", "self_share"]}}},
        )

        intent, reason = await agent._select_intent(
            now_cst=datetime.now(),
            topic_eligible=False,
            topic=None,
            topic_text="none",
            topic_status="none",
        )

        # Rule-first returns "self_share" for no-topic case, bypassing LLM.
        # But if LLM were called, purpose would be correct.
        # Test the LLM path by making topic_eligible=True without topic_followup enabled.
        agent2 = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )
        # With only "check_in" enabled, topic_eligible=False → falls through to self_share
        # which is not in enabled_types → LLM path
        intent2, reason2 = await agent2._select_intent(
            now_cst=datetime.now(),
            topic_eligible=True,
            topic={"followup_count": 1},
            topic_text="dinner plans",
            topic_status="open",
        )
        assert gw.requests
        req = gw.requests[-1]
        assert req.purpose == "scheduler_intent"
        assert req.response_mode == "json_object"  # Phase 3B-1: default is json_object
        assert req.thinking == "disabled"
        assert req.temperature == 0.4
        assert req.max_tokens == 128

    @pytest.mark.asyncio
    async def test_intent_llm_failure_falls_back(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(error=LLMError(
            kind="timeout", message="timed out", provider="deepseek", retryable=True,
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )

        # Rule-first should still work even if LLM fails
        intent, reason = await agent._select_intent(
            now_cst=datetime.now(),
            topic_eligible=False,
            topic=None,
            topic_text="none",
            topic_status="none",
        )
        # Fallback should return a valid intent in enabled_types
        assert intent in ["check_in", "self_share", "light_ping"]


# -------------------------------------------------------------------
# 2. proactive message generation uses LLMGateway
# -------------------------------------------------------------------
class TestProactiveMessageViaGateway:
    @pytest.mark.asyncio
    async def test_generate_message_uses_gateway(self):
        from agent.scheduler import SchedulerAgent, INTENT_RULES

        gw = FakeGateway(response=_make_gw_response(
            '{"message":"在干嘛","emotion":"neutral"}'
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
        )

        gen = await agent._generate_message(
            now_cst=datetime.now(),
            intent="check_in",
            intent_reason="periodic check",
            topic_text="",
            recent_context="none",
        )
        assert gw.requests
        req = gw.requests[0]
        assert req.purpose == "proactive_message"
        assert req.response_mode == "text"
        assert req.thinking == "disabled"
        assert req.temperature == 0.7
        assert req.max_tokens == 256

    @pytest.mark.asyncio
    async def test_generate_message_bad_json_returns_empty(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response("not json at all"))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
        )

        gen = await agent._generate_message(
            now_cst=datetime.now(),
            intent="check_in",
            intent_reason="test",
            topic_text="",
            recent_context="none",
        )
        assert gen["message"] == ""
        assert gen["emotion"] == "neutral"


# -------------------------------------------------------------------
# 3. proactive plan generation uses LLMGateway
# -------------------------------------------------------------------
class TestProactivePlanViaGateway:
    @pytest.mark.asyncio
    async def test_generate_plan_uses_gateway(self, monkeypatch):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"risk":"low","style":"curious","safe_basis":"inner_thought_only","topic_seed":"","avoid":[]}'
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
        )
        # Need a plan template for the LLM to be called
        agent.plan_prompt_template = "plan: {intent} / {intent_reason}"

        plan = await agent._generate_proactive_plan(
            now_str="2026-05-28 10:00",
            intent="check_in",
            intent_reason="test",
            topic_text="",
            recent_context="",
            bot_identity="bot",
            relationship_context="",
            memory_context="",
            recent_openers="",
        )
        assert gw.requests
        req = gw.requests[0]
        assert req.purpose == "proactive_plan"
        assert req.response_mode == "json_object"  # Phase 3B-1: default is json_object
        assert req.thinking == "disabled"
        assert req.temperature == 0.5
        assert req.max_tokens == 256

    @pytest.mark.asyncio
    async def test_plan_failure_returns_empty_dict(self, monkeypatch):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(error=LLMError(
            kind="provider_server_error", message="bang", provider="deepseek",
            status_code=500, retryable=True,
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
        )
        agent.plan_prompt_template = "plan prompt"

        plan = await agent._generate_proactive_plan(
            now_str="2026-05-28",
            intent="check_in", intent_reason="test",
            topic_text="", recent_context="",
            bot_identity="b", relationship_context="",
            memory_context="", recent_openers="",
        )
        assert plan == {}

    @pytest.mark.asyncio
    async def test_no_plan_template_skips_llm(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway()
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
        )
        agent.plan_prompt_template = ""  # disabled

        plan = await agent._generate_proactive_plan(
            now_str="", intent="", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )
        assert plan == {}
        assert gw.requests == []  # LLM never called


# -------------------------------------------------------------------
# 4. _generate_message_from_plan uses LLMGateway
# -------------------------------------------------------------------
class TestMessageFromPlanViaGateway:
    @pytest.mark.asyncio
    async def test_message_from_plan_uses_gateway(self, monkeypatch):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"message":"在干嘛呢","emotion":"neutral","ending_type":"question"}'
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
        )
        agent.message_v2_prompt_template = "msg: {intent} / {style}"

        gen = await agent._generate_message_from_plan(
            now_str="2026-05-28",
            plan={"intent": "check_in", "style": "curious", "safe_basis": "inner", "topic_seed": "", "risk": "low", "avoid": []},
            bot_identity="bot",
            relationship_context="",
        )
        assert gw.requests
        req = gw.requests[0]
        assert req.purpose == "proactive_message_v2"
        assert req.temperature == 0.7
        assert req.max_tokens == 256
        assert gen["message"] == "在干嘛呢"

    @pytest.mark.asyncio
    async def test_message_from_plan_failure_returns_empty(self, monkeypatch):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(error=LLMError(
            kind="timeout", message="timeout", provider="deepseek", retryable=True,
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
        )
        agent.message_v2_prompt_template = "prompt"

        gen = await agent._generate_message_from_plan(
            now_str="", plan={"style": "curious", "intent": "check_in"},
            bot_identity="", relationship_context="",
        )
        assert gen["message"] == ""
        assert gen["emotion"] == "neutral"


# -------------------------------------------------------------------
# 5. raw client fallback still works
# -------------------------------------------------------------------
class TestSchedulerRawClientFallback:
    @pytest.mark.asyncio
    async def test_generate_message_via_gateway(self):
        """The only LLM path is now through LLMGateway."""
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"message":"在干嘛呢","emotion":"neutral"}'
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,  # type: ignore[arg-type]
        )

        gen = await agent._generate_message(
            now_cst=datetime.now(),
            intent="check_in",
            intent_reason="test",
            topic_text="",
            recent_context="",
        )
        assert gen["message"] == "在干嘛呢"


# -------------------------------------------------------------------
# 6. Phase 3B-1: JSON Output toggle and parameter tests
# -------------------------------------------------------------------
class TestSchedulerGatewayParams:
    @pytest.mark.asyncio
    async def test_intent_uses_json_object_by_default(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response('{"intent":"check_in","reason":"ok"}'))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )

        await agent._select_intent(
            now_cst=datetime.now(),
            topic_eligible=True, topic={"followup_count": 0},
            topic_text="test", topic_status="open",
        )

        for req in gw.requests:
            assert req.response_mode == "json_object"
            assert req.thinking == "disabled"
            assert req.tools is None

    @pytest.mark.asyncio
    async def test_intent_disabled_uses_text_mode(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response('{"intent":"check_in","reason":"ok"}'))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={
                "proactive": {"generation": {"enabled_types": ["check_in"]}},
                "json_output": {"scheduler_intent": False},
            },
        )

        await agent._select_intent(
            now_cst=datetime.now(),
            topic_eligible=True, topic={"followup_count": 0},
            topic_text="test", topic_status="open",
        )

        for req in gw.requests:
            assert req.response_mode == "text"

    @pytest.mark.asyncio
    async def test_plan_uses_json_object_by_default(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"risk":"low","style":"curious","safe_basis":"inner_thought_only","topic_seed":"","avoid":[]}'
        ))
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.plan_prompt_template = "plan prompt"

        await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )

        assert gw.requests
        assert gw.requests[0].response_mode == "json_object"

    @pytest.mark.asyncio
    async def test_plan_disabled_uses_text_mode(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"risk":"low","style":"curious","safe_basis":"inner_thought_only","topic_seed":"","avoid":[]}'
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"json_output": {"proactive_plan": False}},
        )
        agent.plan_prompt_template = "plan prompt"

        await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )

        assert gw.requests
        assert gw.requests[0].response_mode == "text"

    @pytest.mark.asyncio
    async def test_generate_message_still_uses_text_mode(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response('{"message":"hi","emotion":"neutral"}'))
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)

        await agent._generate_message(
            now_cst=datetime.now(), intent="check_in",
            intent_reason="", topic_text="", recent_context="",
        )

        for req in gw.requests:
            assert req.response_mode == "text"
            assert req.thinking == "disabled"

    @pytest.mark.asyncio
    async def test_message_from_plan_still_uses_text_mode(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"message":"hi","emotion":"neutral","ending_type":"question"}'
        ))
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.message_v2_prompt_template = "msg prompt"

        await agent._generate_message_from_plan(
            now_str="", plan={"intent": "check_in", "style": "curious",
                              "safe_basis": "inner", "topic_seed": "",
                              "risk": "low", "avoid": []},
            bot_identity="", relationship_context="",
        )

        for req in gw.requests:
            assert req.response_mode == "text"

    @pytest.mark.asyncio
    async def test_thinking_always_disabled(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response('{"intent":"check_in","reason":"ok"}'))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )
        agent.plan_prompt_template = "plan"
        agent.message_v2_prompt_template = "msg"

        await agent._select_intent(
            now_cst=datetime.now(), topic_eligible=True,
            topic={"followup_count": 0}, topic_text="t", topic_status="open",
        )
        await agent._generate_proactive_plan(
            now_str="", intent="", intent_reason="", topic_text="",
            recent_context="", bot_identity="", relationship_context="",
            memory_context="", recent_openers="",
        )
        await agent._generate_message(
            now_cst=datetime.now(), intent="check_in",
            intent_reason="", topic_text="", recent_context="",
        )
        await agent._generate_message_from_plan(
            now_str="", plan={"intent": "check_in"}, bot_identity="",
            relationship_context="",
        )

        for req in gw.requests:
            assert req.thinking == "disabled"


# -------------------------------------------------------------------
# 7. Phase 3B-1: Intent JSON schema validation
# -------------------------------------------------------------------
class TestPhase3B1IntentSchema:
    @pytest.mark.asyncio
    async def test_valid_intent_json_parsed(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"intent":"check_in","reason":"test reason"}'
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in", "light_ping"]}}},
        )

        # Avoid rule-first: topic_eligible=True requires "latest_topic_followup"
        # not in enabled_types; topic_eligible=False requires "self_share" not in
        # enabled_types. Setting enabled_types=["check_in","light_ping"] + topic_eligible=False
        # avoids both fast paths.
        intent, reason = await agent._select_intent(
            now_cst=datetime.now(), topic_eligible=False,
            topic=None, topic_text="none", topic_status="none",
        )
        assert gw.requests
        assert intent == "check_in"

    @pytest.mark.asyncio
    async def test_non_json_content_falls_back(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response("not json at all"))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )

        intent, reason = await agent._select_intent(
            now_cst=datetime.now(), topic_eligible=False,
            topic=None, topic_text="none", topic_status="none",
        )
        assert intent == "check_in"
        assert "fallback" in reason

    @pytest.mark.asyncio
    async def test_intent_not_in_allowed_set_falls_back(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"intent":"latest_topic_followup","reason":"follow"}'
        ))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )

        intent, reason = await agent._select_intent(
            now_cst=datetime.now(), topic_eligible=False,
            topic=None, topic_text="none", topic_status="none",
        )
        assert gw.requests
        assert intent == "check_in"
        assert intent != "latest_topic_followup"

    @pytest.mark.asyncio
    async def test_missing_intent_field_falls_back(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response('{"reason":"no intent"}'))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )

        intent, reason = await agent._select_intent(
            now_cst=datetime.now(), topic_eligible=False,
            topic=None, topic_text="none", topic_status="none",
        )
        assert intent == "check_in"
        assert "fallback" in reason


# -------------------------------------------------------------------
# 8. Phase 3B-1: Proactive plan JSON schema validation
# -------------------------------------------------------------------
class TestPhase3B1PlanSchema:
    @pytest.mark.asyncio
    async def test_valid_plan_json_parsed(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"intent":"check_in","risk":"low","style":"curious",'
            '"safe_basis":"inner_thought_only","topic_seed":"test","avoid":["bad"]}'
        ))
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.plan_prompt_template = "plan prompt"

        plan = await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )
        assert plan != {}
        assert plan["risk"] == "low"
        assert plan["style"] == "curious"
        assert plan["avoid"] == ["bad"]

    @pytest.mark.asyncio
    async def test_non_json_plan_returns_empty(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response("not json plan"))
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.plan_prompt_template = "plan prompt"

        plan = await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )
        assert plan == {}

    @pytest.mark.asyncio
    async def test_invalid_risk_defaults_to_low(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"intent":"check_in","risk":"critical","style":"curious"}'
        ))
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.plan_prompt_template = "plan prompt"

        plan = await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )
        assert plan.get("risk") == "low"

    @pytest.mark.asyncio
    async def test_invalid_style_defaults_to_curious(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"intent":"check_in","risk":"low","style":"aggressive"}'
        ))
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.plan_prompt_template = "plan prompt"

        plan = await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )
        assert plan.get("style") == "curious"

    @pytest.mark.asyncio
    async def test_avoid_not_list_defaults_to_empty(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(
            '{"intent":"check_in","risk":"low","style":"curious","avoid":"not_a_list"}'
        ))
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.plan_prompt_template = "plan prompt"

        plan = await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )
        assert plan.get("avoid") == []


# -------------------------------------------------------------------
# 9. Phase 3B-1: finish_reason=length retry
# -------------------------------------------------------------------
class TestPhase3B1FinishReasonLength:
    @pytest.mark.asyncio
    async def test_intent_length_retry_succeeds(self):
        from agent.scheduler import SchedulerAgent

        call_count = [0]
        responses = [
            _make_gw_response('{"intent":"check_in","reason":"ok"}', finish_reason="length"),
            _make_gw_response('{"intent":"check_in","reason":"retry_ok"}', finish_reason="stop"),
        ]

        class LengthRetryGW:
            def __init__(self):
                self.requests: list = []

            async def complete(self, request):
                self.requests.append(request)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gw = LengthRetryGW()
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )

        intent, reason = await agent._select_intent(
            now_cst=datetime.now(), topic_eligible=False,
            topic=None, topic_text="none", topic_status="none",
        )
        assert intent == "check_in"
        assert reason == "retry_ok"
        assert len(gw.requests) == 2  # original + retry
        for req in gw.requests:
            assert req.response_mode == "json_object"

    @pytest.mark.asyncio
    async def test_intent_length_retry_fails_fallback(self):
        from agent.scheduler import SchedulerAgent

        call_count = [0]
        responses = [
            _make_gw_response('{"intent":"check_in","reason":"ok"}', finish_reason="length"),
            _make_gw_response('{"intent":"check_in","reason":"also"}', finish_reason="length"),
        ]

        class LengthFailGW:
            def __init__(self):
                self.requests: list = []

            async def complete(self, request):
                self.requests.append(request)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gw = LengthFailGW()
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )

        intent, reason = await agent._select_intent(
            now_cst=datetime.now(), topic_eligible=False,
            topic=None, topic_text="none", topic_status="none",
        )
        assert "fallback" in reason
        assert len(gw.requests) == 2

    @pytest.mark.asyncio
    async def test_plan_length_retry_succeeds(self):
        from agent.scheduler import SchedulerAgent

        call_count = [0]
        responses = [
            _make_gw_response('{"intent":"check_in","risk":"low","style":"curious"}', finish_reason="length"),
            _make_gw_response('{"intent":"check_in","risk":"low","style":"curious"}', finish_reason="stop"),
        ]

        class LengthPlanGW:
            def __init__(self):
                self.requests: list = []

            async def complete(self, request):
                self.requests.append(request)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gw = LengthPlanGW()
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.plan_prompt_template = "plan prompt"

        plan = await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )
        assert plan != {}
        assert plan["risk"] == "low"
        assert len(gw.requests) == 2

    @pytest.mark.asyncio
    async def test_plan_length_retry_fails_returns_empty(self):
        from agent.scheduler import SchedulerAgent

        call_count = [0]
        responses = [
            _make_gw_response('{"intent":"check_in","risk":"low"}', finish_reason="length"),
            _make_gw_response('{"intent":"check_in","risk":"low"}', finish_reason="length"),
        ]

        class LengthPlanFailGW:
            def __init__(self):
                self.requests: list = []

            async def complete(self, request):
                self.requests.append(request)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gw = LengthPlanFailGW()
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.plan_prompt_template = "plan prompt"

        plan = await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )
        assert plan == {}
        assert len(gw.requests) == 2

    @pytest.mark.asyncio
    async def test_plan_edge_case_top_level_not_object(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response('[1, 2, 3]'))
        agent = SchedulerAgent(store=None, model="test", llm_gateway=gw)
        agent.plan_prompt_template = "plan prompt"

        plan = await agent._generate_proactive_plan(
            now_str="", intent="check_in", intent_reason="",
            topic_text="", recent_context="", bot_identity="",
            relationship_context="", memory_context="", recent_openers="",
        )
        assert plan == {}

    @pytest.mark.asyncio
    async def test_intent_edge_case_empty_content(self):
        from agent.scheduler import SchedulerAgent

        gw = FakeGateway(response=_make_gw_response(""))
        agent = SchedulerAgent(
            store=None, model="test", llm_gateway=gw,
            config={"proactive": {"generation": {"enabled_types": ["check_in"]}}},
        )

        intent, reason = await agent._select_intent(
            now_cst=datetime.now(), topic_eligible=False,
            topic=None, topic_text="none", topic_status="none",
        )
        assert "fallback" in reason


if __name__ == "__main__":
    test_sleep_hours_blocked()
    test_deny_returns_valid_structure()
    test_scheduler_scenarios()
    test_random_next_due_in_range()
    test_random_next_due_varied()
    test_topic_eligible_when_open()
    test_topic_ineligible_when_closed_or_stale()
    test_topic_ineligible_when_followup_max_reached()
    test_topic_ineligible_when_expired()
    test_topic_ineligible_before_followup_after()
    test_topic_none_is_ineligible()
    test_result_structure_v211()
    test_fallback_messages_all_types()
    test_intent_rules_all_types()
    test_topic_format()
    print("[OK] All scheduler tests passed (V2.1.1)")
