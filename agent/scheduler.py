"""Proactive messaging scheduler — Safeguard #6.

Two-phase check:
  Phase 1: Hard rules (no LLM call) — sleep hours, chat gap, ignore streak
  Phase 2: LLM decision — only if Phase 1 passes

Borrowed design from emoRebot scheduler-agent.ts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from memory.store import MemoryStore

logger = logging.getLogger(__name__)

# Timezone for Chinese users
CST = timezone(timedelta(hours=8))


SCHEDULER_PROMPT = """你是一个消息调度器。判断现在是否应该主动给用户发一条消息。

## 背景
当前的日期时间、天气、用户状态等信息如下。用户在微信上和你聊天。
现在用户没有发消息给你，是你在考虑要不要主动发消息。

## 规则
1. 如果你觉得用户需要安静（深夜、可能在工作、最近对话很少），就不要发
2. 如果你有有趣的事可以说（天气变化、用户之前提过的事、一个话题），可以发
3. 消息要短而自然，像朋友随口提起，不要刻意
4. 如果不确定，选择不发

## 输出格式
{"should_message": false, "message": "", "emotion": "neutral", "reason": ""}

## 上下文
{context}"""

# V2.1: 3-stage proactive pipeline prompt
CANDIDATE_PROMPT = """你是一个消息调度器。判断现在是否有理由主动给用户发消息。

不生成消息内容，只生成候选原因（candidate reason）。

候选原因类型：
- special_date: 今天是特殊日期（节假日、纪念日等）
- unfinished_topic: 上次/昨晚的话题没有结束
- project_push: 用户正在积极推进某个项目
- long_silence: 用户沉默了很长时间
- user_reminder: 用户之前明确留了提醒或约定
- none: 没有任何值得发消息的理由

## 上下文
当前时间: {now_str}
最近对话摘要: {recap_text}
用户最近记忆: {memories_text}
距离上次对话: {chat_gap}
今日已主动发: {proactive_today}

## 输出格式
{{"reason_type": "none", "reason_detail": "", "confidence": 0.0}}

如果reason_type是"none"，confidence必须为0。"""

GENERATION_PROMPT = """你是一个主动消息生成器。用户是辣椒。

根据候选原因生成一条主动消息。

## 候选原因
类型: {reason_type}
详情: {reason_detail}

## 上下文
当前时间: {now_str}
关系状态: {rel_text}
最近记忆: {memories_text}

## 规则
- 自然简短，像微信朋友随口提起
- 不要提"系统/调度/自动/候选原因"这些词
- 不要当成用户说过的话来回复
- 50字以内
- 可以不发（输出空消息）

