"""ChatAgent — v0.2 Phase 1 state-machine-driven chat loop.

Replaces the old procedural for-loop with an explicit Level-0 state machine.
All visible WeChat output must go through: native tool calls → guard → wechat_sender.
assistant.content is never directly sent to WeChat.
send_wechat_messages tool result is never fed back to the model.
skip_reply is a formal terminal tool.
Old JSON actions are NOT recognized.
"""

from __future__ import annotations

import json as _json
import logging
import re as _re
import time
from typing import Any, Callable, Coroutine

from agent.state_machine import (
    TERMINAL_STATES,
    TurnResult,
    TurnScratchpad,
    TurnState,
)
from agent.tool_classifier import classify_tool_calls
from agent.tool_registry import TOOL_REGISTRY

logger = logging.getLogger(__name__)

# Safeguard constants
MAX_TOOL_LOOP = 5
MAX_MESSAGE_LEN = 500
MAX_MESSAGE_COUNT = 8

# Route constants (kept for diagnostics)
ROUTE_SYSTEM_FAST = "SystemFastPath"
ROUTE_CHAT_LITE = "ChatLite"
ROUTE_CHAT_FULL = "ChatFull"

CTX_HOT_ONLY = "hot_only"
CTX_HOT_CHEAP = "hot+cheap_memory"
CTX_HOT_FULL = "hot+full_memory"

# WeChat sender callback type
WeChatSender = Callable[[list[dict[str, Any]]], Coroutine[Any, Any, list[dict[str, Any]]]]


