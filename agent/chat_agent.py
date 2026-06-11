"""Ordinary text chat agent with implicit turn completion."""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Coroutine

from agent.llm_types import LLMRequest
from agent.output_guard import guard_single_message
from agent.state_machine import (
    DeliveryStatus,
    InterruptChecker,
    MAX_LLM_STEPS_PER_TURN,
    MAX_PROTOCOL_RETRIES_PER_TURN,
    MAX_REPLY_MESSAGES_PER_TURN,
    ModelAttempt,
    ModelProfile,
    PostTerminalCallback,
    SOURCE_USER_MESSAGE,
    TurnResult,
    TurnScratchpad,
    TurnState,
    VisibleMessage,
)
from agent.tool_registry import TOOL_REGISTRY
from agent.tools import execute_tool

logger = logging.getLogger(__name__)

ROUTE_SYSTEM_FAST = "SystemFastPath"
ROUTE_CHAT_LITE = "ChatLite"
ROUTE_CHAT_FULL = "ChatFull"
CTX_HOT_ONLY = "hot_only"
CTX_HOT_CHEAP = "hot+cheap_memory"
CTX_HOT_FULL = "hot+full_memory"

WeChatSender = Callable[[list[dict[str, Any]]], Coroutine[Any, Any, list[dict[str, Any]]]]
ProtocolEventWriter = Callable[
    [TurnScratchpad, str, dict[str, Any]],
    Coroutine[Any, Any, None],
]


def _tool_name(call: Any) -> str:
    function = getattr(call, "function", None)
    return str(getattr(function, "name", "") or "")


def _tool_id(call: Any) -> str:
    return str(getattr(call, "id", "") or "")


def _tool_arguments(call: Any) -> dict[str, Any]:
    function = getattr(call, "function", None)
    raw = str(getattr(function, "arguments", "") or "")
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_use_part(call: Any) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": _tool_id(call),
        "name": _tool_name(call),
        "input": _tool_arguments(call),
    }


