"""Chat Agent — core conversation handler with all 10 safeguards.

Design: JSON output format (emoRebot style), not function calling.
This is more reliable with DeepSeek than OpenAI-style function calling.

Safeguards implemented:
  #1 JSON parse fallback → plain text message
  #2 Max actions per turn (8) + max chars per message (500) + max total chars (2000)
  #3 Max tool loop iterations (5)
  #4 Tool whitelist (enforced in tools.py)
  #5 Low importance memories filtered (enforced in memory_worker.py)
  #6 Scheduler hard rules before LLM decision (enforced in scheduler.py)
  #7 Memory Worker is async fire-and-forget (enforced in memory_worker.py)
  #8 SQLite WAL + write lock (enforced in memory/store.py)
  #9 Context compaction (enforced in context_builder.py)
  #10 Test suite (enforced in tests/)
"""

import asyncio
import logging
import json
import time
from typing import Any

from agent.context_builder import build_system_prompt
from agent.tools import execute_tool, TOOL_WHITELIST
from utils.json_parser import parse_agent_output

logger = logging.getLogger(__name__)

# ================================================================
# Safeguard constants
# ================================================================
MAX_ACTIONS = 8          # Safeguard #2
MAX_MESSAGE_LEN = 500     # Single message max chars
MAX_TOTAL_CHARS = 2000    # Total output max chars
MAX_TOOL_LOOP = 5         # Safeguard #3
MAX_SCHEDULE_DELAY_MINUTES = 60 * 24 * 30