class ChatAgent:
    """Main chat agent — v0.2 explicit state machine (Phase 1)."""

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
    ):
        self.llm_gateway = llm_gateway
        self.model = model
        self.persona = persona
        self.personality = personality
        self.temperature = temperature
        self.defaults = defaults or {}
        self.native_tool_calls = native_tool_calls
        self.native_allowed_tools = native_allowed_tools or []
        self.json_output_enabled = json_output_enabled  # always false in v0.2
        self.test_mode = test_mode
        self._last_call_time = 0.0

    # ================================================================
    # Public entry point
    # ================================================================

    async def chat(
        self,
        user_message: str,
        history: list[dict[str, Any]],
        *,
        trigger: str = "user",
        route: str = "",
        context_policy: str = "",
        bot_id: str = "default",
        system_prompt: str = "",
        wechat_sender: WeChatSender | None = None,
    ) -> TurnResult:
        """Process a user message through the v0.2 state machine.

        Returns TurnResult with terminal_state, visible_messages, and diagnostics.
        """
        turn = TurnScratchpad(user_message=user_message, trigger=trigger)
        state = TurnState.BUILD_CONTEXT

        # ── State machine loop ──────────────────────────────────
        while state not in TERMINAL_STATES:
            if turn.llm_call_count >= turn.max_iterations:
                logger.warning(
                    "state_machine: max_iterations=%d exceeded, forcing HARD_FALLBACK",
                    turn.max_iterations,
                )
                turn.add_fallback_event(
                    "max_iterations_exceeded",
                    f"reached {turn.llm_call_count} LLM calls",
                )
                state = TurnState.HARD_FALLBACK
                # Continue the loop so HARD_FALLBACK is handled below

            if logger.isEnabledFor(logging.DEBUG) and state == TurnState.CALL_LLM:
                _dump_llm_payload(turn.messages, turn.turn_id, turn.llm_call_count)

            state = await self._dispatch(
                state, turn,
                system_prompt=system_prompt,
                history=history,
                trigger=trigger,
                wechat_sender=wechat_sender,
            )
            turn.iteration += 1

        # ── Build result ────────────────────────────────────────
        self._last_call_time = time.time()
        source, author, trigger_type = _resolve_outbound_semantics(trigger)
        context_sample = _build_context_sample(turn.messages)

        return TurnResult(
            terminal_state=state,
            turn_id=turn.turn_id,
            user_message=turn.user_message,
            visible_messages=turn.sent_results,
            tool_results=turn.tool_results,
            fallback_events=turn.fallback_events,
            token_usage=turn.token_usage,
            iterations=turn.llm_call_count,
            elapsed_ms=turn.elapsed_ms,
            llm_status=turn.llm_status,
            source=source,
            author=author,
            trigger=trigger_type,
            context_sample=context_sample,
        )

    # ================================================================
    # State dispatcher
    # ================================================================

    async def _dispatch(
        self,
        state: TurnState,
        turn: TurnScratchpad,
        *,
        system_prompt: str,
        history: list[dict[str, Any]],
        trigger: str,
        wechat_sender: WeChatSender | None,
    ) -> TurnState:
        """Route each state to its handler. Returns the next state."""

        if state == TurnState.BUILD_CONTEXT:
            return self._do_build_context(turn, system_prompt, history, trigger)

        elif state == TurnState.CALL_LLM:
            return await self._do_call_llm(turn)

        elif state == TurnState.HANDLE_MODEL_OUTPUT:
            return self._do_handle_model_output(turn)

        elif state == TurnState.CLASSIFY_TOOL_CALLS:
            return self._do_classify_tool_calls(turn)

        elif state == TurnState.EXEC_FACT_TOOLS:
            return await self._do_exec_fact_tools(turn)

        elif state == TurnState.EXEC_STATE_TOOLS:
            return await self._do_exec_state_tools(turn)

        elif state == TurnState.PROTOCOL_RETRY:
            return self._do_protocol_retry(turn)

        elif state == TurnState.TERMINAL_ACTION_BRANCH:
            return self._do_terminal_action_branch(turn)

        elif state == TurnState.PARSE_VISIBLE_BATCH:
            return self._do_parse_visible_batch(turn)

        elif state == TurnState.GUARD_OUTPUT:
            return self._do_guard_output(turn)

        elif state == TurnState.EXEC_SEND_WECHAT:
            return await self._do_exec_send_wechat(turn, wechat_sender)

        elif state == TurnState.DECIDE_SEND_TERMINAL:
            return self._do_decide_send_terminal(turn)

        elif state == TurnState.EXEC_SKIP_REPLY:
            return self._do_exec_skip_reply(turn)

        elif state == TurnState.HARD_FALLBACK:
            return await self._do_hard_fallback(turn, wechat_sender)

        else:
            logger.error("state_machine: unknown state %s", state)
            turn.add_fallback_event("unknown_state", str(state))
            return TurnState.HARD_FALLBACK

    # ================================================================
    # State handlers
    # ================================================================

    def _do_build_context(
        self,
        turn: TurnScratchpad,
        system_prompt: str,
        history: list[dict[str, Any]],
        trigger: str,
    ) -> TurnState:
        """BUILD_CONTEXT: assemble the initial messages array."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt or self._default_system_prompt()},
        ]

        # Short-term history: last 24 messages (12 turns)
        recent = history[-24:] if len(history) > 24 else history
        for h in recent:
            role = h.get("role", "user")
            if role not in ("system", "user", "assistant"):
                role = "user"
            content = _format_history_entry(h)
            if content.strip():
                messages.append({"role": role, "content": content})

        # Current user message
        if trigger == "user":
            messages.append({"role": "user", "content": turn.user_message})
        else:
            messages.append({"role": "system", "content": turn.user_message})

        turn.messages = messages
        return TurnState.CALL_LLM

    async def _do_call_llm(self, turn: TurnScratchpad) -> TurnState:
        """CALL_LLM: invoke the LLM via gateway."""
        from agent.llm_types import LLMRequest

        turn.llm_call_count += 1

        request_tools = None
        if self.native_tool_calls and self.native_allowed_tools:
            request_tools = _build_native_tool_specs(self.native_allowed_tools)
            if request_tools:
                _tool_names = [t["function"]["name"] for t in request_tools]
                logger.info(
                    "native_tools: exposing %d tools: %s",
                    len(_tool_names), ",".join(_tool_names),
                )

        try:
            response = await self.llm_gateway.complete(
                LLMRequest(
                    purpose="chat_turn",
                    model=self.model,
                    messages=turn.messages,
                    temperature=self.temperature,
                    max_tokens=2048,
                    response_mode="text",
                    thinking="disabled",
                    tools=request_tools,
                    tool_choice="auto" if request_tools else None,
                    metadata={
                        "turn_id": turn.turn_id,
                        "iteration": turn.iteration,
                    },
                )
            )
            _merge_llm_usage(turn.token_usage, getattr(response, "usage", None))
            turn._llm_response = response  # transient, consumed by HANDLE_MODEL_OUTPUT
            return TurnState.HANDLE_MODEL_OUTPUT

        except Exception as e:
            logger.error("LLM call failed: %s", e)
            turn.llm_status = "error"
            turn.add_fallback_event("llm_error", str(e)[:200])
            return TurnState.HARD_FALLBACK

    def _do_handle_model_output(self, turn: TurnScratchpad) -> TurnState:
        """HANDLE_MODEL_OUTPUT: inspect what the model returned."""
        response = turn._llm_response
        native_tool_calls = getattr(response, "tool_calls", None) or []
        assistant_content = (response.content or "").strip()

        if native_tool_calls:
            # Serialize and append assistant message with tool_calls
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": assistant_content or "",
            }
            serialized: list[dict[str, Any]] = []
            for tc in native_tool_calls:
                fn = getattr(tc, "function", None)
                serialized.append({
                    "id": getattr(tc, "id", ""),
                    "type": "function",
                    "function": {
                        "name": str(getattr(fn, "name", "") if fn else ""),
                        "arguments": str(getattr(fn, "arguments", "") if fn else ""),
                    },
                })
            assistant_msg["tool_calls"] = serialized
            turn.messages.append(assistant_msg)

            tc_names = [s["function"]["name"] for s in serialized]
            logger.info(
                "native_tools: received %d tool_calls iter=%d: %s",
                len(native_tool_calls), turn.iteration, ",".join(tc_names),
            )

            turn._tool_calls = native_tool_calls  # transient
            return TurnState.CLASSIFY_TOOL_CALLS

        # No tool calls
        if assistant_content:
            turn.add_fallback_event(
                "assistant_content_no_tool_call",
                f"model returned {len(assistant_content)} chars without tool calls",
            )
            # Check for old JSON actions — flag it
            if '"actions"' in assistant_content.lower():
                turn.add_fallback_event(
                    "old_json_actions_detected",
                    "model output looks like old JSON actions format",
                )
        else:
            turn.add_fallback_event(
                "empty_response",
                "no tool calls and no assistant content",
            )

        return TurnState.PROTOCOL_RETRY

    def _do_classify_tool_calls(self, turn: TurnScratchpad) -> TurnState:
        """CLASSIFY_TOOL_CALLS: classify tools and check parallel-call legality."""
        tool_calls = turn._tool_calls
        classified = classify_tool_calls(tool_calls, TOOL_REGISTRY)
        turn._classified = classified  # transient

        if not classified.is_legal:
            turn.add_fallback_event(
                "illegal_tool_combination",
                classified.violation_reason(),
            )
            return TurnState.PROTOCOL_RETRY

        if classified.has_terminal:
            return TurnState.TERMINAL_ACTION_BRANCH

        if classified.has_fact:
            return TurnState.EXEC_FACT_TOOLS

        if classified.has_state:
            return TurnState.EXEC_STATE_TOOLS

        # Shouldn't reach here if tool_calls were present, but safety
        turn.add_fallback_event("no_actionable_tools", "all tools classified but none actionable")
        return TurnState.PROTOCOL_RETRY

    async def _do_exec_fact_tools(self, turn: TurnScratchpad) -> TurnState:
        """EXEC_FACT_TOOLS: execute fact tools (get_time, get_weather).

        Results are appended to messages and the model is called again.
        Fact tools can NOT end the turn.
        """
        classified = turn._classified
        tool_calls = turn._tool_calls

        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            tool_name = str(getattr(fn, "name", "") if fn else "")
            tool_args_raw = str(getattr(fn, "arguments", "") if fn else "")

            if tool_name not in ("get_time", "get_weather"):
                continue

            result = await _execute_native_fact_tool(
                tool_name=tool_name,
                tool_args_raw=tool_args_raw,
                native_allowed_tools=self.native_allowed_tools,
            )
            turn.tool_results.append({"name": tool_name, "result": result[:500]})
            turn.messages.append({
                "role": "tool",
                "tool_call_id": getattr(tc, "id", ""),
                "content": result,
            })
            logger.info(
                "native_tools: fact tool %s executed, result=%s",
                tool_name, _classify_tool_result(result),
            )

        return TurnState.CALL_LLM

    async def _do_exec_state_tools(self, turn: TurnScratchpad) -> TurnState:
        """EXEC_STATE_TOOLS: execute state tools. Phase 1 — no instances yet.

        Reserved for future STATE_TOOL registration (e.g. propose_memory_update).
        Always returns to CALL_LLM after execution.
        """
        turn.add_fallback_event(
            "state_tools_noop",
            "state tools not yet implemented, returning to LLM",
        )
        return TurnState.CALL_LLM

    def _do_protocol_retry(self, turn: TurnScratchpad) -> TurnState:
        """PROTOCOL_RETRY: model didn't follow protocol.

        Injects a system reminder and goes back to CALL_LLM.
        Does NOT feed assistant.content back to the model.
        If retry limit exceeded, goes to HARD_FALLBACK.
        """
        turn.protocol_retry_count += 1

        if turn.protocol_retry_count > turn.max_protocol_retries:
            turn.add_fallback_event(
                "protocol_retry_exceeded",
                f"exceeded max protocol retries ({turn.max_protocol_retries})",
            )
            return TurnState.HARD_FALLBACK

        turn.add_fallback_event(
            "protocol_retry",
            f"attempt {turn.protocol_retry_count}/{turn.max_protocol_retries}",
        )
        logger.info(
            "state_machine: protocol_retry attempt=%d/%d",
            turn.protocol_retry_count, turn.max_protocol_retries,
        )

        # Inject system reminder — pure instruction, no raw content
        retry_note = (
            "你没有调用 send_wechat_messages 或 skip_reply。"
            "必须调用以下终态工具之一来结束本轮："
            "send_wechat_messages 发送回复，或 skip_reply 明确跳过。"
            "不要直接在 assistant content 里写回复。"
            "不要展示或复述历史记录、对话记录、或上下文标签。"
        )
        turn.messages.append({"role": "system", "content": retry_note})

        return TurnState.CALL_LLM

    def _do_terminal_action_branch(self, turn: TurnScratchpad) -> TurnState:
        """TERMINAL_ACTION_BRANCH: route to send or skip based on terminal tool name."""
        terminal_name = turn._classified.terminal_name()

        if terminal_name == "send_wechat_messages":
            return TurnState.PARSE_VISIBLE_BATCH
        elif terminal_name == "skip_reply":
            return TurnState.EXEC_SKIP_REPLY
        else:
            turn.add_fallback_event(
                "unknown_terminal_tool", f"terminal tool '{terminal_name}' not recognized",
            )
            return TurnState.PROTOCOL_RETRY

    # ── Send path ───────────────────────────────────────────────

    def _do_parse_visible_batch(self, turn: TurnScratchpad) -> TurnState:
        """PARSE_VISIBLE_BATCH: extract messages from send_wechat_messages args."""
        classified = turn._classified
        tc = classified.terminal_tools[0]  # Guaranteed exactly one terminal tool
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "")

        try:
            args = _json.loads(args_raw) if args_raw.strip() else {}
        except _json.JSONDecodeError as e:
            turn.add_fallback_event(
                "parse_visible_batch_error",
                f"JSON parse failed: {e}",
            )
            return TurnState.PROTOCOL_RETRY

        if not isinstance(args, dict):
            turn.add_fallback_event(
                "parse_visible_batch_error",
                "args is not a JSON object",
            )
            return TurnState.PROTOCOL_RETRY

        raw_messages = args.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            turn.add_fallback_event(
                "parse_visible_batch_error",
                "messages missing or not a list",
            )
            return TurnState.PROTOCOL_RETRY

        turn.visible_batch = [str(m) for m in raw_messages]
        # Store mode for guard
        turn._send_mode = str(args.get("mode", "normal") or "normal")

        logger.info(
            "state_machine: parsed %d visible messages, mode=%s",
            len(turn.visible_batch), turn._send_mode,
        )
        return TurnState.GUARD_OUTPUT

    def _do_guard_output(self, turn: TurnScratchpad) -> TurnState:
        """GUARD_OUTPUT: run all output guards on the parsed batch."""
        from agent.output_guard import guard_send_message_batch

        guard_result = guard_send_message_batch(
            turn.visible_batch, mode=getattr(turn, "_send_mode", "normal"),
        )

        if guard_result.blocked_count > 0:
            turn.add_fallback_event(
                "guard_blocked",
                f"blocked {guard_result.blocked_count}/{guard_result.total} messages",
            )
        if guard_result.truncated_count > 0:
            turn.add_fallback_event(
                "guard_truncated",
                f"truncated {guard_result.truncated_count} messages",
            )

        if not guard_result.messages:
            turn.add_fallback_event(
                "guard_all_blocked",
                "all messages blocked by output guard",
            )
            return TurnState.HARD_FALLBACK

        turn.guarded_batch = [
            {"text": text, "delay": 0.0}
            for text in guard_result.messages
        ]
        return TurnState.EXEC_SEND_WECHAT

    async def _do_exec_send_wechat(
        self, turn: TurnScratchpad, wechat_sender: WeChatSender | None,
    ) -> TurnState:
        """EXEC_SEND_WECHAT: compute delays and send via wechat_sender callback."""
        from agent.tools import compute_message_delay

        # Record tool result for diagnostics
        turn.tool_results.append({
            "name": "send_wechat_messages",
            "result": f"prepared {len(turn.guarded_batch)} messages",
        })

        if not wechat_sender:
            for m in turn.guarded_batch:
                m["send_status"] = "generated"
            turn.sent_results = list(turn.guarded_batch)
            turn.add_fallback_event("send_no_sender", "no wechat_sender callback")
            return TurnState.DECIDE_SEND_TERMINAL

        # Compute delays
        total = len(turn.guarded_batch)
        for i, m in enumerate(turn.guarded_batch):
            m["delay"] = compute_message_delay(
                i, m["text"], total, test_mode=self.test_mode,
            )

        # Send (Phase 1: single attempt; Phase 2 will add retry loop)
        try:
            turn.sent_results = await wechat_sender(turn.guarded_batch)
            logger.info(
                "state_machine: sent %d messages via wechat_sender",
                len(turn.sent_results),
            )
        except Exception as e:
            logger.error("state_machine: wechat_sender failed: %s", e)
            turn.add_fallback_event("send_error", str(e)[:200])
            for m in turn.guarded_batch:
                m["send_status"] = "failed"
            turn.sent_results = list(turn.guarded_batch)

        return TurnState.DECIDE_SEND_TERMINAL

    def _do_decide_send_terminal(self, turn: TurnScratchpad) -> TurnState:
        """DECIDE_SEND_TERMINAL: determine END_SENT / END_PARTIAL_SENT / END_FAILED."""
        if not turn.sent_results:
            return TurnState.END_FAILED

        delivered = [r for r in turn.sent_results if r.get("send_status") == "delivered"]
        failed = [r for r in turn.sent_results if r.get("send_status") != "delivered"]

        if not delivered and not failed:
            return TurnState.END_FAILED

        if delivered and not failed:
            return TurnState.END_SENT

        if delivered and failed:
            turn.add_fallback_event(
                "partial_send",
                f"{len(delivered)} delivered, {len(failed)} failed",
            )
            return TurnState.END_PARTIAL_SENT

        # All failed
        turn.add_fallback_event("send_all_failed", f"{len(failed)} messages all failed")
        return TurnState.END_FAILED

    # ── Skip path ────────────────────────────────────────────────

    def _do_exec_skip_reply(self, turn: TurnScratchpad) -> TurnState:
        """EXEC_SKIP_REPLY: model explicitly chose not to reply."""
        # Log the reason if provided
        classified = turn._classified
        if classified.terminal_tools:
            tc = classified.terminal_tools[0]
            fn = tc.get("function", {})
            args_raw = fn.get("arguments", "")
            try:
                args = _json.loads(args_raw) if args_raw.strip() else {}
                reason = str(args.get("reason", ""))[:200]
            except _json.JSONDecodeError:
                reason = ""
            if reason:
                turn.add_fallback_event("skip_reply_reason", reason)

        turn.add_fallback_event("skip_reply", "model explicitly skipped reply")
        logger.info("state_machine: skip_reply → END_SKIPPED")
        return TurnState.END_SKIPPED

    # ── Fallback path ───────────────────────────────────────────

    async def _do_hard_fallback(
        self, turn: TurnScratchpad, wechat_sender: WeChatSender | None,
    ) -> TurnState:
        """HARD_FALLBACK: system fixed fallback text.

        Uses unified HARD_FALLBACK_TEXT from output_guard.
        Does NOT use model content.
        Still goes through guard and wechat_sender.
        """
        from agent.output_guard import HARD_FALLBACK_TEXT, guard_single_message

        turn.add_fallback_event("hard_fallback_enter", "entering hard fallback")

        # Guard the hard fallback text
        guarded = guard_single_message(HARD_FALLBACK_TEXT)
        if not guarded:
            turn.add_fallback_event(
                "hard_fallback_guard_failed",
                "hard fallback text blocked by guard",
            )
            return TurnState.END_FAILED

        # Send
        if wechat_sender:
            try:
                fallback_msg = [{"text": guarded, "delay": 0.3}]
                result = await wechat_sender(fallback_msg)
                turn.sent_results = result
                delivered = [r for r in result if r.get("send_status") == "delivered"]
                if delivered:
                    turn.add_fallback_event("hard_fallback_sent", "fallback delivered")
                    return TurnState.END_FALLBACK_SENT
                else:
                    turn.add_fallback_event("hard_fallback_send_failed", "fallback not delivered")
                    return TurnState.END_FAILED
            except Exception as e:
                logger.error("hard_fallback: send failed: %s", e)
                turn.add_fallback_event("hard_fallback_send_error", str(e)[:200])
                return TurnState.END_FAILED
        else:
            # No sender — record prepared message for diagnostics
            turn.sent_results = [{"text": guarded, "delay": 0.0, "send_status": "generated"}]
            turn.add_fallback_event("hard_fallback_no_sender", "no wechat_sender, message prepared")
            return TurnState.END_FALLBACK_SENT

    # ================================================================
    # Default system prompt (fallback when none provided)
    # ================================================================

    def _default_system_prompt(self) -> str:
        """Minimal fallback system prompt."""
        from datetime import datetime

        now = datetime.now().strftime("%Y年%m月%d日 %H:%M 周%u")
        default_city = str(self.defaults.get("default_city", "") or "").strip()

        parts = [
            self.persona,
            self.personality,
            f"当前时间：{now}",
        ]
        if default_city:
            parts.append(f"用户默认天气城市：{default_city}")
        parts.append("可用工具：get_time, get_weather, send_wechat_messages, skip_reply")
        parts.append(
            "通过 send_wechat_messages 发送微信回复。不回复时调用 skip_reply。"
        )

        return "\n\n".join(p.strip() for p in parts if p.strip())


# ================================================================
# Native tool spec builder
# ================================================================

def _build_native_tool_specs(allowed_tools: list[str]) -> list[dict[str, Any]] | None:
    """Build native tool specs filtered by allowed list.

    Only includes tools with native_tool_call_eligible=True that are in
    the allowed list.
    """
    specs: list[dict[str, Any]] = []
    native_names: set[str] = set()

    for tool in TOOL_REGISTRY.iter_defs():
        if not tool.native_tool_call_eligible:
            continue
        if tool.name not in allowed_tools:
            continue
        native_names.add(tool.name)

    if native_names:
        from agent.tool_registry import ToolRegistry

        sub = ToolRegistry()
        for tool in TOOL_REGISTRY.iter_defs():
            if tool.name in native_names:
                sub.register(tool)
        specs.extend(sub.get_native_tool_specs(eligible_only=True))

    return specs or None


# ================================================================
# Fact tool executor
# ================================================================

async def _execute_native_fact_tool(
    tool_name: str,
    tool_args_raw: str,
    native_allowed_tools: list[str],
) -> str:
    """Execute a fact tool (get_time / get_weather) and return structured result."""
    from agent.tools import execute_tool

    if tool_name not in native_allowed_tools:
        return f"错误：工具 {tool_name} 不在允许的原生工具列表中"

    tool_def = TOOL_REGISTRY.get(tool_name)
    if tool_def is None:
        return f"错误：未找到工具 {tool_name}"

    if not tool_def.native_tool_call_eligible:
        return f"错误：工具 {tool_name} 不支持原生 tool call"

    # Parse arguments
    try:
        args = _json.loads(tool_args_raw) if tool_args_raw.strip() else {}
    except _json.JSONDecodeError as e:
        return f"错误：工具参数 JSON 解析失败：{e}"

    if not isinstance(args, dict):
        return "错误：工具参数必须是 JSON 对象"

    # Validate required args
    if isinstance(tool_def.args_schema, dict):
        required = tool_def.args_schema.get("required", [])
        for req_arg in required:
            if req_arg not in args or args[req_arg] is None:
                return f"错误：缺少必需参数 {req_arg}"

    # Execute
    try:
        result = await execute_tool(tool_name, args)
    except Exception as e:
        logger.error("Native fact tool error: tool=%s error=%s", tool_name, e)
        return f"错误：工具执行出错：{e}"

    return str(result) if result is not None else ""


# ================================================================
# Helpers (kept from Phase 1)
# ================================================================

def _format_history_entry(item: dict[str, Any]) -> str:
    """Format one history entry for the LLM context.

    No labels, no metadata, no tool_call_id in content.
    History is delivered via role-based messages only.
    """
    content = str(item.get("content", ""))
    role = str(item.get("role", ""))

    if role == "assistant":
        content = _extract_visible_text(content)

    if not content.strip():
        return ""

    # Hard strip: any call_xxx pattern means this entry is contaminated, skip it
    if _re.search(r'\bcall_[A-Za-z0-9_-]{8,}\b', content):
        logger.warning("_format_history_entry: skipping contaminated entry with call_ pattern")
        return ""

    return content


def _extract_visible_text(content: str) -> str:
    """Extract visible text, stripping tool_call_id contamination."""
    if not content or not content.strip():
        return content

    # Hard strip tool_call_id patterns
    content = _re.sub(r'\bcall_[A-Za-z0-9_-]{8,}\b', '', content)

    # v0.2: old JSON actions are NOT unwrapped. They are treated as plain text.
    # If content looks like old JSON actions, it's already flagged as
    # "old_json_actions_detected" in HANDLE_MODEL_OUTPUT → PROTOCOL_RETRY.

    return content


def _merge_llm_usage(total: dict[str, Any], usage: Any) -> None:
    if usage is None:
        return
    total["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
    total["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
    total["total_tokens"] += int(getattr(usage, "total_tokens", 0) or 0)
    total["estimated"] = bool(total.get("estimated") or getattr(usage, "estimated", False))
    total["cache_hit_tokens"] = total.get("cache_hit_tokens", 0) + int(getattr(usage, "cache_hit_tokens", 0) or 0)
    total["cache_miss_tokens"] = total.get("cache_miss_tokens", 0) + int(getattr(usage, "cache_miss_tokens", 0) or 0)
    total["reasoning_tokens"] = total.get("reasoning_tokens", 0) + int(getattr(usage, "reasoning_tokens", 0) or 0)


def _resolve_outbound_semantics(trigger: str) -> tuple[str, str, str]:
    if trigger == "admin":
        return ("admin", "system", "manual_admin")
    return ("user_reply", "assistant", "user_message")


def _classify_tool_result(result: str) -> str:
    if not result.startswith("错误"):
        return "success"
    if "工具名称为空" in result or "未找到工具" in result or "不可用" in result:
        return "unknown_tool"
    if "不在允许的原生工具列表中" in result:
        return "not_allowed"
    if "不支持原生 tool call" in result:
        return "not_eligible"
    if "JSON 解析失败" in result:
        return "invalid_arguments_json"
    if "缺少必需参数" in result:
        return "missing_required_arg"
    if "工具执行出错" in result:
        return "tool_execution_error"
    return "unknown_error"


def _build_context_sample(messages: list[dict[str, Any]], last_n: int = 8) -> list[dict[str, str]]:
    sample: list[dict[str, str]] = []
    for m in messages[-last_n:]:
        role = str(m.get("role", "?"))
        if role == "system":
            continue
        content = str(m.get("content", ""))
        sample.append({
            "role": role,
            "preview": content[:120].replace("\n", "\\n"),
        })
    return sample


def _dump_llm_payload(messages: list[dict[str, Any]], turn_id: str, iteration: int) -> None:
    """Pre-LLM safety scan — detect call_ leaks before they reach the model."""
    for i, m in enumerate(messages):
        content = str(m.get("content", ""))
        if _re.search(r'\bcall_[A-Za-z0-9_-]{8,}\b', content):
            logger.error(
                "LLM_PAYLOAD_LEAK: call_ pattern found in message[%d] role=%s",
                i, m.get("role", "?"),
            )
        tc = m.get("tool_calls")
        if tc and isinstance(tc, list):
            for tci, t in enumerate(tc):
                if isinstance(t, dict) and "id" in t:
                    cid = str(t["id"])
                    if cid.startswith("call_"):
                        logger.debug(
                            "LLM_PAYLOAD: tool_call_id %s in message[%d].tool_calls[%d]",
                            cid, i, tci,
                        )

    for i, m in enumerate(messages):
        role = str(m.get("role", "?"))
        content = str(m.get("content", ""))
        if role == "system":
            preview = content[:300].replace("\n", "\\n")
        else:
            preview = content[:200].replace("\n", "\\n")
        logger.debug(
            "LLM_PAYLOAD[%d] role=%s preview=%s",
            i + 1, role, preview,
        )