def _assistant_parts(response: Any, calls: list[Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    reasoning = str(getattr(response, "reasoning_content", "") or "")
    if reasoning:
        parts.append({"type": "thinking", "thinking": reasoning})
    content = str(getattr(response, "content", "") or "")
    if content:
        parts.append({"type": "text", "text": content})
    parts.extend(_tool_use_part(call) for call in calls)
    return parts


def _merge_usage(target: dict[str, int], usage: Any) -> None:
    if not usage:
        return
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    if prompt_tokens is None and isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
    if prompt_tokens is not None:
        target["peak_prompt_tokens"] = max(
            int(target.get("peak_prompt_tokens", 0)),
            int(prompt_tokens),
        )
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "reasoning_tokens",
    ):
        value = getattr(usage, key, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(key)
        if value is not None:
            target[key] = int(target.get(key, 0)) + int(value)
    estimated = getattr(usage, "estimated", None)
    if estimated is None and isinstance(usage, dict):
        estimated = usage.get("estimated")
    if estimated is not None:
        target["estimated"] = bool(target.get("estimated", False) or estimated)


class ChatAgent:
    def __init__(
        self,
        model: str,
        persona: str,
        personality: str,
        *,
        temperature: float = 0.7,
        defaults: dict[str, Any] | None = None,
        llm_gateway: Any,
        native_tool_calls: bool = True,
        native_allowed_tools: list[str] | None = None,
        json_output_enabled: bool = False,
        test_mode: bool = False,
        flash_profile: ModelProfile | None = None,
        pro_profile: ModelProfile | None = None,
        routing_mode: str = "manual_pro",
        manual_flash_allow_upgrade: bool = False,
        max_flash_upgrade_attempts: int = 1,
        write_model_history: PostTerminalCallback | None = None,
        post_turn_hooks: list[PostTerminalCallback] | None = None,
        tool_exposure_policy: Any = None,
        context_builder: Any = None,
        context_window_tokens: int = 1_000_000,
    ) -> None:
        del json_output_enabled, flash_profile, manual_flash_allow_upgrade, max_flash_upgrade_attempts
        self.model = model
        self.persona = persona
        self.personality = personality
        self.temperature = temperature
        self.defaults = defaults or {}
        self.llm_gateway = llm_gateway
        self.native_tool_calls = native_tool_calls
        self.native_allowed_tools = native_allowed_tools or [
            "send_text_message",
            "get_time",
            "get_weather",
        ]
        self.test_mode = test_mode
        self._profile = pro_profile or ModelProfile(model_name=model)
        self._routing_mode = routing_mode
        self._write_model_history = write_model_history
        self._post_turn_hooks = list(post_turn_hooks or [])
        self._tool_exposure_policy = tool_exposure_policy
        self._context_builder = context_builder
        self._context_window_tokens = max(int(context_window_tokens), 0)

    async def chat(
        self,
        user_message: str | None,
        history: list[dict[str, Any]],
        *,
        trigger: str = "user",
        route: str = "",
        context_policy: str = "",
        bot_id: str = "default",
        user_id: str = "",
        system_prompt: str = "",
        wechat_sender: WeChatSender | None = None,
        source: str = SOURCE_USER_MESSAGE,
        input_message_ids: list[str] | None = None,
        input_first_seq: int = 0,
        input_latest_seq: int = 0,
        input_cutoff_time: float = 0.0,
        interrupt_checker: InterruptChecker | None = None,
        native_allowed_tools_override: list[str] | None = None,
        write_model_history: PostTerminalCallback | None = None,
        post_turn_hooks: list[PostTerminalCallback] | None = None,
        protocol_event_writer: ProtocolEventWriter | None = None,
        idle_seconds: float | None = None,
        **unused: Any,
    ) -> TurnResult:
        prebuilt_messages = unused.get("prebuilt_messages") if unused else None
        del context_policy, unused
        if source != SOURCE_USER_MESSAGE:
            raise ValueError("only ordinary user-message turns are supported")
        if user_message is None or not str(user_message).strip():
            raise ValueError("user_message is required")

        turn = TurnScratchpad(
            user_message=str(user_message),
            trigger=trigger,
            source=source,
            input_message_ids=list(input_message_ids or []),
            input_first_seq=input_first_seq,
            input_latest_seq=input_latest_seq,
            input_cutoff_time=input_cutoff_time,
            route=route,
            interrupt_checker=interrupt_checker,
            _protocol_event_writer=protocol_event_writer,
        )
        await self._write_event(turn, "turn_started", {
            "user_message": turn.user_message,
            "bot_id": bot_id,
        })
        self._build_context(turn, history, system_prompt, bot_id, user_id,
                           prebuilt_messages=prebuilt_messages,
                           idle_seconds=idle_seconds)

        allowed = native_allowed_tools_override or self.native_allowed_tools
        allowed = [
            name for name in ("send_text_message", "get_time", "get_weather", "memory")
            if name in allowed
        ]
        tools = TOOL_REGISTRY.get_native_tool_specs(allowed_names=allowed)
        protocol_retries = 0
        terminal = TurnState.END_FAILED

        while turn.llm_call_count < MAX_LLM_STEPS_PER_TURN:
            if self._interrupted(turn):
                terminal = (
                    TurnState.END_INTERRUPTED_DURING_REPLY_SEQUENCE
                    if turn.sent_message_count
                    else TurnState.END_CANCELLED
                )
                break

            response = await self._call_model(turn, tools)
            if response is None:
                terminal = TurnState.END_FAILED
                break

            calls = list(getattr(response, "tool_calls", None) or [])
            assistant_message = {
                "role": "assistant",
                "content": _assistant_parts(response, calls),
            }
            turn.messages.append(assistant_message)
            await self._write_event(turn, "message", {
                "message": copy.deepcopy(assistant_message),
                "message_kind": "model_response",
                "call_id": f"call_{turn.turn_id}_{turn.llm_call_count}",
            })

            if not calls:
                if turn.sent_message_count:
                    turn.reply_sequence_status = "completed"
                    terminal = TurnState.END_REPLIED
                    break
                protocol_retries += 1
                turn.add_fallback_event(
                    "missing_send_text_message",
                    "model ended before sending a visible message",
                )
                if protocol_retries > MAX_PROTOCOL_RETRIES_PER_TURN:
                    terminal = TurnState.END_PROTOCOL_ERROR
                    break
                turn.messages.append({
                    "role": "user",
                    "content": (
                        "<system-reminder>\n"
                        "The user cannot see ordinary assistant text. "
                        "Use send_text_message for the visible reply.\n"
                        "</system-reminder>"
                    ),
                })
                continue

            result_parts: list[dict[str, Any]] = []
            for call in calls:
                name = _tool_name(call)
                call_id = _tool_id(call)
                if name not in allowed:
                    result = f"Tool is unavailable: {name}"
                elif name == "send_text_message":
                    result = await self._send_text(turn, call, wechat_sender)
                else:
                    result = await execute_tool(
                        name,
                        _tool_arguments(call),
                        bot_id=bot_id,
                        user_id=user_id,
                        turn_id=turn.turn_id,
                        call_id=call_id,
                    )
                record = {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "result": result,
                }
                turn.tool_results.append(record)
                result_parts.append({
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": result,
                })
                if turn.sent_message_count >= MAX_REPLY_MESSAGES_PER_TURN:
                    terminal = TurnState.END_MAX_LIMIT
                    break
            if terminal == TurnState.END_MAX_LIMIT:
                break

            tool_result_message = {"role": "user", "content": result_parts}
            turn.messages.append(tool_result_message)
            await self._write_event(turn, "message", {
                "message": copy.deepcopy(tool_result_message),
                "message_kind": "tool_result",
            })
        else:
            terminal = TurnState.END_MAX_LIMIT

        turn.terminal_state = terminal
        await self._write_event(turn, "turn_finished", {"terminal_state": terminal.value})

        writer = write_model_history if write_model_history is not None else self._write_model_history
        hooks = post_turn_hooks if post_turn_hooks is not None else self._post_turn_hooks
        if writer is not None:
            await writer(turn, terminal)
        for hook in hooks or []:
            await hook(turn, terminal)

        visible = [
            {
                "text": item.guarded_text or item.original_text,
                "delay": item.delay,
                "send_status": item.delivery_status,
                "delivery_status": item.delivery_status,
            }
            for item in turn.visible_messages
        ]
        return TurnResult(
            terminal_state=terminal,
            turn_id=turn.turn_id,
            user_message=turn.user_message,
            visible_messages=visible,
            tool_results=turn.tool_results,
            fallback_events=turn.fallback_events,
            token_usage=turn.token_usage,
            iterations=turn.llm_call_count,
            elapsed_ms=turn.elapsed_ms,
            llm_status=turn.llm_status,
            context_sample=turn.messages[-6:],
            post_terminal_completed=bool(writer or hooks),
            reply_sequence_status=turn.reply_sequence_status,
            sent_message_count=turn.sent_message_count,
        )

    def _build_context(
        self,
        turn: TurnScratchpad,
        history: list[dict[str, Any]],
        system_prompt: str,
        bot_id: str,
        user_id: str,
        *,
        prebuilt_messages: list[dict[str, Any]] | None = None,
        idle_seconds: float | None = None,
    ) -> None:
        builder = self._context_builder
        if builder is None:
            from agent.runtime_context_builder import ContextBuilder as _CB
            builder = _CB()
        built = builder.build_for_chat(
            persona=self.persona,
            personality=self.personality,
            history=history if prebuilt_messages is None else None,
            prebuilt_messages=prebuilt_messages,
            user_message=turn.user_message,
            bot_id=bot_id,
            user_id=user_id,
            system_prompt=system_prompt,
            turn_id=turn.turn_id,
            idle_seconds=idle_seconds,
        )
        turn.messages = built.messages
        turn._provider_system = built.system

    async def _call_model(
        self,
        turn: TurnScratchpad,
        tools: list[dict[str, Any]],
    ) -> Any | None:
        turn.llm_call_count += 1
        turn.context_window_tokens = self._context_window_tokens
        turn.model_name = self._profile.model_name
        started = time.time()
        attempt = ModelAttempt(
            attempt_index=turn.llm_call_count,
            selected_model="pro",
            routing_mode=self._routing_mode,
            routing_reason="ordinary_chat",
            model_name=self._profile.model_name,
            started_at=started,
        )
        try:
            call_id = f"call_{turn.turn_id}_{turn.llm_call_count}"
            response = await self.llm_gateway.complete(LLMRequest(
                purpose="chat_turn",
                model=self._profile.model_name,
                messages=copy.deepcopy(turn.messages),
                system=copy.deepcopy(getattr(turn, "_provider_system", [])),
                temperature=self._profile.temperature,
                max_tokens=self._profile.max_tokens,
                timeout_s=self._profile.timeout_seconds,
                response_mode="text",
                thinking="enabled",
                tools=tools if self.native_tool_calls else None,
                tool_choice="auto" if tools and self.native_tool_calls else None,
                output_config={"effort": "high"},
                stream=True,
                turn_id=turn.turn_id,
                call_id=call_id,
                metadata={"turn_id": turn.turn_id, "call_id": call_id},
            ))
            _merge_usage(turn.token_usage, getattr(response, "usage", None))
            attempt.result_type = "success"
            return response
        except Exception as exc:
            logger.exception("LLM call failed")
            turn.llm_status = "error"
            turn.add_fallback_event("llm_error", str(exc))
            attempt.result_type = "llm_error"
            attempt.error_type = type(exc).__name__
            return None
        finally:
            attempt.finished_at = time.time()
            attempt.latency_ms = int((attempt.finished_at - started) * 1000)
            turn.model_attempts.append(attempt)

    async def _send_text(
        self,
        turn: TurnScratchpad,
        call: Any,
        sender: WeChatSender | None,
    ) -> str:
        text = str(_tool_arguments(call).get("text", "") or "").strip()
        guarded = guard_single_message(text)
        index = len(turn.visible_messages) + 1
        visible = VisibleMessage(
            visible_message_id=f"vm_{turn.turn_id}_{index}",
            index=index,
            original_text=text,
            guarded_text=guarded,
            guard_status="PASSED" if guarded else "BLOCKED",
            delivery_status=DeliveryStatus.PENDING,
            attempt_count=1,
            tool_call_id=_tool_id(call),
            idempotency_key=f"{turn.turn_id}:{_tool_id(call)}",
        )
        turn.visible_messages.append(visible)
        if not guarded:
            visible.delivery_status = DeliveryStatus.BLOCKED_BY_GUARD
            return "blocked"
        if self._interrupted(turn):
            visible.delivery_status = DeliveryStatus.FAILED_BEFORE_SEND
            return "cancelled"
        if sender is None:
            visible.delivery_status = DeliveryStatus.FAILED_BEFORE_SEND
            visible.error_code = "sender_unavailable"
            return "failed"

        await self._write_event(turn, "outbound_created", {
            "message_id": visible.visible_message_id,
            "text": guarded,
            "position": index,
            "send_status": "created",
        })
        result = await sender([{"text": guarded, "delay": 0.0}])
        sent = result[0] if result else {}
        status = str(sent.get("send_status", "failed"))
        if status == "delivered":
            visible.delivery_status = DeliveryStatus.SENT_CONFIRMED
            visible.sent_at_timestamp = time.time()
            turn.sent_message_count += 1
            model_result = "sent"
        else:
            visible.delivery_status = DeliveryStatus.FAILED_CONFIRMED
            visible.error_code = str(sent.get("send_error", "") or "")
            model_result = "failed"
        await self._write_event(turn, "outbound_result", {
            "message_id": visible.visible_message_id,
            "text": guarded,
            "position": index,
            "send_status": status,
            "send_error": visible.error_code,
        })
        return model_result

    def _interrupted(self, turn: TurnScratchpad) -> bool:
        if turn.interrupt_checker is None:
            return False
        try:
            return bool(turn.interrupt_checker(""))
        except Exception:
            return False

    async def _write_event(
        self,
        turn: TurnScratchpad,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if turn._protocol_event_writer is not None:
            await turn._protocol_event_writer(turn, event_type, payload)
