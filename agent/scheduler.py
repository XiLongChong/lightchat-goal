"""Proactive messaging scheduler 鈥?V2.1.1 due-time + intent model.

V2.1.1 changes:
  - LLM no longer decides whether to send. Due-time schedule controls it.
  - Intent selection replaces candidate reason generation.
  - Only latest eligible topic may be followed up.
  - Broad memory/recap are NOT passed into proactive generation by default.

Pipeline stages:
  1. hard_precheck         鈥?quiet hours, bridge, contact
  2. due_time_check        鈥?next_due_ts has passed?
  3. load_latest_topic     鈥?active topic from DB
  4. select_message_intent 鈥?rules-first, LLM fallback
  5. generate_message      鈥?LLM with intent+constraints
  6. send_and_record       鈥?send via bridge, record results
  7. schedule_next_due     鈥?random next due time
"""

from __future__ import annotations

import asyncio
import logging
import random as _random
import re
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

MINIMAL_INTENT_SELECTION_PROMPT = """Choose one proactive message intent.
Current time: {now_str}
Active topic: {topic_text}
Topic status: {topic_status}
Followup count: {followup_count}
Only output JSON: {{"intent": "check_in", "reason": ""}}"""

MINIMAL_MESSAGE_GENERATION_PROMPT = """Generate one short proactive chat message.
Type: {intent}
Reason: {reason}
Current time: {now_str}
Active topic: {topic_text}
Recent context: {recent_context}
Bot identity: {bot_identity}
Relationship context: {relationship_context}
Stable memory: {memory_context}
Familiarity: {familiarity}/10
Rule: do not pretend the user just said something. {intent_rules}
Only output JSON: {{"message": "", "emotion": "neutral"}}"""

_HALLUCINATION_PATTERNS = [
    "\u6211\u521a\u770b\u5230",
    "\u6211\u521a\u5237\u5230",
    "\u670b\u53cb\u5708",
    "\u6709\u4eba\u8ddf\u6211\u8bf4",
    "\u6211\u542c\u8bf4",
    "\u636e\u8bf4",
    "\u7f51\u4e0a\u8bf4",
    "\u6211\u8bb0\u5f97\u4f60\u4e0a\u6b21",
    "\u4f60\u4e4b\u524d\u4e0d\u662f",
    "\u4f60\u4e0a\u6b21\u8bf4",
]

_BANNED_REPEAT_OPENERS = [
    "\u7a81\u7136\u60f3\u5230",
    "\u6211\u521a\u624d\u5728\u60f3",
    "\u6709\u4e2a\u5c0f\u95ee\u9898",
    "\u540e\u6765\u5462",
]

INTENT_RULES = {
    "check_in": "Simple low-pressure check-in.",
    "latest_topic_followup": "Follow up only on the latest eligible active topic.",
    "clingy_followup": "Light clingy followup without blame or emotional pressure.",
    "self_share": "Share a harmless small thought without fabricated perception.",
    "light_ping": "Very short and low pressure.",
}

FALLBACK_MESSAGES = {
    "check_in": "\u5728\u5e72\u561b",
    "latest_topic_followup": "\u540e\u6765\u5462",
    "clingy_followup": "\u4eba\u5462",
    "self_share": "\u7a81\u7136\u60f3\u95ee\u4f60\u4e2a\u4e8b",
    "light_ping": "\u55ef",
}

OPENER_FALLBACKS = {
    "check_in": ["\u5728\u5e72\u561b", "\u5fd9\u5b8c\u4e86\u5417", "\u73b0\u5728\u65b9\u4fbf\u8bf4\u8bdd\u5417"],
    "latest_topic_followup": ["\u540e\u6765\u5462", "\u73b0\u5728\u600e\u4e48\u6837\u4e86", "\u6709\u65b0\u8fdb\u5c55\u5417"],
    "clingy_followup": ["\u4eba\u5462", "\u53bb\u54ea\u4e86", "\u600e\u4e48\u4e0d\u7406\u6211"],
    "self_share": ["\u6211\u521a\u624d\u5728\u60f3", "\u6709\u4e2a\u5c0f\u95ee\u9898", "\u8111\u5b50\u91cc\u5192\u51fa\u6765\u4e00\u53e5"],
    "light_ping": ["\u55ef", "\u6233\u4e00\u4e0b", "\u8fd8\u5728\u5417"],
}


