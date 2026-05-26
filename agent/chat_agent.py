from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, TYPE_CHECKING

from agent.context_builder import build_system_prompt, build_system_prompt_v21
from agent.output_guard import guard_single_message, guard_messages
from agent.tools import execute_tool, TOOL_WHITELIST
from utils.json_parser import parse_agent_output

if TYPE_CHECKING:
    from agent.bot_profile import BotProfileManager

logger = logging.getLogger(__name__)

# ================================================================
# Safeguard constants
# ================================================================
MAX_ACTIONS = 8          # Safeguard #2
MAX_MESSAGE_LEN = 500     # Single message max chars
MAX_TOTAL_CHARS = 2000    # Total output max chars
MAX_TOOL_LOOP = 5         # Safeguard #3
MAX_SCHEDULE_DELAY_MINUTES = 60 * 24 * 30

# ================================================================
# V2 Route constants
# ================================================================
ROUTE_SYSTEM_FAST = "SystemFastPath"
ROUTE_CHAT_LITE = "ChatLite"
ROUTE_CHAT_FULL = "ChatFull"

# ================================================================
# V2 Context policy constants
# ================================================================
CTX_HOT_ONLY = "hot_only"
CTX_HOT_CHEAP = "hot+cheap_memory"
CTX_HOT_FULL = "hot+full_memory"


class ChatAgent:
    """Main chat agent that processes user messages and returns actions."""

    def __init__(
        self,
        client: Any,  # AsyncOpenAI
        model: str,
        persona: str,
        personality: str,
        temperature: float = 0.7,
        defaults: dict[str, Any] | None = None,
        bot_manager: "BotProfileManager | None" = None,
    ):
        self.client = client
        self.model = model
        self.persona = persona
        self.personality = personality
        self.temperature = temperature
        self.defaults = defaults or {}
        self.bot_manager = bot_manager
        self._last_call_time = 0.0

    async def chat(
        self,
        user_message: str,
        memories: list[dict[str, Any]],
        history: list[dict[str, Any]],
        relationship: dict[str, Any],
        history_summary: str = "",
        trigger: str = "user",
        route: str = "",
        context_policy: str = "",
        bot_id: str = "",
        recap: str = "",
    ) -> dict[str, Any]:
        """Process a user message.

        V2.1: accepts bot_id for bot-scoped prompt assembly.
        When bot_manager is set, uses prompt pack components.

        Returns:
            {
                "messages": [...],
                "tool_calls": [...],
                "scheduled_messages": [...],
                "done": True,
                "iterations": 2,
                "elapsed_ms": 123,
                "token_usage": {...},
                "fallback_events": [...],
                "turn_id": "turn_xxx",
                "route": "ChatLite",
                "context_policy": "hot+cheap_memory",
                "llm_status": "ok",
                # V2.1
                "source": "user_reply",
                "author": "assistant",
                "trigger": "user_message",
            }
        """
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        start_time = time.time()
        llm_status = "ok"

        # V2.1: Compute effective tool list for this bot
        effective_tools = list(TOOL_WHITELIST)
        if self.bot_manager and bot_id:
            bot = self.bot_manager.get_bot(bot_id)
            if bot and bot.enabled_tools:
                effective_tools = [
                    t for t in bot.enabled_tools
                    if t in TOOL_WHITELIST
                ]

        # Build system prompt — V2.1 prompt pack or legacy
        if self.bot_manager and bot_id:
            system_prompt = build_system_prompt_v21(
                bot_manager=self.bot_manager,
                bot_id=bot_id,
                relationship=relationship,
                memories=memories,
                tool_names=effective_tools,
                trigger=trigger,
                trigger_content=user_message,
                defaults=self.defaults,
                history_summary=history_summary,
                recap=recap,
            )
        else:
            system_prompt = build_system_prompt(
                persona=self.persona,
                personality=self.personality,
                memories=memories,
                relationship=relationship,
                tool_names=effective_tools,
                trigger=trigger,
                defaults=self.defaults,
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
        # V2.1: only real user messages go in user role.
        # System-triggered tasks (proactive, fixed_morning, fixed_night,
        # scheduled, admin) use system role.
        if trigger == "user":
            messages.append({"role": "user", "content": current_content})
        else:
            messages.append({"role": "system", "content": current_content})

        # Agent loop (safeguard #3: max iterations)
        all_messages: list[dict[str, Any]] = []
        scheduled_messages: list[dict[str, Any]] = []
        all_tool_results: list[dict[str, str]] = []
        fallback_events: list[dict[str, str]] = []
        raw_output = ""
        iterations = 0
        token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated": False,
        }

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
                _merge_token_usage(
                    token_usage,
                    _extract_token_usage(response, messages, raw_output),
                )
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                llm_status = "error"
                fallback_events.append({
                    "type": "llm_error",
                    "detail": str(e)[:200],
                })
                all_messages.append({
                    "type": "message",
                    "text": "（信号不太好，稍等我一下）",
                    "delay": 0,
                })
                break

            # Safeguard #1: parse with fallback
            parsed = parse_agent_output(raw_output)
            if parsed.get("_fallback_reason"):
                fallback_events.append({
                    "type": str(parsed["_fallback_reason"]),
                    "detail": raw_output[:200],
                })

            actions = parsed.get("actions", [])

            # Safeguard #2: hard limits
            actions = actions[:MAX_ACTIONS]

            # Process actions
            tool_results_this_round: list[str] = []
            for action in actions:
                action_type = action.get("type", "")

                if action_type == "message":
                    text = str(action.get("text", ""))
                    text = guard_single_message(text, issues=_fallback_issue_list(fallback_events))
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

                    # V2.1: Reject tools not enabled for this bot
                    if name not in effective_tools:
                        tool_results_this_round.append(
                            f"[{name}] 此工具未对当前bot启用"
                        )
                        all_tool_results.append({
                            "name": name,
                            "result": "工具未启用",
                        })
                        continue

                    # wechat_reply is a "runtime tool" — intercepted as message
                    if name == "wechat_reply":
                        text = str(args.get("text", ""))
                        text = guard_single_message(text, issues=_fallback_issue_list(fallback_events))
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

        # Apply UnifiedOutputGuard to all messages (V2: final pass)
        raw_messages = [m for m in all_messages if m["type"] == "message"]
        final_messages = [
            {"text": m["text"], "delay": m.get("delay", 0)}
            for m in guard_messages(raw_messages)
        ]

        # V2.1: resolve outbound semantics from trigger
        source, author, trigger_type = _resolve_outbound_semantics(trigger)

        return {
            "messages": final_messages,
            "scheduled_messages": scheduled_messages,
            "tool_calls": all_tool_results,
            "done": True,
            "iterations": iterations,
            "elapsed_ms": int((time.time() - start_time) * 1000),
            "token_usage": token_usage,
            "fallback_events": fallback_events,
            "turn_id": turn_id,
            "route": route or ROUTE_CHAT_FULL,
            "context_policy": context_policy or CTX_HOT_ONLY,
            "llm_status": llm_status,
            # V2.1 outbound semantics
            "source": source,
            "author": author,
            "trigger": trigger_type,
        }


