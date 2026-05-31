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
ELIGIBILITY_BLOCK_USER_QUIET = "user_message_quiet_window"
ELIGIBILITY_BLOCK_DAILY_CAP = "daily_cap_exceeded"
ELIGIBILITY_BLOCK_USER_OPT_OUT = "user_opt_out"
ELIGIBILITY_BLOCK_SCHEDULER_BUSY = "scheduler_busy"
ELIGIBILITY_BLOCK_MIN_INTERVAL = "min_interval_not_met"
ELIGIBILITY_BLOCK_SLEEP_HOURS = "sleep_hours"


class ProactiveScheduler:
    """主动消息调度器。

    Lock ownership rule: ProactiveScheduler internally manages the lock.
    External callers (main.py) must NOT acquire `_lock` directly.
    admin trigger and heartbeat share the same lock and are mutually exclusive.
    """

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
        return load_proactive_settings(self._config)

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
                    write_model_history=write_model_history,
                    post_turn_hooks=post_turn_hooks,
                )
                return {"status": "ok", "result": result}
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
            interrupt_checker=interrupt_checker,
            write_model_history=write_model_history,
            post_turn_hooks=post_turn_hooks or [],
        )
        return result

    # ══════════════════════════════════════════════════════════════
    # Phase 5: Heartbeat + eligibility
    # ══════════════════════════════════════════════════════════════

    async def run_heartbeat(
        self,
        user_id: str,
        bot_id: str,
        *,
        history_provider: Any = None,
        system_prompt_provider: Any = None,
        wechat_sender_provider: Any = None,
        interrupt_checker_provider: Any = None,
        write_model_history: Any = None,
        post_turn_hooks: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Heartbeat entry point. Runs eligibility pre-checks, then executes if eligible.

        v3.1 §5.1-5.2: System pre-checks run BEFORE any LLM call.
        Internal lock ensures mutual exclusion with manual triggers.
        """
        self.heartbeat_tick_count += 1

        # ── Fast pre-checks (no store access) ────────────────────
        settings = self._get_settings()
        if not settings.enabled:
            return self._eligibility_fail(ELIGIBILITY_BLOCK_PROACTIVE_DISABLED)

        if self._lock.locked():
            return self._eligibility_fail(ELIGIBILITY_BLOCK_SCHEDULER_BUSY)

        async with self._lock:
            eligible, reason = await self._check_eligibility(user_id, bot_id)
            if not eligible:
                return self._eligibility_fail(reason)

            self.eligibility_pass_count += 1

            # ── v1.6-A: determine trigger mode based on last_visible_message_at ──
            trigger_mode, block_reason, context_meta = await self._determine_trigger_mode(
                user_id, bot_id, settings,
            )
            if trigger_mode is None:
                return {"status": "skipped", "reason": block_reason or "too_early"}

            # ── Build impulse ──
            if trigger_mode == "required":
                impulse = self._build_required_impulse(user_id, context_meta)
            else:
                impulse = await self._select_impulse(user_id, bot_id)
                if not impulse:
                    return {"status": "skipped", "reason": "no_impulse"}
                impulse["trigger_mode"] = "optional"
                impulse["trigger_kind"] = "autonomous"
                # Merge context_meta into impulse
                for k, v in (context_meta or {}).items():
                    impulse.setdefault(k, v)

            # Add context fields (last_user_message + assistant_messages_after_last_user)
            if self._store:
                try:
                    history = await self._store.get_recent_conversation_history(
                        user_id, limit=10, bot_id=bot_id,
                    )
                    self._enrich_impulse_with_context(impulse, history)
                except Exception:
                    pass

            turn_id = f"heartbeat_{user_id}_{int(time.time() * 1000)}"
            self._active_proactive_turn_id = turn_id
            logger.info("proactive_scheduler: heartbeat start turn_id=%s", turn_id)
            try:
                result = await self._execute_proactive_turn(
                    user_id=user_id,
                    impulse_meta=impulse,
                    bot_id=bot_id,
                    turn_id=turn_id,
                    history_provider=history_provider,
                    system_prompt_provider=system_prompt_provider,
                    wechat_sender_provider=wechat_sender_provider,
                    interrupt_checker_provider=interrupt_checker_provider,
                    write_model_history=write_model_history,
                    post_turn_hooks=post_turn_hooks,
                )
                return {"status": "ok", "result": result}
            except Exception as e:
                logger.error("proactive_scheduler: heartbeat turn failed: %s", e)
                return {"status": "error", "reason": str(e)[:500]}
            finally:
                self._active_proactive_turn_id = None

    async def _check_eligibility(
        self, user_id: str, bot_id: str,
    ) -> tuple[bool, str]:
        """Run all eligibility pre-checks (no LLM).

        v1.6-A: User message quiet window and min_interval removed.
        Frequency is now controlled by last_visible_message_at + intensity profile.
        Uses proactive settings (runtime_settings.json or config.yaml fallback).

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

    # ══════════════════════════════════════════════════════════════
    # v1.6-A: trigger mode determination
    # ══════════════════════════════════════════════════════════════

    async def _determine_trigger_mode(
        self, user_id: str, bot_id: str, settings: ProactiveSettings,
    ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        """Based on last_visible_message_at + intensity profile.

        Returns (trigger_mode, block_reason, context_meta):
          - ("optional", None, {...})  → trigger allowed, skip_proactive OK
          - ("required", None, {...})  → trigger required, skip_proactive forbidden
          - (None, "too_early", None)  → not yet time
          - (None, "no_visible_message_yet", None) → no visible message recorded
        """
        if self._store is None:
            return (None, "no_store", None)

        try:
            last_visible = await self._store.get_last_visible_message_at(user_id, bot_id)
        except Exception:
            last_visible = 0.0

        if last_visible <= 0:
            return (None, "no_visible_message_yet", None)

        elapsed = time.time() - last_visible
        intensity = settings.autonomous_intensity
        profile = get_intensity_profile(intensity)

        optional_after = int(profile.get("optional_after_minutes", 120)) * 60
        required_after = int(profile.get("required_after_minutes", 180)) * 60

        # Build context meta used for proactive turn context
        context_meta = {
            "last_visible_message_at": last_visible,
            "elapsed_since_last_visible_message_seconds": int(elapsed),
            "trigger_kind": "autonomous",
        }

        if elapsed >= required_after:
            context_meta["trigger_mode"] = "required"
            context_meta["trigger_source"] = "REQUIRED_PRESENCE"
            return ("required", None, context_meta)
        elif elapsed >= optional_after:
            context_meta["trigger_mode"] = "optional"
            return ("optional", None, context_meta)  # trigger_source filled by impulse pipeline
        else:
            return (None, "too_early", None)

    def _eligibility_fail(self, reason: str) -> dict[str, Any]:
        """Record eligibility failure metric and return skipped result."""
        self.eligibility_fail_count += 1
        self.eligibility_fail_reasons[reason] = self.eligibility_fail_reasons.get(reason, 0) + 1
        return {"status": "skipped", "reason": reason}

    # ══════════════════════════════════════════════════════════════
    # Phase 6: Impulse generation / dedup / expire / score / select
    # ══════════════════════════════════════════════════════════════

    async def _select_impulse(
        self, user_id: str, bot_id: str,
    ) -> dict[str, Any] | None:
        """Select the best impulse candidate via rule-based pipeline.

        v3.1 §6: No LLM calls — pure rule-based generation.
        Steps: generate → dedup → expire → score → select best.
        """
        candidates = await self._generate_impulse_candidates(user_id, bot_id)
        if not candidates:
            return None

        # Deduplicate by dedupe_key
        candidates = self._dedup_candidates(candidates)
        if not candidates:
            return None

        # Expire old candidates
        candidates = self._expire_candidates(candidates)
        if not candidates:
            return None

        # Score and select best
        scored = self._score_candidates(candidates)
        best = self._select_best(scored)
        if not best:
            return None

        logger.info(
            "proactive_scheduler: selected impulse type=%s score=%.1f candidates=%d",
            best.get("impulse_type"), best.get("_score", 0), len(candidates),
        )
        return best

    async def _generate_impulse_candidates(
        self, user_id: str, bot_id: str,
    ) -> list[dict[str, Any]]:
        """Generate impulse candidates from rule-based sources.

        v3.1 §6.1: Four impulse types, no LLM:
          ECHO — recent user messages contain emotional keywords
          TOPIC_CONTINUE — active_topics has unclosed topics
          PRESENCE — time since last interaction > threshold
          SCHEDULED_RITUAL — scheduled time reached

        Max 1 per type, max 3 total per check.
        """
        cfg = self._config.get("proactive", {})
        max_total = int(cfg.get("impulse", {}).get("max_candidates_per_check", 3))
        max_per_type = int(cfg.get("impulse", {}).get("max_per_type", 1))
        now_ts = time.time()
        tz = timezone(timedelta(hours=8))
        now_dt = datetime.now(tz)

        candidates: list[dict[str, Any]] = []

        # ── ECHO: recent user messages with emotional keywords ──
        if self._store and len(candidates) < max_total:
            try:
                history = await self._store.get_recent_conversation_history(
                    user_id, limit=6, bot_id=bot_id,
                )
                echo_candidates = self._generate_echo_impulses(
                    history, user_id, now_ts, max_per_type,
                )
                candidates.extend(echo_candidates[:max_per_type])
            except Exception as e:
                logger.warning("proactive_scheduler: ECHO generation failed: %s", e)

        # ── TOPIC_CONTINUE: unclosed active topics ──
        # Phase 6: topics are looked up individually since store has get_active_topic(id).
        # Phase 7+ can add a list-active-topics query.
        if self._store and len(candidates) < max_total:
            try:
                # Try to get dialogue_state's last_active_topic_id
                ds = await self._store.get_dialogue_state(user_id, bot_id)
                topic_id = str(ds.get("active_topic_id", "")) if ds else ""
                if topic_id:
                    topic = await self._store.get_active_topic(topic_id)
                    if topic and not topic.get("closed_at"):
                        topic_candidates = self._generate_topic_impulses(
                            [topic], user_id, now_ts, max_per_type,
                        )
                        candidates.extend(topic_candidates[:max_per_type])
            except Exception as e:
                logger.warning("proactive_scheduler: TOPIC_CONTINUE generation failed: %s", e)

        # ── PRESENCE: time since last interaction ──
        if len(candidates) < max_total:
            try:
                contact = await self._store.get_contact_state() if self._store else {}
                # Check last interaction time from dialogue state
                if self._store:
                    ds = await self._store.get_dialogue_state(user_id, bot_id)
                    last_interaction = max(
                        float(ds.get("last_user_message_at", 0)),
                        float(ds.get("last_assistant_message_at", 0)),
                    ) if ds else 0
                else:
                    last_interaction = 0

                presence_threshold_s = 3600  # 1 hour default
                if last_interaction > 0 and (now_ts - last_interaction) > presence_threshold_s:
                    candidates.append(self._build_presence_impulse(
                        user_id, now_ts, last_interaction,
                    ))
            except Exception as e:
                logger.warning("proactive_scheduler: PRESENCE generation failed: %s", e)

        # ── SCHEDULED_RITUAL: due-time check ──
        if self._store and len(candidates) < max_total:
            try:
                ps = await self._store.get_proactive_schedule_state(user_id, bot_id)
                next_due = float(ps.get("next_due_ts", 0)) if ps else 0
                if next_due > 0 and now_ts >= next_due:
                    candidates.append(self._build_ritual_impulse(
                        user_id, now_ts, ps,
                    ))
            except Exception as e:
                logger.warning("proactive_scheduler: SCHEDULED_RITUAL generation failed: %s", e)

        return candidates[:max_total]

    # ── ECHO generation ─────────────────────────────────────────

    _EMOTION_KEYWORDS = [
        "开心", "难过", "烦", "累", "困", "无聊", "生气", "焦虑",
        "紧张", "兴奋", "期待", "失望", "孤独", "想", "好想",
        "哈哈", "唉", "哎", "嗯", "……",
    ]

    def _generate_echo_impulses(
        self, history: list[dict], user_id: str, now_ts: float, max_count: int,
    ) -> list[dict[str, Any]]:
        """Generate ECHO impulses from recent user messages containing emotional keywords."""
        candidates = []
        recent_user_msgs = [
            m for m in history[-6:]
            if m.get("role") == "user"
        ]
        for msg in recent_user_msgs[-3:]:  # last 3 user messages
            content = str(msg.get("content", ""))[:200]
            ts = float(msg.get("ts", 0))
            if any(kw in content for kw in self._EMOTION_KEYWORDS):
                candidates.append({
                    "impulse_id": f"echo_{user_id}_{int(ts * 1000) if ts else int(now_ts * 1000)}",
                    "impulse_type": "ECHO",
                    "proactive_chain_id": f"chain_echo_{int(now_ts * 1000)}",
                    "trigger_reason_summary": f"用户最近消息包含情绪表达：{content[:60]}",
                    "evidence_pack": {
                        "source_topic_id": "",
                        "source_message_ids": [str(msg.get("id", ""))],
                        "recent_proactive_messages": [],
                        "relationship_temperature": None,
                        "topic_snapshot": f"最近用户消息：{content[:80]}",
                    },
                    "dedupe_key": f"echo_{user_id}_{ts}",
                    "reply_window_seconds": 14400,
                    "max_wechat_reply": int(
                        self._config.get("proactive", {})
                        .get("turn", {})
                        .get("default_max_wechat_reply_per_turn", 3)
                    ),
                    "_source_ts": ts,
                })
                if len(candidates) >= max_count:
                    break
        return candidates

    # ── TOPIC_CONTINUE generation ────────────────────────────────

    def _generate_topic_impulses(
        self, topics: list[dict], user_id: str, now_ts: float, max_count: int,
    ) -> list[dict[str, Any]]:
        """Generate TOPIC_CONTINUE impulses from unclosed active topics."""
        candidates = []
        for topic in topics[:max_count]:
            topic_id = str(topic.get("id", ""))
            topic_name = str(topic.get("topic_name", ""))
            topic_snapshot = str(topic.get("snapshot", ""))
            candidates.append({
                "impulse_id": f"topic_{topic_id}_{int(now_ts * 1000)}",
                "impulse_type": "TOPIC_CONTINUE",
                "proactive_chain_id": f"chain_topic_{topic_id}",
                "trigger_reason_summary": f"未关闭的话题：{topic_name}",
                "evidence_pack": {
                    "source_topic_id": topic_id,
                    "source_message_ids": [],
                    "recent_proactive_messages": [],
                    "relationship_temperature": None,
                    "topic_snapshot": topic_snapshot[:200] if topic_snapshot else topic_name,
                },
                "dedupe_key": f"topic_{user_id}_{topic_id}",
                "reply_window_seconds": 28800,
                "max_wechat_reply": int(
                    self._config.get("proactive", {})
                    .get("turn", {})
                    .get("default_max_wechat_reply_per_turn", 3)
                ),
                "_source_ts": now_ts,
            })
        return candidates

    # ── PRESENCE impulse ─────────────────────────────────────────

    def _build_presence_impulse(
        self, user_id: str, now_ts: float, last_interaction: float,
    ) -> dict[str, Any]:
        """Build a PRESENCE impulse when user has been away for a while."""
        gap_minutes = int((now_ts - last_interaction) / 60)
        return {
            "impulse_id": f"presence_{user_id}_{int(now_ts * 1000)}",
            "impulse_type": "PRESENCE",
            "proactive_chain_id": f"chain_presence_{int(now_ts * 1000)}",
            "trigger_reason_summary": f"距上次互动已过 {gap_minutes} 分钟，检查是否适合联系",
            "evidence_pack": {
                "source_topic_id": "",
                "source_message_ids": [],
                "recent_proactive_messages": [],
                "relationship_temperature": None,
                "topic_snapshot": "",
            },
            "dedupe_key": f"presence_{user_id}_{int(now_ts / 3600)}",  # hourly dedup
            "reply_window_seconds": 43200,
            "max_wechat_reply": int(
                self._config.get("proactive", {})
                .get("turn", {})
                .get("default_max_wechat_reply_per_turn", 3)
            ),
            "_source_ts": now_ts,
        }

    # ── SCHEDULED_RITUAL impulse ─────────────────────────────────

    def _build_ritual_impulse(
        self, user_id: str, now_ts: float, schedule_state: dict,
    ) -> dict[str, Any]:
        """Build a SCHEDULED_RITUAL impulse from due-time schedule."""
        return {
            "impulse_id": f"ritual_{user_id}_{int(now_ts * 1000)}",
            "impulse_type": "SCHEDULED_RITUAL",
            "proactive_chain_id": f"chain_ritual_{int(now_ts * 1000)}",
            "trigger_reason_summary": "定时触发的主动消息",
            "evidence_pack": {
                "source_topic_id": "",
                "source_message_ids": [],
                "recent_proactive_messages": [],
                "relationship_temperature": None,
                "topic_snapshot": str(schedule_state.get("intent_hint", "")),
            },
            "dedupe_key": f"ritual_{user_id}_{int(now_ts / 3600)}",
            "reply_window_seconds": 86400,
            "max_wechat_reply": int(
                self._config.get("proactive", {})
                .get("turn", {})
                .get("default_max_wechat_reply_per_turn", 3)
            ),
            "_source_ts": now_ts,
        }

    # ── v1.6-A: Required impulse + context enrichment ────────────

    def _build_required_impulse(
        self, user_id: str, context_meta: dict[str, Any],
    ) -> dict[str, Any]:
        """Build REQUIRED_PRESENCE impulse when elapsed >= required_after."""
        now_ts = time.time()
        return {
            "impulse_id": f"required_{user_id}_{int(now_ts * 1000)}",
            "impulse_type": "REQUIRED_PRESENCE",
            "proactive_chain_id": f"chain_required_{int(now_ts * 1000)}",
            "trigger_reason_summary": "超过当前强度的最迟触发时间",
            "trigger_mode": "required",
            "trigger_kind": "autonomous",
            "trigger_source": "REQUIRED_PRESENCE",
            "evidence_pack": {
                "source_topic_id": "",
                "source_message_ids": [],
                "recent_proactive_messages": [],
                "relationship_temperature": None,
                "topic_snapshot": "",
            },
            "dedupe_key": f"required_{user_id}_{int(now_ts / 3600)}",
            "reply_window_seconds": 43200,
            "max_wechat_reply": int(
                self._config.get("proactive", {})
                .get("turn", {})
                .get("default_max_wechat_reply_per_turn", 3)
            ),
            "last_visible_message_at": context_meta.get("last_visible_message_at", 0),
            "elapsed_since_last_visible_message_seconds": context_meta.get(
                "elapsed_since_last_visible_message_seconds", 0,
            ),
            "_source_ts": now_ts,
        }

    def _enrich_impulse_with_context(
        self, impulse: dict[str, Any], history: list[dict],
    ) -> None:
        """Add last_user_message and assistant_messages_after_last_user to impulse.

        Only uses visible conversation_history messages (role=user or role=assistant
        with non-fallback source). Dry-run / send failed / guard blocked content
        is not in conversation_history and therefore not included.
        """
        user_msgs = [m for m in history if m.get("role") == "user"]
        if user_msgs:
            last_user = user_msgs[-1]
            impulse["last_user_message"] = str(last_user.get("content", ""))[:500]

            # Find assistant messages after the last user message
            last_user_ts = float(last_user.get("ts", 0))
            assistant_after = [
                m for m in history
                if m.get("role") == "assistant"
                and m.get("source") != "assistant:fallback"
                and float(m.get("ts", 0)) > last_user_ts
            ]
            impulse["assistant_messages_after_last_user"] = [
                str(m.get("content", ""))[:300]
                for m in assistant_after[-6:]  # max 6 messages, each ≤ 300 chars
            ]

        # Forbidden claims for the model
        impulse["forbidden_claims"] = [
            "不要检测到用户很久没回复",
            "不要根据设置来找用户",
            "不要暴露主动消息触发机制",
            "不要像系统提醒或通知",
            "不要编造用户正在做什么",
        ]

    # ── Dedup / Expire / Score / Select ──────────────────────────

    def _dedup_candidates(
        self, candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove duplicate candidates by dedupe_key."""
        seen: set[str] = set()
        unique = []
        for c in candidates:
            key = str(c.get("dedupe_key", ""))
            if key and key not in seen:
                seen.add(key)
                unique.append(c)
        dedup_count = len(candidates) - len(unique)
        if dedup_count > 0:
            logger.info("proactive_scheduler: dedup removed %d candidates", dedup_count)
        return unique

    def _expire_candidates(
        self, candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Expire candidates past their TTL."""
        cfg = self._config.get("proactive", {})
        ttl_map = cfg.get("impulse", {}).get("default_ttl_hours", {
            "ECHO": 4, "TOPIC_CONTINUE": 8, "PRESENCE": 12, "SCHEDULED_RITUAL": 24,
        })
        now_ts = time.time()
        valid = []
        for c in candidates:
            impulse_type = str(c.get("impulse_type", ""))
            source_ts = float(c.get("_source_ts", now_ts))
            ttl_hours = float(ttl_map.get(impulse_type, 24))
            if (now_ts - source_ts) < (ttl_hours * 3600):
                valid.append(c)
        expired = len(candidates) - len(valid)
        if expired > 0:
            logger.info("proactive_scheduler: expired %d candidates", expired)
        return valid

    def _score_candidates(
        self, candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Score candidates with fixed base + adjustments.

        v3.1 §6.2: Simplified scoring:
          ECHO: base 60 + content length bonus (up to 20)
          TOPIC_CONTINUE: base 70 + topic freshness (up to 15)
          PRESENCE: base 50 + gap duration bonus (up to 30)
          SCHEDULED_RITUAL: base 80
        """
        now_ts = time.time()
        for c in candidates:
            impulse_type = str(c.get("impulse_type", ""))
            base = 50

            if impulse_type == "ECHO":
                base = 60
                content = str(c.get("evidence_pack", {}).get("topic_snapshot", ""))
                base += min(20, len(content) // 5)
            elif impulse_type == "TOPIC_CONTINUE":
                base = 70
                source_ts = float(c.get("_source_ts", now_ts))
                freshness_hours = (now_ts - source_ts) / 3600
                base += max(0, 15 - int(freshness_hours))
            elif impulse_type == "PRESENCE":
                base = 50
                source_ts = float(c.get("_source_ts", now_ts))
                gap_hours = (now_ts - source_ts) / 3600
                base += min(30, int(gap_hours * 2))
            elif impulse_type == "SCHEDULED_RITUAL":
                base = 80

            c["_score"] = float(base)

        return sorted(candidates, key=lambda c: c.get("_score", 0), reverse=True)

    def _select_best(
        self, scored: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Select the highest-scoring candidate. Removes _score and _source_ts."""
        if not scored:
            return None
        # Sort by score descending (defensive — caller may have already sorted)
        best = max(scored, key=lambda c: c.get("_score", 0))
        # Return a copy with internal fields cleaned
        result = {k: v for k, v in best.items() if k not in ("_score", "_source_ts")}
        return result

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
            last_interaction = max(
                float(ds.get("last_user_message_at", 0)),
                float(ds.get("last_assistant_message_at", 0)),
            ) if ds else 0
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
                last_visible = await self._store.get_last_visible_message_at(user_id, bot_id)
                if last_visible > 0:
                    effective_now_ts = debug_now.timestamp() if debug_now else time.time()
                    if simulate_user_silent_seconds is not None:
                        last_visible = effective_now_ts - simulate_user_silent_seconds
                    elapsed_s = int(effective_now_ts - last_visible)
                    intensity = settings.autonomous_intensity
                    profile = get_intensity_profile(intensity)
                    if elapsed_s >= profile.get("required_after_minutes", 180) * 60:
                        trigger_mode = "required"
                    elif elapsed_s >= profile.get("optional_after_minutes", 120) * 60:
                        trigger_mode = "optional"
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
            "elapsed_since_last_visible_seconds": elapsed_s,
            "trigger_mode": trigger_mode,
            "autonomous_trigger_count_today": trigger_count,
            "daily_autonomous_trigger_cap": trigger_cap,
            "daily_active_message_cap": daily_msg_cap,
            "intensity": settings.autonomous_intensity,
        }

    async def run_debug_impulse_pipeline(
        self,
        user_id: str,
        bot_id: str,
        *,
        forced_impulse_type: str = "AUTO",
        debug_now: Any = None,
    ) -> dict[str, Any]:
        """Run impulse generation pipeline for debug console. Does NOT call LLM.

        forced_impulse_type:
          "AUTO" → full pipeline (generate all types → dedup → expire → score → select)
          "ECHO" / "TOPIC_CONTINUE" / "PRESENCE" / "SCHEDULED_RITUAL" →
            only generate that type, still go through dedup/expire/score/select.

        Returns {"eligible": bool, "candidates": [...], "selected": {...}|None,
                 "filtering_dedup": {...}, "filtering_expire": {...},
                 "filtering_score": {...}, "filtering_threshold": {...},
                 "selection_reason": str}.
        """
        # Eligibility must pass first
        eligibility = await self.run_debug_eligibility(
            user_id, bot_id, debug_now=debug_now,
        )
        if not eligibility["eligible"]:
            return {
                "eligible": False,
                "candidates": [],
                "selected": None,
                "filtering_dedup": {},
                "filtering_expire": {},
                "filtering_score": {},
                "filtering_threshold": {},
                "selection_reason": eligibility["final_reason"],
            }

        # Generate candidates — if forced, only generate that type
        if forced_impulse_type == "AUTO":
            candidates = await self._generate_impulse_candidates(user_id, bot_id)
        else:
            candidates = await self._generate_forced_impulse_candidates(
                user_id, bot_id, forced_impulse_type,
            )

        if not candidates:
            return {
                "eligible": True,
                "candidates": [],
                "selected": None,
                "filtering_dedup": {"input": 0, "output": 0, "removed": []},
                "filtering_expire": {"input": 0, "output": 0, "removed": []},
                "filtering_score": {"input": 0, "scored": []},
                "filtering_threshold": {"min_score": 0, "passed": False},
                "selection_reason": "no candidates generated",
            }

        # Dedup
        before_dedup = len(candidates)
        candidates = self._dedup_candidates(candidates)
        dedup_info = {
            "input": before_dedup,
            "output": len(candidates),
            "removed": before_dedup - len(candidates),
        }

        # Expire
        before_expire = len(candidates)
        candidates = self._expire_candidates(candidates)
        expire_info = {
            "input": before_expire,
            "output": len(candidates),
            "removed": before_expire - len(candidates),
        }

        # Score
        scored = self._score_candidates(candidates)
        score_info = {
            "input": len(candidates),
            "scored": [
                {"type": c.get("impulse_type", "?"), "score": c.get("_score", 0)}
                for c in scored
            ],
        }

        # Threshold (min score = 0 for now, always pass)
        threshold_info = {"min_score": 0, "passed": len(scored) > 0}

        # Select best
        selected = self._select_best(scored)

        return {
            "eligible": True,
            "candidates": [
                {
                    "type": c.get("impulse_type", ""),
                    "score": c.get("_score", 0) if "_score" in c else None,
                    "reason": c.get("trigger_reason_summary", ""),
                    "selected": selected is not None and c is selected,
                }
                for c in candidates
            ],
            "selected": selected,
            "filtering_dedup": dedup_info,
            "filtering_expire": expire_info,
            "filtering_score": score_info,
            "filtering_threshold": threshold_info,
            "selection_reason": "" if selected else "no candidate passed score threshold",
        }

    async def _generate_forced_impulse_candidates(
        self, user_id: str, bot_id: str, impulse_type: str,
    ) -> list[dict[str, Any]]:
        """Generate candidates for a single forced impulse type.

        v2.1: Only limits the source type. The result still goes through
        dedup/expire/score/select in run_debug_impulse_pipeline().
        """
        now_ts = time.time()
        if impulse_type == "ECHO":
            if self._store:
                try:
                    history = await self._store.get_recent_conversation_history(
                        user_id, limit=6, bot_id=bot_id,
                    )
                    return self._generate_echo_impulses(history, user_id, now_ts, 3)
                except Exception:
                    pass
            return []
        elif impulse_type == "TOPIC_CONTINUE":
            if self._store:
                try:
                    ds = await self._store.get_dialogue_state(user_id, bot_id)
                    topic_id = str(ds.get("active_topic_id", "")) if ds else ""
                    if topic_id:
                        topic = await self._store.get_active_topic(topic_id)
                        if topic and not topic.get("closed_at"):
                            return self._generate_topic_impulses([topic], user_id, now_ts, 1)
                except Exception:
                    pass
            return []
        elif impulse_type == "PRESENCE":
            last_interaction = 0.0
            if self._store:
                try:
                    ds = await self._store.get_dialogue_state(user_id, bot_id)
                    last_interaction = max(
                        float(ds.get("last_user_message_at", 0)),
                        float(ds.get("last_assistant_message_at", 0)),
                    ) if ds else 0
                except Exception:
                    pass
            return [self._build_presence_impulse(user_id, now_ts, last_interaction)]
        elif impulse_type == "SCHEDULED_RITUAL":
            ps = {}
            if self._store:
                try:
                    ps = await self._store.get_proactive_schedule_state(user_id, bot_id) or {}
                except Exception:
                    pass
            return [self._build_ritual_impulse(user_id, now_ts, ps)]
        return []

    async def run_debug_turn(
        self,
        user_id: str,
        bot_id: str,
        *,
        impulse_meta: dict[str, Any],
        chat_agent_kwargs: dict[str, Any],
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
                    **chat_agent_kwargs,
                )
                return {
                    "status": "ok",
                    "result": result,
                    "turn_id": turn_id,
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
