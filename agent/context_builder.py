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
    defaults = defaults or {}
    default_city = str(defaults.get("default_city", "") or "").strip()
    defaults_text = "无"
    if default_city:
        defaults_text = f"用户默认天气城市：{default_city}。用户只问“天气怎么样/今天天气怎么样”时，直接按这个城市查天气，不要先反问城市。"

    # Memory section: do not inject full facts by default. The model can search
    # when the current turn actually needs past context.
    memories_text = "长期记忆不会默认展开。需要过去事实、偏好或项目进展时，先调用 search_memory；没有工具结果不要说“我记得”。"
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

    if trigger == "proactive":
        trigger_text = """本轮是系统唤醒，不是辣椒发来的消息。
你是在主动开口，不能把唤醒指令当成辣椒说过的话。
不要说“你刚才说/你问我/你不是要”等把系统任务误认成用户消息的话。
主动消息要像微信里自然冒出来的一句，短，少解释。"""
    else:
        trigger_text = """本轮是辣椒发来的微信消息。
历史消息里如果带有时间间隔，要先判断上一句话是不是已经过去一段时间。
隔了十几分钟以上时，不要默认上一句话还在同一个瞬间里。"""

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

## 本轮来源
{trigger_text}

## 对话节奏
普通闲聊默认只发 1 条微信。
情绪或复杂话题最多先发 2 条，除非辣椒明确要你展开。
不要为了显得懂而连续分析、解释、下判断。
不要主动分析动机、投射、戒断、心理机制，除非辣椒明确问“为什么会这样”。
问句如果很简单，先按简单问题答，不要升维。

## 当前时间
{now}

## 默认事实
{defaults_text}

## 关系状态
{rel_text}

## 重要记忆
{memories_text}

## 可用工具
- wechat_reply(text, delay) — 回复微信，每次调用发一条消息
- schedule_wechat_message(delay_minutes, messages) — 安排未来发送微信消息。messages 是数组，每项包含 text 和 delay。比如 10 分钟后发两条：{{"delay_minutes":10,"messages":[{{"text":"第一条","delay":0}},{{"text":"第二条","delay":1}}]}}
- get_weather(city) — 查天气
- get_time(timezone) — 查时间
- search_memory(query, top_k) — 按需搜索长期记忆。需要过去事实时先查，没查到就说不确定
- read_persona_file() — 读取当前人设文件。只能读取这一个配置允许的文件
- propose_persona_change(content, reason) — 提出人设文件修改草案，等待辣椒微信确认后由系统写入
- send_email(to, subject, body) — 发送邮件。用户没有指定收件人时可以留空 to，系统会使用默认收件人

## 输出格式
只输出 JSON，不要任何其他文字：
{{"actions":[{{"type":"tool","name":"wechat_reply","args":{{"text":"回复内容","delay":0}}}}],"done":true}}
你可以在一轮里多次调用 wechat_reply 来分段回复。

## 硬规则
1. 想现在发几条，就输出几个 wechat_reply action。不要把多条消息塞进同一个 text。
2. 用户要求“过一会儿/几分钟后/几点提醒我/之后发消息”时，必须调用 schedule_wechat_message，由代码定时发送。
3. 用户要求发邮件时，先用 send_email 执行，再用 wechat_reply 简短说明结果。
4. 不要用空格、换行、分隔符来假装多条消息。消息条数只由 actions 数组决定。
5. 不准编造你拥有的能力。你不能直接读项目目录、代码、屏幕或任意文件；只能使用本轮消息、系统上下文和真实工具结果。
6. 要修改人设时，只能调用 propose_persona_change 提草案，并用 wechat_reply 问辣椒是否确认。"""
    return prompt


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
