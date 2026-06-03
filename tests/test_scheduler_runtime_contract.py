"""Phase 9: SchedulerRuntime contract tests.

Validates heartbeat, proactive gate, skip/forced-speak, quiet hours,
and intensity frequency control.

Spec: docs/runtime-specs/05-scheduler-runtime-spec.zh.md
Test matrix: SR-001 to SR-014
"""

from __future__ import annotations

import time
import pytest
from datetime import datetime

from agent.scheduler_runtime import (
    SchedulerRuntime,
    SchedulerConfig,
    InitiativeState,
    InitiativeGateDecision,
    TriggerPriority,
    INTENSITY_PROFILES,
)


@pytest.fixture
def runtime():
    return SchedulerRuntime()


@pytest.fixture
def enabled_runtime():
    return SchedulerRuntime(config=SchedulerConfig(
        enabled=True, autonomous_enabled=True,
        autonomous_intensity="daily",
        quiet_hours_enabled=False,
    ))


# ═══════════════════════════════════════════════════════════════════
# SR-001, SR-002: Heartbeat with switch off
# ═══════════════════════════════════════════════════════════════════


class TestHeartbeatSwitch:
    def test_global_disabled_no_turn(self, runtime):
        runtime.config.enabled = False
        result = runtime.heartbeat_tick()
        assert result == TriggerPriority.NONE

    def test_autonomous_disabled_returns_none(self, runtime):
        runtime.config.enabled = True
        runtime.config.autonomous_enabled = False
        result = runtime.heartbeat_tick()
        assert result == TriggerPriority.NONE

    def test_enabled_heartbeat_returns_priority(self, enabled_runtime):
        result = enabled_runtime.heartbeat_tick()
        assert result != TriggerPriority.NONE


# ═══════════════════════════════════════════════════════════════════
# SR-003: Heartbeat never generates text
# ═══════════════════════════════════════════════════════════════════


class TestHeartbeatNoTextGeneration:
    def test_heartbeat_returns_enum_only(self, enabled_runtime):
        result = enabled_runtime.heartbeat_tick()
        assert isinstance(result, TriggerPriority)
        # Heartbeat doesn't generate text, doesn't call models


# ═══════════════════════════════════════════════════════════════════
# SR-007: Quiet hours block proactive
# ═══════════════════════════════════════════════════════════════════


class TestQuietHours:
    def test_quiet_hours_block_proactive(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            quiet_hours_enabled=True,
            quiet_hours_start="23:00", quiet_hours_end="08:00",
        ))
        # 03:00 local
        night_ts = datetime(2026, 6, 3, 3, 0, 0).timestamp()
        decision = rt.check_proactive_gate(now_ts=night_ts)
        assert decision.allowed is False
        assert decision.quiet_hours_blocked is True
        assert decision.reason == "quiet_hours"

    def test_not_in_quiet_hours_allows(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            quiet_hours_enabled=True,
            quiet_hours_start="23:00", quiet_hours_end="08:00",
        ))
        day_ts = datetime(2026, 6, 3, 14, 0, 0).timestamp()
        decision = rt.check_proactive_gate(now_ts=day_ts)
        assert decision.quiet_hours_blocked is False

    def test_quiet_hours_boundary_start_inclusive(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            quiet_hours_enabled=True,
            quiet_hours_start="23:00", quiet_hours_end="08:00",
        ))
        start_ts = datetime(2026, 6, 3, 23, 0, 0).timestamp()
        assert rt._in_quiet_hours(start_ts) is True

    def test_quiet_hours_boundary_end_exclusive(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            quiet_hours_enabled=True,
            quiet_hours_start="23:00", quiet_hours_end="08:00",
        ))
        end_ts = datetime(2026, 6, 4, 8, 0, 0).timestamp()
        assert rt._in_quiet_hours(end_ts) is False

    def test_quiet_hours_disabled_allows_all(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            quiet_hours_enabled=False,
            quiet_hours_start="23:00", quiet_hours_end="08:00",
        ))
        night_ts = datetime(2026, 6, 3, 3, 0, 0).timestamp()
        decision = rt.check_proactive_gate(now_ts=night_ts)
        assert decision.quiet_hours_blocked is False


# ═══════════════════════════════════════════════════════════════════
# SR-008: Frequency tier caps
# ═══════════════════════════════════════════════════════════════════


class TestFrequencyCaps:
    def test_cap_not_reached_allows(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            autonomous_intensity="daily", quiet_hours_enabled=False,
        ))
        rt._get_state("u1").daily_trigger_count = 4  # cap=5
        decision = rt.check_proactive_gate(user_id="u1")
        assert decision.allowed is True

    def test_cap_reached_blocks(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            autonomous_intensity="daily", quiet_hours_enabled=False,
        ))
        rt._get_state("u1").daily_trigger_count = 5  # cap=5
        decision = rt.check_proactive_gate(user_id="u1")
        assert decision.allowed is False
        assert decision.reason == "trigger_cap_reached"

    def test_quiet_intensity_cap_is_2(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            autonomous_intensity="quiet", quiet_hours_enabled=False,
        ))
        rt._get_state("u1").daily_trigger_count = 2
        decision = rt.check_proactive_gate(user_id="u1")
        assert decision.allowed is False


