"""ChatAgent — v1.2 Level-1 state-machine-driven chat loop.

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
    ContextBuildResult,
    DeliveryStatus,
    ErrorClass,
    InterruptChecker,
    PostTerminalCallback,
    TurnResult,
    TurnScratchpad,
    TurnState,
    VisibleMessage,
    classify_error,
)
from agent.tool_classifier import classify_tool_calls
from agent.tool_registry import TOOL_REGISTRY

logger = logging.getLogger(__name__)

# Safeguard constants
MAX_TOOL_LOOP = 5
MAX_MESSAGE_LEN = 500
MAX_MESSAGES_PER_TURN = 16        # model MUST stay within this; exceeding → PROTOCOL_RETRY
LEGACY_MAX_MESSAGE_COUNT = 8      # kept for output_guard.py compat; NOT used for truncation

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
    """Main chat agent — v1.2 explicit Level-1 state machine."""

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
        # ── v1.2: post-terminal callbacks injected by main.py ─────
        write_model_history: PostTerminalCallback | None = None,
        post_turn_hooks: list[PostTerminalCallback] | None = None,
    ):
        self.llm_gateway = llm_gateway
        self.model = model
        self.persona = persona
        self.personality = personality
        self.temperature = temperature
        self.defaults = defaults or {}
        self.native_tool_calls = native_tool_calls
        self.native_allowed_tools = native_allowed_tools or []
        self.json_output_enabled = json_output_enabled
        self.test_mode = test_mode
        self._last_call_time = 0.0
        self._interrupt_checker: InterruptChecker | None = None
        # v1.2: post-terminal pipeline callbacks
        self._write_model_history: PostTerminalCallback | None = write_model_history
        self._post_turn_hooks: list[PostTerminalCallback] = list(post_turn_hooks or [])

    def set_interrupt_checker(self, checker: InterruptChecker | None) -> None:
        """Inject an interrupt checker. Called before each turn.

        v1.2 signature: (interrupt_token: str) -> bool
        Returns True when the turn identified by interrupt_token has been
        superseded and should be cancelled (END_CANCELLED).

        In test_mode, a missing checker defaults to always-false.
        In production, a checker MUST be provided — ValueError otherwise.
        """
        self._interrupt_checker = checker

    def _is_interrupted(self, token: str = "") -> bool:
        """Check whether the current turn should be cancelled.

        Production: checker is required. If missing, raises ValueError.
        Test mode: missing checker defaults to always-false.
        """
        if not self._interrupt_checker:
            if self.test_mode:
                return False
            raise ValueError(
                "interrupt_checker is required in production mode. "
                "Inject via chat(interrupt_checker=...) or set_interrupt_checker()."
            )
        try:
            return self._interrupt_checker(token)
        except Exception:
            return False

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
        source: str = "user_message",
        input_message_ids: list[str] | None = None,
        input_first_seq: int = 0,
        input_latest_seq: int = 0,
        input_cutoff_time: float = 0.0,
        interrupt_checker: InterruptChecker | None = None,
        # ── v1.2 Phase 5C: per-request post-terminal callbacks ──
        write_model_history: PostTerminalCallback | None = None,
        post_turn_hooks: list[PostTerminalCallback] | None = None,
    ) -> TurnResult:
        """Process a user message through the v1.2 state machine.

        Returns TurnResult with terminal_state, visible_messages, and diagnostics.
        WRITE_MODEL_HISTORY and POST_TURN_HOOKS execute inside this method
        before TurnResult is assembled.
        """
        # ── Inject interrupt checker for this turn ──────────────
        if interrupt_checker is not None:
            self._interrupt_checker = interrupt_checker

        turn = TurnScratchpad(
            user_message=user_message,
            trigger=trigger,
            source=source,
            input_message_ids=list(input_message_ids or []),
            input_first_seq=input_first_seq,
            input_latest_seq=input_latest_seq,
            input_cutoff_time=input_cutoff_time,
        )
        state = TurnState.RECEIVE_EVENT

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

        # ── v1.2: Post-terminal pipeline (inside ChatAgent) ─────
        # Per-request callbacks override instance-level ones.
        # post_terminal_completed = True only when callbacks were injected
        # and executed here.  False means main.py must run them manually.
        _wmh = write_model_history if write_model_history is not None else self._write_model_history
        _pth = post_turn_hooks if post_turn_hooks is not None else self._post_turn_hooks
        _has_callbacks = bool(_wmh or _pth)
        turn.terminal_state = state
        if _has_callbacks:
            await self._run_post_terminal_pipeline(turn, state, _wmh, _pth)

        # ── Build result (after pipeline completes) ─────────────
        self._last_call_time = time.time()
        source_out, author, trigger_type = _resolve_outbound_semantics(trigger)
        context_sample = _build_context_sample(turn.messages)

        # v1.2: build backward-compat dict from VisibleMessage objects
        # Include both old key (send_status) and new key (delivery_status)
        legacy_visible = [
            {
                "text": vm.guarded_text or vm.original_text,
                "delay": vm.delay,
                "send_status": vm.delivery_status,        # legacy key
                "delivery_status": vm.delivery_status,     # v1.2 key
            }
            for vm in turn.visible_messages
        ]

        return TurnResult(
            terminal_state=state,
            turn_id=turn.turn_id,
            user_message=turn.user_message,
            visible_messages=legacy_visible if legacy_visible else turn.sent_results,
            tool_results=turn.tool_results,
            fallback_events=turn.fallback_events,
            token_usage=turn.token_usage,
            iterations=turn.llm_call_count,
            elapsed_ms=turn.elapsed_ms,
            llm_status=turn.llm_status,
            source=source_out,
            author=author,
            trigger=trigger_type,
            context_sample=context_sample,
            post_terminal_completed=_has_callbacks,
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

        if state == TurnState.RECEIVE_EVENT:
            return self._do_receive_event(turn)

        elif state == TurnState.CREATE_TURN:
            return self._do_create_turn(turn)

        elif state == TurnState.INTERRUPT_CHECK:
            return self._do_interrupt_check(turn)

        elif state == TurnState.BUILD_CONTEXT:
            return self._do_build_context(turn, system_prompt, history, trigger)

        elif state == TurnState.CLASSIFY_CONTEXT_ERROR:
            return self._do_classify_context_error(turn)

        elif state == TurnState.OPTIONAL_PREPROCESS:
            return await self._do_optional_preprocess(turn)

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

        elif state == TurnState.CLASSIFY_TOOL_ERROR:
            return self._do_classify_tool_error(turn)

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

        elif state == TurnState.SEND_RETRY_UNDELIVERED_ONLY:
            return await self._do_send_retry(turn, wechat_sender)

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
    # v1.2: Lifecycle state handlers
    # ================================================================

    def _do_receive_event(self, turn: TurnScratchpad) -> TurnState:
        """RECEIVE_EVENT: normalize turn input → CREATE_TURN."""
        turn.status = "receiving"
        return TurnState.CREATE_TURN

    def _do_create_turn(self, turn: TurnScratchpad) -> TurnState:
        """CREATE_TURN: assign turn metadata, generate interrupt_token.

        v1.2 §4.2: generates turn_id (already done in TurnScratchpad init),
        interrupt_token, sets status to running.
        Then immediately enters INTERRUPT_CHECK before any processing.
        """
        turn.interrupt_token = f"it_{turn.turn_id}_{int(time.time() * 1000)}"
        turn.status = "running"
        turn._resume_state = TurnState.BUILD_CONTEXT

        logger.info(
            "create_turn: turn_id=%s input_latest_seq=%d input_message_ids=%d "
            "input_cutoff_time=%.3f",
            turn.turn_id, turn.input_latest_seq,
            len(turn.input_message_ids), turn.input_cutoff_time,
        )
        return TurnState.INTERRUPT_CHECK

    def _do_interrupt_check(self, turn: TurnScratchpad) -> TurnState:
        """INTERRUPT_CHECK: verify current turn is still valid.

        Resume to _resume_state if valid, else END_CANCELLED.
        """
        interrupted = self._is_interrupted(turn.interrupt_token)
        logger.info(
            "interrupt_check: input_latest_seq=%d interrupted=%s",
            turn.input_latest_seq, interrupted,
        )
        if interrupted:
            turn.add_fallback_event(
                "interrupted_at_check",
                f"turn {turn.turn_id} superseded by newer event",
            )
            return TurnState.END_CANCELLED
        return turn._resume_state or TurnState.BUILD_CONTEXT

    # ================================================================
    # v1.2: Context lifecycle handlers
    # ================================================================

    def _do_classify_context_error(self, turn: TurnScratchpad) -> TurnState:
        """CLASSIFY_CONTEXT_ERROR: inspect ContextBuildResult + registry health.

        v1.2 §6: checks both ContextBuildResult.fatal_error AND global
        fatal conditions (tool registry unavailable, terminal tool schema
        missing).  RECOVERABLE warnings → continue.  FATAL → HARD_FALLBACK.
        """
        result: ContextBuildResult = getattr(turn, "_context_result", None)
        if result is None:
            turn.add_fallback_event("context_result_missing", "no ContextBuildResult found")
            return TurnState.HARD_FALLBACK

        turn.messages = result.messages
        turn.context_warnings = list(result.warnings)

        # ── Check ContextBuildResult fatal_error ──────────────────
        if result.fatal_error:
            turn.add_fallback_event("fatal_context_error", result.fatal_error)
            return TurnState.HARD_FALLBACK

        # ── v1.2 §6.4: Global fatal checks ────────────────────────
        # Tool registry must have at least one reachable terminal tool
        registry_empty = not list(TOOL_REGISTRY.iter_defs())
        if registry_empty:
            turn.add_fallback_event("fatal_context_error", "tool_registry_unavailable")
            return TurnState.HARD_FALLBACK

        # At least one terminal tool must be registered and have a schema
        terminal_names = {
            t.name for t in TOOL_REGISTRY.iter_defs()
            if t.tool_category == "terminal_tool"
        }
        if not terminal_names:
            turn.add_fallback_event("fatal_context_error", "no_terminal_tool_registered")
            return TurnState.HARD_FALLBACK

        # Validate we can build tool specs for the allowed tools
        if self.native_tool_calls and self.native_allowed_tools:
            specs = _build_native_tool_specs(self.native_allowed_tools)
            if not specs:
                turn.add_fallback_event(
                    "fatal_context_error",
                    "tool_schema_validation: no native tool specs could be built "
                    f"for allowed_tools={self.native_allowed_tools}",
                )
                return TurnState.HARD_FALLBACK

        return TurnState.OPTIONAL_PREPROCESS

    async def _do_optional_preprocess(self, turn: TurnScratchpad) -> TurnState:
        """OPTIONAL_PREPROCESS: pre-fetch deterministic facts.

        v1.2 §7: only pre-fetches current time (no weather auto-fetch).
        Merges time into the first system message — does NOT append
        a separate system message at the tail.
        Cannot generate replies, send WeChat, or write assistant history.
        """
        try:
            from datetime import datetime
            now = datetime.now().strftime("%Y年%m月%d日 %H:%M 周%u")
            time_note = f"\n\n[系统] 当前时间：{now}"
            if turn.messages and turn.messages[0].get("role") == "system":
                turn.messages[0]["content"] = turn.messages[0]["content"] + time_note
            else:
                logger.warning(
                    "optional_preprocess: messages[0] missing or not system role=%s — skipping",
                    turn.messages[0].get("role", "?") if turn.messages else "empty",
                )
        except Exception as e:
            turn.context_warnings.append({
                "source": "time_prefetch",
                "detail": f"pre_fetch_time failed: {e}",
            })

        return TurnState.CALL_LLM

    # ================================================================
    # v1.2: CLASSIFY_TOOL_ERROR handler
    # ================================================================

    def _do_classify_tool_error(self, turn: TurnScratchpad) -> TurnState:
        """CLASSIFY_TOOL_ERROR: classify tool execution results.

        v1.2 §10.3:
        - success / recoverable (timeout, general failure) → CALL_LLM
        - invalid args (JSON parse, missing required) → PROTOCOL_RETRY
        - fatal (registry corrupted, executor unavailable) → HARD_FALLBACK
        """
        for tr in turn.tool_results:
            result = str(tr.get("result", ""))
            et = _classify_tool_result(result)

            if et == "success":
                continue  # ok

            if et in ("tool_execution_error", "unknown_error"):
                # Recoverable: feed failure to model → CALL_LLM
                logger.warning("classify_tool_error: recoverable error for %s: %s",
                               tr.get("name", "?"), result[:80])
                continue

            if et in ("invalid_arguments_json", "missing_required_arg"):
                turn.add_fallback_event("tool_invalid_args",
                                        f"{tr.get('name', '?')}: {result[:200]}")
                return TurnState.PROTOCOL_RETRY

            if et in ("unknown_tool", "not_allowed", "not_eligible"):
                # Should have been caught by classifier; treat as protocol error
                turn.add_fallback_event("tool_protocol_mismatch",
                                        f"{tr.get('name', '?')}: {result[:200]}")
                return TurnState.PROTOCOL_RETRY

        return TurnState.CALL_LLM

    # ================================================================
    # v1.2: Post-terminal pipeline (inside ChatAgent)
    # ================================================================

    async def _run_post_terminal_pipeline(
        self,
        turn: TurnScratchpad,
        terminal_state: TurnState,
        write_model_history: PostTerminalCallback | None = None,
        post_turn_hooks: list[PostTerminalCallback] | None = None,
    ) -> None:
        """Execute WRITE_MODEL_HISTORY → POST_TURN_HOOKS after terminal state.

        v1.2: callbacks are injected per-request via chat() params, or
        at instance level via __init__.  Hooks must NOT modify terminal_state.
        Individual hook failures do not block other hooks.
        This runs BEFORE TurnResult is assembled.
        """
        # Step 1: Write model history
        if write_model_history:
            try:
                await write_model_history(turn, terminal_state)
            except Exception as e:
                logger.error("write_model_history callback failed: %s", e)
                turn.add_fallback_event("write_model_history_error", str(e)[:200])

        # Step 2: Post-turn hooks
        for hook in (post_turn_hooks or []):
            try:
                await hook(turn, terminal_state)
            except Exception as e:
                logger.error("post_turn_hook failed: %s", e)
                turn.add_fallback_event("hook_error", str(e)[:200])

    def _do_build_context(
        self,
        turn: TurnScratchpad,
        system_prompt: str,
        history: list[dict[str, Any]],
        trigger: str,
    ) -> TurnState:
        """BUILD_CONTEXT: assemble ContextBuildResult → CLASSIFY_CONTEXT_ERROR.

        v1.2: system_prompt empty is a CONTEXT WARNING (recorded),
        NOT a fatal error. An empty or whitespace-only system_prompt
        that was explicitly passed in is still used. The caller
        (main.py) is responsible for providing a non-empty prompt.
        Only a genuinely missing/broken prompt construction is FATAL.
        """
        warnings: list[dict[str, str]] = []
        fatal_error = ""

        # System prompt — build or validate
        prompt = system_prompt.strip() if system_prompt else ""
        if not prompt:
            prompt = self._default_system_prompt()
            warnings.append({
                "source": "system_prompt_build",
                "detail": "no system_prompt provided by caller, using default",
            })

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
        ]

        # Short-term history
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

        # Store result on scratchpad
        result = ContextBuildResult(
            messages=messages,
            warnings=warnings,
            fatal_error=fatal_error,
        )
        turn._context_result = result  # transient, consumed by CLASSIFY_CONTEXT_ERROR
        return TurnState.CLASSIFY_CONTEXT_ERROR

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

        tool_choice = "required" if request_tools else None
        # v1.2 diagnostic: log last 3 message roles (no content — privacy)
        _last_roles = [m.get("role", "?") for m in turn.messages[-3:]]

        logger.info(
            "call_llm: iter=%d model=%s tools_count=%d tool_names=%s "
            "tool_choice=%s last_roles=%s",
            turn.llm_call_count,
            self.model,
            len(request_tools) if request_tools else 0,
            ",".join(_tool_names) if request_tools else "",
            tool_choice,
            _last_roles,
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
                    tool_choice=tool_choice,
                    metadata={
                        "turn_id": turn.turn_id,
                        "iteration": turn.iteration,
                    },
                )
            )
            _merge_llm_usage(turn.token_usage, getattr(response, "usage", None))
            turn._llm_response = response
            return TurnState.HANDLE_MODEL_OUTPUT

        except Exception as e:
            logger.error("LLM call failed: %s", e)
            turn.llm_status = "error"
            turn.add_fallback_event("llm_error", str(e)[:200])
            return TurnState.HARD_FALLBACK

    def _do_handle_model_output(self, turn: TurnScratchpad) -> TurnState:
        """HANDLE_MODEL_OUTPUT: inspect what the model returned.

        Interrupt check #1: after CALL_LLM returns.
        """
        if self._is_interrupted():
            turn.add_fallback_event("interrupted_after_llm", "turn superseded by newer event")
            return TurnState.END_CANCELLED

        response = turn._llm_response
        native_tool_calls = getattr(response, "tool_calls", None) or []
        assistant_content = (response.content or "").strip()
        finish_reason = getattr(response, "finish_reason", "") or ""

        # ── v1.2 diagnostic log: HANDLE_MODEL_OUTPUT ─────────────
        logger.info(
            "handle_model_output: finish_reason=%s has_content=%s content_preview=%s "
            "raw_tool_calls=%d parsed_tool_calls=%d iter=%d",
            finish_reason,
            bool(assistant_content),
            assistant_content[:120].replace("\n", "\\n"),
            len(native_tool_calls) if native_tool_calls else 0,
            len(native_tool_calls),
            turn.iteration,
        )

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

            turn._tool_calls = native_tool_calls
            return TurnState.CLASSIFY_TOOL_CALLS

        # No tool calls — diagnose violation reason
        if assistant_content:
            if '"actions"' in assistant_content.lower():
                violation = "old_json_actions"
                turn.add_fallback_event(
                    "old_json_actions_detected",
                    "model output looks like old JSON actions format",
                )
            else:
                violation = "assistant_content_only"
            turn.add_fallback_event(
                "assistant_content_no_tool_call",
                f"model returned {len(assistant_content)} chars without tool calls",
            )
        else:
            violation = "empty_response"
            turn.add_fallback_event(
                "empty_response",
                "no tool calls and no assistant content",
            )

        logger.info(
            "handle_model_output: violation_reason=%s iter=%d",
            violation, turn.iteration,
        )
        return TurnState.PROTOCOL_RETRY

    def _do_classify_tool_calls(self, turn: TurnScratchpad) -> TurnState:
        """CLASSIFY_TOOL_CALLS: classify tools and check parallel-call legality."""
        tool_calls = turn._tool_calls
        classified = classify_tool_calls(tool_calls, TOOL_REGISTRY)
        turn._classified = classified

        if not classified.is_legal:
            violation = classified.violation_reason()
            logger.info(
                "classify_tool_calls: violation_reason=invalid_tool_combo detail=%s iter=%d",
                violation, turn.iteration,
            )
            turn.add_fallback_event("illegal_tool_combination", violation)
            return TurnState.PROTOCOL_RETRY

        if classified.has_terminal:
            return TurnState.TERMINAL_ACTION_BRANCH

        if classified.has_fact:
            return TurnState.EXEC_FACT_TOOLS

        if classified.has_state:
            return TurnState.EXEC_STATE_TOOLS

        # Shouldn't reach here if tool_calls were present, but safety
        logger.info(
            "classify_tool_calls: violation_reason=no_tool_calls "
            "total=%d iter=%d",
            classified.total, turn.iteration,
        )
        turn.add_fallback_event("no_actionable_tools", "all tools classified but none actionable")
        return TurnState.PROTOCOL_RETRY

    async def _do_exec_fact_tools(self, turn: TurnScratchpad) -> TurnState:
        """EXEC_FACT_TOOLS: execute fact tools (get_time, get_weather).

        v1.2: multiple FACT_TOOLs execute in parallel via asyncio.gather.
        Single-tool failures do not abort sibling tools.
        Results are merged in original tool_call order and appended to messages.
        After execution → INTERRUPT_CHECK → CLASSIFY_TOOL_ERROR.
        Fact tools can NOT end the turn.
        """
        import asyncio

        tool_calls = turn._tool_calls
        fact_tcs = [
            tc for tc in tool_calls
            if str(getattr(getattr(tc, "function", None), "name", "") if getattr(tc, "function", None) else "")
            in ("get_time", "get_weather")
        ]

        if not fact_tcs:
            turn.add_fallback_event("fact_tools_empty", "no fact tools to execute")
            return TurnState.CALL_LLM

        async def _run_one(tc) -> dict[str, Any]:
            fn = getattr(tc, "function", None)
            tool_name = str(getattr(fn, "name", "") if fn else "")
            tool_args_raw = str(getattr(fn, "arguments", "") if fn else "")
            call_id = getattr(tc, "id", "")
            try:
                result_text = await _execute_native_fact_tool(
                    tool_name=tool_name,
                    tool_args_raw=tool_args_raw,
                    native_allowed_tools=self.native_allowed_tools,
                )
                return {
                    "tool_name": tool_name,
                    "call_id": call_id,
                    "result": result_text[:500],
                    "success": not result_text.startswith("错误"),
                    "error_type": _classify_tool_result(result_text),
                }
            except Exception as e:
                logger.error("fact_tool %s exception: %s", tool_name, e)
                return {
                    "tool_name": tool_name,
                    "call_id": call_id,
                    "result": f"错误：工具执行异常：{e}",
                    "success": False,
                    "error_type": "tool_execution_error",
                }

        tasks = [_run_one(tc) for tc in fact_tcs]
        results = await asyncio.gather(*tasks)

        # Merge results in original order
        for r in results:
            turn.tool_results.append({"name": r["tool_name"], "result": r["result"]})
            turn.messages.append({
                "role": "tool",
                "tool_call_id": r["call_id"],
                "content": r["result"],
            })
            logger.info(
                "native_tools: fact tool %s executed, result=%s",
                r["tool_name"], r["error_type"],
            )

        turn._resume_state = TurnState.CLASSIFY_TOOL_ERROR
        return TurnState.INTERRUPT_CHECK

    async def _do_exec_state_tools(self, turn: TurnScratchpad) -> TurnState:
        """EXEC_STATE_TOOLS: execute state tools (v1.2 strict mode).

        No STATE_TOOL registrations exist yet — reaching this state means
        the classifier allowed a tool that shouldn't be here.
        Future: registered STATE_TOOLs execute → INTERRUPT_CHECK →
        CLASSIFY_TOOL_ERROR → CALL_LLM.
        """
        # Check if any STATE_TOOL is registered
        has_state_tools = any(
            t.tool_category == "state_tool" for t in TOOL_REGISTRY.iter_defs()
        )
        if not has_state_tools:
            # Should never reach here — classifier would catch unknown tools
            turn.add_fallback_event(
                "state_tools_unreachable",
                "no STATE_TOOL registered; protocol error",
            )
            return TurnState.PROTOCOL_RETRY

        # v1.2: registered STATE_TOOLs — execute (future)
        turn._resume_state = TurnState.CLASSIFY_TOOL_ERROR
        return TurnState.INTERRUPT_CHECK

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

        # Inject system reminder — minimal, tool_choice=required enforces this
        retry_note = "必须调用 send_wechat_messages 或 skip_reply 结束本轮。"
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
        """PARSE_VISIBLE_BATCH: extract messages from send_wechat_messages args.

        v1.2: creates VisibleMessage objects with visible_message_id,
        original_text, and index.  Also populates legacy turn.visible_batch
        for guard_send_message_batch compatibility.
        """
        classified = turn._classified
        tc = classified.terminal_tools[0]
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

        raw_texts = [str(m) for m in raw_messages]
        parsed_count = len(raw_texts)

        # v1.2: enforce max messages per turn — no silent truncation
        if parsed_count > MAX_MESSAGES_PER_TURN:
            turn.add_fallback_event(
                "too_many_messages",
                f"model requested {parsed_count} messages, limit={MAX_MESSAGES_PER_TURN}",
            )
            # Inject retry note so model re-calls send_wechat_messages compressed
            turn.messages.append({
                "role": "system",
                "content": (
                    f"你一次性请求发送了 {parsed_count} 条消息，超过了上限 {MAX_MESSAGES_PER_TURN} 条。"
                    f"请压缩到 {MAX_MESSAGES_PER_TURN} 条以内，重新调用 send_wechat_messages。"
                    '如果内容太多，可以分段讲，或者最后说"我先讲到这里，你想听我继续再说"。'
                ),
            })
            return TurnState.PROTOCOL_RETRY

        # v1.2: create VisibleMessage for each parsed message
        turn.visible_messages = [
            VisibleMessage(
                visible_message_id=f"vm_{turn.turn_id}_{i}",
                index=i,
                original_text=text,
            )
            for i, text in enumerate(raw_texts)
        ]

        turn.visible_batch = raw_texts
        turn._send_mode = str(args.get("mode", "normal") or "normal")
        turn._parsed_count = parsed_count  # transient diagnostic

        logger.info(
            "state_machine: parsed %d visible messages, mode=%s",
            parsed_count, turn._send_mode,
        )
        return TurnState.GUARD_OUTPUT

    def _do_guard_output(self, turn: TurnScratchpad) -> TurnState:
        """GUARD_OUTPUT: run output guard on every parsed VisibleMessage.

        v1.2: each VisibleMessage gets guarded_text/guard_status via
        guard_single_message.  Messages blocked by guard get
        delivery_status=BLOCKED_BY_GUARD.  If all blocked → HARD_FALLBACK.
        No silent truncation — parsed_count already enforced at PARSE_VISIBLE_BATCH.
        Interrupt check before guard.
        """
        if self._is_interrupted():
            turn.add_fallback_event("interrupted_before_guard", "turn superseded by newer event")
            return TurnState.END_CANCELLED

        from agent.output_guard import guard_single_message

        # ── Per-message guard ──────────────────────────────────────
        guard_blocked = 0
        for vm in turn.visible_messages:
            guarded = guard_single_message(vm.original_text)
            if guarded:
                vm.guarded_text = guarded
                vm.guard_status = "PASSED" if guarded == vm.original_text else "SANITIZED"
            else:
                vm.guarded_text = ""
                vm.guard_status = "BLOCKED"
                vm.delivery_status = DeliveryStatus.BLOCKED_BY_GUARD
                guard_blocked += 1

        if guard_blocked > 0:
            turn.add_fallback_event(
                "guard_blocked",
                f"blocked {guard_blocked} messages by guard",
            )

        passed = [vm for vm in turn.visible_messages if vm.guard_status != "BLOCKED"]

        if not passed:
            turn.add_fallback_event(
                "guard_all_blocked",
                "all messages blocked by output guard",
            )
            return TurnState.HARD_FALLBACK

        # ── Build legacy guarded_batch for wechat_sender callback ──
        turn.guarded_batch = [
            {"text": vm.guarded_text, "delay": 0.0, "_vm_index": vm.index}
            for vm in passed
        ]
        return TurnState.EXEC_SEND_WECHAT

    async def _do_exec_send_wechat(
        self, turn: TurnScratchpad, wechat_sender: WeChatSender | None,
    ) -> TurnState:
        """EXEC_SEND_WECHAT: send via wechat_sender, map to five-state delivery.

        v1.2: maps old send_status → DeliveryStatus on each VisibleMessage.
        Only FAILED_BEFORE_SEND and FAILED_CONFIRMED trigger retry.
        SENT_CONFIRMED, UNKNOWN_AFTER_ATTEMPT, BLOCKED_BY_GUARD do NOT retry.
        Interrupt check before sending.
        """
        if self._is_interrupted():
            turn.add_fallback_event("interrupted_before_send", "turn superseded by newer event")
            return TurnState.END_CANCELLED

        from agent.tools import compute_message_delay

        turn.tool_results.append({
            "name": "send_wechat_messages",
            "result": f"prepared {len(turn.guarded_batch)} messages",
        })

        if not wechat_sender:
            for vm in turn.visible_messages:
                if vm.guard_status != "BLOCKED":
                    vm.delivery_status = DeliveryStatus.PENDING
            turn.sent_results = list(turn.guarded_batch)
            turn.add_fallback_event("send_no_sender", "no wechat_sender callback")
            return TurnState.DECIDE_SEND_TERMINAL

        # Compute delays
        total = len(turn.guarded_batch)
        for i, m in enumerate(turn.guarded_batch):
            m["delay"] = compute_message_delay(
                i, m["text"], total, test_mode=self.test_mode,
            )

        # Send via callback
        try:
            raw_results = await wechat_sender(turn.guarded_batch)
            logger.info(
                "state_machine: sent %d messages via wechat_sender",
                len(raw_results),
            )
        except Exception as e:
            logger.error("state_machine: wechat_sender failed: %s", e)
            turn.add_fallback_event("send_error", str(e)[:200])
            # All attempted messages → UNKNOWN_AFTER_ATTEMPT (don't know if sent)
            for m in turn.guarded_batch:
                vm_idx = m.get("_vm_index")
                if vm_idx is not None and vm_idx < len(turn.visible_messages):
                    vm = turn.visible_messages[vm_idx]
                    if vm.delivery_status != DeliveryStatus.BLOCKED_BY_GUARD:
                        vm.delivery_status = DeliveryStatus.UNKNOWN_AFTER_ATTEMPT
                        vm.error_code = str(e)[:200]
            turn.sent_results = list(turn.guarded_batch)
            return TurnState.DECIDE_SEND_TERMINAL

        # ── Map results back to VisibleMessage (v1.2 five-state) ──
        turn.sent_results = raw_results
        for r in raw_results:
            vm_idx = r.get("_vm_index")
            if vm_idx is None or vm_idx >= len(turn.visible_messages):
                continue
            vm = turn.visible_messages[vm_idx]
            if vm.delivery_status == DeliveryStatus.BLOCKED_BY_GUARD:
                continue  # already blocked
            old_status = r.get("send_status", "")
            if old_status == "delivered":
                vm.delivery_status = DeliveryStatus.SENT_CONFIRMED
                vm.attempt_count += 1
            elif old_status == "failed":
                error = r.get("send_error", "")
                if "no_bridge" in str(error).lower():
                    vm.delivery_status = DeliveryStatus.FAILED_BEFORE_SEND
                else:
                    vm.delivery_status = DeliveryStatus.UNKNOWN_AFTER_ATTEMPT
                vm.error_code = str(error)[:200]
            elif old_status == "blocked":
                vm.delivery_status = DeliveryStatus.FAILED_BEFORE_SEND
                vm.error_code = r.get("send_error", "empty text")
            elif old_status == "generated":
                vm.delivery_status = DeliveryStatus.PENDING
            else:
                vm.delivery_status = DeliveryStatus.UNKNOWN_AFTER_ATTEMPT
            vm.attempt_count += 1

        # ── Retry decision (v1.2: only specific statuses) ──
        retryable = [
            vm for vm in turn.visible_messages
            if vm.delivery_status in (
                DeliveryStatus.FAILED_BEFORE_SEND,
                DeliveryStatus.FAILED_CONFIRMED,
            )
        ]
        if retryable and turn.send_attempt < turn.max_send_retries:
            return TurnState.SEND_RETRY_UNDELIVERED_ONLY

        return TurnState.DECIDE_SEND_TERMINAL

    async def _do_send_retry(
        self, turn: TurnScratchpad, wechat_sender: WeChatSender | None,
    ) -> TurnState:
        """SEND_RETRY_UNDELIVERED_ONLY: retry only retry-safe messages.

        v1.2 §13.4: only retries FAILED_BEFORE_SEND and FAILED_CONFIRMED.
        Never retries SENT_CONFIRMED, UNKNOWN_AFTER_ATTEMPT, BLOCKED_BY_GUARD.
        Does NOT re-invoke the model.
        """
        if not wechat_sender:
            turn.add_fallback_event("send_retry_no_sender", "cannot retry without sender")
            return TurnState.DECIDE_SEND_TERMINAL

        # v1.2: only collect retryable messages
        retryable_vms = [
            vm for vm in turn.visible_messages
            if vm.delivery_status in (
                DeliveryStatus.FAILED_BEFORE_SEND,
                DeliveryStatus.FAILED_CONFIRMED,
            )
        ]
        if not retryable_vms:
            return TurnState.DECIDE_SEND_TERMINAL

        turn.send_attempt += 1
        turn.add_fallback_event(
            "send_retry_attempt",
            f"retrying {len(retryable_vms)} messages (attempt {turn.send_attempt})",
        )
        logger.info(
            "state_machine: send_retry attempt=%d retryable=%d",
            turn.send_attempt, len(retryable_vms),
        )

        # Build retry batch
        retry_batch: list[dict[str, Any]] = [
            {"text": vm.guarded_text or vm.original_text,
             "delay": vm.delay,
             "_vm_index": vm.index}
            for vm in retryable_vms
        ]

        try:
            retry_results = await wechat_sender(retry_batch)
        except Exception as e:
            logger.error("state_machine: send_retry failed: %s", e)
            turn.add_fallback_event("send_retry_error", str(e)[:200])
            for vm in retryable_vms:
                if vm.delivery_status in (DeliveryStatus.FAILED_BEFORE_SEND, DeliveryStatus.FAILED_CONFIRMED):
                    vm.delivery_status = DeliveryStatus.UNKNOWN_AFTER_ATTEMPT
                    vm.error_code = str(e)[:200]
            return TurnState.DECIDE_SEND_TERMINAL

        # Map retry results back to VisibleMessage
        for rr in retry_results:
            vm_idx = rr.get("_vm_index")
            if vm_idx is None or vm_idx >= len(turn.visible_messages):
                continue
            vm = turn.visible_messages[vm_idx]
            old_status = rr.get("send_status", "")
            if old_status == "delivered":
                vm.delivery_status = DeliveryStatus.SENT_CONFIRMED
            elif old_status in ("failed", "blocked"):
                vm.delivery_status = DeliveryStatus.UNKNOWN_AFTER_ATTEMPT
                vm.error_code = rr.get("send_error", "")[:200]
            vm.attempt_count += 1

        # Also update legacy sent_results for diagnostics
        for rr in retry_results:
            vm_idx = rr.get("_vm_index")
            if vm_idx is not None and vm_idx < len(turn.sent_results):
                turn.sent_results[vm_idx] = rr

        return TurnState.DECIDE_SEND_TERMINAL

    def _do_decide_send_terminal(self, turn: TurnScratchpad) -> TurnState:
        """DECIDE_SEND_TERMINAL: determine END_SENT / END_PARTIAL_SENT / END_FAILED.

        v1.2 §13.5: compares parsed_count, guard_passed_count, attempted_count,
        delivered_count.  No silent message loss — every gap has a reason.
        """
        parsed_count = len(turn.visible_messages)
        guard_blocked = sum(
            1 for vm in turn.visible_messages
            if vm.delivery_status == DeliveryStatus.BLOCKED_BY_GUARD
        )
        guard_passed = parsed_count - guard_blocked
        sent_confirmed = sum(
            1 for vm in turn.visible_messages
            if vm.delivery_status == DeliveryStatus.SENT_CONFIRMED
        )
        failed_before = sum(
            1 for vm in turn.visible_messages
            if vm.delivery_status == DeliveryStatus.FAILED_BEFORE_SEND
        )
        unknown = sum(
            1 for vm in turn.visible_messages
            if vm.delivery_status == DeliveryStatus.UNKNOWN_AFTER_ATTEMPT
        )
        pending = sum(
            1 for vm in turn.visible_messages
            if vm.delivery_status in (DeliveryStatus.PENDING, "")
        )
        attempted = sent_confirmed + failed_before + unknown

        terminal = TurnState.END_FAILED
        if parsed_count == 0:
            terminal = TurnState.END_FAILED
        elif sent_confirmed == parsed_count:
            terminal = TurnState.END_SENT
        elif sent_confirmed > 0:
            terminal = TurnState.END_PARTIAL_SENT
        elif attempted > 0:
            terminal = TurnState.END_FAILED
        elif guard_blocked == parsed_count:
            terminal = TurnState.END_FAILED
            turn.add_fallback_event("send_all_guard_blocked", f"{parsed_count} blocked by guard")
        else:
            terminal = TurnState.END_FAILED

        logger.info(
            "decide_send_terminal: parsed=%d guard_passed=%d guard_blocked=%d "
            "attempted=%d delivered=%d unknown=%d pending=%d -> %s",
            parsed_count, guard_passed, guard_blocked,
            attempted, sent_confirmed, unknown, pending,
            terminal,
        )

        if terminal == TurnState.END_PARTIAL_SENT:
            turn.add_fallback_event(
                "partial_send",
                f"{sent_confirmed}/{parsed_count} sent_confirmed "
                f"(guard_blocked={guard_blocked} unknown={unknown})",
            )

        return terminal

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

        v1.2: INTERRUPT_CHECK before sending.  Uses VisibleMessage for
        fallback tracking.  Does NOT use model content.
        """
        from agent.output_guard import HARD_FALLBACK_TEXT, guard_single_message

        # ── v1.2 diagnostic: why we reached HARD_FALLBACK ─────────
        fallback_reason = "unknown"
        last_violation = ""
        for ev in reversed(turn.fallback_events):
            t = ev.get("type", "")
            if t in ("assistant_content_no_tool_call", "empty_response",
                     "old_json_actions_detected", "illegal_tool_combination",
                     "protocol_retry_exceeded", "llm_error",
                     "fatal_context_error", "fatal_tool_error",
                     "guard_all_blocked", "max_iterations_exceeded"):
                fallback_reason = t
                last_violation = ev.get("detail", "")[:200]
                break

        logger.info(
            "hard_fallback: reason=%s last_violation=%s protocol_retry_count=%d iter=%d",
            fallback_reason, last_violation,
            turn.protocol_retry_count, turn.iteration,
        )

        turn.add_fallback_event("hard_fallback_enter", "entering hard fallback")

        # v1.2: interrupt check before sending fallback
        if self._is_interrupted():
            turn.add_fallback_event("interrupted_before_fallback", "turn superseded by newer event")
            return TurnState.END_CANCELLED

        # Guard the hard fallback text
        guarded = guard_single_message(HARD_FALLBACK_TEXT)
        if not guarded:
            turn.add_fallback_event(
                "hard_fallback_guard_failed",
                "hard fallback text blocked by guard",
            )
            return TurnState.END_FAILED

        # v1.2: create VisibleMessage for fallback
        fallback_vm = VisibleMessage(
            visible_message_id=f"vm_fallback_{turn.turn_id}",
            index=0,
            original_text=HARD_FALLBACK_TEXT,
            guarded_text=guarded,
            guard_status="PASSED" if guarded == HARD_FALLBACK_TEXT else "SANITIZED",
            delivery_status=DeliveryStatus.PENDING,
        )
        turn.visible_messages = [fallback_vm]

        if wechat_sender:
            try:
                fallback_batch = [{"text": guarded, "delay": 0.3, "_vm_index": 0}]
                result = await wechat_sender(fallback_batch)
                turn.sent_results = result
                old_status = (result[0].get("send_status", "") if result else "")
                if old_status == "delivered":
                    fallback_vm.delivery_status = DeliveryStatus.SENT_CONFIRMED
                    turn.add_fallback_event("hard_fallback_sent", "fallback delivered")
                    return TurnState.END_FALLBACK_SENT
                else:
                    fallback_vm.delivery_status = DeliveryStatus.UNKNOWN_AFTER_ATTEMPT
                    turn.add_fallback_event("hard_fallback_send_failed", "fallback not delivered")
                    return TurnState.END_FAILED
            except Exception as e:
                logger.error("hard_fallback: send failed: %s", e)
                fallback_vm.delivery_status = DeliveryStatus.UNKNOWN_AFTER_ATTEMPT
                fallback_vm.error_code = str(e)[:200]
                turn.add_fallback_event("hard_fallback_send_error", str(e)[:200])
                return TurnState.END_FAILED
        else:
            # v1.2: no sender → cannot deliver → END_FAILED (not END_FALLBACK_SENT)
            fallback_vm.delivery_status = DeliveryStatus.FAILED_BEFORE_SEND
            turn.sent_results = [{"text": guarded, "delay": 0.0, "send_status": "failed"}]
            turn.add_fallback_event("hard_fallback_no_sender", "no wechat_sender, cannot send")
            return TurnState.END_FAILED

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
    # v1.2: quick role summary for diagnostics
    _roles = [m.get("role", "?") for m in messages]
    role_summary = ",".join(_roles[-6:]) if len(_roles) > 6 else ",".join(_roles)
    logger.debug("LLM_PAYLOAD: msg_count=%d roles_head=%s roles_tail=%s",
                 len(messages), ",".join(_roles[:4]), ",".join(_roles[-4:]))

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
