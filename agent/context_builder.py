"""Context utilities — v0.2 Phase 3.

History compaction and system prompt construction.
Full context assembly is handled by chat_agent.ChatAgent.
Dead features (relationship, memory_policy, recap, multi-bot) removed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


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

    recent_turns = turns[-max_turns:]
    old_turns = turns[:-max_turns]

    summary_lines = ["[更早的对话摘要]"]
    for t in old_turns[-10:]:
        user_short = t["user"][:60].replace("\n", " ")
        if t["assistant"]:
            summary_lines.append(f"- 用户：{user_short}")
    summary = "\n".join(summary_lines)

    compacted = []
    for t in recent_turns:
        compacted.append({"role": "user", "content": t["user"]})
        if t["assistant"]:
            compacted.append({"role": "assistant", "content": t["assistant"]})

    return compacted, summary


def build_chat_system_prompt(
    persona: str,
    personality: str = "",
    tool_names: list[str] | None = None,
) -> str:
    """Build a minimal chat system prompt.

    Time and city are appended as separate system messages in chat_agent
    so the main prompt stays stable across turns for prompt cache hits.
    """
    tool_list = ", ".join(tool_names) if tool_names else "send_wechat_messages, get_time, get_weather, skip_reply"

    sections = [
        persona,
        personality if personality else "",
    ]

    sections.append(f"可用工具：{tool_list}\n")

    # Add base protocol from prompts/ if available
    protocol = _read_prompt_component("base_protocol")
    if protocol:
        sections.append(protocol)

    # Add tool policy from prompts/ if available
    tool_policy = _read_prompt_component("tool_policy")
    if tool_policy:
        sections.append(tool_policy)

    return "\n\n".join(s.strip() for s in sections if s.strip())


def _read_prompt_component(name: str) -> str:
    """Read a prompt component from prompts/ directory. Returns '' if missing."""
    path = Path("prompts") / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
        # Filter out sections that reference disabled features
        lines = text.split("\n")
        filtered: list[str] = []
        skip_block = False
        for line in lines:
            stripped = line.strip().lower()
            if any(kw in stripped for kw in (
                "search_memory", "## other managed tools",
                "send_email", "schedule_wechat_message",
                "proactive", "emotion", "multi-bot",
            )):
                skip_block = True
                continue
            if skip_block and stripped.startswith("##"):
                skip_block = False
            if not skip_block:
                filtered.append(line)
        return "\n".join(filtered)
    except Exception:
        return ""
