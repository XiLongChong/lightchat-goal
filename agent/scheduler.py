"""ProactiveScheduler — v3.1 proactive message orchestration.

Orchestrates proactive message turns. Phase 4: manual trigger + delivered-only
history. Phase 5: heartbeat + eligibility pre-checks.
Phase 6: impulse generation/dedup/score/select.

Lock ownership: ProactiveScheduler owns the lock. main.py delegates through
run_manual_trigger() / run_heartbeat() — never acquires the lock directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as datetime_time, timezone, timedelta
from typing import Any, Callable

from agent.proactive_settings import (
    ProactiveSettings, get_intensity_profile, load_proactive_settings,
)

logger = logging.getLogger(__name__)

# v3.1 §5.1: Known eligibility block reasons (for metrics).
ELIGIBILITY_BLOCK_PROACTIVE_DISABLED = "proactive_disabled"
ELIGIBILITY_BLOCK_BRIDGE_OFFLINE = "bridge_offline"
ELIGIBILITY_BLOCK_DAILY_CAP = "daily_cap_exceeded"
ELIGIBILITY_BLOCK_USER_OPT_OUT = "user_opt_out"
ELIGIBILITY_BLOCK_SCHEDULER_BUSY = "scheduler_busy"
ELIGIBILITY_BLOCK_SLEEP_HOURS = "sleep_hours"


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _visible_transcript_items(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return user-visible chat bubbles from raw history.

    Old proactive history rows may contain several delivered bubbles joined with
    newlines. Split those rows so context metadata follows the same bubble
    boundaries the user saw.
    """
    visible: list[dict[str, Any]] = []
    for item in history or []:
        role = str(item.get("role", ""))
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content", ""))
        if not content.strip():
            continue
        source = str(item.get("source", ""))
        parts = [content]
        if role == "assistant" and source == "assistant:proactive" and "\n" in content:
            parts = [p.strip() for p in content.splitlines() if p.strip()]
        for part in parts:
            cloned = dict(item)
            cloned["role"] = role
            cloned["content"] = part
            visible.append(cloned)
    return visible


