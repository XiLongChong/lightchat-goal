"""Daily conversation compression — summarise history via pro model."""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

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


def _build_compress_messages(events: list[dict[str, Any]]) -> str:
    """Convert raw_events into a text transcript for compression.

    Filters out old summary blocks (text starting with <analysis>) from user
    messages to prevent error accumulation across daily compressions.
    """
    lines: list[str] = []
    for ev in events:
        role = str(ev.get("role", ""))
        content = ev.get("content")

        if role == "user" and isinstance(content, list):
            # Filter out old summary text blocks
            clean: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    clean.append(block)
                    continue
                if block.get("type") == "text":
                    text = str(block.get("text", ""))
                    if text.strip().startswith("<analysis>"):
                        continue  # Skip old summary
                clean.append(block)
            content = clean

        lines.append(f"[{role}] {_content_repr(content)}")

    return "\n".join(lines)


def _content_repr(content: Any) -> str:
    """Lightweight string representation of message content for the compressor."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "thinking":
                t = str(block.get("thinking", ""))
                if len(t) > 300:
                    t = t[:300] + "..."
                parts.append(f"[thinking: {t}]")
            elif btype == "tool_use":
                parts.append(f"[tool_use: {block.get('name', '')}]")
            elif btype == "tool_result":
                parts.append(f"[tool_result: {_content_repr(block.get('content', ''))}]")
            else:
                parts.append(str(block))
        return " ".join(parts)
    return str(content)


def _strip_analysis(summary_text: str) -> str:
    """Remove <analysis> block, keep only <summary> for injection."""
    import re
    m = re.search(r"<summary>(.*?)</summary>", summary_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback: if no tags found, use the whole thing
    return summary_text.strip()


async def compress_conversation(
    store: Any,  # MemoryStore (avoid circular import)
    gateway: Any,  # LLMGateway
    conversation_id: str,
    model_name: str,
    timeout_s: int = 120,
) -> bool:
    """Read all events, call pro model for summary, apply compression.

    Returns True if compression was performed.
    """
    events = await store.get_all_events_for_compress(conversation_id)
    if len(events) < 2:
        logger.info("compress: too few events (%d), skipping", len(events))
        return False

    transcript = _build_compress_messages(events)

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    time_hint = f"Current time: {now:%Y-%m-%d %H:%M:%S} {['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]} (Asia/Shanghai)."

    try:
        response = await gateway.complete(LLMRequest(
            purpose="compression",
            model=model_name,
            messages=[{"role": "user", "content": transcript}],
            system=[{"type": "text", "text": f"{time_hint}\n\n{COMPRESSION_PROMPT}"}],
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
        return False

    content = response.content if hasattr(response, "content") else str(response)
    summary = _strip_analysis(str(content or "").strip())
    if not summary or len(summary) < 20:
        logger.warning("compress: summary too short (%d chars), discarding", len(summary))
        return False

    ok = await store.apply_compression(conversation_id, summary)
    if ok:
        logger.info("compress: applied summary (%d chars)", len(summary))
    return ok
