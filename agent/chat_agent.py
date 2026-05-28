from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, TYPE_CHECKING

from agent.context_builder import build_system_prompt, build_system_prompt_v21
from agent.output_guard import guard_single_message, guard_messages
from agent.tools import execute_tool, TOOL_WHITELIST
from agent.tool_registry import is_action as _is_registry_action
from utils.json_parser import parse_agent_output, parse_agent_json_output, validate_agent_output

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
        model: str,
        persona: str,
        personality: str,
        *,
        temperature: float = 0.7,
        defaults: dict[str, Any] | None = None,
        bot_manager: "BotProfileManager | None" = None,
        llm_gateway: Any,
        json_output_enabled: bool = True,
        native_tool_calls: bool = False,
        native_allowed_tools: list[str] | None = None,
    ):
        self.llm_gateway = llm_gateway
        self.model = model
        self.persona = persona
        self.personality = personality
        self.temperature = temperature
        self.defaults = defaults or {}
        self.bot_manager = bot_manager
        self.json_output_enabled = json_output_enabled
        self._last_call_time = 0.0
        # Phase 5A: native tool calls feature flag
        self.native_tool_calls = native_tool_calls
        self.native_allowed_tools = native_allowed_tools or []

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
        emotion_inject: str = "",
        must_reply: bool = False,
    ) -> dict[str, Any]:
        """Process a user message.

        V2.1: accepts bot_id for bot-scoped prompt assembly.
        V2.2: accepts emotion_inject and must_reply for emotion-aware chat.

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
                "skip_reply": bool,
                "skip_reason": str,
                # V2.1
                "source": "user_reply",
                "author": "assistant",
                "trigger": "user_message",
                # V2.2
                "silence_overridden": bool,
                "force_reply_used": bool,
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
                emotion_inject=emotion_inject,
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
        skip_reply = False
        skip_reason = ""
        raw_output = ""
        iterations = 0
        token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated": False,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "reasoning_tokens": 0,
        }

        for iteration in range(MAX_TOOL_LOOP):
            iterations = iteration + 1

            try:
                from agent.llm_types import LLMRequest
                response_mode = "json_object" if self.json_output_enabled else "text"

                # Phase 5A: build native tool specs if enabled
                request_tools = None
                if self.native_tool_calls:
                    request_tools = _build_native_tool_specs(
                        self.native_allowed_tools,
                    )
                    if request_tools:
                        _tool_names = [t["function"]["name"] for t in request_tools]
                        logger.info(
                            "native_tools: exposing %d tools: %s",
                            len(_tool_names), ",".join(_tool_names),
                        )

                llm_response = await self.llm_gateway.complete(
                    LLMRequest(
                        purpose="chat_turn",
                        model=self.model,
                        messages=messages,
                        temperature=self.temperature,
                        max_tokens=2048,
                        response_mode=response_mode,
                        thinking="disabled",
                        tools=request_tools,
                        tool_choice="auto" if request_tools else None,
                        metadata={
                            "turn_id": turn_id,
                            "iteration": iteration,
                        },
                    )
                )
                raw_output = llm_response.content or ""
                finish_reason = llm_response.finish_reason or ""
                _merge_llm_usage(token_usage, llm_response.usage)
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

            # Phase 5A: Native tool call processing
            native_tool_calls = getattr(llm_response, "tool_calls", None) or []
            if native_tool_calls and self.native_tool_calls:
                tc_names = [str(getattr(getattr(tc, "function", None), "name", "") or "?") for tc in native_tool_calls]
                logger.info(
                    "native_tools: received %d tool_calls iter=%d: %s",
                    len(native_tool_calls), iterations, ",".join(tc_names),
                )
                # Build assistant message with tool_calls for the conversation
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": raw_output or "",
                }
                # Serialize tool_calls for the message
                serialized_tool_calls: list[dict[str, Any]] = []
                for tc in native_tool_calls:
                    tc_dict: dict[str, Any] = {
                        "id": getattr(tc, "id", ""),
                        "type": "function",
                        "function": {
                            "name": getattr(getattr(tc, "function", None), "name", ""),
                            "arguments": getattr(getattr(tc, "function", None), "arguments", ""),
                        },
                    }
                    serialized_tool_calls.append(tc_dict)
                assistant_msg["tool_calls"] = serialized_tool_calls
                messages.append(assistant_msg)

                # Process each tool call
                for tc in native_tool_calls:
                    fn = getattr(tc, "function", None)
                    tool_name = str(getattr(fn, "name", "") if fn else "")
                    tool_args_raw = str(getattr(fn, "arguments", "") if fn else "")

                    tool_result = await _execute_native_tool(
                        tool_name=tool_name,
                        tool_args_raw=tool_args_raw,
                        native_allowed_tools=self.native_allowed_tools,
                    )
                    all_tool_results.append({
                        "name": tool_name,
                        "result": tool_result[:500],
                    })
                    # Log tool execution result
                    _result_status = _classify_native_tool_result(tool_result)
                    logger.info(
                        "native_tools: result tool=%s status=%s",
                        tool_name, _result_status,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": getattr(tc, "id", ""),
                        "content": tool_result,
                    })

                # Go back to LLM for final JSON actions
                continue

            # Guard: empty content from provider
            if not raw_output.strip():
                logger.warning("chat_agent: empty content from LLM")
                fallback_events.append({
                    "type": "invalid_response",
                    "detail": "empty_content",
                })
                all_messages.append({
                    "type": "message",
                    "text": "（信号不太好，稍等我一下）",
                    "delay": 0,
                })
                break

            # Guard: finish_reason=length means truncated, likely broken JSON.
            # In strict json mode: retry once; if retry still fails, fallback.
            # In text mode: log and try to parse as-is (legacy behaviour).
            if finish_reason == "length":
                logger.warning("chat_agent: finish_reason=length, output may be truncated")
                fallback_events.append({
                    "type": "invalid_response",
                    "detail": "finish_reason_length",
                })

                if self.json_output_enabled:
                    # Strict mode: one retry with identical request
                    try:
                        from agent.llm_types import LLMRequest as _LLMR
                        retry_resp = await self.llm_gateway.complete(
                            _LLMR(
                                purpose="chat_turn",
                                model=self.model,
                                messages=messages,
                                temperature=self.temperature,
                                max_tokens=2048,
                                response_mode="json_object",
                                thinking="disabled",
                                metadata={
                                    "turn_id": turn_id,
                                    "iteration": iteration,
                                    "retry_reason": "length",
                                },
                            )
                        )
                        _merge_llm_usage(token_usage, retry_resp.usage)
                        retry_content = retry_resp.content or ""
                        retry_finish = retry_resp.finish_reason or ""

                        if retry_content.strip() and retry_finish != "length":
                            raw_output = retry_content
                            finish_reason = retry_finish
                            logger.info("chat_agent: finish_reason=length retry succeeded")
                            fallback_events.append({
                                "type": "finish_reason_length_retry",
                                "detail": "success",
                            })
                        else:
                            logger.warning("chat_agent: finish_reason=length retry also failed")
                            fallback_events.append({
                                "type": "finish_reason_length_retry",
                                "detail": "failed",
                            })
                            all_messages.append({
                                "type": "message",
                                "text": "（信号不太好，稍等我一下）",
                                "delay": 0,
                            })
                            break
                    except Exception:
                        logger.warning("chat_agent: finish_reason=length retry error", exc_info=True)
                        fallback_events.append({
                            "type": "finish_reason_length_retry",
                            "detail": "error",
                        })
                        all_messages.append({
                            "type": "message",
                            "text": "（信号不太好，稍等我一下）",
                            "delay": 0,
                        })
                        break
                # else: text mode — fall through and parse whatever we got

            # Safeguard #1: parse with strict or legacy parser
            if self.json_output_enabled:
                parsed = parse_agent_json_output(raw_output)
            else:
                parsed = parse_agent_output(raw_output)

            if parsed.get("_fallback_reason"):
                reason = str(parsed["_fallback_reason"])
                fallback_events.append({
                    "type": reason,
                    "detail": raw_output[:200],
                })
                logger.warning(
                    "chat_agent: JSON parse fallback reason=%s raw_preview=%s",
                    reason, raw_output[:200],
                )

            # Schema validation (Phase 3A)
            schema_errors = validate_agent_output(parsed)
            if schema_errors:
                logger.warning(
                    "chat_agent: schema validation errors: %s",
                    "; ".join(schema_errors),
                )
                for err in schema_errors:
                    fallback_events.append({
                        "type": "schema_error",
                        "detail": err[:200],
                    })
                # If parsed was already a fallback (no actions, etc.), keep it.
                # If it had actions but schema errors, we try to salvage by
                # dropping invalid actions from the list.
                if not parsed.get("_fallback_reason"):
                    actions = parsed.get("actions", [])
                    valid_actions = _filter_valid_actions(actions)
                    if valid_actions:
                        parsed["actions"] = valid_actions
                        logger.info(
                            "chat_agent: filtered %d invalid actions, kept %d",
                            len(actions) - len(valid_actions), len(valid_actions),
                        )
                    elif actions:
                        # All actions were invalid — fallback to a safe message
                        logger.warning("chat_agent: all actions invalid after schema check")
                        parsed = {
                            "actions": [],
                            "done": True,
                            "_fallback_reason": "all_actions_invalid",
                        }
                        fallback_events.append({
                            "type": "all_actions_invalid",
                            "detail": f"dropped {len(actions)} invalid actions",
                        })

            # Strict json mode: if parse produced a fallback and there are no
            # salvageable actions, send a safe fallback message to the user.
            if self.json_output_enabled and parsed.get("_fallback_reason") and not parsed.get("actions"):
                fallback_events.append({
                    "type": "strict_json_fallback",
                    "detail": parsed["_fallback_reason"],
                })
                all_messages.append({
                    "type": "message",
                    "text": "（信号不太好，稍等我一下）",
                    "delay": 0,
                })
                break

            actions = parsed.get("actions", [])
            if parsed.get("skip") or parsed.get("no_reply"):
                skip_reply = True
                skip_reason = str(
                    parsed.get("reason")
                    or parsed.get("skip_reason")
                    or "model requested no reply"
                )[:200]
                fallback_events.append({
                    "type": "user_silent_request",
                    "detail": skip_reason,
                })

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

                elif action_type in {"skip", "no_reply"}:
                    skip_reply = True
                    skip_reason = str(
                        action.get("reason")
                        or action.get("text")
                        or "model requested no reply"
                    )[:200]
                    fallback_events.append({
                        "type": "user_silent_request",
                        "detail": skip_reason,
                    })

                elif action_type == "tool":
                    name = str(action.get("name", "")).strip()
                    args = action.get("args", {})
                    if not isinstance(args, dict):
                        args = {}

                    # Phase 4B: Reject unknown action/tool names
                    from agent.tool_registry import (
                        is_external_tool as _is_ext_tool,
                        TOOL_REGISTRY as _TOOL_REG,
                    )
                    if not _is_registry_action(name) and not _is_ext_tool(name):
                        logger.warning("chat_agent: unknown action/tool '%s' rejected", name)
                        tool_results_this_round.append(
                            f"[{name}] 未知操作，已忽略"
                        )
                        all_tool_results.append({
                            "name": name,
                            "result": "未知操作",
                        })
                        continue

                    # Log high-side-effect tool usage
                    tool_def = _TOOL_REG.get(name) if _is_ext_tool(name) else None
                    if tool_def and tool_def.side_effect_level == "high":
                        logger.info("chat_agent: executing high-side-effect tool: %s", name)

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

                    result = await execute_tool(name, args, bot_id=bot_id)
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
            logger.warning(
                "native_tools: MAX_TOOL_LOOP exit iterations=%d fallback_message=1",
                iterations,
            )
            all_messages.append({
                "type": "message",
                "text": "（让我想想...）",
                "delay": 0,
            })
        elif iterations >= MAX_TOOL_LOOP:
            logger.warning(
                "native_tools: MAX_TOOL_LOOP exit iterations=%d has_messages=%d",
                iterations, len(all_messages),
            )

        self._last_call_time = start_time

        # Apply UnifiedOutputGuard to all messages (V2: final pass)
        raw_messages = [m for m in all_messages if m["type"] == "message"]
        final_messages = [
            {"text": m["text"], "delay": m.get("delay", 0)}
            for m in guard_messages(raw_messages)
        ]
        if not final_messages and not scheduled_messages and not skip_reply:
            skip_reply = True
            skip_reason = "no outbound message produced"

        # V2.2: must_reply override — force a reply if silence limit reached
        silence_overridden = False
        force_reply_used = False
        if must_reply and (skip_reply or not final_messages):
            silence_overridden = True
            fallback_events.append({
                "type": "silence_overridden",
                "detail": f"must_reply forced reply; model skip_reason={skip_reason[:100]}",
            })
            # Use a fallback reply matching the emotional state
            force_text = _emotion_force_reply_text(emotion_inject)
            final_messages = [{"text": force_text, "delay": 0}]
            skip_reply = False
            skip_reason = ""
            force_reply_used = True

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
            "skip_reply": skip_reply,
            "skip_reason": skip_reason,
            "turn_id": turn_id,
            "route": route or ROUTE_CHAT_FULL,
            "context_policy": context_policy or CTX_HOT_ONLY,
            "llm_status": llm_status,
            # V2.1 outbound semantics
            "source": source,
            "author": author,
            "trigger": trigger_type,
            # V2.2
            "silence_overridden": silence_overridden,
            "force_reply_used": force_reply_used,
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


def _merge_llm_usage(total: dict[str, Any], usage: Any):
    """Merge LLMUsage dataclass into the token usage dict.

    Extends _merge_token_usage with cache_hit / cache_miss / reasoning tokens.
    """
    total["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
    total["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
    total["total_tokens"] += int(getattr(usage, "total_tokens", 0) or 0)
    total["estimated"] = bool(total.get("estimated") or getattr(usage, "estimated", False))
    total["cache_hit_tokens"] = total.get("cache_hit_tokens", 0) + int(getattr(usage, "cache_hit_tokens", 0) or 0)
    total["cache_miss_tokens"] = total.get("cache_miss_tokens", 0) + int(getattr(usage, "cache_miss_tokens", 0) or 0)
    total["reasoning_tokens"] = total.get("reasoning_tokens", 0) + int(getattr(usage, "reasoning_tokens", 0) or 0)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / 1.8))


def _format_history_content(item: dict[str, Any]) -> str:
    """Attach lightweight timing metadata without changing message roles.

    The role is already correctly carried by the API message field
    (user/assistant/system).  We do NOT inject role labels into the
    content — those confuse the model into echo-chambering them back.

    Bug B fix: for assistant messages stored as JSON actions, extract
    the readable text so the LLM sees plain natural language instead of
    raw JSON tool-call records.
    """
    content = str(item.get("content", ""))
    ts = item.get("ts")
    role = str(item.get("role", "user")).strip()
    source = str(item.get("source", "")).strip()
    if not ts:
        return content

    label = _format_ts(ts)
    source_part = f" | 来源:{source}" if source else ""

    # Extract readable text from assistant JSON action records.
    # The raw JSON like {"actions":[{"type":"tool","name":"wechat_reply",
    # "args":{"text":"..."}}]} is hard for the LLM to parse; plain text
    # prevents it from misattributing bot words to the user.
    if role == "assistant":
        try:
            import json as _json
            parsed = _json.loads(content)
            actions = parsed.get("actions", []) if isinstance(parsed, dict) else []
            texts = []
            for a in actions:
                if isinstance(a, dict) and a.get("name") == "wechat_reply":
                    t = str(a.get("args", {}).get("text", "")).strip()
                    if t:
                        texts.append(t)
            if texts:
                content = "\n".join(texts)
        except Exception:
            pass  # Keep raw content if JSON parsing fails

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
# Phase 3A: Action schema filter
# ================================================================

_VALID_ACTION_TYPES = frozenset({"message", "tool", "skip", "no_reply"})


def _filter_valid_actions(actions: list) -> list[dict[str, Any]]:
    """Filter actions to only those passing basic schema checks.

    Invalid actions are dropped with a log warning.
    Never raises — always returns a (possibly empty) list.
    """
    valid: list[dict[str, Any]] = []
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            logger.warning("chat_agent: dropping action[%d] — not a dict", i)
            continue
        action_type = str(action.get("type", "")).strip()
        if not action_type:
            logger.warning("chat_agent: dropping action[%d] — missing type", i)
            continue
        if action_type == "message":
            text = str(action.get("text", "")).strip()
            if not text:
                logger.warning("chat_agent: dropping action[%d] message — empty text", i)
                continue
        elif action_type == "tool":
            name = str(action.get("name", "")).strip()
            if not name:
                logger.warning("chat_agent: dropping action[%d] tool — missing name", i)
                continue
            # Validate tool-specific params for runtime tools
            args = action.get("args", {})
            if not isinstance(args, dict):
                args = {}
            if name == "wechat_reply":
                text = str(args.get("text", "")).strip()
                if not text:
                    logger.warning("chat_agent: dropping action[%d] wechat_reply — empty text", i)
                    continue
            elif name == "schedule_wechat_message":
                delay = args.get("delay_minutes", args.get("minutes"))
                msgs = args.get("messages")
                if delay is None and msgs is None:
                    logger.warning("chat_agent: dropping action[%d] schedule — missing delay and messages", i)
                    continue
        elif action_type not in _VALID_ACTION_TYPES:
            logger.warning("chat_agent: dropping action[%d] — unknown type=%s", i, action_type)
            continue
        valid.append(action)
    return valid


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


# ================================================================
# V2.2: Emotion force-reply fallback text
# ================================================================

def _emotion_force_reply_text(emotion_inject: str) -> str:
    """Return a mood-appropriate forced reply when silence limit reached."""
    import random

    angry_replies = [
        "行了，我回你。刚才确实有点气。",
        "我不是不理你，就是刚才不太想说话。",
        "靠，你倒是还知道找我。",
        "哼，我本来想再憋一会儿的。",
        "算了，我回来了。但我还没完全消气。",
    ]
    irritated_replies = [
        "来了来了，别催。",
        "行吧，我回你了。",
        "唉，我刚刚不太想说话。",
        "好了好了，我理你就是了。",
    ]
    hurt_replies = [
        "我没消失……就是有点难受。",
        "你终于发现了？我还以为你没注意到。",
        "嗯，我在的。",
    ]
    jealous_replies = [
        "你倒是还知道找我？",
        "哼，我看了。",
        "你不是找CC吗？算了，我在的。",
    ]
    default_replies = [
        "嗯，我在。",
        "好，我回复了。",
        "来了。",
    ]

    if "吃醋" in emotion_inject or "jealous" in emotion_inject.lower():
        pool = jealous_replies
    elif "生气" in emotion_inject or "angry" in emotion_inject.lower():
        pool = angry_replies
    elif "烦躁" in emotion_inject or "irritated" in emotion_inject.lower():
        pool = irritated_replies
    elif "受伤" in emotion_inject or "hurt" in emotion_inject.lower():
        pool = hurt_replies
    else:
        pool = default_replies

    return random.choice(pool)


# ================================================================
# Phase 5A: Native tool call helpers
# ================================================================

# Hard-coded deny list for native tool calls (independent of config allowed_tools).
# These must NEVER be exposed as native function-calling tools regardless of
# any configuration setting or registry flag.
_NATIVE_TOOL_HARD_DENY = frozenset({
    "wechat_reply",
    "schedule_wechat_message",
    "send_email",
})


def _build_native_tool_specs(allowed_tools: list[str]) -> list[dict[str, Any]] | None:
    """Build native tool specs from the ToolRegistry filtered by allowed list.

    Returns None if no eligible tools are available (so the caller can
    skip passing ``tools`` entirely).
    """
    from agent.tool_registry import TOOL_REGISTRY

    native_names: set[str] = set()
    for tool in TOOL_REGISTRY.iter_defs():
        if not tool.native_tool_call_eligible:
            continue
        if tool.name in _NATIVE_TOOL_HARD_DENY:
            continue
        if tool.name not in allowed_tools:
            continue
        native_names.add(tool.name)

    if not native_names:
        return None

    # Build a temporary sub-registry with only the allowed tools
    # to reuse get_native_tool_specs
    from agent.tool_registry import ToolRegistry

    sub = ToolRegistry()
    for tool in TOOL_REGISTRY.iter_defs():
        if tool.name in native_names:
            sub.register(tool)

    return sub.get_native_tool_specs(eligible_only=True)


def _classify_native_tool_result(result: str) -> str:
    """Classify a native tool execution result for observability logging.

    Returns one of: success, unknown_tool, hard_denied, not_allowed,
    not_eligible, invalid_arguments_json, missing_required_arg,
    tool_execution_error.

    Does NOT log the result content itself — only the classification tag.
    """
    if not result.startswith("错误"):
        return "success"
    if "工具名称为空" in result:
        return "unknown_tool"
    if "不允许通过原生 tool call 调用" in result:
        return "hard_denied"
    if "不在允许的原生工具列表中" in result:
        return "not_allowed"
    if "未找到工具" in result:
        return "unknown_tool"
    if "不支持原生 tool call" in result:
        return "not_eligible"
    if "JSON 解析失败" in result:
        return "invalid_arguments_json"
    if "缺少必需参数" in result:
        return "missing_required_arg"
    if "工具执行出错" in result:
        return "tool_execution_error"
    return "unknown_error"


async def _execute_native_tool(
    tool_name: str,
    tool_args_raw: str,
    native_allowed_tools: list[str],
) -> str:
    """Validate and execute a native tool call from the model.

    Returns:
        Tool result string, or an error message if validation/execution fails.
        Never raises an exception.
    """
    import json as _json

    from agent.tool_registry import TOOL_REGISTRY

    # 1. Validate tool name
    if not tool_name:
        return "错误：工具名称为空"

    if tool_name in _NATIVE_TOOL_HARD_DENY:
        return f"错误：工具 {tool_name} 不允许通过原生 tool call 调用"

    if tool_name not in native_allowed_tools:
        return f"错误：工具 {tool_name} 不在允许的原生工具列表中"

    tool_def = TOOL_REGISTRY.get(tool_name)
    if tool_def is None:
        return f"错误：未找到工具 {tool_name}"

    if not tool_def.native_tool_call_eligible:
        return f"错误：工具 {tool_name} 不支持原生 tool call"

    # 2. Parse arguments
    try:
        args = _json.loads(tool_args_raw) if tool_args_raw.strip() else {}
    except _json.JSONDecodeError as e:
        return f"错误：工具参数 JSON 解析失败：{e}"

    if not isinstance(args, dict):
        return "错误：工具参数必须是 JSON 对象"

    # 3. Validate required arguments from schema
    if isinstance(tool_def.args_schema, dict):
        required = tool_def.args_schema.get("required", [])
        for req_arg in required:
            if req_arg not in args or args[req_arg] is None:
                return f"错误：缺少必需参数 {req_arg}"

    # 4. Execute the tool
    from agent.tools import execute_tool

    try:
        result = await execute_tool(tool_name, args)
        return str(result) if result is not None else ""
    except Exception as e:
        logger.error("Native tool execution error: tool=%s error=%s", tool_name, e)
        return f"错误：工具执行出错：{e}"