class ProactiveScheduler:
    """主动消息调度器。

    Lock ownership rule: ProactiveScheduler internally manages the lock.
    External callers (main.py) must NOT acquire `_lock` directly.
    admin trigger and heartbeat share the same lock and are mutually exclusive.
    """

    # ── v4.0: Cold-start timing ────────────────────────────────────
    COLD_START_SECONDS: int = 120  # hardcoded 2 minutes

    def __init__(
        self,
        chat_agent: Any = None,
        config: dict[str, Any] | None = None,
        store: Any = None,
        bridge_online_check: Any = None,
        settings_provider: Any = None,
    ):
        self._lock = asyncio.Lock()
        self._active_proactive_turn_id: str | None = None
        self._chat_agent = chat_agent
        self._config = config or {}
        self._store = store
        self._bridge_online_check = bridge_online_check  # callable → bool
        self._settings_provider = settings_provider
        # ── Phase 5 metrics ──
        self.heartbeat_tick_count: int = 0
        self.eligibility_pass_count: int = 0
        self.eligibility_fail_count: int = 0
        self.eligibility_fail_reasons: dict[str, int] = {}

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    def _get_settings(self) -> ProactiveSettings:
        """Get current proactive settings. Falls back to config.yaml."""
        if self._settings_provider:
            try:
                return self._settings_provider()
            except Exception:
                pass
        cfg_enabled = bool((self._config.get("features") or {}).get("proactive", False))
        quiet_cfg = (self._config.get("proactive") or {}).get("eligibility", {}).get("sleep_hours", {})
        return ProactiveSettings(
            enabled=cfg_enabled,
            autonomous_enabled=cfg_enabled,
            autonomous_intensity="daily",
            quiet_hours_enabled=True,
            quiet_hours_start=str(quiet_cfg.get("start", "00:00")),
            quiet_hours_end=str(quiet_cfg.get("end", "08:00")),
            daily_active_message_cap=int(
                (self._config.get("proactive") or {}).get("eligibility", {}).get("daily_cap", 50)
            ),
            scheduled_enabled=True,
            scheduled_morning_enabled=True,
            scheduled_morning_time="08:00",
            scheduled_night_enabled=True,
            scheduled_night_time="23:00",
        )

    # ── Manual trigger (admin API) ────────────────────────────────

    async def run_manual_trigger(
        self,
        user_id: str,
        impulse_meta: dict[str, Any],
        bot_id: str,
        *,
        history_provider: Any = None,
        system_prompt_provider: Any = None,
        wechat_sender_provider: Any = None,
        interrupt_checker_provider: Any = None,
        input_latest_seq: int = 0,
        write_model_history: Any = None,
        post_turn_hooks: list[Any] | None = None,
    ) -> dict[str, Any]:
        """admin 手动触发入口。内部负责锁。

        v3.1 §4.1: If lock is already held (another proactive turn running),
        returns {"status": "skipped", "reason": "active_proactive_turn_running"}.
        No queuing, no concurrency.
        """
        if self._lock.locked():
            return {"status": "skipped", "reason": "active_proactive_turn_running"}

        async with self._lock:
            turn_id = f"proactive_{user_id}_{int(time.time() * 1000)}"
            self._active_proactive_turn_id = turn_id
            logger.info("proactive_scheduler: manual trigger start turn_id=%s", turn_id)
            try:
                result = await self._execute_proactive_turn(
                    user_id=user_id,
                    impulse_meta=impulse_meta,
                    bot_id=bot_id,
                    turn_id=turn_id,
                    history_provider=history_provider,
                    system_prompt_provider=system_prompt_provider,
                    wechat_sender_provider=wechat_sender_provider,
                    interrupt_checker_provider=interrupt_checker_provider,
                    input_latest_seq=input_latest_seq,
                    write_model_history=write_model_history,
                    post_turn_hooks=post_turn_hooks,
                )
                return {"status": "ok", "result": result, "interrupt_baseline": input_latest_seq}
            except Exception as e:
                logger.error("proactive_scheduler: manual trigger failed: %s", e)
                return {"status": "error", "reason": str(e)[:500]}
            finally:
                self._active_proactive_turn_id = None

    async def _execute_proactive_turn(
        self,
        user_id: str,
        impulse_meta: dict[str, Any],
        bot_id: str,
        turn_id: str,
        *,
        history_provider: Any = None,
        system_prompt_provider: Any = None,
        wechat_sender_provider: Any = None,
        interrupt_checker_provider: Any = None,
        input_latest_seq: int = 0,
        write_model_history: Any = None,
        post_turn_hooks: list[Any] | None = None,
    ) -> Any:
        """Create and execute a single proactive turn via ChatAgent.

        v3.1 §4.3: user_message=None (not empty string).
        Source = SOURCE_PROACTIVE_IMPULSE.
        turn_id is generated by the caller (run_manual_trigger / run_heartbeat).
        """
        from agent.state_machine import SOURCE_PROACTIVE_IMPULSE

        # Resolve providers
        if history_provider and callable(history_provider):
            history = await history_provider(user_id, bot_id)
        elif hasattr(history_provider, "__call__"):
            history = await history_provider(user_id, bot_id)
        else:
            history = []

        if system_prompt_provider and callable(system_prompt_provider):
            system_prompt = await system_prompt_provider(user_id)
        elif hasattr(system_prompt_provider, "__call__"):
            system_prompt = await system_prompt_provider(user_id)
        else:
            system_prompt = ""

        if wechat_sender_provider and callable(wechat_sender_provider):
            wechat_sender = wechat_sender_provider
        else:
            wechat_sender = None

        if interrupt_checker_provider and callable(interrupt_checker_provider):
            interrupt_checker = interrupt_checker_provider
        else:
            interrupt_checker = None

        if self._chat_agent is None:
            raise RuntimeError("ProactiveScheduler has no chat_agent")

        result = await self._chat_agent.chat(
            user_message=None,
            history=history,
            trigger="proactive",
            source=SOURCE_PROACTIVE_IMPULSE,
            bot_id=bot_id,
            system_prompt=system_prompt,
            wechat_sender=wechat_sender,
            proactive_meta=impulse_meta,
            input_latest_seq=input_latest_seq,
            interrupt_checker=interrupt_checker,
            write_model_history=write_model_history,
            post_turn_hooks=post_turn_hooks or [],
        )
        return result

    # v4.0 removed: Phase 5: Heartbeat + eligibility
    # v4.0 removed: run_heartbeat

    # v4.0 removed: run_scheduled_heartbeat

    # v4.0 removed: _claim_next_scheduled_impulse

    def _scheduled_due_datetime(self, now: datetime, hhmm: str) -> datetime:
        try:
            due_t = datetime_time.fromisoformat(str(hhmm))
        except Exception:
            due_t = datetime_time(hour=8)
        return datetime.combine(now.date(), due_t, tzinfo=now.tzinfo)

    def _quiet_hours_end_datetime(self, due_dt: datetime, quiet_end: str) -> datetime:
        try:
            end_t = datetime_time.fromisoformat(str(quiet_end))
        except Exception:
            end_t = datetime_time(hour=8)
        end_dt = datetime.combine(due_dt.date(), end_t, tzinfo=due_dt.tzinfo)
        if end_dt < due_dt:
            end_dt += timedelta(days=1)
        return end_dt

    # v4.0 removed: _build_default_scheduled_impulse

    # v4.0 removed: _build_dynamic_scheduled_impulse

    async def _mark_dynamic_scheduled_result(self, task_id: str, result: Any) -> None:
        try:
            delivered = int(getattr(result, "proactive_delivered_count", 0) or 0)
            terminal = str(getattr(result, "terminal_state", ""))
            if delivered > 0:
                await self._store.mark_scheduled_message_status(task_id, "sent", "")
            else:
                await self._store.mark_scheduled_message_status(task_id, "failed", terminal[:200])
        except Exception:
            pass

    async def _check_eligibility(
        self, user_id: str, bot_id: str,
    ) -> tuple[bool, str]:
        """Run eligibility pre-checks (no LLM).

        v4.0: Frequency is controlled by cold-start 2min + intensity range.
        No optional/required thresholds.

        Returns (eligible: bool, block_reason: str).
        """
        settings = self._get_settings()

        # 1. Master switch
        if not settings.enabled:
            return False, ELIGIBILITY_BLOCK_PROACTIVE_DISABLED

        # 2. Autonomous sub-switch
        if not settings.autonomous_enabled:
            return False, "autonomous_disabled"

        # 3. Bridge online
        if self._bridge_online_check and callable(self._bridge_online_check):
            try:
                if not self._bridge_online_check():
                    return False, ELIGIBILITY_BLOCK_BRIDGE_OFFLINE
            except Exception:
                pass

        # 4. Quiet hours
        if settings.quiet_hours_enabled:
            quiet_cfg = {"start": settings.quiet_hours_start, "end": settings.quiet_hours_end}
            if self._in_sleep_hours(quiet_cfg):
                return False, ELIGIBILITY_BLOCK_SLEEP_HOURS

        if self._store is None:
            return True, ""

        # 5. Daily active message cap (counts all proactive delivered messages)
        try:
            tz = timezone(timedelta(hours=8))
            now = datetime.now(tz)
            today_start = datetime.combine(now.date(), datetime_time.min, tzinfo=tz).timestamp()
            sent_today = await self._store.count_today_proactive_sent(today_start)
            cap = int(settings.daily_active_message_cap)
            if sent_today >= cap:
                return False, ELIGIBILITY_BLOCK_DAILY_CAP
        except Exception:
            pass

        # 6. Daily autonomous trigger cap (counts successful autonomous triggers)
        try:
            intensity = settings.autonomous_intensity
            profile = get_intensity_profile(intensity)
            trigger_cap = int(profile.get("daily_trigger_cap", 5))
            count, _ = await self._store.get_autonomous_trigger_count(user_id, bot_id)
            if count >= trigger_cap:
                return False, "autonomous_trigger_cap_reached"
        except Exception:
            pass

        # 7. User opt-out (proactive_paused_until)
        try:
            ds = await self._store.get_dialogue_state(user_id, bot_id)
            paused_until = float(ds.get("proactive_paused_until", 0)) if ds else 0
            if paused_until > time.time():
                return False, ELIGIBILITY_BLOCK_USER_OPT_OUT
        except Exception:
            pass

        return True, ""

    def _in_sleep_hours(self, sleep_cfg: dict, *, debug_now: Any = None) -> bool:
        """Check if current time is within configured sleep hours.

        v3.1: debug_now (datetime|None) overrides real time for testing.
        """
        try:
            tz = timezone(timedelta(hours=8))
            if debug_now is not None:
                if not debug_now.tzinfo:
                    debug_now = debug_now.replace(tzinfo=tz)
                now = debug_now.time()
            else:
                now = datetime.now(tz).time()
            start_str = str(sleep_cfg.get("start", "23:00"))
            end_str = str(sleep_cfg.get("end", "08:00"))
            start = datetime_time.fromisoformat(start_str)
            end = datetime_time.fromisoformat(end_str)
            if start <= end:
                return start <= now <= end
            else:
                # Spans midnight (e.g. 23:00-08:00)
                return now >= start or now <= end
        except Exception:
            return False

    # v4.0 removed: v1.6-A trigger mode determination
    # v4.0 removed: _determine_trigger_mode

    def _eligibility_fail(self, reason: str) -> dict[str, Any]:
        """Record eligibility failure metric and return skipped result."""
        self.eligibility_fail_count += 1
        self.eligibility_fail_reasons[reason] = self.eligibility_fail_reasons.get(reason, 0) + 1
        return {"status": "skipped", "reason": reason}

    # v4.0 removed: Phase 6: Impulse generation / dedup / expire / score / select
    # v4.0 removed: _select_impulse

    # v4.0 removed: _generate_impulse_candidates

    # v4.0 removed: _resolve_presence_last_interaction

    # v4.0 removed: ECHO generation
    # v4.0 removed: _EMOTION_KEYWORDS
    # v4.0 removed: _generate_echo_impulses

    # v4.0 removed: TOPIC_CONTINUE generation
    # v4.0 removed: _generate_topic_impulses

    # v4.0 removed: PRESENCE impulse
    # v4.0 removed: _build_presence_impulse

    # v4.0 removed: SCHEDULED_RITUAL impulse
    # v4.0 removed: _build_ritual_impulse

    # v4.0 removed: v1.6-A required impulse
    # v4.0 removed: _build_required_impulse

    def _enrich_impulse_with_context(
        self, impulse: dict[str, Any], history: list[dict],
    ) -> None:
        """Add last_user_message and assistant_messages_after_last_user to impulse.

        Only uses visible conversation_history messages (role=user or role=assistant
        with non-fallback source). Dry-run / send failed / guard blocked content
        is not in conversation_history and therefore not included.
        """
        visible = _visible_transcript_items(history)
        user_msgs = [m for m in visible if m.get("role") == "user"]
        if user_msgs:
            last_user = user_msgs[-1]
            impulse["last_user_message"] = str(last_user.get("content", ""))[:500]

            # Find assistant messages after the last user message
            last_user_ts = float(last_user.get("ts", 0))
            assistant_after = [
                m for m in visible
                if m.get("role") == "assistant"
                and m.get("source") != "assistant:fallback"
                and float(m.get("ts", 0)) > last_user_ts
            ]
            impulse["assistant_messages_after_last_user"] = [
                str(m.get("content", ""))[:300]
                for m in assistant_after[-6:]  # max 6 messages, each ≤ 300 chars
            ]

        evidence = impulse.setdefault("evidence_pack", {})
        if isinstance(evidence, dict):
            recent_proactive = [
                {
                    "text": str(m.get("content", ""))[:300],
                    "ts": float(m.get("ts", 0) or 0),
                }
                for m in visible
                if m.get("role") == "assistant"
                and str(m.get("source", "")) == "assistant:proactive"
                and str(m.get("content", "")).strip()
            ]
            evidence["recent_proactive_messages"] = recent_proactive[-6:]

        # Forbidden claims for the model
        impulse["forbidden_claims"] = [
            "不要检测到用户很久没回复",
            "不要根据设置来找用户",
            "不要暴露主动消息触发机制",
            "不要像系统提醒或通知",
            "不要编造用户正在做什么",
        ]

    # v4.0 removed: Dedup / Expire / Score / Select pipeline
    # v4.0 removed: _dedup_candidates

    # v4.0 removed: _expire_candidates

    # v4.0 removed: _score_candidates

    # v4.0 removed: _select_best

    # ══════════════════════════════════════════════════════════════
    # Phase 7: Relationship temperature
    # ══════════════════════════════════════════════════════════════

    async def update_relationship_temperature(
        self, user_id: str, bot_id: str,
        event: str,
        delta: int = 0,
    ) -> float:
        """Update relationship temperature based on events.

        v3.1 §7.4:
          User replies to proactive message: +2
          User replies quickly: +3
          User cold reply: -1
          User long silence: -2
          User explicit rejection: -5 (enter quiet period)
          Normal chat sustained: +1
          Every 6h no interaction: decay 1 toward 0

        Returns new temperature value. Range [-10, +10].
        """
        if self._store is None:
            return 0.0

        cfg = self._config.get("proactive", {}).get("relationship", {})
        temp_max = int(cfg.get("temperature_max", 10))
        temp_min = int(cfg.get("temperature_min", -10))

        try:
            current = await self._store.get_relationship()
            current_temp = float(current.get("relationship_temperature", 0)) if current else 0
        except Exception:
            current_temp = 0

        # Apply decay (every 6h without interaction)
        decay_hours = int(cfg.get("temperature_decay_hours", 6))
        try:
            ds = await self._store.get_dialogue_state(user_id, bot_id)
            last_interaction = _safe_float(ds.get("last_visible_message_at", 0)) if ds else 0.0
            if last_interaction > 0:
                hours_since = (time.time() - last_interaction) / 3600
                decay_steps = int(hours_since / decay_hours)
                if decay_steps > 0:
                    # Decay toward 0
                    if current_temp > 0:
                        current_temp = max(0, current_temp - decay_steps)
                    elif current_temp < 0:
                        current_temp = min(0, current_temp + decay_steps)
        except Exception:
            pass

        # Apply event delta
        new_temp = current_temp + delta
        new_temp = max(temp_min, min(temp_max, new_temp))

        try:
            await self._store.update_relationship({
                "relationship_temperature": new_temp,
            })
        except Exception:
            pass

        logger.info(
            "proactive_scheduler: relationship_temp %s: %.1f → %.1f (event=%s delta=%d)",
            user_id, current_temp, new_temp, event, delta,
        )
        return new_temp

    async def attribute_user_reply_to_proactive(
        self, user_id: str, bot_id: str, user_message_ts: float,
    ) -> dict[str, Any] | None:
        """Check if a user message is a reply to a previous proactive message.

        v3.1 §7.5: Multi-condition check:
          1. last_proactive_id exists
          2. last proactive delivery_state is SENT_CONFIRMED or PARTIAL_SENT
          3. User message time within reply_window
          4. active_proactive_chain state is OPEN or COOLING

        Returns attribution info dict or None.
        """
        if self._store is None:
            return None

        try:
            ds = await self._store.get_dialogue_state(user_id, bot_id)
            if not ds:
                return None

            last_proactive_id = str(ds.get("last_proactive_id", ""))
            if not last_proactive_id:
                return None

            # Check reply window (default 4h)
            last_proactive_ts = float(ds.get("last_proactive_at", 0))
            if last_proactive_ts <= 0:
                return None

            reply_window = int(
                self._config.get("proactive", {})
                .get("attribution", {})
                .get("reply_window_seconds", {}).get("ECHO", 14400)
            )
            if (user_message_ts - last_proactive_ts) > reply_window:
                return None

            return {
                "is_reply_to_proactive": True,
                "proactive_origin_id": last_proactive_id,
                "proactive_chain_id": str(ds.get("active_proactive_chain", "")),
                "reply_delay_seconds": user_message_ts - last_proactive_ts,
            }
        except Exception:
            return None

    # ══════════════════════════════════════════════════════════════
    # Phase 7: Metrics snapshot (for admin API)
    # ══════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════
    # v3.1: Debug-only methods (proactive test console)
    # ══════════════════════════════════════════════════════════════

    async def run_debug_eligibility(
        self,
        user_id: str,
        bot_id: str,
        *,
        debug_now: Any = None,                      # datetime | None
        simulate_user_silent_seconds: int | None = None,
        simulate_last_proactive_seconds: int | None = None,
    ) -> dict[str, Any]:
        """v1.6-A: Run eligibility check for debug console. Does NOT call LLM.

        Removed: user message quiet window, min_interval.
        Added: settings-based checks, autonomous trigger cap, last_visible_message_at,
        trigger_mode determination.

        Returns {"eligible": bool, "checks": [...], "final_reason": str,
                 "last_visible_message_at": float, "elapsed_since_last_visible_seconds": int,
                 "trigger_mode": str | None, "autonomous_trigger_count_today": int,
                 "daily_autonomous_trigger_cap": int, "daily_active_message_cap": int,
                 "intensity": str}.
        """
        checks: list[dict[str, Any]] = []
        settings = self._get_settings()

        # 1. Master switch
        checks.append({
            "name": "proactive_master_switch",
            "passed": settings.enabled,
            "actual": settings.enabled,
            "required": True,
            "reason": None if settings.enabled else "proactive master switch is off",
        })

        # 2. Autonomous sub-switch
        checks.append({
            "name": "autonomous_enabled",
            "passed": settings.autonomous_enabled,
            "actual": settings.autonomous_enabled,
            "required": True,
            "reason": None if settings.autonomous_enabled else "autonomous sub-switch is off",
        })

        # 3. Bridge online
        bridge_ok = True
        if self._bridge_online_check and callable(self._bridge_online_check):
            try:
                bridge_ok = self._bridge_online_check()
            except Exception:
                bridge_ok = False
        checks.append({
            "name": "bridge_online",
            "passed": bridge_ok,
            "actual": bridge_ok,
            "required": True,
            "reason": None if bridge_ok else "WeChat bridge is offline",
        })

        # 4. Quiet hours
        quiet_cfg = {}
        if settings.quiet_hours_enabled:
            quiet_cfg = {"start": settings.quiet_hours_start, "end": settings.quiet_hours_end}
        in_sleep = bool(quiet_cfg and self._in_sleep_hours(quiet_cfg, debug_now=debug_now))
        checks.append({
            "name": "quiet_hours",
            "passed": not in_sleep,
            "actual": in_sleep,
            "required": False,
            "reason": "current time is in quiet hours" if in_sleep else None,
        })

        # 5. Daily active message cap
        daily_msg_cap = int(settings.daily_active_message_cap)
        daily_msg_passed = True
        daily_msg_actual = "N/A"
        if self._store:
            try:
                tz = timezone(timedelta(hours=8))
                effective_dt = debug_now if debug_now else datetime.now(tz)
                if not effective_dt.tzinfo:
                    effective_dt = effective_dt.replace(tzinfo=tz)
                today_start = datetime.combine(effective_dt.date(), datetime_time.min, tzinfo=tz).timestamp()
                sent_today = await self._store.count_today_proactive_sent(today_start)
                daily_msg_passed = sent_today < daily_msg_cap
                daily_msg_actual = f"{sent_today} / {daily_msg_cap}"
            except Exception:
                pass
        checks.append({
            "name": "daily_active_message_cap",
            "passed": daily_msg_passed,
            "actual": daily_msg_actual,
            "required": f"< {daily_msg_cap}",
            "reason": None if daily_msg_passed else "daily message cap reached",
        })

        # 6. Daily autonomous trigger cap
        trigger_cap = 5
        trigger_count = 0
        if self._store:
            try:
                intensity = settings.autonomous_intensity
                profile = get_intensity_profile(intensity)
                trigger_cap = int(profile.get("daily_trigger_cap", 5))
                trigger_count, _ = await self._store.get_autonomous_trigger_count(user_id, bot_id)
            except Exception:
                pass
        trigger_passed = trigger_count < trigger_cap
        checks.append({
            "name": "daily_autonomous_trigger_cap",
            "passed": trigger_passed,
            "actual": f"{trigger_count} / {trigger_cap}",
            "required": f"< {trigger_cap}",
            "reason": None if trigger_passed else "autonomous trigger cap reached",
        })

        # 7. User opt-out
        optout_passed = True
        if self._store:
            try:
                ds = await self._store.get_dialogue_state(user_id, bot_id)
                paused_until = float(ds.get("proactive_paused_until", 0)) if ds else 0
                effective_now = debug_now.timestamp() if debug_now else time.time()
                optout_passed = paused_until <= effective_now
            except Exception:
                pass
        checks.append({
            "name": "user_opt_out",
            "passed": optout_passed,
            "actual": "paused" if not optout_passed else "not paused",
            "required": "not paused",
            "reason": None if optout_passed else "user has opted out",
        })

        # 8. Scheduler busy
        busy = self._lock.locked()
        checks.append({
            "name": "scheduler_busy",
            "passed": not busy,
            "actual": busy,
            "required": False,
            "reason": "another proactive turn is running" if busy else None,
        })

        # ── last_visible_message_at + trigger mode ──
        last_visible = 0.0
        elapsed_s = 0
        trigger_mode = None
        if self._store:
            try:
                if hasattr(self._store, "resolve_last_visible_message_at"):
                    resolved = await self._store.resolve_last_visible_message_at(user_id, bot_id)
                    last_visible = _safe_float(resolved.get("timestamp"), 0.0)
                    last_visible_reason = str(resolved.get("reason", ""))
                else:
                    last_visible = _safe_float(
                        await self._store.get_last_visible_message_at(user_id, bot_id),
                        0.0,
                    )
                    last_visible_reason = "existing_last_visible_message_at" if last_visible > 0 else "missing_last_visible_message_at"
                if last_visible > 0:
                    effective_now_ts = debug_now.timestamp() if debug_now else time.time()
                    if simulate_user_silent_seconds is not None:
                        last_visible = effective_now_ts - simulate_user_silent_seconds
                    elapsed_s = int(effective_now_ts - last_visible)
                    # v4.0: cold-start at hardcoded 2 minutes
                    if elapsed_s >= self.COLD_START_SECONDS:
                        trigger_mode = "cold_start"
                    else:
                        trigger_mode = None
            except Exception:
                pass

        all_passed = all(c["passed"] for c in checks)
        failed = [c for c in checks if not c["passed"]]
        final_reason = failed[0]["reason"] if failed else ""

        return {
            "eligible": all_passed,
            "checks": checks,
            "final_reason": final_reason,
            "last_visible_message_at": last_visible,
            "last_visible_message_reason": locals().get("last_visible_reason", ""),
            "elapsed_since_last_visible_seconds": elapsed_s,
            "trigger_mode": trigger_mode,
            "autonomous_trigger_count_today": trigger_count,
            "daily_autonomous_trigger_cap": trigger_cap,
            "daily_active_message_cap": daily_msg_cap,
            "intensity": settings.autonomous_intensity,
        }

    # v4.0 removed: run_debug_impulse_pipeline

    # v4.0 removed: _generate_forced_impulse_candidates

    async def run_debug_turn(
        self,
        user_id: str,
        bot_id: str,
        *,
        impulse_meta: dict[str, Any],
        chat_agent_kwargs: dict[str, Any],
        input_latest_seq: int = 0,
    ) -> dict[str, Any]:
        """Execute a proactive turn under the scheduler lock (debug-only).

        v2.1: shares _lock with run_manual_trigger / run_heartbeat.
        Dry-run and real-send both use the scheduler lock to prevent
        concurrent proactive turns for the same user.

        chat_agent_kwargs contains: history, system_prompt, wechat_sender,
        write_model_history, post_turn_hooks, trigger, interrupt_checker.
        """
        if self._lock.locked():
            return {"status": "skipped", "reason": "active_proactive_turn_running"}

        async with self._lock:
            turn_id = f"debug_{user_id}_{int(time.time() * 1000)}"
            self._active_proactive_turn_id = turn_id
            logger.info("proactive_scheduler: debug turn start turn_id=%s", turn_id)
            try:
                from agent.state_machine import SOURCE_PROACTIVE_IMPULSE

                result = await self._chat_agent.chat(
                    user_message=None,
                    source=SOURCE_PROACTIVE_IMPULSE,
                    proactive_meta=impulse_meta,
                    input_latest_seq=input_latest_seq,
                    **chat_agent_kwargs,
                )
                return {
                    "status": "ok",
                    "result": result,
                    "turn_id": turn_id,
                    "interrupt_baseline": input_latest_seq,
                }
            except Exception as e:
                logger.error("proactive_scheduler: debug turn failed: %s", e)
                return {
                    "status": "error",
                    "reason": str(e)[:500],
                    "turn_id": turn_id,
                }
            finally:
                self._active_proactive_turn_id = None

    # ══════════════════════════════════════════════════════════════

    def get_metrics(self) -> dict[str, Any]:
        """Return current scheduler metrics snapshot for admin API."""
        return {
            "heartbeat_tick_count": self.heartbeat_tick_count,
            "eligibility_pass_count": self.eligibility_pass_count,
            "eligibility_fail_count": self.eligibility_fail_count,
            "eligibility_fail_reasons": dict(self.eligibility_fail_reasons),
            "is_busy": self.is_busy,
            "active_turn_id": self._active_proactive_turn_id,
        }