## 输出格式
{{"message": "", "emotion": "neutral"}}"""


class SchedulerAgent:
    """Periodic proactive messaging agent."""

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
        self.sleep_start = cfg.get("sleep_start", 0)
        self.sleep_end = cfg.get("sleep_end", 7)
        self.min_chat_gap_hours = cfg.get("min_chat_gap_hours", 2)
        self.max_ignored_streak = cfg.get("max_ignored_streak", 3)
        self.max_daily = cfg.get("max_daily_proactive", 5)
        self.min_interval = cfg.get("min_proactive_interval", 90)

    async def should_proactive_message(
        self,
        recent_memories: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Check if we should send a proactive message.

        Returns:
            {"should_message": bool, "message": str, "emotion": str, "reason": str}
        """
        # ---- Phase 1: Hard rules (no LLM call) ----
        now_cst = datetime.now(CST)
        hour = now_cst.hour

        # Sleep hours check
        if self.sleep_start < self.sleep_end:
            # Normal range: e.g. 0-7
            if self.sleep_start <= hour < self.sleep_end:
                return self._deny("sleep hours")
        else:
            # Wraps around midnight: e.g. 23-7
            if hour >= self.sleep_start or hour < self.sleep_end:
                return self._deny("sleep hours")

        # Scheduler state from DB
        state = await self.store.get_scheduler_state()
        last_chat = state.get("last_chat_time", 0)
        last_proactive = state.get("last_proactive_time", 0)
        ignored = state.get("consecutive_ignored", 0)
        msgs_today = state.get("messages_today", 0)

        # Min chat gap: don't interrupt recent conversation
        from time import time
        if last_chat > 0:
            hours_since_chat = (time() - last_chat) / 3600
            if hours_since_chat < self.min_chat_gap_hours:
                return self._deny(f"chat too recent ({hours_since_chat:.1f}h ago)")

        # Ignored streak: user hasn't responded to our proactive messages
        if ignored >= self.max_ignored_streak:
            return self._deny(f"ignored {ignored} times in a row")

        # Daily cap
        if msgs_today >= self.max_daily:
            return self._deny(f"daily cap reached ({msgs_today}/{self.max_daily})")

        # Min interval between proactive messages
        if last_proactive > 0:
            mins_since_last = (time() - last_proactive) / 60
            if mins_since_last < self.min_interval:
                return self._deny(f"too soon ({mins_since_last:.0f}m ago)")

        # ---- Phase 2: LLM decision ----
        try:
            return await self._llm_decide(now_cst, recent_memories, state)
        except Exception as e:
            logger.warning(f"Scheduler LLM call failed: {e}")
            return self._deny(f"LLM error: {e}")

    def _deny(self, reason: str) -> dict[str, Any]:
        return {
            "should_message": False,
            "message": "",
            "emotion": "neutral",
            "reason": reason,
        }

    async def _llm_decide(
        self,
        now_cst: datetime,
        recent_memories: list[dict[str, Any]] | None,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Phase 2: Ask LLM to decide if we should message."""
        import time as time_module

        now_str = now_cst.strftime("%Y年%m月%d日 %H:%M 周%u")

        memories_text = "无"
        if recent_memories:
            lines = []
            for m in recent_memories[:5]:
                lines.append(f"- {m.get('content', '')}")
            memories_text = "\n".join(lines)

        context = f"""当前时间: {now_str}
用户最近相关的记忆:
{memories_text}
今日已主动发消息: {state.get('messages_today', 0)}/{self.max_daily} 条
连续被用户忽略: {state.get('consecutive_ignored', 0)} 次"""

        prompt = SCHEDULER_PROMPT.format(context=context)

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个消息调度器。只输出JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=256,
        )

        raw = response.choices[0].message.content or ""

        import json
        from utils.json_parser import extract_json

        try:
            json_str = extract_json(raw)
            result = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            return self._deny("JSON parse failed (fallback: don't send)")

        should_send = result.get("should_message", False)
        if not isinstance(should_send, bool):
            should_send = False

        message = str(result.get("message", ""))[:200]
        emotion = str(result.get("emotion", "neutral"))[:20]
        reason = str(result.get("reason", ""))[:100]

        # Additional safety: refuse empty messages
        if should_send and not message.strip():
            should_send = False
            reason = "empty message refused"

        return {
            "should_message": should_send,
            "message": message,
            "emotion": emotion,
            "reason": reason,
        }

    async def record_sent(self):
        """Call after successfully sending a proactive message."""
        from time import time

        state = await self.store.get_scheduler_state()
        await self.store.update_scheduler_state({
            "messages_today": state.get("messages_today", 0) + 1,
            "last_proactive_time": time(),
            "consecutive_ignored": 0,
        })

    async def record_chat_activity(self):
        """Call when user sends a message (resets ignore counter)."""
        from time import time

        await self.store.update_scheduler_state({
            "last_chat_time": time(),
            "consecutive_ignored": 0,
        })

    async def reset_daily_counter(self):
        """Reset the daily message counter. Call at midnight."""
        await self.store.update_scheduler_state({
            "messages_today": 0,
        })

    # ================================================================
    # V2.1: 3-stage proactive pipeline
    # ================================================================

    async def run_proactive_pipeline(
        self,
        recent_memories: list[dict[str, Any]] | None = None,
        recap_text: str = "",
        bot_id: str = "",
    ) -> dict[str, Any]:
        """V2.1 3-stage proactive pipeline.

        Stage 1: Candidate reason generation
        Stage 2: Decision gate
        Stage 3: Message generation (only if gate passes)

        Returns:
            {"sent": bool, "message": str, "reason_type": str,
             "reason_detail": str, "stage": str}
        """
        from time import time as time_module

        now_cst = datetime.now(CST)
        now_ts = time_module()

        # ---- Hard pre-check (before any LLM call) ----
        hour = now_cst.hour
        if self.sleep_start < self.sleep_end:
            if self.sleep_start <= hour < self.sleep_end:
                return self._pipeline_result(
                    False, "sleep hours", stage="pre_check"
                )
        else:
            if hour >= self.sleep_start or hour < self.sleep_end:
                return self._pipeline_result(
                    False, "sleep hours", stage="pre_check"
                )

        state = await self.store.get_scheduler_state()
        last_chat = state.get("last_chat_time", 0)
        last_proactive = state.get("last_proactive_time", 0)
        ignored = state.get("consecutive_ignored", 0)
        msgs_today = state.get("messages_today", 0)

        if last_chat > 0:
            hours_since_chat = (now_ts - last_chat) / 3600
            if hours_since_chat < self.min_chat_gap_hours:
                return self._pipeline_result(
                    False, f"chat too recent ({hours_since_chat:.1f}h)",
                    stage="pre_check"
                )

        if ignored >= self.max_ignored_streak:
            return self._pipeline_result(
                False, f"ignored {ignored} times in a row",
                stage="pre_check"
            )

        if msgs_today >= self.max_daily:
            return self._pipeline_result(
                False, f"daily cap ({msgs_today}/{self.max_daily})",
                stage="pre_check"
            )

        if last_proactive > 0:
            mins_since = (now_ts - last_proactive) / 60
            if mins_since < self.min_interval:
                return self._pipeline_result(
                    False, f"too soon ({mins_since:.0f}m ago)",
                    stage="pre_check"
                )

        # ---- Stage 1: Candidate reason generation ----
        try:
            candidate = await self._generate_candidates(
                now_cst, recent_memories, recap_text, state
            )
        except Exception as e:
            logger.warning(f"Stage 1 candidate generation failed: {e}")
            return self._pipeline_result(
                False, f"candidate error: {e}", stage="stage1"
            )

        candidate_id = ""
        try:
            candidate_id = await self.store.create_proactive_candidate(
                reason_type=candidate.get("reason_type", "none"),
                reason_detail=candidate.get("reason_detail", ""),
            )
        except Exception as e:
            logger.warning(f"Failed to persist candidate: {e}")

        reason_type = candidate.get("reason_type", "none")
        if reason_type == "none":
            return self._pipeline_result(
                False, "no candidate reason", stage="stage1",
                candidate_id=candidate_id,
            )

        # ---- Stage 2: Decision gate ----
        gate_result = self._decision_gate(
            reason_type=reason_type,
            reason_detail=candidate.get("reason_detail", ""),
            state=state,
        )
        if not gate_result["pass"]:
            if candidate_id:
                await self.store.update_proactive_candidate(
                    candidate_id,
                    passed_gate=False,
                    gate_detail=gate_result["reason"],
                )
            return self._pipeline_result(
                False, gate_result["reason"], stage="stage2",
                reason_type=reason_type,
                candidate_id=candidate_id,
            )

        if candidate_id:
            await self.store.update_proactive_candidate(
                candidate_id,
                passed_gate=True,
                gate_detail="passed",
            )

        # ---- Stage 3: Message generation ----
        try:
            gen_result = await self._generate_proactive_message(
                now_cst,
                reason_type=reason_type,
                reason_detail=candidate.get("reason_detail", ""),
                recent_memories=recent_memories,
            )
        except Exception as e:
            logger.warning(f"Stage 3 message generation failed: {e}")
            return self._pipeline_result(
                False, f"generation error: {e}", stage="stage3",
                reason_type=reason_type,
                candidate_id=candidate_id,
            )

        message = gen_result.get("message", "").strip()[:200]
        emotion = gen_result.get("emotion", "neutral")

        if not message:
            return self._pipeline_result(
                False, "empty message refused", stage="stage3",
                reason_type=reason_type,
                candidate_id=candidate_id,
            )

        if candidate_id:
            await self.store.update_proactive_candidate(
                candidate_id,
                generated_message=message,
            )

        return {
            "sent": True,
            "message": message,
            "emotion": emotion,
            "reason": f"{reason_type}: {candidate.get('reason_detail', '')}",
            "reason_type": reason_type,
            "reason_detail": candidate.get("reason_detail", ""),
            "candidate_id": candidate_id,
            "stage": "completed",
        }

    async def _generate_candidates(
        self,
        now_cst: datetime,
        recent_memories: list[dict[str, Any]] | None,
        recap_text: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Stage 1: Generate candidate reasons for proactive messaging."""
        from time import time as time_module

        now_str = now_cst.strftime("%Y年%m月%d日 %H:%M 周%u")

        memories_text = "无"
        if recent_memories:
            lines = [f"- {m.get('content', '')}" for m in recent_memories[:5]]
            memories_text = "\n".join(lines)

        last_chat = state.get("last_chat_time", 0)
        if last_chat > 0:
            hours = (time_module() - last_chat) / 3600
            chat_gap = f"{hours:.1f}小时"
        else:
            chat_gap = "从未聊过"

        proactive_today = state.get("messages_today", 0)

        prompt = CANDIDATE_PROMPT.format(
            now_str=now_str,
            recap_text=recap_text or "无",
            memories_text=memories_text,
            chat_gap=chat_gap,
            proactive_today=proactive_today,
        )

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个消息调度器。只输出JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=256,
        )

        raw = response.choices[0].message.content or ""

        import json
        from utils.json_parser import extract_json

        try:
            json_str = extract_json(raw)
            result = json.loads(json_str)
            return {
                "reason_type": str(result.get("reason_type", "none")),
                "reason_detail": str(result.get("reason_detail", "")),
                "confidence": float(result.get("confidence", 0)),
            }
        except (json.JSONDecodeError, ValueError):
            return {"reason_type": "none", "reason_detail": "", "confidence": 0.0}

    def _decision_gate(
        self,
        reason_type: str,
        reason_detail: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Stage 2: Decision gate — check whether to proceed.

        Pure logic, no LLM call. Returns {"pass": bool, "reason": str}.
        """
        # Check reason_type is valid
        valid_reasons = {
            "special_date", "unfinished_topic", "project_push",
            "long_silence", "user_reminder", "morning_greeting",
        }
        if reason_type not in valid_reasons:
            return {"pass": False, "reason": f"invalid reason_type: {reason_type}"}

        # Check reason_detail is concrete enough (at least 3 chars)
        if len(reason_detail.strip()) < 3:
            return {"pass": False, "reason": "reason_detail too vague"}

        # All other checks already done in pre_check
        return {"pass": True, "reason": "ok"}

    async def _generate_proactive_message(
        self,
        now_cst: datetime,
        reason_type: str,
        reason_detail: str,
        recent_memories: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Stage 3: Generate the actual proactive message text."""
        now_str = now_cst.strftime("%Y年%m月%d日 %H:%M 周%u")
        rel_text = "熟悉度: 5/10 | 氛围: neutral"

        memories_text = "无"
        if recent_memories:
            lines = [f"- {m.get('content', '')}" for m in recent_memories[:3]]
            memories_text = "\n".join(lines)

        prompt = GENERATION_PROMPT.format(
            reason_type=reason_type,
            reason_detail=reason_detail,
            now_str=now_str,
            rel_text=rel_text,
            memories_text=memories_text,
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

    async def record_proactive_sent(self, candidate_id: str):
        """Mark a proactive candidate as sent."""
        await self.store.update_proactive_candidate(candidate_id, sent=True)
        await self.record_sent()

    @staticmethod
    def _pipeline_result(
        sent: bool,
        reason: str,
        stage: str = "",
        reason_type: str = "",
        candidate_id: str = "",
    ) -> dict[str, Any]:
        return {
            "sent": sent,
            "message": "",
            "emotion": "neutral",
            "reason": reason,
            "reason_type": reason_type,
            "candidate_id": candidate_id,
            "stage": stage,
        }