# ═══════════════════════════════════════════════════════════════════
# SR-009: Consecutive skip forces next speak
# ═══════════════════════════════════════════════════════════════════


class TestSkipForcedSpeak:
    def test_no_skip_history_does_not_force_speak(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            autonomous_intensity="daily", quiet_hours_enabled=False,
        ))
        decision = rt.check_proactive_gate(user_id="u1")
        assert decision.requires_forced_speak is False

    def test_last_skipped_forces_speak(self):
        rt = SchedulerRuntime(config=SchedulerConfig(
            enabled=True, autonomous_enabled=True,
            autonomous_intensity="daily", quiet_hours_enabled=False,
        ))
        rt._get_state("u1").last_decision = "skipped"
        decision = rt.check_proactive_gate(user_id="u1")
        assert decision.requires_forced_speak is True

    def test_skip_then_speak_resets_state(self, enabled_runtime):
        enabled_runtime.record_skipped("u1")
        state = enabled_runtime._get_state("u1")
        assert state.last_decision == "skipped"
        assert state.consecutive_skip_count == 1

        enabled_runtime.record_spoke("u1")
        state = enabled_runtime._get_state("u1")
        assert state.last_decision == "spoke"
        assert state.consecutive_skip_count == 0

    def test_spoke_after_forced_speak_resets(self, enabled_runtime):
        enabled_runtime._get_state("u1").last_decision = "skipped"
        decision = enabled_runtime.check_proactive_gate(user_id="u1")
        assert decision.requires_forced_speak is True

        # After speaking, forced_speak clears
        enabled_runtime.record_spoke("u1")
        decision2 = enabled_runtime.check_proactive_gate(user_id="u1")
        assert decision2.requires_forced_speak is False


# ═══════════════════════════════════════════════════════════════════
# SR-013: Circuit breaker / lock
# ═══════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    def test_lock_held_blocks_heartbeat(self, enabled_runtime):
        enabled_runtime.lock_held = True
        result = enabled_runtime.heartbeat_tick()
        assert result == TriggerPriority.NONE

    def test_lock_released_allows_heartbeat(self, enabled_runtime):
        enabled_runtime.lock_held = True
        assert enabled_runtime.heartbeat_tick() == TriggerPriority.NONE
        enabled_runtime.lock_held = False
        assert enabled_runtime.heartbeat_tick() != TriggerPriority.NONE

    def test_bridge_offline_blocks_gate(self, enabled_runtime):
        decision = enabled_runtime.check_proactive_gate(bridge_online=False)
        assert decision.allowed is False
        assert decision.reason == "bridge_offline"


# ═══════════════════════════════════════════════════════════════════
# Intensity profiles
# ═══════════════════════════════════════════════════════════════════


class TestIntensityProfiles:
    def test_all_profiles_have_valid_ranges(self):
        for name, profile in INTENSITY_PROFILES.items():
            assert profile["min"] > 0
            assert profile["max"] >= profile["min"]
            assert profile["cap"] > 0

    def test_get_next_wake_range(self, enabled_runtime):
        mn, mx = enabled_runtime.get_next_wake_range()
        assert mn == 120  # daily
        assert mx == 480


# ═══════════════════════════════════════════════════════════════════
# State tracking
# ═══════════════════════════════════════════════════════════════════


class TestStateTracking:
    def test_record_failed_does_not_increment_count(self, enabled_runtime):
        state = enabled_runtime._get_state("u1")
        state.daily_trigger_count = 3
        enabled_runtime.record_failed("u1")
        assert state.daily_trigger_count == 3  # unchanged

    def test_record_spoke_increments_count(self, enabled_runtime):
        enabled_runtime.record_spoke("u1")
        state = enabled_runtime._get_state("u1")
        assert state.daily_trigger_count == 1
        assert state.last_decision == "spoke"

    def test_record_skipped_increments_count(self, enabled_runtime):
        enabled_runtime.record_skipped("u1")
        state = enabled_runtime._get_state("u1")
        assert state.daily_trigger_count == 1
        assert state.last_decision == "skipped"

    def test_multiple_users_have_independent_states(self, enabled_runtime):
        enabled_runtime.record_spoke("u1")
        enabled_runtime.record_skipped("u2")
        assert enabled_runtime._get_state("u1").last_decision == "spoke"
        assert enabled_runtime._get_state("u2").last_decision == "skipped"
