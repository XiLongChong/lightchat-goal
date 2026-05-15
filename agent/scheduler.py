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