class _FallbackIssueBridge:
    """Adapter that converts output_guard issues into fallback_events entries."""

    def __init__(self, fallback_events: list[dict[str, str]]):
        self._events = fallback_events

    def append(self, issue: str):
        self._events.append({"type": issue, "detail": ""})


def _fallback_issue_list(fallback_events: list[dict[str, str]]) -> list:
    """Return an object whose append() adds fallback event entries."""
    return _FallbackIssueBridge(fallback_events)  # type: ignore[return-value]


def _safe_float(value: Any, min_val: float, max_val: float) -> float:
    """Safely parse a float within bounds."""
    try:
        v = float(value)
        return max(min_val, min(max_val, v))
    except (ValueError, TypeError):
        return 0.0


def _extract_token_usage(
    response: Any,
    messages: list[dict[str, Any]],
    raw_output: str,
) -> dict[str, int | bool]:
    usage = getattr(response, "usage", None)
    if usage is not None:
        prompt_tokens = _usage_value(usage, "prompt_tokens")
        completion_tokens = _usage_value(usage, "completion_tokens")
        total_tokens = _usage_value(usage, "total_tokens")
        if total_tokens or prompt_tokens or completion_tokens:
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens or prompt_tokens + completion_tokens,
                "estimated": False,
            }

    prompt_text = "\n".join(str(m.get("content", "")) for m in messages)
    prompt_tokens = _estimate_tokens(prompt_text)
    completion_tokens = _estimate_tokens(raw_output)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated": True,
    }


def _usage_value(usage: Any, key: str) -> int:
    if isinstance(usage, dict):
        value = usage.get(key, 0)
    else:
        value = getattr(usage, key, 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _merge_token_usage(total: dict[str, Any], item: dict[str, Any]):
    total["prompt_tokens"] += int(item.get("prompt_tokens", 0) or 0)
    total["completion_tokens"] += int(item.get("completion_tokens", 0) or 0)
    total["total_tokens"] += int(item.get("total_tokens", 0) or 0)
    total["estimated"] = bool(total.get("estimated") or item.get("estimated"))


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / 1.8))


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
    if trigger == "user":
        return f"[用户消息 | {label}{gap}]\n{user_message}"

    # All system-triggered messages: label clearly so the model knows
    # this is NOT a user message
    trigger_labels = {
        "proactive": "系统唤醒 — 主动开口任务",
        "fixed_morning": "系统定时 — 早安消息",
        "fixed_night": "系统定时 — 晚间陪伴日记",
        "scheduled": "系统定时 — 用户预约提醒",
        "admin": "系统 — 管理员手动触发",
    }
    trigger_label = trigger_labels.get(trigger, f"系统任务 ({trigger})")
    return (
        f"[{trigger_label} | {label}{gap}]\n"
        "下面是系统任务，不是用户发来的消息：\n"
        f"{user_message}"
    )


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


# ================================================================
# V2.1: Outbound message semantics
# ================================================================

def _resolve_outbound_semantics(trigger: str) -> tuple[str, str, str]:
    """Resolve source, author, trigger_type from trigger string.

    Per V2.1 design:
      source: user_reply | fixed_morning | scheduled_reminder | proactive | admin
      author: assistant   | system
      trigger: user_message | fixed_time | user_scheduled | proactive_reason | manual_admin
    """
    if trigger == "user":
        return ("user_reply", "assistant", "user_message")
    elif trigger == "proactive":
        return ("proactive", "assistant", "proactive_reason")
    elif trigger == "fixed_morning":
        return ("fixed_morning", "system", "fixed_time")
    elif trigger == "fixed_night":
        return ("fixed_night", "system", "fixed_time")
    elif trigger == "scheduled":
        return ("scheduled_reminder", "system", "user_scheduled")
    elif trigger == "admin":
        return ("admin", "system", "manual_admin")
    return ("user_reply", "assistant", "user_message")