class ChatAgent:
    """Main chat agent that processes user messages and returns actions."""

    def __init__(
        self,
        client: Any,  # AsyncOpenAI
        model: str,
        persona: str,
        personality: str,
        temperature: float = 0.7,
    ):
        self.client = client
        self.model = model
        self.persona = persona
        self.personality = personality
        self.temperature = temperature
        self._last_call_time = 0.0

    async def chat(
        self,
        user_message: str,
        memories: list[dict[str, Any]],
        history: list[dict[str, Any]],
        relationship: dict[str, Any],
        history_summary: str = "",
        trigger: str = "user",
    ) -> dict[str, Any]:
        """Process a user message.

        Returns:
            {
                "messages": [{"text": "...", "delay": 1.0}, ...],
                "tool_calls": [{"name": "...", "result": "..."}],
                "done": True,
                "raw_output": "...",
                "iterations": 2,
            }
        """
        start_time = time.time()

        # Build system prompt
        system_prompt = build_system_prompt(
            persona=self.persona,
            personality=self.personality,
            memories=memories,
            relationship=relationship,
            tool_names=list(TOOL_WHITELIST),
            trigger=trigger,
        )

        # Assemble messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]
        if history_summary.strip():
            messages.append({
                "role": "system",
                "content": "更早对话摘要：\n" + history_summary.strip()[-3000:],
            })

        # Add recent history — limited to last 8 turns to prevent
        # the formatting rules from being diluted by long context.
        # Each turn = 2 messages (user + assistant), so 16 max.
        recent = history[-16:] if len(history) > 16 else history
        for h in recent:
            role = h.get("role", "user")
            if role not in {"system", "user", "assistant"}:
                role = "user"
            messages.append({
                "role": role,
                "content": _format_history_content(h),
            })

        current_content = _format_current_turn(user_message, history, trigger)
        if trigger == "proactive":
            messages.append({"role": "system", "content": current_content})
        else:
            messages.append({"role": "user", "content": current_content})

        # Agent loop (safeguard #3: max iterations)
        all_messages: list[dict[str, Any]] = []
        scheduled_messages: list[dict[str, Any]] = []
        all_tool_results: list[dict[str, str]] = []
        raw_output = ""
        iterations = 0

        for iteration in range(MAX_TOOL_LOOP):
            iterations = iteration + 1

            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=2048,
                )
                raw_output = response.choices[0].message.content or ""
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                all_messages.append({
                    "type": "message",
                    "text": "（信号不太好，稍等我一下）",
                    "delay": 0,
                })
                break

            # Safeguard #1: parse with fallback
            parsed = parse_agent_output(raw_output)

            actions = parsed.get("actions", [])

            # Safeguard #2: hard limits
            actions = actions[:MAX_ACTIONS]

            # Process actions
            tool_results_this_round: list[str] = []
            for action in actions:
                action_type = action.get("type", "")

                if action_type == "message":
                    text = str(action.get("text", ""))
                    text = _sanitize_outbound_text(text)[:MAX_MESSAGE_LEN]
                    if text:
                        delay = _safe_float(action.get("delay", 0), 0, 10)
                        all_messages.append({
                            "type": "message",
                            "text": text,
                            "delay": delay,
                        })

                elif action_type == "tool":
                    name = str(action.get("name", "")).strip()
                    args = action.get("args", {})
                    if not isinstance(args, dict):
                        args = {}

                    # wechat_reply is a "runtime tool" — intercepted as message
                    if name == "wechat_reply":
                        text = str(args.get("text", ""))
                        text = _sanitize_outbound_text(text)[:MAX_MESSAGE_LEN]
                        if text:
                            delay = _safe_float(args.get("delay", 0), 0, 10)
                            all_messages.append({
                                "type": "message",
                                "text": text,
                                "delay": delay,
                            })
                        continue

                    if name == "schedule_wechat_message":
                        task = _parse_schedule_task(args)
                        if task:
                            scheduled_messages.append(task)
                            tool_results_this_round.append(
                                f"[{name}] 已安排 {len(task['messages'])} 条消息"
                            )
                            all_tool_results.append({
                                "name": name,
                                "result": f"已安排 {len(task['messages'])} 条消息",
                            })
                        else:
                            tool_results_this_round.append(
                                f"[{name}] 定时消息参数无效，未安排"
                            )
                            all_tool_results.append({
                                "name": name,
                                "result": "定时消息参数无效，未安排",
                            })
                        continue

                    result = await execute_tool(name, args)
                    tool_results_this_round.append(f"[{name}] {result}")
                    all_tool_results.append({
                        "name": name,
                        "result": result,
                    })

            # Safeguard #2 (continued): total char limit
            total_chars = sum(
                len(m.get("text", "")) for m in all_messages
                if m["type"] == "message"
            )
            if total_chars >= MAX_TOTAL_CHARS:
                # Truncate last message if needed
                if all_messages and all_messages[-1]["type"] == "message":
                    excess = total_chars - MAX_TOTAL_CHARS
                    if excess > 0:
                        last_text = all_messages[-1]["text"]
                        all_messages[-1]["text"] = last_text[:max(0, len(last_text) - excess)] + "…"
                break

            # Check if done
            if parsed.get("done", True):
                break

            # Not done — inject tool results and continue
            if tool_results_this_round:
                messages.append({
                    "role": "assistant",
                    "content": raw_output,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "工具执行结果：\n" +
                        "\n".join(tool_results_this_round) +
                        "\n\n如果还需要更多信息，继续输出JSON。"
                        "如果已经足够回复，设 done: true 并输出 message。"
                    ),
                })
            else:
                break

        # If we hit max loop, ensure we have at least one message
        if iterations >= MAX_TOOL_LOOP and not all_messages:
            all_messages.append({
                "type": "message",
                "text": "（让我想想...）",
                "delay": 0,
            })

        self._last_call_time = start_time

        final_messages = [m for m in all_messages if m["type"] == "message"]

        return {
            "messages": final_messages,
            "scheduled_messages": scheduled_messages,
            "tool_calls": all_tool_results,
            "done": True,
            "iterations": iterations,
            "elapsed_ms": int((time.time() - start_time) * 1000),
        }


