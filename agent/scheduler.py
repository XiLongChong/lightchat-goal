"""Proactive messaging scheduler — V2.1.1 due-time + intent model.

V2.1.1 changes:
  - LLM no longer decides whether to send. Due-time schedule controls it.
  - Intent selection replaces candidate reason generation.
  - Only latest eligible topic may be followed up.
  - Broad memory/recap are NOT passed into proactive generation by default.

Pipeline stages:
  1. hard_precheck         — sleep hours, bridge, contact
  2. due_time_check        — next_due_ts has passed?
  3. load_latest_topic     — active topic from DB
  4. select_message_intent — rules-first, LLM fallback
  5. generate_message      — LLM with intent+constraints
  6. send_and_record       — send via bridge, record results
  7. schedule_next_due     — random next due time
"""

from __future__ import annotations

import asyncio
import logging
import random as _random
import time as _time_module
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from memory.store import MemoryStore

logger = logging.getLogger(__name__)

# Timezone for Chinese users
CST = timezone(timedelta(hours=8))

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

MINIMAL_INTENT_SELECTION_PROMPT = """选择一条主动消息意图。
当前时间: {now_str}
活跃话题: {topic_text}
话题状态: {topic_status}
已跟进次数: {followup_count}
只输出 JSON: {{"intent": "check_in", "reason": ""}}"""

MINIMAL_MESSAGE_GENERATION_PROMPT = """生成一条简短主动微信消息。
类型: {intent}
理由: {reason}
当前时间: {now_str}
活跃话题: {topic_text}
最近对话背景: {recent_context}
关系熟悉度: {familiarity}/10
规则: 不要假装用户刚说过话。{intent_rules}
只输出 JSON: {{"message": "", "emotion": "neutral"}}"""

INTENT_RULES = {
    "check_in": "简单打招呼或问在做什么，像朋友随口问一句",
    "latest_topic_followup": "跟进上面活跃话题的最新状态。不要重复话题历史，只问最新进展。不超过2次跟进",
    "self_share": "分享一个无害的小想法、观察、笑话或闲聊话题。不要声称你做了真人才能做的事。不要用旧话题",
    "light_ping": "极短、低压力，一句话以内。用户不回也没关系",
}

FALLBACK_MESSAGES = {
    "check_in": "在干嘛",
    "latest_topic_followup": "后来呢",
    "self_share": "突然想问你个事",
    "light_ping": "嗯",
}


