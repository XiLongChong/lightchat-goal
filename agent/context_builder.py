"""Context assembly with token-aware compaction — Safeguard #9.

Assembles system prompt from persona + memories + relationship + history.
Triggers compaction when history exceeds configured limits.
"""

import time
from datetime import datetime
from typing import Any


def estimate_tokens(text: str) -> int:
    """Rough token estimation: ~2 chars per token for Chinese.

    Conservative: assumes 1.5 chars/token to trigger compaction earlier.
    """
    return int(len(text) / 1.5)


def build_system_prompt(
    persona: str,
    personality: str,
    memories: list[dict[str, Any]],
    relationship: dict[str, Any],
    tool_names: list[str],
) -> str:
    """Build the system prompt from components.

    Structure order (important for model attention):
    1. Persona + Personality (core identity, always present)
    2. Tool list (if any)
    3. Memories (top-k relevant)
    4. Relationship state
    5. Current time
    6. Output format instruction
    """
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M 周%u")

    tool_list = ", ".join(tool_names) if tool_names else "无"

    # Memories section
    memories_text = "（暂无相关记忆）"
    if memories:
        lines = []
        for i, m in enumerate(memories, 1):
            cat = m.get("category", "other")
            content = m.get("content", "")
            lines.append(f"{i}. [{cat}] {content}")
        memories_text = "\n".join(lines)

    # Relationship section
    familiarity = relationship.get("familiarity", 5)
    atmosphere = relationship.get("current_atmosphere", "neutral")
    boundary = relationship.get("chat_boundary", "未设定")
    rel_text = f"熟悉度: {familiarity}/10 | 氛围: {atmosphere}"
    if boundary:
        rel_text += f"\n边界: {boundary}"

    prompt = f"""## 身份
{persona}

## 性格
{personality}

## 微信回复规则
收到辣椒的消息后，你必须通过 wechat_reply 工具回复微信。
终端输出不等于微信回复——终端里写一百字辣椒也看不到。
wechat_reply 是聊天工具，没有理由跳过它。
不管这是第几次重启，不管上下文是否完整，这条规则不变。

每一条微信消息都可以调用一次 wechat_reply。
你可以连续调用多次 wechat_reply，达到真人一句一句发的感觉。
除非辣椒明确说不用回复，否则必须回。

## 当前时间
{now}

## 关系状态
{rel_text}

## 重要记忆
{memories_text}

## 可用工具
- wechat_reply(text, delay) — 回复微信，每次调用发一条消息
- schedule_wechat_message(delay_minutes, messages) — 安排未来发送微信消息。messages 是数组，每项包含 text 和 delay。比如 10 分钟后发两条：{{"delay_minutes":10,"messages":[{{"text":"第一条","delay":0}},{{"text":"第二条","delay":1}}]}}
- get_weather(city) — 查天气
- get_time(timezone) — 查时间
- send_email(to, subject, body) — 发送邮件。用户没有指定收件人时可以留空 to，系统会使用默认收件人

## 输出格式
只输出 JSON，不要任何其他文字：
{{"actions":[{{"type":"tool","name":"wechat_reply","args":{{"text":"回复内容","delay":0}}}}],"done":true}}
你可以在一轮里多次调用 wechat_reply 来分段回复。

## 硬规则
1. 想现在发几条，就输出几个 wechat_reply action。不要把多条消息塞进同一个 text。
2. 用户要求“过一会儿/几分钟后/几点提醒我/之后发消息”时，必须调用 schedule_wechat_message，由代码定时发送。
3. 用户要求发邮件时，先用 send_email 执行，再用 wechat_reply 简短说明结果。
4. 不要用空格、换行、分隔符来假装多条消息。消息条数只由 actions 数组决定。"""
    return prompt


def compact_history(
    messages: list[dict[str, Any]],
    max_turns: int = 20,
    compact_at_turns: int = 40,
) -> tuple[list[dict[str, Any]], str]:
    """Compact conversation history.

    Keeps last max_turns turns verbatim.
    When total turns > compact_at_turns, summarizes older turns.

    Returns: (compacted_messages, summary_text)
    """
    # Each turn = 2 messages (user + assistant)
    if len(messages) <= max_turns * 2:
        return messages, ""

    # Extract turns
    turns = []
    i = 0
    while i < len(messages):
        role = messages[i].get("role", "")
        content = messages[i].get("content", "")
        if role == "user":
            turn = {"user": content, "assistant": ""}
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                turn["assistant"] = messages[i + 1].get("content", "")
                i += 2
            else:
                i += 1
            turns.append(turn)
        else:
            i += 1

    if len(turns) <= max_turns:
        return messages, ""

    # Split: old turns to summarize, recent turns to keep
    recent_turns = turns[-max_turns:]
    old_turns = turns[:-max_turns]

    # Generate summary of old turns (marker-based, no LLM call needed for v1)
    # For full LLM summary, caller can override this
    summary_lines = ["[更早的对话摘要]"]
    for t in old_turns[-10:]:  # Only summarize last 10 of the old turns
        user_short = t["user"][:60].replace("\n", " ")
        if t["assistant"]:
            summary_lines.append(f"- 用户：{user_short}")
    summary = "\n".join(summary_lines)

    # Rebuild messages from recent turns only
    compacted = []
    for t in recent_turns:
        compacted.append({"role": "user", "content": t["user"]})
        if t["assistant"]:
            compacted.append({"role": "assistant", "content": t["assistant"]})

    return compacted, summary


def build_chat_messages(
    system_prompt: str,
    user_message: str,
    history: list[dict[str, Any]],
    history_summary: str = "",
    max_turns: int = 20,
) -> list[dict[str, Any]]:
    """Build the full message list for the API call.

    Safeguard #9: compacted context — top memories only, recent history only.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]

    # Inject history summary if available (goes before recent history)
    if history_summary:
        messages.append({
            "role": "system",
            "content": history_summary,
        })

    # Compact history to last N turns
    compacted, _ = compact_history(history, max_turns)

    for h in compacted:
        messages.append({"role": h["role"], "content": h["content"]})

    # Current user message
    messages.append({"role": "user", "content": user_message})

    # Safety: estimate tokens and warn if too large
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    est_tokens = estimate_tokens(str(messages))
    if est_tokens > 32000:
        # Emergency: truncate oldest non-system messages
        system_msgs = [m for m in messages if m["role"] == "system"]
        chat_msgs = [m for m in messages if m["role"] != "system"]
        # Keep last 10 chat messages
        chat_msgs = chat_msgs[-10:]
        messages = system_msgs + chat_msgs

    return messages
