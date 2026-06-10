"""Daily conversation compression — summarise history via pro model."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from agent.llm_types import LLMRequest

logger = logging.getLogger(__name__)

COMPRESSION_PROMPT = """You are compressing a WeChat chat history between a user and an AI assistant into a context summary for the next session. You will be given the full conversation. Your output will replace the raw chat history — the assistant will ONLY see your summary, not the original messages.

CRITICAL: Do NOT call any tools. Respond with TEXT ONLY.
Your entire response must be plain text: an <analysis> block followed by a <summary> block.

<analysis>
Organize your thoughts here. Go through the conversation chronologically, identify what matters and what doesn't. This block will be discarded — it is only for you to think through the compression.
</analysis>

<summary>
Output the structured summary below. The assistant will ONLY see this block — be precise and thorough.
</summary>

## What to preserve in the summary

1. User Identity & Preferences
   - Name, nickname, gender, age, occupation, location — anything the user revealed about themselves
   - Likes, dislikes, hobbies, habits, dietary preferences
   - Communication style: how they talk, sentence length, tone, emoji usage

2. Emotional State
   - Current mood and recent emotional shifts
   - Things bothering them or making them happy
   - Relationship tone with the assistant (trusting, skeptical, playful, etc.)

3. Topics Discussed
   - For each topic or subject: what was discussed, key observations, conclusions reached
   - Knowledge or expertise the user demonstrated
   - Opinions the user expressed on specific subjects

4. Decisions & Commitments
   - What the user decided to do or said they would do
   - What the assistant promised to follow up on
   - Pending tasks or "let's talk about this later" items

5. Tool Actions
   - Describe tool calls and their outcomes in plain language. Do NOT reproduce raw JSON.

6. Verbatim Quotes (IMPORTANT)
   - Preserve VERBATIM the user's messages that contain: strong emotions, personal revelations, important decisions, or unique phrasing that reveals character
   - List each as: [context] "exact message"

## What to ignore

- Pure greetings and farewells with no substance
- Completed one-off topics with no follow-up or lasting impact
- Repeated or near-identical content
- System reminders, command output, local-command blocks — these are tool artifacts, not conversation

## Format

<analysis>
[Your working notes — will be discarded]
</analysis>

<summary>
1. User Identity & Preferences:
   ...

2. Emotional State:
   ...

3. Topics Discussed:
   ...

4. Decisions & Commitments:
   ...

5. Tool Actions:
   ...

6. Verbatim Quotes:
   "[message]"
   "[message]"
   ...

7. Pending:
   ...
</summary>"""

# Compression window: 3:56 AM — 4:25 AM (29 minutes, China time)
_WINDOW_START_OFFSET = timedelta(minutes=-4)   # 3:56 relative to 4:00
_WINDOW_END_OFFSET = timedelta(minutes=25)      # 4:25 relative to 4:00

_CST = ZoneInfo("Asia/Shanghai")


def compute_summary_date() -> str:
    """Return the date a summary belongs to.

    Before 5:00 AM → previous day (covers the 3:56–4:25 compression window).
    5:00 AM and after → same day.
    """
    now = datetime.now(_CST)
    if now.hour < 5:
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def _filter_summary_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip <summary> text blocks from user messages to prevent re-compression."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        role = msg.get("role", "")
        if role == "user" and isinstance(content, list):
            filtered: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    filtered.append(block)
                    continue
                if block.get("type") == "text":
                    text = str(block.get("text", ""))
                    if text.strip().startswith("<summary>"):
                        continue
                filtered.append(block)
            if not filtered:
                continue
            result.append({"role": role, "content": filtered})
        else:
            result.append({"role": role, "content": content})
    return result


def _build_compression_call(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the messages array for a compression LLM call.

    1. Filter out old <summary> blocks
    2. Append a fake user message with the compression prompt
    """
    msgs = _filter_summary_blocks(messages)
    msgs.append({"role": "user", "content": COMPRESSION_PROMPT})
    return msgs


def _extract_summary(text: str) -> str | None:
    """Extract the <summary> block from the model response."""
    m = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL)
    if m:
        summary = m.group(1).strip()
        if len(summary) >= 20:
            return summary
    return None


def _in_compress_window(now: datetime) -> bool:
    """Check whether *now* falls inside the daily compression window."""
    today_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
    return today_4am + _WINDOW_START_OFFSET <= now < today_4am + _WINDOW_END_OFFSET


async def compress_conversation(
    messages: list[dict[str, Any]],
    gateway: Any,  # LLMGateway
    model_name: str,
    timeout_s: int = 120,
) -> str | None:
    """Call the pro model to summarise *messages*.  Returns summary text or None.

    *messages* should be the current in-memory conversation (conversation_ctx._messages).
    """
    if len(messages) < 4:
        logger.info("compress: too few messages (%d), skipping", len(messages))
        return None

    msgs = _build_compression_call(messages)

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    time_hint = (
        f"Current time: {now:%Y-%m-%d %H:%M:%S} "
        f"{['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]} "
        f"(Asia/Shanghai)."
    )

    try:
        response = await gateway.complete(LLMRequest(
            purpose="compression",
            model=model_name,
            messages=msgs,
            system=[{"type": "text", "text": time_hint}],
            temperature=0.3,
            max_tokens=4096,
            timeout_s=timeout_s,
            response_mode="text",
            thinking="disabled",
            tools=None,
            tool_choice=None,
            stream=False,
        ))
    except Exception:
        logger.exception("compress: LLM call failed")
        return None

    content = response.content if hasattr(response, "content") else str(response)
    summary = _extract_summary(str(content or "").strip())
    if not summary:
        logger.warning("compress: no valid summary in response")
        return None

    logger.info("compress: summary extracted (%d chars)", len(summary))
    return summary


async def maybe_compress_daily(
    messages: list[dict[str, Any]],
    last_compressed_at: float,
    gateway: Any,
    model_name: str,
) -> str | None:
    """Daily compression entry point.  Returns summary text or None.

    Guards: time-window check + already-compressed-today check.
    """
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)

    if not _in_compress_window(now):
        return None

    today_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if last_compressed_at >= today_4am.timestamp():
        return None

    logger.info("Starting daily compression (last=%s)", last_compressed_at)
    return await compress_conversation(messages, gateway, model_name)