class SchedulerAgent:
    """Periodic proactive messaging agent — V2.1.1 due-time model."""

    def __init__(
        self,
        store: MemoryStore,
        client: Any,  # AsyncOpenAI
        model: str,
        config: dict[str, Any] | None = None,
    ):
        self.store = store
        self.client = client
        self.model = model

        cfg = config or {}
        prompts_dir = cfg.get("prompts_dir", "")
        self.prompts_dir = Path(prompts_dir) if prompts_dir else PROMPTS_DIR
        self.intent_prompt_template = self._load_prompt_template(
            "scheduler_intent.md",
            MINIMAL_INTENT_SELECTION_PROMPT,
        )
        self.message_prompt_template = self._load_prompt_template(
            "scheduler_message.md",
            MINIMAL_MESSAGE_GENERATION_PROMPT,
        )
        self.sleep_start = cfg.get("sleep_start", 0)
        self.sleep_end = cfg.get("sleep_end", 7)
        self.max_ignored_streak = cfg.get("max_ignored_streak", 3)
        self.max_daily = cfg.get("max_daily_proactive", 5)

        # V2.1.1 proactive config
        pro_cfg = cfg.get("proactive", {})
        timing = pro_cfg.get("timing", {})
        self.min_interval = timing.get("min_interval_minutes", 30)
        self.max_interval = timing.get("max_interval_minutes", 60)
        gen_cfg = pro_cfg.get("generation", {})
        self.enabled_types = gen_cfg.get("enabled_types",
            ["check_in", "latest_topic_followup", "self_share", "light_ping"])
        self.latest_topic_only = gen_cfg.get("latest_topic_only", True)
        self.allow_llm_skip = gen_cfg.get("allow_llm_skip", False)
        topic_cfg = pro_cfg.get("topic", {})
        self.followup_max = topic_cfg.get("followup_max_count", 2)
        self.topic_expire_minutes = topic_cfg.get("default_expire_minutes", 240)

    def _load_prompt_template(self, filename: str, fallback: str) -> str:
        """Load a prompt template from the configured prompts directory."""
        path = self.prompts_dir / filename
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("Scheduler prompt missing, using fallback: %s", path)
            return fallback

    # ================================================================
    # Legacy compatibility (kept for existing callers)
    # ================================================================

    async def should_proactive_message(
        self,
        recent_memories: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Legacy path — delegates to V2.1.1 pipeline."""
        result = await self.run_proactive_pipeline(
            recent_memories=recent_memories,
        )
        if result.get("sent"):
            return {
                "should_message": True,
                "message": result.get("message", ""),
                "emotion": result.get("emotion", "neutral"),
                "reason": result.get("reason", ""),
            }
        return self._deny(result.get("reason", "unknown"))

    def _deny(self, reason: str) -> dict[str, Any]:
        return {
            "should_message": False,
            "message": "",
            "emotion": "neutral",
            "reason": reason,
        }

    # ================================================================
    # V2.1.1: Due-time + intent proactive pipeline
    # ================================================================

    async def run_proactive_pipeline(
        self,
        recent_memories: list[dict[str, Any]] | None = None,
        recap_text: str = "",
        bot_id: str = "",
        prerequisites: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """V2.1.1 pipeline: due-time check → intent → generate → send.

        No LLM "should send" decision. The scheduler guarantees one attempt
        after the random due time passes, unless hard blocks apply.

        Returns:
            {"sent": bool, "message": str, "intent": str,
             "reason": str, "stage": str}
        """
        now_cst = datetime.now(CST)
        now_ts = _time_module.time()

        # ---- Stage 1: hard_precheck ----
        prereq = prerequisites or {}
        pre_user_id = prereq.get("user_id", "")
        if not prereq.get("bot_active", True):
            await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
            return self._result(False, "bot not active", stage="pre_check")
        if not prereq.get("proactive_enabled", True):
            await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
            return self._result(False, "proactive disabled", stage="pre_check")
        if not prereq.get("bridge_available", True):
            await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
            return self._result(False, "bridge unavailable", stage="pre_check")
        if not prereq.get("valid_contact", True):
            await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
            return self._result(False, "no contact", stage="pre_check")

        hour = now_cst.hour
        if self.sleep_start < self.sleep_end:
            if self.sleep_start <= hour < self.sleep_end:
                await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
                return self._result(False, "sleep hours", stage="pre_check")
        else:
            if hour >= self.sleep_start or hour < self.sleep_end:
                await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
                return self._result(False, "sleep hours", stage="pre_check")

        # Daily cap
        today_start = datetime.combine(
            now_cst.date(),
            datetime.min.time(),
            tzinfo=CST,
        ).timestamp()
        sent_today = await self.store.count_today_proactive_sent(today_start)
        if sent_today >= self.max_daily:
            await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
            return self._result(False, f"daily cap ({sent_today}/{self.max_daily})", stage="pre_check")

        # ---- Stage 2: due_time_check ----
        user_id = prereq.get("user_id", "")
        sched_state = await self.store.get_proactive_schedule_state(
            user_id=user_id, bot_id=bot_id,
        )
        next_due = sched_state.get("next_due_ts", 0)

        if next_due == 0:
            # First time — schedule initial due time
            next_due = self._random_next_due(now_ts)
            await self.store.update_proactive_schedule_state(
                user_id=user_id, bot_id=bot_id,
                next_due_ts=next_due,
            )
            return self._result(False, f"initial due scheduled ({next_due - now_ts:.0f}s)", stage="due_time")

        if now_ts < next_due:
            remaining = next_due - now_ts
            return self._result(False, f"not yet due ({remaining:.0f}s remaining)", stage="due_time")

        # Due time has passed — proceed
        await self.store.update_proactive_schedule_state(
            user_id=user_id, bot_id=bot_id,
            last_attempt_ts=now_ts,
        )

        # ---- Stage 3: load_latest_topic ----
        topic = await self.store.get_active_topic(user_id, bot_id)
        topic_eligible = self._topic_is_eligible(topic)
        topic_text = self._format_topic(topic) if topic_eligible else "无活跃话题"
        topic_status = topic.get("status", "none") if topic else "none"
        recent_context = await self._load_recent_context(user_id, bot_id)

        # ---- Stage 4: select_message_intent ----
        intent = "check_in"
        intent_reason = ""
        try:
            intent, intent_reason = await self._select_intent(
                now_cst, topic_eligible, topic, topic_text, topic_status
            )
        except Exception as e:
            logger.warning(f"Intent selection failed, using check_in fallback: {e}")

        if intent == "latest_topic_followup" and not topic_eligible:
            intent = "check_in"
            intent_reason = "topic not eligible, fell back to check_in"

        if intent not in self.enabled_types:
            intent = "check_in"

        # ---- Stage 5: generate_message ----
        message = ""
        emotion = "neutral"
        try:
            gen = await self._generate_message(
                now_cst, intent, intent_reason, topic_text,
                recent_context,
                topic.get("followup_count", 0) if topic else 0,
            )
            message = gen.get("message", "").strip()[:200]
            emotion = gen.get("emotion", "neutral")
        except Exception as e:
            logger.warning(f"Message generation failed: {e}")

        if not message:
            message = FALLBACK_MESSAGES.get(intent, "在干嘛")

        # ---- Stage 6: record candidate (does NOT mutate schedule/topic state) ----
        candidate_id = ""
        try:
            candidate_id = await self.store.create_proactive_candidate(
                reason_type=intent,
                reason_detail=intent_reason,
            )
        except Exception as e:
            logger.warning(f"Failed to persist candidate: {e}")

        if candidate_id:
            await self.store.update_proactive_candidate(
                candidate_id,
                passed_gate=True,
                gate_detail="v2.1.1 due-time",
                generated_message=message,
            )

        # State mutations (followup_count, next_due, last_sent_ts) are deferred
        # to commit_proactive_send(), called by main.py only after WeChat send
        # succeeds. This avoids state drift when send fails.

        return {
            "sent": True,
            "message": message,
            "emotion": emotion,
            "reason": f"{intent}: {intent_reason}",
            "intent": intent,
            "intent_reason": intent_reason,
            "candidate_id": candidate_id,
            "topic_id": topic.get("id", "") if topic else "",
            "user_id": user_id,
            "bot_id": bot_id,
            "stage": "completed",
        }

    def _topic_is_eligible(self, topic: dict[str, Any] | None) -> bool:
        """Check if a topic is eligible for follow-up."""
        if not topic:
            return False
        status = topic.get("status", "")
        if status in ("closed", "stale", "answered"):
            return False
        # Check followup cap
        count = topic.get("followup_count", 0)
        if count >= self.followup_max:
            return False
        # Check expiration
        expires = topic.get("expires_at", 0)
        if expires > 0 and _time_module.time() > expires:
            return False
        # Check followup not too early (honor followup_after_ts)
        followup_after = topic.get("followup_after_ts", 0)
        if followup_after > 0 and _time_module.time() < followup_after:
            return False
        return True

    @staticmethod
    def _format_topic(topic: dict[str, Any]) -> str:
        if not topic:
            return ""
        title = topic.get("title", "")
        summary = topic.get("summary", "")
        status = topic.get("status", "")
        parts = [f"[{status}] {title}"]
        if summary:
            parts.append(f"摘要: {summary[:120]}")
        return "\n".join(parts)

    async def _load_recent_context(self, user_id: str, bot_id: str) -> str:
        """Load a tiny recent-turn context for proactive continuity."""
        try:
            turns = await self.store.get_recent_turns(user_id, bot_id, limit=3)
        except Exception as e:
            logger.debug(f"Failed to load recent proactive context: {e}")
            return "无"
        return self._format_recent_context(turns)

    @staticmethod
    def _format_recent_context(turns: list[dict[str, Any]]) -> str:
        if not turns:
            return "无"
        import json

        lines: list[str] = []
        for turn in turns[-3:]:
            user_text = str(turn.get("user_messages", "")).strip()
            assistant_text = ""
            raw_actions = str(turn.get("assistant_actions", "") or "")
            try:
                parsed = json.loads(raw_actions)
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed = []
            if isinstance(parsed, list):
                replies = [
                    str(item.get("text", "")).strip()
                    for item in parsed
                    if isinstance(item, dict) and str(item.get("text", "")).strip()
                ]
                assistant_text = " / ".join(replies[:2])
            if user_text:
                lines.append(f"用户: {user_text[:80]}")
            if assistant_text:
                lines.append(f"bot: {assistant_text[:80]}")
        return "\n".join(lines[-6:]) if lines else "无"

    async def _select_intent(
        self,
        now_cst: datetime,
        topic_eligible: bool,
        topic: dict[str, Any] | None,
        topic_text: str,
        topic_status: str,
    ) -> tuple[str, str]:
        """Select intent — rules-first, optional LLM refinement."""
        import time as time_module

        now_str = now_cst.strftime("%Y年%m月%d日 %H:%M 周%u")
        followup_count = topic.get("followup_count", 0) if topic else 0

        # Rule-first: if eligible topic exists, prefer followup
        if topic_eligible and "latest_topic_followup" in self.enabled_types:
            return ("latest_topic_followup", "eligible topic exists")

        prompt = self.intent_prompt_template.format(
            now_str=now_str,
            topic_text=topic_text,
            topic_status=topic_status,
            followup_count=followup_count,
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "只输出JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=128,
            )
            raw = response.choices[0].message.content or ""

            import json
            from utils.json_parser import extract_json
            json_str = extract_json(raw)
            result = json.loads(json_str)
            intent = str(result.get("intent", "check_in"))
            reason = str(result.get("reason", ""))
            if intent in self.enabled_types:
                return (intent, reason)
        except Exception as e:
            logger.debug(f"LLM intent selection failed: {e}")

        # Fallback: pick first non-followup enabled type
        fallback = "check_in"
        for t in self.enabled_types:
            if t != "latest_topic_followup":
                fallback = t
                break
        return (fallback, "intent selection fallback")

    async def _generate_message(
        self,
        now_cst: datetime,
        intent: str,
        intent_reason: str,
        topic_text: str,
        recent_context: str,
        followup_count: int = 0,
    ) -> dict[str, Any]:
        """Generate message text for the selected intent."""
        now_str = now_cst.strftime("%Y年%m月%d日 %H:%M 周%u")
        intent_rules = INTENT_RULES.get(intent, "自然简短的消息")
        familiarity = 5  # Default

        prompt = self.message_prompt_template.format(
            intent=intent,
            reason=intent_reason,
            now_str=now_str,
            topic_text=topic_text,
            recent_context=recent_context,
            familiarity=familiarity,
            intent_rules=intent_rules,
        )

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "只输出JSON。不要多说话。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=256,
        )

        raw = response.choices[0].message.content or ""

        import json
        from utils.json_parser import extract_json

        try:
            json_str = extract_json(raw)
            result = json.loads(json_str)
            return {
                "message": str(result.get("message", ""))[:200],
                "emotion": str(result.get("emotion", "neutral")),
            }
        except (json.JSONDecodeError, ValueError):
            return {"message": "", "emotion": "neutral"}

    def _random_next_due(self, now_ts: float) -> float:
        """Compute random next due time within configured range."""
        min_secs = max(1, self.min_interval) * 60
        max_secs = max(min_secs + 1, self.max_interval * 60)
        delay = _random.uniform(min_secs, max_secs)
        return now_ts + delay

    # ================================================================
    # V2.1.1: Post-send commit and reschedule
    # ================================================================

    async def _reschedule_after_skip(
        self, user_id: str, bot_id: str, now_ts: float,
    ) -> None:
        """Recompute next_due after a hard-block skip.

        Prevents repeated attempts during quiet hours or while
        bridge/contact is unavailable.
        """
        if not user_id or not bot_id:
            return
        try:
            next_due = self._random_next_due(now_ts)
            await self.store.update_proactive_schedule_state(
                user_id=user_id, bot_id=bot_id,
                next_due_ts=next_due,
            )
        except Exception as e:
            logger.warning(f"Failed to reschedule after skip: {e}")

    async def commit_proactive_send(
        self,
        user_id: str,
        bot_id: str,
        topic_id: str = "",
        intent: str = "",
    ) -> None:
        """Commit state mutations AFTER a successful WeChat send.

        Called by main.py only when the bridge confirms delivery.
        This prevents state drift when send fails.
        """
        now = _time_module.time()

        # Persist next due time + last_sent_ts
        next_due = self._random_next_due(now)
        await self.store.update_proactive_schedule_state(
            user_id=user_id, bot_id=bot_id,
            next_due_ts=next_due,
            last_sent_ts=now,
        )

        # Increment topic followup count if applicable
        if intent == "latest_topic_followup" and topic_id:
            await self.store.increment_topic_followup(topic_id)

    # ================================================================
    # Record keeping
    # ================================================================

    async def record_sent(self):
        """Call after successfully sending a proactive message."""
        now = _time_module.time()
        state = await self.store.get_scheduler_state()
        await self.store.update_scheduler_state({
            "messages_today": state.get("messages_today", 0) + 1,
            "last_proactive_time": now,
            "consecutive_ignored": 0,
        })

    async def record_chat_activity(self):
        """Call when user sends a message (resets ignore counter)."""
        await self.store.update_scheduler_state({
            "last_chat_time": _time_module.time(),
            "consecutive_ignored": 0,
        })

    async def reset_daily_counter(self):
        """Reset the daily message counter. Call at midnight."""
        await self.store.update_scheduler_state({"messages_today": 0})

    async def record_proactive_sent(self, candidate_id: str):
        """Mark a proactive candidate as sent."""
        await self.store.update_proactive_candidate(candidate_id, sent=True)
        await self.record_sent()

    @staticmethod
    def _result(sent: bool, reason: str, stage: str = "", intent: str = "") -> dict[str, Any]:
        return {
            "sent": sent,
            "message": "",
            "emotion": "neutral",
            "reason": reason,
            "intent": intent,
            "candidate_id": "",
            "stage": stage,
        }
