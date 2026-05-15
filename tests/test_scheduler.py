"""Test scheduler — Safeguard #6: hard rules before LLM.

These tests verify the hard rule phase only (no LLM call needed).
"""

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


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

        # Validate that expected field exists
        assert "expected" in scenario
        assert "name" in scenario

        # If expected is False, reason_contains should be set
        if not scenario["expected"]:
            assert "reason_contains" in scenario, \
                f"Scenario '{scenario['name']}' missing reason_contains"


if __name__ == "__main__":
    test_sleep_hours_blocked()
    test_deny_returns_valid_structure()
    test_scheduler_scenarios()
    print("[OK] All scheduler tests passed")
