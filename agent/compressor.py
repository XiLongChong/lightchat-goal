"""Daily conversation compression — summarise history via pro model."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from agent.llm_types import LLMRequest
from memory.long_term import MemoryOperation

logger = logging.getLogger(__name__)

COMPRESSION_PROMPT = """You are compressing a WeChat chat history between a user and an AI assistant into a context summary for the next session. You will be given the full conversation. Your output will replace the raw chat history — the assistant will ONLY see your summary, not the original messages.

CRITICAL: Do NOT call any tools. Respond with TEXT ONLY.
Your entire response must contain an <analysis> block, a <summary> block, and
a <memory_operations> JSON block.

<analysis>
Organize your thoughts here. Go through the conversation chronologically, identify what matters and what doesn't. This block will be discarded — it is only for you to think through the compression.
</analysis>

<summary>
Output the structured summary below. The assistant will ONLY see this block — be precise and thorough.
</summary>

<memory_operations>
[]
</memory_operations>

## Long-term memory review

Long-term memory must be rare. Save only information that the user explicitly
asked to remember, or information that is stable, cannot be reliably recovered
from recent summaries, and whose loss would cause material misunderstanding or
damage relationship continuity.

- Current explicit user information overrides existing memory.
- Existing memory is fallible historical context.
- Do not save temporary mood, ordinary chat, reminders, or pending tasks.
- When uncertain, return no memory operation.
- Commands may only be "create" or "str_replace". Never delete.
- Change at most 3 topic files, plus at most one MEMORY.md update.
- Every operation requires a non-empty reason and verbatim evidence from today's conversation.
- A new topic requires both a topic-file create and a MEMORY.md update.
- Keep topic files as current truth; do not preserve obsolete values.
- Every topic file starts with "# Title" and includes "更新时间：YYYY-MM-DD".
- MEMORY.md entries use: - [Title](filename.md) - one sentence summary
- Topic filenames must be lowercase English kebab-case Markdown.

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
</summary>

<memory_operations>
[]
</memory_operations>"""

# Compression window: 3:56 AM — 4:25 AM (29 minutes, China time)
_WINDOW_START_OFFSET = timedelta(minutes=-4)   # 3:56 relative to 4:00
_WINDOW_END_OFFSET = timedelta(minutes=25)      # 4:25 relative to 4:00

_CST = ZoneInfo("Asia/Shanghai")


@dataclass(slots=True)
class CompressionResult:
    summary: str
    memory_operations: list[MemoryOperation]


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
    memory_context: str = "",
) -> list[dict[str, Any]]:
    """Build the messages array for a compression LLM call.

    1. Filter out old <summary> blocks
    2. Append a fake user message with the compression prompt
    """
    msgs = _filter_summary_blocks(messages)
    prompt = COMPRESSION_PROMPT
    if memory_context.strip():
        prompt += (
            "\n\n## Existing long-term memory\n"
            "Use this exact content for deduplication and precise replacements.\n"
            "<existing_long_term_memory>\n"
            f"{memory_context.strip()}\n"
            "</existing_long_term_memory>"
        )
    msgs.append({"role": "user", "content": prompt})
    return msgs


def _extract_summary(text: str) -> str | None:
    """Extract the <summary> block from the model response."""
    m = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL)
    if m:
        summary = m.group(1).strip()
        if len(summary) >= 20:
            return summary
    return None


def _extract_memory_operations(text: str) -> list[MemoryOperation] | None:
    match = re.search(r"<memory_operations>(.*?)</memory_operations>", text, re.DOTALL)
    if not match:
        return None
    try:
        raw = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list):
        return None
    operations: list[MemoryOperation] = []
    topic_paths: set[str] = set()
    index_updates = 0
    for item in raw:
        if not isinstance(item, dict):
            return None
        command = str(item.get("command") or "")
        path = str(item.get("path") or "")
        reason = str(item.get("reason") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if command not in {"create", "str_replace"} or not path or not reason or not evidence:
            return None
        if path == "MEMORY.md":
            index_updates += 1
        else:
            topic_paths.add(path)
        operations.append(MemoryOperation(
            command=command,
            path=path,
            file_text=str(item.get("file_text") or ""),
            old_str=str(item.get("old_str") or ""),
            new_str=str(item.get("new_str") or ""),
            reason=reason,
            evidence=evidence,
        ))
    if len(topic_paths) > 3 or index_updates > 1:
        return None
    return operations


def validate_memory_operations(
    messages: list[dict[str, Any]],
    operations: list[MemoryOperation],
) -> bool:
    user_text_parts: list[str] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            user_text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text") or "")
                    if not text.lstrip().startswith("<summary>"):
                        user_text_parts.append(text)
    user_text = "\n".join(user_text_parts)
    index_updates = [op for op in operations if op.path == "MEMORY.md"]
    for operation in operations:
        if operation.evidence not in user_text:
            return False
        if operation.command == "create" and operation.path != "MEMORY.md":
            if not any(operation.path in (op.file_text + op.new_str) for op in index_updates):
                return False
    return True


def _in_compress_window(now: datetime) -> bool:
    """Check whether *now* falls inside the daily compression window."""
    today_4am = now.replace(hour=4, minute=0, second=0, microsecond=0)
    return today_4am + _WINDOW_START_OFFSET <= now < today_4am + _WINDOW_END_OFFSET


async def compress_conversation(
    messages: list[dict[str, Any]],
    gateway: Any,  # LLMGateway
    model_name: str,
    timeout_s: int = 120,
    memory_context: str = "",
) -> CompressionResult | None:
    """Call the pro model to summarise messages and review long-term memory.

    *messages* should be the current in-memory conversation (conversation_ctx._messages).
    """
    if len(messages) < 4:
        logger.info("compress: too few messages (%d), skipping", len(messages))
        return None

    msgs = _build_compression_call(messages, memory_context)

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
    response_text = str(content or "").strip()
    summary = _extract_summary(response_text)
    operations = _extract_memory_operations(response_text)
    if (
        not summary
        or operations is None
        or not validate_memory_operations(messages, operations)
    ):
        logger.warning("compress: no valid summary or memory operations in response")
        return None

    logger.info(
        "compress: summary extracted (%d chars), memory operations=%d",
        len(summary),
        len(operations),
    )
    return CompressionResult(summary=summary, memory_operations=operations)


async def maybe_compress_daily(
    messages: list[dict[str, Any]],
    last_compressed_at: float,
    gateway: Any,
    model_name: str,
    memory_context: str = "",
) -> CompressionResult | None:
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
    return await compress_conversation(
        messages,
        gateway,
        model_name,
        memory_context=memory_context,
    )