class SchedulerAgent:
    """Periodic proactive messaging agent 鈥?V2.1.1 due-time model."""

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
        # V2.2: Two-stage proactive generation
        self.plan_prompt_template = self._load_prompt_template(
            "proactive_plan.md",
            "",
        )
        self.message_v2_prompt_template = self._load_prompt_template(
            "proactive_message_v2.md",
            "",
        )
        self.sleep_start = cfg.get("sleep_start", 0)
        self.sleep_end = cfg.get("sleep_end", 7)
        self.quiet_hours_enabled = cfg.get("quiet_hours_enabled", True)
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
        if gen_cfg.get("clingy_mode_enabled", False) and "clingy_followup" not in self.enabled_types:
            self.enabled_types = ["clingy_followup", *self.enabled_types]
        self.max_proactive_messages = max(1, min(int(gen_cfg.get("max_messages", 1) or 1), 3))
        self.clingy_mode_enabled = bool(gen_cfg.get("clingy_mode_enabled", False))
        self.clingy_after_minutes = int(gen_cfg.get("clingy_after_minutes", 20) or 20)
        clingy_cfg = pro_cfg.get("clingy", {})
        self.max_followups_per_unanswered = int(clingy_cfg.get("max_followups_per_unanswered", 3) or 3)
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
        """Legacy path 鈥?delegates to V2.1.1 pipeline."""
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
    # V2.1.2: Independent clingy followup state machine
    # ================================================================

    async def run_clingy_check(
        self,
        now_ts: float,
        now_cst: datetime,
        prerequisites: dict[str, Any],
        bot_identity: str = "",
        relationship: dict[str, Any] | None = None,
        stable_memories: list[dict[str, Any]] | None = None,
        dialogue_state: dict[str, Any] | None = None,
        emotion_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Check if a clingy followup is due and generate one.

        V2.2: Uses dialogue_state.wait_state as source of truth.
        Old clingy_state is only used as fallback compatibility.
        """
        result: dict[str, Any] = {"sent": False, "message": "", "emotion": "neutral",
                                   "reason": "", "intent": "", "candidate_id": "",
                                   "stage": "clingy", "block_proactive": False}

        if not self.clingy_mode_enabled:
            result["reason"] = "clingy mode disabled"
            return result

        user_id = prerequisites.get("user_id", "")
        bot_id = prerequisites.get("bot_id", "")

        # V2.2: Dialogue state is the source of truth for wait_state
        ds = dialogue_state or {}
        wait_state = str(ds.get("wait_state", "none"))

        if wait_state == "bot_silent":
            result["reason"] = "dialogue_state: bot is silent — no clingy"
            return result
        if wait_state == "user_waiting_assistant":
            result["reason"] = "dialogue_state: user waiting for assistant — no clingy"
            return result
        if wait_state == "paused_by_user":
            result["reason"] = "dialogue_state: paused by user"
            return result
        if wait_state == "topic_closed":
            result["reason"] = "dialogue_state: topic closed"
            return result

        if wait_state == "none":
            # V2.3: Reconcile with clingy_state. If awaiting_user_reply=1,
            # dialogue_state may be stale. Check turn ledger for ground truth.
            stored_state = await self.store.get_clingy_state(user_id, bot_id)
            if stored_state.get("awaiting_user_reply"):
                logger.warning(
                    f"Reconcile: wait_state=none but clingy.awaiting=1 for {user_id}/{bot_id}"
                )
                recent_turns = await self._load_recent_turns(user_id, bot_id, limit=5)
                last_asst_ts = 0.0
                last_user_ts = 0.0
                asst_needs_reply = False
                for t in reversed(recent_turns):
                    created = float(t.get("created_at") or 0)
                    src = str(t.get("source", "")).strip()
                    has_asst = bool(str(t.get("assistant_actions", "")).strip())
                    has_user = bool(str(t.get("user_messages", "")).strip())
                    if not last_asst_ts and has_asst:
                        last_asst_ts = created
                        asst_text = str(t.get("assistant_actions", ""))[:500]
                        if re.search(r"[?？]", asst_text[-200:]) or re.search(r"(还是|或者|A[.、]|B[.、])", asst_text[-200:]):
                            asst_needs_reply = True
                    if not last_user_ts and has_user and src != "proactive":
                        last_user_ts = created

                if last_asst_ts > last_user_ts:
                    if asst_needs_reply:
                        logger.info(f"Reconcile: repairing wait_state to assistant_waiting_user for {user_id}")
                        # Fall through to clingy check below
                    else:
                        logger.info(f"Reconcile: clearing stale clingy_state (asst last, no reply needed)")
                        await self.store.clear_clingy_awaiting(user_id, bot_id)
                        result["reason"] = "dialogue_state: none, clingy reconciled and cleared"
                        return result
                elif last_user_ts > last_asst_ts:
                    logger.info(f"Reconcile: clearing stale clingy_state (user last)")
                    await self.store.clear_clingy_awaiting(user_id, bot_id)
                    result["reason"] = "dialogue_state: none, clingy reconciled (user last)"
                    return result
                else:
                    logger.warning(f"Reconcile: undecided for {user_id}/{bot_id}, conservative skip")
                    result["reason"] = "dialogue_state: none, reconcile undecided — conservative skip"
                    return result
            else:
                result["reason"] = "dialogue_state: no waiting relationship (none)"
                return result

        # Only assistant_waiting_user is eligible for clingy
        if wait_state != "assistant_waiting_user":
            result["reason"] = f"dialogue_state: wait_state={wait_state} 鈥?not assistant_waiting_user, no clingy"
            return result

        # 鈹€鈹€ Derive effective_awaiting from message ledger 鈹€鈹€
        stored_state = await self.store.get_clingy_state(user_id, bot_id)
        stored_awaiting = bool(stored_state.get("awaiting_user_reply"))

        recent_turns = await self._load_recent_turns(user_id, bot_id, limit=10)
        last_delivered_assistant_at = 0.0
        last_real_user_inbound_at = 0.0
        for t in reversed(recent_turns):
            created = float(t.get("created_at") or 0)
            source = str(t.get("source", "")).strip()
            has_assistant = bool(str(t.get("assistant_actions", "")).strip())
            has_user = bool(str(t.get("user_messages", "")).strip())
            send_status = str(t.get("send_status", "")).strip()
            if not last_delivered_assistant_at and has_assistant and send_status in ("delivered", ""):
                last_delivered_assistant_at = created
            if not last_real_user_inbound_at and has_user and source != "proactive":
                last_real_user_inbound_at = created

        derived_awaiting = last_delivered_assistant_at > last_real_user_inbound_at

        # Diagnostic: stored vs derived mismatch 鈥?auto-heal first
        if stored_awaiting != derived_awaiting:
            logger.warning(
                f"Clingy state mismatch for {user_id}/{bot_id}: "
                f"stored_awaiting={stored_awaiting} derived_awaiting={derived_awaiting} "
                f"last_asst={last_delivered_assistant_at:.0f} last_user={last_real_user_inbound_at:.0f}"
            )
            if derived_awaiting and not stored_awaiting:
                await self.store.update_clingy_state(
                    user_id=user_id, bot_id=bot_id,
                    awaiting_user_reply=1,
                    next_clingy_due_at=now_ts + self.clingy_after_minutes * 60,
                    clingy_followup_count=0,
                )
                stored_awaiting = True
            elif not derived_awaiting and stored_awaiting:
                await self.store.update_clingy_state(
                    user_id=user_id, bot_id=bot_id,
                    awaiting_user_reply=0,
                    next_clingy_due_at=0,
                )
                stored_awaiting = False

        # Compute AFTER auto-heal so the fixed stored value is used
        effective_awaiting = derived_awaiting or stored_awaiting

        if not effective_awaiting:
            result["reason"] = "not awaiting user reply (ledger + stored both false)"
            return result

        next_due = stored_state.get("next_clingy_due_at", 0)
        if next_due == 0 or now_ts < next_due:
            remaining = max(0, next_due - now_ts)
            result["reason"] = f"clingy not yet due ({remaining:.0f}s remaining)"
            result["block_proactive"] = True  # Don't let proactive steal the slot
            return result

        count = stored_state.get("clingy_followup_count", 0)
        if count >= self.max_followups_per_unanswered:
            result["reason"] = f"clingy max reached ({count}/{self.max_followups_per_unanswered})"
            return result

        # Hard prechecks (shared with proactive)
        if not prerequisites.get("bot_active", True):
            result["reason"] = "bot not active"; return result
        if not prerequisites.get("bridge_available", True):
            result["reason"] = "bridge unavailable"; return result
        if not prerequisites.get("valid_contact", True):
            result["reason"] = prerequisites.get("contact_block_reason") or "no contact"; return result
        if self.quiet_hours_enabled and self._is_quiet_hour(now_cst.hour):
            result["reason"] = "quiet hours"; return result

        # Daily cap check
        today_start = datetime.combine(now_cst.date(), datetime.min.time(), tzinfo=CST).timestamp()
        sent_today = await self.store.count_today_proactive_sent(today_start)
        if sent_today >= self.max_daily:
            result["reason"] = f"daily cap ({sent_today}/{self.max_daily})"; return result

        # Generate clingy message (recent_turns already loaded above)
        intent = "clingy_followup"
        intent_reason = f"unanswered {stored_state.get('last_proactive_intent', 'proactive')} 鈥?followup #{count + 1}"

        recent_context = self._format_recent_context(recent_turns)
        relationship_context = self._format_relationship(relationship or {})
        memory_context = self._format_memory_context(stable_memories or [])
        last_opener = await self._load_last_opener()

        generated_messages: list[dict[str, Any]] = []
        emotion = "neutral"
        try:
            gen = await self._generate_message(
                now_cst, intent, intent_reason, "",
                recent_context, bot_identity,
                relationship_context, memory_context,
                last_opener, 0,
            )
            generated_messages = self._normalize_generated_messages(
                gen.get("messages") or gen.get("message") or "",
                intent, last_opener,
            )
            emotion = gen.get("emotion", "neutral")
        except Exception as e:
            logger.warning(
                "Clingy message generation failed; fallback_reason=model_generation_failed: %s",
                e,
            )

        if not generated_messages:
            logger.info("Using clingy fallback messages; fallback_reason=empty_generation")
            generated_messages = self._fallback_messages(intent, last_opener)

        message = "\n".join(m["text"] for m in generated_messages if m.get("text"))
        if not message.strip():
            result["reason"] = "empty clingy message"; return result

        # V2.2: Guard proactive message against forbidden patterns
        message, violations = self._guard_proactive_message(message)
        if violations:
            logger.warning("Clingy message guard violations: %s", violations)

        candidate_id = ""
        try:
            candidate_id = await self.store.create_proactive_candidate(
                reason_type=intent, reason_detail=intent_reason,
                user_id=user_id, bot_id=bot_id,
                source="proactive", trigger="proactive_reason",
            )
        except Exception as e:
            logger.warning(f"Failed to persist clingy candidate: {e}")

        if candidate_id:
            await self.store.update_proactive_candidate(
                candidate_id, passed_gate=True,
                gate_detail="clingy state machine",
                generated_message=message,
            )

        result.update({
            "sent": True, "message": message, "messages": generated_messages,
            "emotion": emotion, "reason": intent_reason, "intent": intent,
            "intent_reason": intent_reason, "candidate_id": candidate_id,
            "topic_id": "", "user_id": user_id, "bot_id": bot_id,
            "block_proactive": False,
        })
        return result

    # ================================================================
    # V2.1.1: Due-time + intent proactive pipeline
    # ================================================================

    async def run_proactive_pipeline(
        self,
        recent_memories: list[dict[str, Any]] | None = None,
        recap_text: str = "",
        bot_id: str = "",
        prerequisites: dict[str, Any] | None = None,
        bot_identity: str = "",
        relationship: dict[str, Any] | None = None,
        stable_memories: list[dict[str, Any]] | None = None,
        dialogue_state: dict[str, Any] | None = None,
        emotion_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """V2.1.1 pipeline: due-time check 鈫?intent 鈫?generate 鈫?send.

        V2.2: Respects dialogue_state for proactive gating.
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
            reason = prereq.get("contact_block_reason") or "no contact"
            return self._result(False, reason, stage="pre_check")

        if self.quiet_hours_enabled and self._is_quiet_hour(now_cst.hour):
            await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
            return self._result(False, "quiet hours", stage="pre_check")

        # V2.2: Dialogue state gate for proactive
        ds = dialogue_state or {}
        wait_state = str(ds.get("wait_state", "none"))
        if wait_state == "bot_silent":
            await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
            return self._result(False, "dialogue_state: bot is silent", stage="pre_check")
        if wait_state == "user_waiting_assistant":
            await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
            return self._result(False, "dialogue_state: waiting for user reply", stage="pre_check")
        if wait_state == "paused_by_user":
            proactive_paused = float(ds.get("proactive_paused_until", 0))
            if proactive_paused > now_ts:
                await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
                return self._result(False, "dialogue_state: paused by user", stage="pre_check")

        if wait_state == "none":
            # V2.3: Reconcile: if clingy_state.awaiting=1, conservative skip proactive
            clingy = await self.store.get_clingy_state(pre_user_id, bot_id)
            if clingy.get("awaiting_user_reply"):
                logger.warning(
                    f"Proactive: wait_state=none but clingy.awaiting=1 for {pre_user_id}/{bot_id} — conservative skip"
                )
                await self._reschedule_after_skip(pre_user_id, bot_id, now_ts)
                return self._result(False, "dialogue_state: none but awaiting=1 — skip", stage="pre_check")

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
            # First time 鈥?schedule initial due time
            next_due = self._random_next_due(now_ts)
            await self.store.update_proactive_schedule_state(
                user_id=user_id, bot_id=bot_id,
                next_due_ts=next_due,
            )
            return self._result(False, f"initial due scheduled ({next_due - now_ts:.0f}s)", stage="due_time")

        if now_ts < next_due:
            remaining = next_due - now_ts
            return self._result(False, f"not yet due ({remaining:.0f}s remaining)", stage="due_time")

        # Due time has passed 鈥?proceed
        await self.store.update_proactive_schedule_state(
            user_id=user_id, bot_id=bot_id,
            last_attempt_ts=now_ts,
        )

        # ---- Stage 3: load_latest_topic ----
        topic = await self.store.get_active_topic(user_id, bot_id)
        topic_eligible = self._topic_is_eligible(topic)
        topic_text = self._format_topic(topic) if topic_eligible else "no active topic"
        topic_status = topic.get("status", "none") if topic else "none"
        recent_turns = await self._load_recent_turns(user_id, bot_id, limit=3)
        recent_context = self._format_recent_context(recent_turns)
        relationship_context = self._format_relationship(relationship or {})
        memory_context = self._format_memory_context(stable_memories or recent_memories or [])
        last_opener = await self._load_last_opener()

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

        # V2.1.2: clingy_followup is now handled by the independent
        # run_clingy_check state machine 鈥?no longer inline here.

        if intent not in self.enabled_types:
            intent = "check_in"

        # ---- Stage 5: generate_message (V2.2 two-stage) ----
        generated_messages: list[dict[str, Any]] = []
        emotion = "neutral"
        style = ""
        safe_basis = ""
        topic_seed = ""
        try:
            gen = await self._generate_proactive_message(
                now_cst, intent, intent_reason, topic_text,
                recent_context,
                bot_identity,
                relationship_context,
                memory_context,
                topic.get("followup_count", 0) if topic else 0,
            )
            if gen.get("skipped"):
                return self._result(False, gen.get("skip_reason", "plan risk=high"), stage="generate")
            generated_messages = self._normalize_generated_messages(
                gen.get("messages") or gen.get("message") or "",
                intent,
                last_opener,
            )
            emotion = gen.get("emotion", "neutral")
            style = gen.get("style", "")
            safe_basis = gen.get("safe_basis", "")
            topic_seed = gen.get("topic_seed", "")
        except TypeError as e:
            logger.exception(
                "Proactive message generation code error; fallback_reason=code_error"
            )
            return self._result(False, f"message generation code error: {e}", stage="generate")
        except Exception as e:
            logger.warning(
                "Message generation failed; fallback_reason=model_generation_failed: %s",
                e,
            )

        if not generated_messages:
            logger.info("Using proactive fallback messages; fallback_reason=empty_generation")
            generated_messages = self._fallback_messages(intent, last_opener)

        message = "\n".join(m["text"] for m in generated_messages if m.get("text"))

        # V2.2: Guard proactive message
        message, violations = self._guard_proactive_message(message)
        if violations:
            logger.warning("Proactive fallback/message guard violations: %s", violations)

        # ---- Stage 6: record candidate ----
        candidate_id = ""
        try:
            detail_parts = [intent_reason]
            if style:
                detail_parts.append(f"style={style}")
            if safe_basis:
                detail_parts.append(f"basis={safe_basis}")
            if topic_seed:
                detail_parts.append(f"seed={topic_seed}")
            candidate_id = await self.store.create_proactive_candidate(
                reason_type=intent,
                reason_detail=" | ".join(detail_parts),
                user_id=user_id,
                bot_id=bot_id,
                source="proactive",
                trigger="proactive_reason",
            )
        except Exception as e:
            logger.warning(f"Failed to persist candidate: {e}")

        if candidate_id:
            await self.store.update_proactive_candidate(
                candidate_id,
                passed_gate=True,
                gate_detail=f"v2.2 plan+message | style={style} | basis={safe_basis}",
                generated_message=message,
            )

        # State mutations (followup_count, next_due, last_sent_ts) are deferred
        # to commit_proactive_send(), called by main.py only after WeChat send
        # succeeds. This avoids state drift when send fails.

        return {
            "sent": True,
            "message": message,
            "messages": generated_messages,
            "emotion": emotion,
            "reason": intent_reason,
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
            parts.append(f"鎽樿: {summary[:120]}")
        return "\n".join(parts)

    async def _load_recent_context(self, user_id: str, bot_id: str) -> str:
        """Load a tiny recent-turn context for proactive continuity."""
        return self._format_recent_context(
            await self._load_recent_turns(user_id, bot_id, limit=3)
        )

    async def _load_recent_turns(
        self,
        user_id: str,
        bot_id: str,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        try:
            return await self.store.get_recent_turns(user_id, bot_id, limit=limit)
        except Exception as e:
            logger.debug(f"Failed to load recent proactive context: {e}")
            return []

    @staticmethod
    def _format_recent_context(turns: list[dict[str, Any]]) -> str:
        if not turns:
            return "none"
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
                lines.append(f"\u7528\u6237: {user_text[:80]}")
            if assistant_text:
                lines.append(f"bot: {assistant_text[:80]}")
        return "\n".join(lines[-6:]) if lines else "none"

    async def _load_last_opener(self) -> str:
        try:
            candidates = await self.store.get_recent_proactive_candidates(limit=5)
        except Exception:
            return ""
        for item in candidates:
            if not item.get("sent"):
                continue
            opener = self._extract_opener(str(item.get("generated_message", "")))
            if opener:
                return opener
        return ""

    def _last_turn_allows_clingy(
        self,
        turns: list[dict[str, Any]],
        now_ts: float,
    ) -> bool:
        """Check if the last turn is eligible for clingy followup.

        Requires: last turn has bot content, enough time elapsed.
        No longer rejects proactive source 鈥?clingy state machine
        handles its own gating via awaiting_user_reply + count cap.
        """
        if not turns:
            return False
        last = turns[-1]
        if not str(last.get("assistant_actions", "")).strip():
            return False
        created = float(last.get("created_at") or 0)
        if created and now_ts - created < self.clingy_after_minutes * 60:
            return False
        return True

    @staticmethod
    def _extract_opener(text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        first = text.splitlines()[0].strip()
        for sep in ("，", "。", "；", "?", "？", " ", ":", "："):
            if sep in first:
                first = first.split(sep, 1)[0]
                break
        return first[:12]

    def _is_quiet_hour(self, hour: int) -> bool:
        """Return whether an hour falls inside the configured quiet window."""
        if self.sleep_start == self.sleep_end:
            return True
        if self.sleep_start < self.sleep_end:
            return self.sleep_start <= hour < self.sleep_end
        return hour >= self.sleep_start or hour < self.sleep_end

    @staticmethod
    def _format_relationship(relationship: dict[str, Any]) -> str:
        if not relationship:
            return "none"
        familiarity = relationship.get("familiarity", 5)
        atmosphere = relationship.get("current_atmosphere", "neutral")
        boundary = relationship.get("chat_boundary", "unset")
        style = relationship.get("interaction_style", "casual")
        return (
            f"familiarity: {familiarity}/10; "
            f"atmosphere: {atmosphere}; "
            f"style: {style}; "
            f"boundary: {boundary}"
        )

    @staticmethod
    def _format_memory_context(memories: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for item in memories[:5]:
            content = str(item.get("content", "")).strip()
            category = str(item.get("category", "")).strip()
            if content:
                prefix = f"{category}: " if category else ""
                lines.append(f"- {prefix}{content[:120]}")
        return "\n".join(lines) if lines else "none"

    async def _select_intent(
        self,
        now_cst: datetime,
        topic_eligible: bool,
        topic: dict[str, Any] | None,
        topic_text: str,
        topic_status: str,
    ) -> tuple[str, str]:
        """Select intent 鈥?rules-first, optional LLM refinement."""
        import time as time_module

        now_str = now_cst.strftime("%Y-%m-%d %H:%M %a")
        followup_count = topic.get("followup_count", 0) if topic else 0

        # Rule-first: if eligible topic exists, prefer followup
        if topic_eligible and "latest_topic_followup" in self.enabled_types:
            return ("latest_topic_followup", "eligible topic exists")
        if not topic_eligible and "self_share" in self.enabled_types:
            return ("self_share", "no active topic, prefer self share")

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
                    {"role": "system", "content": "Only output JSON."},
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

        # Fallback: prefer self_share for no-topic proactive starts.
        fallback = "self_share" if "self_share" in self.enabled_types else "check_in"
        if fallback not in self.enabled_types:
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
        bot_identity: str = "",
        relationship_context: str = "",
        memory_context: str = "",
        last_opener: str = "",
        followup_count: int = 0,
    ) -> dict[str, Any]:
        """Generate message text for the selected intent."""
        now_str = now_cst.strftime("%Y-%m-%d %H:%M %a")
        intent_rules = INTENT_RULES.get(intent, "natural short message")
        familiarity = 5  # Default

        prompt = self.message_prompt_template.format(
            intent=intent,
            reason=intent_reason,
            now_str=now_str,
            topic_text=topic_text,
            recent_context=recent_context,
            bot_identity=(bot_identity or "Use current bot identity naturally.")[:1600],
            relationship_context=relationship_context or "none",
            memory_context=memory_context or "none",
            last_opener=last_opener or "none",
            max_messages=self.max_proactive_messages if intent == "clingy_followup" else 1,
            familiarity=familiarity,
            intent_rules=intent_rules,
        )

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Only output JSON."},
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
            raw_messages = result.get("messages")
            if not raw_messages:
                raw_messages = result.get("message", "")
            return {
                "messages": raw_messages,
                "message": str(result.get("message", ""))[:200],
                "emotion": str(result.get("emotion", "neutral")),
            }
        except (json.JSONDecodeError, ValueError):
            return {"message": "", "emotion": "neutral"}

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
    # V2.2: Two-stage proactive message generation (plan + message)
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

    async def _generate_proactive_plan(
        self,
        now_str: str,
        intent: str,
        intent_reason: str,
        topic_text: str,
        recent_context: str,
        bot_identity: str,
        relationship_context: str,
        memory_context: str,
        recent_openers: str,
    ) -> dict[str, Any]:
        """Stage 1: generate a safe plan before writing the message."""
        if not self.plan_prompt_template:
            return {}
        prompt = self.plan_prompt_template.format(
            now_str=now_str, intent=intent, intent_reason=intent_reason,
            topic_text=topic_text, recent_context=recent_context,
            bot_identity=(bot_identity or "...")[:1200],
            relationship_context=relationship_context or "none",
            memory_context=memory_context or "none",
            recent_openers=recent_openers or "none",
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Only output JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                max_tokens=256,
            )
            raw = response.choices[0].message.content or ""
            import json as _json
            from utils.json_parser import extract_json
            return _json.loads(extract_json(raw))
        except Exception as e:
            logger.warning(f"Proactive plan generation failed: {e}")
            return {}

    async def _generate_message_from_plan(
        self,
        now_str: str,
        plan: dict[str, Any],
        bot_identity: str,
        relationship_context: str,
    ) -> dict[str, Any]:
        """Stage 2: generate the actual message from the safety plan."""
        if not self.message_v2_prompt_template:
            return {"message": "", "emotion": "neutral"}
        intent = plan.get("intent", "check_in")
        style = plan.get("style", "curious")
        safe_basis = plan.get("safe_basis", "inner_thought_only")
        topic_seed = plan.get("topic_seed", "")
        risk = plan.get("risk", "low")
        avoid = plan.get("avoid", [])
        avoid_list = ", ".join(avoid) if avoid else "none"

        prompt = self.message_v2_prompt_template.format(
            intent=intent, style=style, safe_basis=safe_basis,
            topic_seed=topic_seed, risk=risk, avoid_list=avoid_list,
            now_str=now_str,
            bot_identity=(bot_identity or "...")[:1200],
            relationship_context=relationship_context or "none",
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Only output JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=256,
            )
            raw = response.choices[0].message.content or ""
            import json as _json
            from utils.json_parser import extract_json
            result = _json.loads(extract_json(raw))
            raw_messages = result.get("messages")
            if not raw_messages:
                raw_messages = result.get("message", "")
            return {
                "messages": raw_messages,
                "message": str(result.get("message", ""))[:200],
                "emotion": str(result.get("emotion", "neutral")),
                "ending_type": str(result.get("ending_type", "question")),
                "style": style,
            }
        except Exception as e:
            logger.warning(f"Message-from-plan generation failed: {e}")
            return {"message": "", "emotion": "neutral", "style": style}

    @staticmethod
    def _guard_proactive_message(
        text: str,
        plan: dict[str, Any] | None = None,
        emotion_policy: dict[str, Any] | None = None,
    ) -> tuple[str, list[str]]:
        """Check generated message against plan safety constraints.

        Returns (sanitized_text, passed_guard).
        If risk=high 鈫?reject.
        If message contains external perception but safe_basis doesn't allow it 鈫?reject.
        """
        violations: list[str] = []
        if not text or not text.strip():
            return text, violations
        plan = plan or {}
        risk = plan.get("risk", "low")
        if risk == "high":
            logger.warning(f"Proactive guard: risk=high, rejecting message")
            violations.append("plan_risk_high")

        safe_basis = plan.get("safe_basis", "inner_thought_only")
        # For inner_thought_only: check for external perception patterns
        if safe_basis == "inner_thought_only":
            for pattern in _HALLUCINATION_PATTERNS:
                if pattern in text:
                    logger.warning(f"Proactive guard: blocked hallucination pattern '{pattern}' in: {text[:60]}")
                    violations.append(f"hallucination: {pattern}")

        # For active_topic/memory: allow topic references but still block fabrication
        if safe_basis in ("active_topic", "memory"):
            # Block fabricated user quotes
            fabrication_patterns = [
                "\u6211\u8bb0\u5f97\u4f60\u4e0a\u6b21",
                "\u4f60\u4e4b\u524d\u4e0d\u662f",
                "\u4f60\u4e0a\u6b21\u8bf4",
                "\u4f60\u4ee5\u524d",
                "\u4f60\u8bf4\u8fc7",
                "\u4f60\u544a\u8bc9\u8fc7\u6211",
            ]
            for pattern in fabrication_patterns:
                if pattern in text:
                    logger.warning(f"Proactive guard: blocked fabrication '{pattern}' in: {text[:60]}")
                    violations.append(f"fabrication: {pattern}")

        # Check plan's avoid list
        avoid_list = plan.get("avoid", [])
        if isinstance(avoid_list, list):
            for phrase in avoid_list:
                if phrase and phrase in text:
                    logger.warning(f"Proactive guard: blocked avoided phrase '{phrase}'")
                    violations.append(f"avoid_phrase: {phrase}")

        clingy_escalation = [
            ("\u4f60\u662f\u4e0d\u662f\u4e0d\u60f3\u7406\u6211", "emotional_pressure"),
            ("\u4f60\u600e\u4e48\u53c8\u4e0d\u56de\u6211", "escalating_complaint"),
            ("\u4f60\u662f\u4e0d\u662f\u8ba8\u538c\u6211", "self_blame_pressure"),
        ]
        for pattern, label in clingy_escalation:
            if pattern in text:
                violations.append(f"clingy_escalation: {label}")

        if violations:
            return "", violations
        return text, violations

    async def _load_recent_openers_detailed(self, limit: int = 10) -> list[dict[str, Any]]:
        """Load recent proactive openers with detail for dedup."""
        try:
            candidates = await self.store.get_recent_proactive_candidates(limit=limit)
        except Exception:
            return []
        result: list[dict[str, Any]] = []
        for item in candidates:
            if not item.get("sent"):
                continue
            msg = str(item.get("generated_message", ""))
            opener = self._extract_opener(msg)
            if opener:
                result.append({
                    "opener": opener,
                    "intent": item.get("reason_type", ""),
                    "full_text": msg[:80],
                })
        return result

    async def _generate_proactive_message(
        self,
        now_cst: datetime,
        intent: str,
        intent_reason: str,
        topic_text: str,
        recent_context: str,
        bot_identity: str,
        relationship_context: str,
        memory_context: str,
        followup_count: int = 0,
    ) -> dict[str, Any]:
        """V2.2: Two-stage proactive message generation with guard."""
        now_str = now_cst.strftime("%Y骞?m鏈?d鏃?%H:%M 鍛?u")

        # Load recent openers for dedup
        recent = await self._load_recent_openers_detailed(limit=10)
        recent_openers_str = "\n".join(
            f"#{i+1} opener={r['opener']} intent={r['intent']} text={r['full_text']}"
            for i, r in enumerate(recent)
        ) if recent else "none"

        # Check for banned opener repeats
        banned_count = 0
        for r in recent[:5]:
            for banned in _BANNED_REPEAT_OPENERS:
                if banned in r.get("opener", "") or banned in r.get("full_text", ""):
                    banned_count += 1
                    break

        # Stage 1: Generate plan (with up to 2 retries for banned openers)
        plan = {}
        for attempt in range(3):
            plan = await self._generate_proactive_plan(
                now_str, intent, intent_reason, topic_text,
                recent_context, bot_identity, relationship_context,
                memory_context, recent_openers_str,
            )
            if not plan:
                break
            # Check if risk=high 鈫?skip
            if plan.get("risk") == "high":
                logger.info(f"Proactive plan: risk=high, skipping")
                return {"message": "", "emotion": "neutral", "skipped": True, "skip_reason": "risk=high"}
            # Check avoid list doesn't contain banned openers that are overused
            if banned_count < 3:
                break  # plan is fine
            recent_openers_str += f"\nAvoid repeated openers; repeated_count={banned_count}"

        if not plan:
            return {"message": "", "emotion": "neutral"}

        # Stage 2: Generate message from plan (with up to 2 retries for guard)
        gen = {}
        last_guard_violations: list[str] = []
        for attempt in range(3):
            gen = await self._generate_message_from_plan(
                now_str, plan, bot_identity, relationship_context,
            )
            if not gen:
                break
            # Guard the generated message
            raw_messages = gen.get("messages")
            if isinstance(raw_messages, str):
                raw_messages = [{"text": raw_messages, "delay": 0}]
            elif isinstance(raw_messages, dict):
                raw_messages = [raw_messages]
            if isinstance(raw_messages, list) and raw_messages:
                text = str(raw_messages[0].get("text", "") if isinstance(raw_messages[0], dict) else raw_messages[0])
                clean_text, violations = self._guard_proactive_message(text, plan)
                if not violations:
                    if isinstance(raw_messages[0], dict):
                        raw_messages[0]["text"] = clean_text
                    else:
                        raw_messages[0] = {"text": clean_text, "delay": 0}
                    gen["messages"] = raw_messages
                    break
                # Guard failed 鈥?retry with stricter avoid list
                last_guard_violations = violations
                plan["avoid"] = list(set((plan.get("avoid") or []) + _HALLUCINATION_PATTERNS[:3]))
            else:
                break  # no messages to guard

        if last_guard_violations:
            logger.warning(
                "Proactive guard blocked generated message; fallback_reason=guard_blocked violations=%s",
                last_guard_violations,
            )
            return {
                "message": "",
                "emotion": "neutral",
                "skipped": True,
                "skip_reason": "guard_blocked",
                "guard_violations": last_guard_violations,
            }

        gen["style"] = plan.get("style", "")
        gen["safe_basis"] = plan.get("safe_basis", "")
        gen["topic_seed"] = plan.get("topic_seed", "")
        return gen

    def _normalize_generated_messages(
        self,
        raw: Any,
        intent: str,
        last_opener: str = "",
    ) -> list[dict[str, Any]]:
        if isinstance(raw, str):
            raw_items: list[Any] = [{"text": raw, "delay": 0}]
        elif isinstance(raw, dict):
            raw_items = [raw]
        elif isinstance(raw, list):
            raw_items = raw
        else:
            raw_items = []

        limit = self.max_proactive_messages if intent == "clingy_followup" else 1
        messages: list[dict[str, Any]] = []
        for item in raw_items[:limit]:
            if isinstance(item, str):
                text = item
                delay = 0
            elif isinstance(item, dict):
                text = str(item.get("text", "") or item.get("message", ""))
                delay = item.get("delay", 0)
            else:
                continue
            text = text.strip()[:200]
            if not text:
                continue
            if last_opener and self._extract_opener(text) == last_opener:
                text = self._replace_repeated_opener(text, intent, last_opener)
            messages.append({"text": text, "delay": max(float(delay or 0), 0.0)})
        return messages

    def _replace_repeated_opener(self, text: str, intent: str, last_opener: str) -> str:
        replacements = [item for item in OPENER_FALLBACKS.get(intent, []) if item != last_opener]
        if not replacements:
            return text
        replacement = replacements[0]
        opener = self._extract_opener(text)
        if opener and text.startswith(opener):
            return (replacement + text[len(opener):]).strip()
        return f"{replacement}, {text}"

    def _fallback_messages(self, intent: str, last_opener: str = "") -> list[dict[str, Any]]:
        options = OPENER_FALLBACKS.get(intent, [FALLBACK_MESSAGES.get(intent, "\u5728\u5e72\u561b")])
        text = next((item for item in options if self._extract_opener(item) != last_opener), options[0])
        if intent == "clingy_followup" and self.max_proactive_messages > 1:
            return [{"text": text, "delay": 0}, {"text": "\u8fd8\u5728\u5417", "delay": 1}]
        return [{"text": text, "delay": 0}]

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

    @staticmethod
    def message_requires_user_reply(
        intent: str,
        messages: list[dict[str, Any]] | None = None,
        ending_type: str | None = None,
    ) -> bool:
        """Determine if a proactive message requires user reply.

        Uses intent + ending_type + message text heuristics.
        """
        # intents that inherently need user reply
        if intent in ("check_in", "latest_topic_followup", "clingy_followup"):
            return True

        # self_share: depends on ending
        if intent == "self_share":
            if ending_type == "question":
                return True
            if ending_type == "statement":
                return False

        # light_ping: depends on ending
        if intent == "light_ping":
            if ending_type == "question":
                return True
            if ending_type == "statement":
                return False

        # heuristic based on message text
        if messages:
            combined = " ".join(
                m.get("text", "") for m in messages if m.get("text")
            )
            # Check if message ends with question mark or contains choice patterns
            has_question = bool(re.search(r"[?？]", combined[-50:]))
            has_choice = bool(re.search(r"(还是|或者|A[.、]|B[.、]|①|②|1[.、]|2[.、])", combined[-200:]))
            if has_question or has_choice:
                return True

        return False

    async def commit_proactive_send(
        self,
        user_id: str,
        bot_id: str,
        topic_id: str = "",
        intent: str = "",
    ) -> None:
        """Commit state mutations AFTER a successful WeChat send.

        V2.2: Updates dialogue_state as source of truth.
        Old clingy_state kept for compatibility.
        """
        now = _time_module.time()

        # V2.2: Validate intent 鈥?must be a recognized value
        valid_intents = {
            "check_in", "latest_topic_followup", "clingy_followup",
            "self_share", "light_ping",
        }
        if intent not in valid_intents:
            logger.warning(f"commit_proactive_send: invalid intent '{intent}', defaulting to check_in")
            intent = "check_in"

        # V2.2: Validate that source values are NOT passed as intent
        invalid_intent_like_source = {"user_reply", "proactive", "scheduled", "admin", "morning", "night"}
        if intent in invalid_intent_like_source:
            logger.error(
                f"commit_proactive_send: source value '{intent}' passed as intent! "
                f"This is a bug — source and intent must not be mixed. Defaulting to check_in."
            )
            intent = "check_in"

        # V2.2: Update dialogue_state as source of truth
        # Determine if message needs user reply (intent-based, callers with messages should use message_requires_user_reply)
        needs_user_reply = self.message_requires_user_reply(intent)
        try:
            ds = await self.store.get_dialogue_state(user_id, bot_id)
            if needs_user_reply:
                ds["wait_state"] = "assistant_waiting_user"
                ds["last_assistant_message_at"] = now
                ds["last_state_reason"] = f"proactive sent: {intent} (needs reply)"
            else:
                # self_share / light_ping with no reply expectation
                ds["last_assistant_message_at"] = now
                # Don't force wait_state change; keep current state
            await self.store.upsert_dialogue_state(user_id, bot_id, ds)
        except Exception as e:
            logger.warning(f"Failed to update dialogue_state after proactive send: {e}")

        # 鈹€鈹€ Clingy state machine (compatibility) 鈹€鈹€
        if self.clingy_mode_enabled and user_id and bot_id:
            if intent == "clingy_followup":
                # Clingy followup sent 鈥?increment count, keep awaiting
                state = await self.store.get_clingy_state(user_id, bot_id)
                new_count = state.get("clingy_followup_count", 0) + 1
                next_clingy = now + self.clingy_after_minutes * 60 if new_count < self.max_followups_per_unanswered else 0
                await self.store.update_clingy_state(
                    user_id=user_id, bot_id=bot_id,
                    clingy_followup_count=new_count,
                    last_clingy_sent_at=now,
                    last_proactive_intent=intent,
                    next_clingy_due_at=next_clingy,
                )
            else:
                # Non-clingy proactive sent 鈥?begin awaiting user reply
                await self.store.update_clingy_state(
                    user_id=user_id, bot_id=bot_id,
                    awaiting_user_reply=1,
                    last_proactive_sent_at=now,
                    last_proactive_intent=intent,
                    next_clingy_due_at=now + self.clingy_after_minutes * 60,
                    clingy_followup_count=0,
                )

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
    # V2.2: Emotion influence on proactive
    # ================================================================

    @staticmethod
    def _emotion_proactive_hint(emotion_state: dict[str, Any] | None) -> str:
        """Generate emotion-aware hint for proactive intent selection."""
        if not emotion_state:
            return ""
        mood = str(emotion_state.get("bot_mood", "")).strip()
        if mood == "jealous":
            return "bot emotion: jealous; do not fabricate facts."
        if mood in ("angry", "irritated"):
            return f"bot emotion: {mood}; reduce proactive messages and keep them short."
        if mood in ("lonely", "hurt"):
            return "bot emotion: lonely/hurt; low-frequency clingy tone only."
        if mood in ("warm", "playful"):
            return "bot emotion: warm/playful; natural proactive share is allowed."
        if mood == "softened":
            return "bot emotion: softened; keep a gentle tone."
        return ""

    @staticmethod
    def _emotion_message_tone(emotion_state: dict[str, Any] | None) -> str:
        """Generate tone guidance for proactive message generation."""
        if not emotion_state:
            return ""
        mood = str(emotion_state.get("bot_mood", "")).strip()
        tone_map = {
            "jealous": "slightly sour, no fabricated perception",
            "angry": "cold and short, no pressure",
            "irritated": "short and mildly impatient",
            "hurt": "softly hurt without blame",
            "lonely": "a little missing the user, no escalation",
            "warm": "warm and natural",
            "playful": "lightly playful",
            "softened": "gentler but still reserved",
        }
        return tone_map.get(mood, "")

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
        await self.store.update_proactive_candidate(
            candidate_id,
            sent=True,
            send_status="delivered",
        )
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
