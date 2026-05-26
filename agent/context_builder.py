"""Context assembly with token-aware compaction — Safeguard #9.

V2.1: Supports prompt-pack assembly via BotProfileManager.
Legacy path kept for backward compatibility.
Triggers compaction when history exceeds configured limits.
"""

import time
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.bot_profile import BotProfileManager


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
    trigger: str = "user",
    defaults: dict[str, Any] | None = None,
) -> str:
    """Build a minimal legacy system prompt.

    Normal V2.1 runtime should use build_system_prompt_v21(), which delegates
    to BotProfileManager and the files under prompts/. This fallback only keeps
    old tests or old direct ChatAgent callers from losing protocol basics.
    """
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M 周%u")

    tool_list = ", ".join(tool_names) if tool_names else "无"
    defaults = defaults or {}
    default_city = str(defaults.get("default_city", "") or "").strip()
    defaults_text = "无"
    if default_city:
        defaults_text = f"用户默认天气城市：{default_city}。用户只问“天气怎么样/今天天气怎么样”时，直接按这个城市查天气，不要先反问城市。"

    memories_text = "长期记忆不会默认展开。需要过去事实时先调用 search_memory。"
    if memories:
        lines = ["可搜索记忆线索："]
        for i, m in enumerate(memories, 1):
            cat = m.get("category", "other")
            content = m.get("content", "")
            lines.append(f"{i}. [{cat}] {content[:80]}")
        memories_text = "\n".join(lines)

    # Relationship section
    familiarity = relationship.get("familiarity", 5)
    atmosphere = relationship.get("current_atmosphere", "neutral")
    boundary = relationship.get("chat_boundary", "未设定")
    rel_text = f"熟悉度: {familiarity}/10 | 氛围: {atmosphere}"
    if boundary:
        rel_text += f"\n边界: {boundary}"

    if trigger == "user":
        trigger_text = "本轮是真实用户消息。"
    else:
        trigger_text = (
            "本轮是系统触发，不是用户刚发来的消息。"
            "不要把系统任务当成用户说过的话。"
        )

    return f"""## Legacy fallback prompt
正常 V2.1 路径应使用 prompts/ 目录下的 prompt pack。

## 身份
{persona}

## 性格
{personality}

## 本轮来源
{trigger_text}

## 当前时间
{now}

## 默认事实
{defaults_text}

## 关系状态
{rel_text}

## 重要记忆
{memories_text}

## 可用工具
{tool_list}

## 输出格式
只输出 JSON，不要任何其他文字：
{{"actions":[{{"type":"tool","name":"wechat_reply","args":{{"text":"回复内容","delay":0}}}}],"done":true}}
不要编造没有上下文、记忆或工具结果支撑的事实。"""


def build_system_prompt_v21(
    bot_manager: "BotProfileManager",
    bot_id: str,
    relationship: dict[str, Any],
    memories: list[dict[str, Any]],
    tool_names: list[str],
    trigger: str = "user",
    trigger_content: str = "",
    defaults: dict[str, Any] | None = None,
    history_summary: str = "",
    recap: str = "",
) -> str:
    """Build system prompt from bot's prompt pack components.

    V2.1: delegates to BotProfileManager.build_system_prompt,
    which assembles from base_protocol, bot_identity, relationship_context,
    memory_policy, tool_policy, and trigger_context components.
    """
    return bot_manager.build_system_prompt(
        bot_id=bot_id,
        relationship=relationship,
        memories=memories,
        tool_names=tool_names,
        trigger=trigger,
        trigger_content=trigger_content,
        defaults=defaults,
        history_summary=history_summary,
        recap=recap,
    )


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
