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


def test_sleep_hours_blocked():
    """During sleep hours, proactive message is blocked."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None,  # type: ignore
        client=None,  # type: ignore
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
        client=None,  # type: ignore
        model="test",
        config={"quiet_hours_enabled": False, "sleep_start": 0, "sleep_end": 7},
    )

    assert agent._is_quiet_hour(1) is True
    assert agent.quiet_hours_enabled is False


def test_deny_returns_valid_structure():
    """All deny results have the correct fields."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, client=None, model="test",  # type: ignore
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
        store=None, client=None, model="test",  # type: ignore
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
        store=None, client=None, model="test",  # type: ignore
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
        store=None, client=None, model="test",  # type: ignore
    )

    assert agent._topic_is_eligible({"status": "open", "followup_count": 0})
    assert agent._topic_is_eligible({"status": "waiting_user_result", "followup_count": 0})


def test_topic_ineligible_when_closed_or_stale():
    """Closed/stale/answered topics are never eligible."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, client=None, model="test",  # type: ignore
    )

    assert not agent._topic_is_eligible({"status": "closed", "followup_count": 0})
    assert not agent._topic_is_eligible({"status": "stale", "followup_count": 0})
    assert not agent._topic_is_eligible({"status": "answered", "followup_count": 0})


def test_topic_ineligible_when_followup_max_reached():
    """Topic with max followups is not eligible."""
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(
        store=None, client=None, model="test",  # type: ignore
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
        store=None, client=None, model="test",  # type: ignore
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
        store=None, client=None, model="test",  # type: ignore
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
        store=None, client=None, model="test",  # type: ignore
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
        client=None,  # type: ignore
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
        client=None,  # type: ignore
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
        store=None, client=None, model="test",  # type: ignore
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
        store=None, client=None, model="test",  # type: ignore
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

    agent = SchedulerAgent(store=None, client=None, model="test")  # type: ignore[arg-type]

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
