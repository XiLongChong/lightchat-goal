"""SchedulerRuntime — heartbeat, claim, proactive gate decisions.

Phase 9: Pure decision layer. Decides WHEN to create a turn and
WHAT TYPE, never generates text or calls models.

Spec: docs/runtime-specs/05-scheduler-runtime-spec.zh.md
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime as dt, time as dtime
from enum import StrEnum
from typing import Any


# ── Intensity profiles ─────────────────────────────────────────────

INTENSITY_PROFILES: dict[str, dict[str, int]] = {
    "quiet":     {"cap": 2,  "min": 360, "max": 720},
    "daily":     {"cap": 5,  "min": 120, "max": 480},
    "caring":    {"cap": 10, "min": 30,  "max": 120},
    "companion": {"cap": 20, "min": 5,   "max": 20},
}


# ── Priority enum ──────────────────────────────────────────────────


class TriggerPriority(StrEnum):
    REMINDER = "reminder"
    RITUAL = "ritual"
    PROACTIVE_WAKE = "proactive_wake"
    PROACTIVE_COLD = "proactive_cold"
    NONE = "none"


# ── SchedulerConfig ────────────────────────────────────────────────


@dataclass
class SchedulerConfig:
    enabled: bool = False
    autonomous_enabled: bool = False
    autonomous_intensity: str = "daily"
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"
    heartbeat_interval: int = 30
    daily_message_cap: int = 50
    cold_start_seconds: int = 120
    scheduled_enabled: bool = True
    reminder_enabled: bool = True


# ── InitiativeState ────────────────────────────────────────────────


@dataclass
class InitiativeState:
    last_trigger_at: float | None = None
    last_spoke_at: float | None = None
    last_skipped_at: float | None = None
    consecutive_skip_count: int = 0
    last_decision: str = "unset"  # "spoke" | "skipped" | "unset"
    daily_trigger_count: int = 0
    daily_trigger_date: str = ""
    intensity_profile: str = "daily"


# ── InitiativeGateDecision ─────────────────────────────────────────


@dataclass
class InitiativeGateDecision:
    allowed: bool = False
    reason: str = ""
    intensity_profile: str = "daily"
    daily_trigger_count: int = 0
    daily_trigger_cap: int = 5
    quiet_hours_blocked: bool = False
    requires_forced_speak: bool = False
    elapsed_since_last_visible: float = 0.0


# ── SchedulerRuntime ───────────────────────────────────────────────


@dataclass
class SchedulerRuntime:
    config: SchedulerConfig = field(default_factory=SchedulerConfig)
    initiative_states: dict[str, InitiativeState] = field(default_factory=dict)
    lock_held: bool = False
    heartbeat_count: int = 0

    # ── Heartbeat tick ────────────────────────────────────────────

    def heartbeat_tick(self, now_ts: float | None = None) -> TriggerPriority:
        """Single heartbeat cycle. Returns the priority of what should trigger."""
        ts = now_ts or time.time()
        self.heartbeat_count += 1

        if not self.config.enabled:
            return TriggerPriority.NONE

        if self.lock_held:
            return TriggerPriority.NONE

        # Priority 1: user reminder (always checked)
        if self.config.reminder_enabled:
            # In real implementation, this would claim_due_reminder()
            pass  # No persistent store in facade test

        # Priority 2: scheduled ritual
        if self.config.scheduled_enabled:
            pass  # No persistent store in facade test

        # Priority 3: proactive wake-up or cold-start
        if self.config.autonomous_enabled:
            return TriggerPriority.PROACTIVE_COLD

        return TriggerPriority.NONE

    # ── Proactive gate check ──────────────────────────────────────

    def check_proactive_gate(
        self,
        user_id: str = "",
        now_ts: float | None = None,
        last_visible_ts: float = 0.0,
        bridge_online: bool = True,
    ) -> InitiativeGateDecision:
        """Check all proactive eligibility gates. Returns decision with reason."""
        ts = now_ts or time.time()
        profile = INTENSITY_PROFILES.get(
            self.config.autonomous_intensity,
            INTENSITY_PROFILES["daily"],
        )

        # 1. autonomous_enabled
        if not self.config.autonomous_enabled:
            return InitiativeGateDecision(
                allowed=False, reason="autonomous_disabled",
                intensity_profile=self.config.autonomous_intensity,
            )

        # 2. Bridge online
        if not bridge_online:
            return InitiativeGateDecision(
                allowed=False, reason="bridge_offline",
                intensity_profile=self.config.autonomous_intensity,
            )

        # 3. Quiet hours
        if self.config.quiet_hours_enabled and self._in_quiet_hours(ts):
            return InitiativeGateDecision(
                allowed=False, reason="quiet_hours",
                quiet_hours_blocked=True,
                intensity_profile=self.config.autonomous_intensity,
            )

        # 4. Daily trigger cap
        state = self._get_state(user_id)
        if state.daily_trigger_count >= profile["cap"]:
            return InitiativeGateDecision(
                allowed=False, reason="trigger_cap_reached",
                daily_trigger_count=state.daily_trigger_count,
                daily_trigger_cap=profile["cap"],
                intensity_profile=self.config.autonomous_intensity,
            )

        # 5. Cold-start elapsed check
        elapsed = ts - last_visible_ts if last_visible_ts > 0 else 999999
        if elapsed < self.config.cold_start_seconds:
            return InitiativeGateDecision(
                allowed=False, reason="too_soon",
                elapsed_since_last_visible=elapsed,
                intensity_profile=self.config.autonomous_intensity,
            )

        # 6. Determine forced_speak
        requires_forced_speak = (state.last_decision == "skipped")

        return InitiativeGateDecision(
            allowed=True, reason="",
            intensity_profile=self.config.autonomous_intensity,
            daily_trigger_count=state.daily_trigger_count,
            daily_trigger_cap=profile["cap"],
            requires_forced_speak=requires_forced_speak,
            elapsed_since_last_visible=elapsed,
        )

    # ── State updates after turn ──────────────────────────────────

    def record_spoke(self, user_id: str, now_ts: float | None = None):
        ts = now_ts or time.time()
        state = self._get_state(user_id)
        state.last_trigger_at = ts
        state.last_spoke_at = ts
        state.last_decision = "spoke"
        state.consecutive_skip_count = 0
        state.daily_trigger_count += 1

    def record_skipped(self, user_id: str, now_ts: float | None = None):
        ts = now_ts or time.time()
        state = self._get_state(user_id)
        state.last_trigger_at = ts
        state.last_skipped_at = ts
        state.last_decision = "skipped"
        state.consecutive_skip_count = 1
        state.daily_trigger_count += 1

    def record_failed(self, user_id: str):
        """LLM error: don't bump trigger count, don't change skip state."""
        pass

    # ── Helpers ────────────────────────────────────────────────────

    def _get_state(self, user_id: str) -> InitiativeState:
        if user_id not in self.initiative_states:
            self.initiative_states[user_id] = InitiativeState(
                intensity_profile=self.config.autonomous_intensity,
            )
        return self.initiative_states[user_id]

    def _in_quiet_hours(self, now_ts: float) -> bool:
        start_str = self.config.quiet_hours_start
        end_str = self.config.quiet_hours_end
        try:
            start = dtime.fromisoformat(start_str)
            end = dtime.fromisoformat(end_str)
        except ValueError:
            return False

        now_dt = dt.fromtimestamp(now_ts)
        now_time = now_dt.time()

        if start <= end:
            return start <= now_time < end
        else:
            # Crosses midnight: e.g. 23:00-08:00
            return now_time >= start or now_time < end

    def get_next_wake_range(self) -> tuple[int, int]:
        """Return (min_minutes, max_minutes) for the current intensity."""
        profile = INTENSITY_PROFILES.get(
            self.config.autonomous_intensity,
            INTENSITY_PROFILES["daily"],
        )
        return profile["min"], profile["max"]