def _safe_float(value: Any, min_val: float, max_val: float) -> float:
    """Safely parse a float within bounds."""
    try:
        v = float(value)
        return max(min_val, min(max_val, v))
    except (ValueError, TypeError):
        return 0.0


def _format_history_content(item: dict[str, Any]) -> str:
    """Attach lightweight timing metadata without changing message roles."""
    content = str(item.get("content", ""))
    ts = item.get("ts")
    source = str(item.get("source", "")).strip()
    if not ts:
        return content

    label = _format_ts(ts)
    source_part = f" | 来源:{source}" if source else ""
    return f"[历史消息 | {label}{source_part}]\n{content}"


def _format_current_turn(user_message: str, history: list[dict[str, Any]], trigger: str) -> str:
    now = time.time()
    label = _format_ts(now)
    gap = _last_message_gap(history, now)
    if trigger == "proactive":
        return (
            f"[系统唤醒 | {label}{gap}]\n"
            "下面是系统给你的主动开口任务，不是辣椒发来的消息：\n"
            f"{user_message}"
        )
    return f"[辣椒消息 | {label}{gap}]\n{user_message}"


def _last_message_gap(history: list[dict[str, Any]], now: float) -> str:
    for item in reversed(history):
        ts = item.get("ts")
        if ts:
            try:
                seconds = max(0, now - float(ts))
            except (TypeError, ValueError):
                continue
            return f" | 距上一条{_human_gap(seconds)}"
    return ""


def _human_gap(seconds: float) -> str:
    if seconds < 90:
        return "不到1分钟"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}分钟"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours}小时{minutes % 60}分钟"
    days = int(hours // 24)
    return f"{days}天"


def _format_ts(ts: Any) -> str:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        value = time.time()
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def _sanitize_outbound_text(text: str) -> str:
    """Never let protocol JSON leak into WeChat as a visible reply."""
    text = str(text).strip()
    if not text:
        return ""
    if _looks_like_protocol_json(text):
        logger.warning("Blocked protocol-shaped text from being sent to WeChat")
        return "我刚刚卡了一下 重新说"
    return text


def _looks_like_protocol_json(text: str) -> bool:
    sample = text.strip()
    if not sample:
        return False
    lowered = sample.lower()
    if "\"actions\"" not in lowered and "'actions'" not in lowered:
        return False
    if "wechat_reply" not in lowered and "\"done\"" not in lowered and "'done'" not in lowered:
        return False
    try:
        parsed = json.loads(sample)
    except (json.JSONDecodeError, TypeError, ValueError):
        return sample.startswith("{") and "actions" in lowered
    return isinstance(parsed, dict) and "actions" in parsed


def _parse_schedule_task(args: dict[str, Any]) -> dict[str, Any] | None:
    """Validate model-created scheduled message intent.

    The model decides content and rough timing; code clamps the timing and
    stores normalized messages for the delivery worker.
    """
    delay_minutes = _safe_float(
        args.get("delay_minutes", args.get("minutes", 0)),
        0.1,
        MAX_SCHEDULE_DELAY_MINUTES,
    )

    raw_messages = args.get("messages", [])
    if isinstance(raw_messages, str):
        raw_messages = [{"text": raw_messages, "delay": 0}]
    if isinstance(raw_messages, dict):
        raw_messages = [raw_messages]
    if not isinstance(raw_messages, list):
        return None

    messages: list[dict[str, Any]] = []
    for item in raw_messages[:MAX_ACTIONS]:
        if isinstance(item, str):
            text = item
            delay = 0
        elif isinstance(item, dict):
            text = str(item.get("text", ""))
            delay = item.get("delay", 0)
        else:
            continue

        text = text.strip()[:MAX_MESSAGE_LEN]
        if not text:
            continue
        messages.append({
            "text": text,
            "delay": _safe_float(delay, 0, 10),
        })

    if not messages:
        return None

    return {
        "delay_minutes": delay_minutes,
        "messages": messages,
    }
