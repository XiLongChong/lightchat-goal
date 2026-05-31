"""ChatAgent — v1.5 strict CC causal chain state machine.

One tool call per LLM response.  wechat_reply = one bubble (NOT terminal).
done = only normal terminal tool.  Tool results always fed back to model.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import random
import re as _re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine

from agent.state_machine import (
    MAX_LLM_STEPS_PER_TURN,
    MAX_PROTOCOL_RETRIES_PER_TURN,
    MAX_REPLY_MESSAGES_PER_TURN,
    MAX_SEND_FAILURES_PER_TURN,
    POST_SEND_DELAY_LONG_NARRATIVE,
    POST_SEND_DELAY_NARRATIVE_THRESHOLD_CHARS,
    POST_SEND_DELAY_POLL_INTERVAL_MS,
    POST_SEND_DELAY_TIERS,
    TERMINAL_STATES,
    ContextBuildResult,
    DeliveryStatus,
    InterruptChecker,
    ModelAttempt,
    ModelProfile,
    PostTerminalCallback,
    TurnResult,
    TurnScratchpad,
    TurnState,
    VisibleMessage,
)
from agent.tool_classifier import classify_single_tool_call
from agent.tool_registry import TOOL_REGISTRY

logger = logging.getLogger(__name__)

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
        # ── v1.3: model routing ──────────────────────────────────
        flash_profile: ModelProfile | None = None,
        pro_profile: ModelProfile | None = None,
        routing_mode: str = "auto",
        manual_flash_allow_upgrade: bool = False,
        max_flash_upgrade_attempts: int = 1,
        # ── v1.2: post-terminal callbacks injected by main.py ─────
        write_model_history: PostTerminalCallback | None = None,
        post_turn_hooks: list[PostTerminalCallback] | None = None,
    ):
        self.llm_gateway = llm_gateway
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

        # ── v1.3: model routing ──────────────────────────────────
        self._flash_profile = flash_profile or ModelProfile()
        self._pro_profile = pro_profile or ModelProfile(model_name="deepseek-v4-pro", max_tokens=1200, timeout_seconds=40)
        self._routing_mode = routing_mode
        self._manual_flash_allow_upgrade = manual_flash_allow_upgrade
        self._max_flash_upgrade_attempts = max_flash_upgrade_attempts

        # ── v1.2: post-terminal pipeline callbacks ───────────────
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
            route=route,
            max_flash_upgrade_attempts=self._max_flash_upgrade_attempts,
            max_llm_steps_per_turn=MAX_LLM_STEPS_PER_TURN,
            max_reply_messages_per_turn=MAX_REPLY_MESSAGES_PER_TURN,
            max_protocol_retries=MAX_PROTOCOL_RETRIES_PER_TURN,
            max_send_failures_per_turn=MAX_SEND_FAILURES_PER_TURN,
        )
        state = TurnState.RECEIVE_EVENT

        # ── State machine loop ──────────────────────────────────
        while state not in TERMINAL_STATES:
            if turn.llm_call_count >= turn.max_llm_steps_per_turn:
                logger.warning(
                    "state_machine: max_llm_steps=%d exceeded, forcing HARD_FALLBACK",
                    turn.max_llm_steps_per_turn,
                )
                turn.add_fallback_event(
                    "max_llm_steps_exceeded",
                    f"reached {turn.llm_call_count} LLM calls",
                )
                state = TurnState.HARD_FALLBACK

            effective_elapsed_s = (turn.elapsed_ms - turn._thinking_delay_accumulated_ms) / 1000
            if effective_elapsed_s > turn.turn_timeout_seconds:
                logger.warning(
                    "turn timeout: effective_elapsed=%.1fs (raw=%.1fs delay=%.1fs) turn_id=%s",
                    effective_elapsed_s, turn.elapsed_ms / 1000,
                    turn._thinking_delay_accumulated_ms / 1000, turn.turn_id,
                )
                turn.reply_sequence_status = "max_limit_reached"
                state = TurnState.END_MAX_LIMIT
                break

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
            reply_sequence_status=turn.reply_sequence_status,
            sent_message_count=turn.sent_message_count,
            done_status=turn.done_status,
            done_note=turn.done_note,
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

        elif state == TurnState.CLASSIFY_ONE_TOOL_CALL:
            return self._do_classify_one_tool_call(turn)

        elif state == TurnState.EXEC_FACT_TOOLS:
            return await self._do_exec_fact_tools(turn)

        elif state == TurnState.VALIDATE_REPLY_ARGS:
            return self._do_validate_reply_args(turn)

        elif state == TurnState.GUARD_OUTPUT:
            return self._do_guard_output(turn)

        elif state == TurnState.PRE_SEND_INTERRUPT_CHECK:
            return self._do_pre_send_interrupt_check(turn)

        elif state == TurnState.EXEC_SEND_ONE:
            return await self._do_exec_send_one(turn, wechat_sender)

        elif state == TurnState.APPEND_SEND_RESULT:
            return self._do_append_send_result(turn)

        elif state == TurnState.POST_SEND_THINKING_DELAY:
            return await self._do_post_send_thinking_delay(turn)

        elif state == TurnState.POST_SEND_INTERRUPT_CHECK:
            return self._do_post_send_interrupt_check(turn)

        elif state == TurnState.LIMIT_CHECK:
            return self._do_limit_check(turn)

        elif state == TurnState.VALIDATE_DONE_ARGS:
            return self._do_validate_done_args(turn)

        elif state == TurnState.PROTOCOL_RETRY:
            return self._do_protocol_retry(turn)

        elif state == TurnState.FLASH_TO_PRO_UPGRADE:
            return self._do_flash_to_pro_upgrade(turn)

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

        # At least one native-eligible terminal tool (done) must be registered
        terminal_names = {
            t.name for t in TOOL_REGISTRY.iter_defs()
            if t.tool_category == "terminal_tool" and t.native_tool_call_eligible
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

        # ── Temporal context: time + gap + resume_type + time dividers ──
        from agent.temporal_context import TemporalContextBuilder
        _tz = timezone(timedelta(hours=8))
        now_dt = datetime.now(tz=_tz)
        current_user_ts = turn.input_cutoff_time if turn.input_cutoff_time > 0 else now_dt.timestamp()

        temporal_builder = TemporalContextBuilder(timezone_offset_hours=8)
        temporal_result = temporal_builder.build(
            now=now_dt,
            history=history,
            current_user_ts=current_user_ts,
        )
        messages.append({"role": "system", "content": temporal_result.temporal_message})

        # Store temporal diagnostics on turn for post-turn observability
        turn.add_fallback_event("temporal_context", str(temporal_result.diagnostics))

        # City as separate system message (cache-stable)
        default_city = str(self.defaults.get("default_city", "") or "").strip()
        if default_city:
            messages.append({
                "role": "system",
                "content": f"用户默认天气城市：{default_city}。用户只问天气时，直接查这个城市，不要反问。",
            })

        # Short-term history (with temporal dividers from augmented history)
        recent = temporal_result.augmented_history[-24:] if len(temporal_result.augmented_history) > 24 else temporal_result.augmented_history
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

    # ================================================================
    # v1.3: Model router
    # ================================================================

    def _select_model(self, turn: TurnScratchpad) -> tuple[str, str, "ModelProfile"]:
        """Return (model_label, routing_reason, profile).

        Priority: _force_model_label > manual override > auto rules.
        """
        # ── 0. 强制选择（FLASH_TO_PRO_UPGRADE 设置）──────────────
        if turn._force_model_label:
            label = turn._force_model_label
            turn._force_model_label = ""
            reason = "flash_upgraded_to_pro" if label == "pro" else f"force_{label}"
            profile = self._pro_profile if label == "pro" else self._flash_profile
            return (label, reason, profile)

        # ── 1. 手动模式 ──────────────────────────────────────────
        if self._routing_mode == "manual_flash":
            return ("flash", "manual_flash", self._flash_profile)
        if self._routing_mode == "manual_pro":
            return ("pro", "manual_pro", self._pro_profile)

        # ── 2. auto 模式 ─────────────────────────────────────────
        last = turn.model_attempts[-1] if turn.model_attempts else None
        if last and last.selected_model == "flash" and last.result_type in (
            "protocol_violation", "llm_error",
        ):
            return ("pro", "flash_failed", self._pro_profile)

        if turn.route == "ChatFull":
            return ("pro", "complex_chat", self._pro_profile)

        return ("flash", "daily_chat", self._flash_profile)

    async def _do_call_llm(self, turn: TurnScratchpad) -> TurnState:
        """CALL_LLM: invoke the LLM via gateway."""
        from agent.llm_types import LLMRequest

        turn.llm_call_count += 1

        # ── v1.3: Model routing ──────────────────────────────────
        model_label, routing_reason, profile = self._select_model(turn)
        is_upgrade = bool(turn._force_model_upgraded_from)

        attempt = ModelAttempt(
            attempt_index=len(turn.model_attempts) + 1,
            selected_model=model_label,
            routing_mode=self._routing_mode,
            routing_reason=routing_reason,
            model_name=profile.model_name,
            started_at=time.time(),
        )
        if is_upgrade:
            attempt.upgraded_from = turn._force_model_upgraded_from
            turn._force_model_upgraded_from = ""
        turn._current_attempt = attempt

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
        _last_roles = [m.get("role", "?") for m in turn.messages[-3:]]

        # v1.3: upgrade attempts → no gateway-level retry (state machine controls it)
        _retry = 0 if is_upgrade else None  # None → gateway uses its default

        logger.info(
            "call_llm: iter=%d model=%s label=%s reason=%s "
            "tools_count=%d tool_names=%s tool_choice=%s last_roles=%s "
            "upgraded=%s retry=%s",
            turn.llm_call_count,
            profile.model_name,
            model_label,
            routing_reason,
            len(request_tools) if request_tools else 0,
            ",".join(_tool_names) if request_tools else "",
            tool_choice,
            _last_roles,
            attempt.upgraded_from or "none",
            _retry if _retry is not None else "default",
        )

        try:
            response = await self.llm_gateway.complete(
                LLMRequest(
                    purpose="chat_turn",
                    model=profile.model_name,
                    messages=turn.messages,
                    temperature=profile.temperature,
                    max_tokens=profile.max_tokens,
                    timeout_s=profile.timeout_seconds,
                    response_mode="text",
                    thinking="disabled",
                    tools=request_tools,
                    tool_choice=tool_choice,
                    retry=_retry,
                    metadata={
                        "turn_id": turn.turn_id,
                        "iteration": turn.iteration,
                        "model_label": model_label,
                    },
                )
            )
            attempt.finished_at = time.time()
            attempt.latency_ms = int((attempt.finished_at - attempt.started_at) * 1000)
            attempt.result_type = "success"
            _merge_llm_usage(turn.token_usage, getattr(response, "usage", None))
            turn._llm_response = response
            turn.model_attempts.append(attempt)
            return TurnState.HANDLE_MODEL_OUTPUT

        except Exception as e:
            attempt.finished_at = time.time()
            attempt.latency_ms = int((attempt.finished_at - attempt.started_at) * 1000)
            attempt.result_type = "llm_error"
            attempt.error_type = type(e).__name__
            turn.model_attempts.append(attempt)
            logger.error("LLM call failed: %s model=%s", e, profile.model_name)
            turn.llm_status = "error"
            turn.add_fallback_event("llm_error", str(e)[:200])

            # v1.3: flash error → try upgrade; pro error → HARD_FALLBACK
            if model_label == "flash" and (
                self._routing_mode == "auto"
                or (self._routing_mode == "manual_flash" and self._manual_flash_allow_upgrade)
            ):
                if turn.flash_upgrade_count < turn.max_flash_upgrade_attempts:
                    return TurnState.FLASH_TO_PRO_UPGRADE
            return TurnState.HARD_FALLBACK

    def _do_handle_model_output(self, turn: TurnScratchpad) -> TurnState:
        """HANDLE_MODEL_OUTPUT: inspect what the model returned.

        v1.5: exactly 1 tool call required.
        Interrupt after CALL_LLM: differentiates END_CANCELLED /
        END_SUPERSEDED_BEFORE_SEND / END_INTERRUPTED_DURING_REPLY_SEQUENCE.
        """
        if self._is_interrupted():
            turn.add_fallback_event("interrupted_after_llm", "turn superseded by newer event")
            if turn._has_pending_wechat_reply:
                return TurnState.END_SUPERSEDED_BEFORE_SEND
            if turn.sent_message_count > 0:
                turn.reply_sequence_status = "interrupted_by_user"
                return TurnState.END_INTERRUPTED_DURING_REPLY_SEQUENCE
            return TurnState.END_CANCELLED

        response = turn._llm_response
        native_tool_calls = getattr(response, "tool_calls", None) or []
        assistant_content = (response.content or "").strip()
        finish_reason = getattr(response, "finish_reason", "") or ""

        logger.info(
            "handle_model_output: finish_reason=%s has_content=%s content_preview=%s "
            "raw_tool_calls=%d iter=%d sent_count=%d",
            finish_reason,
            bool(assistant_content),
            assistant_content[:120].replace("\n", "\\n"),
            len(native_tool_calls) if native_tool_calls else 0,
            turn.iteration,
            turn.sent_message_count,
        )

        # ── v1.5: exactly 1 tool call required ───────────────────
        if not native_tool_calls:
            if assistant_content:
                if '"actions"' in assistant_content.lower():
                    violation = "old_json_actions"
                    turn.add_fallback_event("old_json_actions_detected",
                        "model output looks like old JSON actions format")
                else:
                    violation = "assistant_content_only"
                turn.add_fallback_event("assistant_content_no_tool_call",
                    f"model returned {len(assistant_content)} chars without tool calls")
            else:
                violation = "empty_response"
                turn.add_fallback_event("empty_response",
                    "no tool calls and no assistant content")
            if turn._current_attempt:
                turn._current_attempt.result_type = "protocol_violation"
                turn._current_attempt.error_type = violation
            logger.info("handle_model_output: violation=%s iter=%d", violation, turn.iteration)
            return TurnState.PROTOCOL_RETRY

        if len(native_tool_calls) > 1:
            tc_names = []
            for tc in native_tool_calls:
                fn = getattr(tc, "function", None)
                tc_names.append(str(getattr(fn, "name", "") if fn else ""))
            turn.add_fallback_event("multiple_tool_calls",
                f"expected exactly 1 tool call, got {len(native_tool_calls)}: {','.join(tc_names)}")
            logger.info("handle_model_output: violation=multiple_tool_calls count=%d iter=%d",
                       len(native_tool_calls), turn.iteration)
            if turn._current_attempt:
                turn._current_attempt.result_type = "protocol_violation"
                turn._current_attempt.error_type = "multiple_tool_calls"
            return TurnState.PROTOCOL_RETRY

        # ── Exactly 1 tool call — serialize and append ───────────
        tc = native_tool_calls[0]
        fn = getattr(tc, "function", None)
        tool_name = str(getattr(fn, "name", "") if fn else "")
        tool_args = str(getattr(fn, "arguments", "") if fn else "")
        tool_call_id = str(getattr(tc, "id", ""))

        serialized = {
            "id": tool_call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": tool_args},
        }

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": assistant_content or "",
            "tool_calls": [serialized],
        }
        turn.messages.append(assistant_msg)

        turn._tool_calls = [tc]
        turn._current_tool_call_id = tool_call_id

        logger.info(
            "native_tools: 1 tool_call iter=%d name=%s call_id=%s",
            turn.iteration, tool_name, tool_call_id,
        )

        # ── Flag pending wechat_reply for interrupt differentiation ──
        if tool_name == "wechat_reply":
            turn._has_pending_wechat_reply = True

        return TurnState.CLASSIFY_ONE_TOOL_CALL

    def _do_classify_one_tool_call(self, turn: TurnScratchpad) -> TurnState:
        """CLASSIFY_ONE_TOOL_CALL: classify single tool call, route by tool_name.

        v1.5: wechat_reply MUST route by name (NOT by tool_category).
        It is registered as fact_tool but must never enter EXEC_FACT_TOOLS.
        """
        tool_calls = turn._tool_calls
        if not tool_calls:
            turn.add_fallback_event("no_tool_calls", "empty tool_calls in CLASSIFY_ONE_TOOL_CALL")
            return TurnState.PROTOCOL_RETRY

        tc = tool_calls[0]
        classified = classify_single_tool_call(tc, TOOL_REGISTRY)
        turn._classified_single = classified

        if classified.is_deprecated:
            logger.info("classify_one_tool_call: deprecated=%s iter=%d", classified.tool_name, turn.iteration)
            turn.add_fallback_event("deprecated_tool", classified.tool_name)
            turn.messages.append({"role": "system", "content": classified.deprecated_hint})
            return TurnState.PROTOCOL_RETRY

        if not classified.is_legal:
            logger.info("classify_one_tool_call: illegal=%s reason=%s iter=%d",
                       classified.tool_name, classified.violation_reason, turn.iteration)
            turn.add_fallback_event("illegal_tool", classified.violation_reason)
            return TurnState.PROTOCOL_RETRY

        name = classified.tool_name

        # ── Name-based routing (NOT category-based) ─────────────
        # wechat_reply is registered as fact_tool but MUST NOT enter EXEC_FACT_TOOLS.
        if name in ("get_time", "get_weather"):
            return TurnState.EXEC_FACT_TOOLS
        elif name == "wechat_reply":
            # Check if already at reply limit
            if turn.sent_message_count >= turn.max_reply_messages_per_turn:
                logger.info("classify_one_tool_call: wechat_reply after limit sent_count=%d", turn.sent_message_count)
                turn.add_fallback_event("wechat_reply_after_limit",
                    f"sent_count={turn.sent_message_count} >= max={turn.max_reply_messages_per_turn}")
                return TurnState.END_MAX_LIMIT
            return TurnState.VALIDATE_REPLY_ARGS
        elif name == "done":
            return TurnState.VALIDATE_DONE_ARGS

        turn.add_fallback_event("unroutable_tool", f"tool '{name}' has no route")
        return TurnState.PROTOCOL_RETRY

    async def _do_exec_fact_tools(self, turn: TurnScratchpad) -> TurnState:
        """EXEC_FACT_TOOLS: execute fact tools (get_time, get_weather).

        v1.5: only get_time and get_weather enter here (wechat_reply is name-routed
        to VALIDATE_REPLY_ARGS). Results feed back to model → CALL_LLM.
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

        turn._resume_state = TurnState.CALL_LLM
        return TurnState.INTERRUPT_CHECK

    def _do_flash_to_pro_upgrade(self, turn: TurnScratchpad) -> TurnState:
        """FLASH_TO_PRO_UPGRADE: upgrade flash → pro.

        v1.3: sets _force_model_label and _force_model_upgraded_from
        so the next CALL_LLM uses pro with retry=0.
        Budget check against max_flash_upgrade_attempts.
        """
        if turn.flash_upgrade_count >= turn.max_flash_upgrade_attempts:
            turn.add_fallback_event(
                "flash_upgrade_exceeded",
                f"exceeded max flash upgrades ({turn.max_flash_upgrade_attempts})",
            )
            return TurnState.HARD_FALLBACK

        turn.flash_upgrade_count += 1
        turn._force_model_label = "pro"
        turn._force_model_upgraded_from = "flash"

        logger.info(
            "flash_to_pro_upgrade: count=%d/%d upgrading to pro",
            turn.flash_upgrade_count, turn.max_flash_upgrade_attempts,
        )
        return TurnState.CALL_LLM

    # ================================================================
    # v1.5: wechat_reply pipeline — single bubble send
    # ================================================================

    def _do_validate_reply_args(self, turn: TurnScratchpad) -> TurnState:
        """VALIDATE_REPLY_ARGS: validate wechat_reply(text).

        v1.5: text must be string, non-empty, ≤500 chars, no newlines.
        Returns recoverable error tool result on failure → CALL_LLM.
        """
        from agent.output_guard import validate_reply_text

        classified = turn._classified_single
        serialized = classified.serialized or {}
        fn = serialized.get("function", {})
        args_raw = fn.get("arguments", "")

        try:
            args = _json.loads(args_raw) if args_raw.strip() else {}
        except _json.JSONDecodeError as e:
            result = {"status": "error", "recoverable": True,
                      "reason": "invalid_json", "hint": f"参数 JSON 解析失败：{e}"}
            turn.messages.append({
                "role": "tool", "tool_call_id": turn._current_tool_call_id,
                "content": _json.dumps(result, ensure_ascii=False),
            })
            logger.info("validate_reply_args: invalid_json")
            return TurnState.CALL_LLM

        text = str(args.get("text", "")) if isinstance(args, dict) else ""

        is_valid, error_reason = validate_reply_text(text)
        if not is_valid:
            hint_map = {
                "text_must_be_string": "text 必须是字符串，请重新调用 wechat_reply。",
                "text_empty": "text 不能为空，请输入要发送的消息内容。",
                "text_too_long": f"text 超过 500 字符限制（当前 {len(text)} 字符），请缩短后重试。",
                "text_contains_newline": "text 不能包含换行符。想发多条请多次调用 wechat_reply。",
            }
            result = {"status": "error", "recoverable": True,
                      "reason": error_reason,
                      "hint": hint_map.get(error_reason, "请修正后重新调用 wechat_reply。")}
            turn.messages.append({
                "role": "tool", "tool_call_id": turn._current_tool_call_id,
                "content": _json.dumps(result, ensure_ascii=False),
            })
            logger.info("validate_reply_args: invalid reason=%s len=%d",
                       error_reason, len(text))
            return TurnState.CALL_LLM

        turn._current_reply_text = text
        logger.info("validate_reply_args: valid len=%d preview=%s",
                   len(text), text[:60].replace("\n", "\\n"))
        return TurnState.GUARD_OUTPUT

    def _do_guard_output(self, turn: TurnScratchpad) -> TurnState:
        """GUARD_OUTPUT: guard single wechat_reply text.

        v1.5: guards one message at a time (wechat_reply pipeline).
        Stores guarded_text on turn._guarded_text.
        If blocked → recoverable error tool result → CALL_LLM.
        """
        from agent.output_guard import guard_single_message

        text = turn._current_reply_text
        guarded = guard_single_message(text)

        if not guarded:
            result = {"status": "error", "recoverable": True,
                      "reason": "guard_blocked",
                      "hint": "消息被输出保护拦截（可能包含敏感内容或协议泄露）。请修改后重新调用 wechat_reply。"}
            turn.messages.append({
                "role": "tool", "tool_call_id": turn._current_tool_call_id,
                "content": _json.dumps(result, ensure_ascii=False),
            })
            logger.info("guard_output: blocked preview=%s", text[:60])
            return TurnState.CALL_LLM

        turn._guarded_text = guarded
        logger.info("guard_output: passed len=%d sanitized=%s",
                   len(guarded), guarded != text)
        return TurnState.PRE_SEND_INTERRUPT_CHECK

    def _do_pre_send_interrupt_check(self, turn: TurnScratchpad) -> TurnState:
        """PRE_SEND_INTERRUPT_CHECK: check before actually sending.

        v1.5: if interrupted before send → END_SUPERSEDED_BEFORE_SEND.
        The model's wechat_reply text is discarded, NOT sent to user.
        """
        if self._is_interrupted():
            turn.add_fallback_event("interrupted_before_send",
                "turn superseded before wechat_reply could be sent")
            logger.info("pre_send_interrupt: interrupted=True → END_SUPERSEDED_BEFORE_SEND")
            return TurnState.END_SUPERSEDED_BEFORE_SEND
        return TurnState.EXEC_SEND_ONE

    async def _do_exec_send_one(
        self, turn: TurnScratchpad, wechat_sender: WeChatSender | None,
    ) -> TurnState:
        """EXEC_SEND_ONE: send a single WeChat bubble.

        v1.5: sends turn._guarded_text (NOT original_text).
        Checks wechat_sender return value for delivered/failed/rate_limited/blocked/unknown.
        Only delivered → sent_message_count += 1.
        Idempotency: turn_id + tool_call_id.
        """
        guarded_text = turn._guarded_text
        original_text = turn._current_reply_text
        tool_call_id = turn._current_tool_call_id
        idempotency_key = f"{turn.turn_id}_{tool_call_id}"

        def _append_result(result: dict) -> None:
            turn.messages.append({
                "role": "tool", "tool_call_id": tool_call_id,
                "content": _json.dumps(result, ensure_ascii=False),
            })

        # ── Idempotency check ────────────────────────────────────
        if idempotency_key in turn._sent_idempotency_keys:
            result = {"status": "ok", "delivery": "already_sent",
                      "message_index": turn.sent_message_count}
            _append_result(result)
            logger.info("exec_send_one: idempotent already_sent key=%s", idempotency_key)
            return TurnState.APPEND_SEND_RESULT

        # ── Clear pending flag (we're about to send) ─────────────
        turn._has_pending_wechat_reply = False

        # ── No sender → cannot send ──────────────────────────────
        if not wechat_sender:
            result = {"status": "error", "recoverable": False,
                      "reason": "wechat_sender_unavailable",
                      "hint": "发送通道不可用。请调用 done(status='failed')。"}
            _append_result(result)
            turn.send_failure_count += 1
            logger.info("exec_send_one: no wechat_sender")
            return TurnState.APPEND_SEND_RESULT

        # ── Send single bubble (guarded_text) ────────────────────
        send_batch = [{"text": guarded_text, "delay": 0.0, "_vm_index": 0}]
        try:
            raw_results = await wechat_sender(send_batch)
        except Exception as e:
            turn.send_failure_count += 1
            result = {"status": "error", "recoverable": False,
                      "reason": "send_exception", "hint": str(e)[:200]}
            _append_result(result)
            logger.error("exec_send_one: exception=%s", e)
            return TurnState.APPEND_SEND_RESULT

        # ── Check wechat_sender return value ─────────────────────
        if not raw_results or not isinstance(raw_results, list) or not raw_results:
            turn.send_failure_count += 1
            result = {"status": "error", "recoverable": False,
                      "reason": "send_no_result", "hint": "wechat_sender returned empty"}
            _append_result(result)
            logger.info("exec_send_one: empty result")
            return TurnState.APPEND_SEND_RESULT

        send_status = raw_results[0].get("send_status", "unknown") if raw_results else "unknown"

        if send_status == "delivered":
            turn._last_send_delivered = True
            turn.sent_message_count += 1
            turn._sent_idempotency_keys.add(idempotency_key)
            vm = VisibleMessage(
                visible_message_id=f"vm_{turn.turn_id}_{turn.sent_message_count}",
                index=turn.sent_message_count,
                original_text=original_text,
                guarded_text=guarded_text,
                guard_status="PASSED" if guarded_text == original_text else "SANITIZED",
                tool_call_id=tool_call_id,
                idempotency_key=idempotency_key,
                delivery_status=DeliveryStatus.SENT_CONFIRMED,
                sent_at_timestamp=time.time(),
            )
            turn.visible_messages.append(vm)
            result = {"status": "ok", "delivery": "sent",
                      "message_index": turn.sent_message_count}
            # ── Embed limit_reached in this tool result if at limit ──
            if turn.sent_message_count >= turn.max_reply_messages_per_turn:
                result["limit_reached"] = True
                result["hint"] = "本轮微信消息数量已达上限，请调用 done 结束。"
            turn._sent_idempotency_keys.discard(idempotency_key)
            # re-add after result construction for completeness
            logger.info("exec_send_one: delivered reply_index=%d idempotency_key=%s",
                       turn.sent_message_count, idempotency_key)
        elif send_status == "failed":
            turn.send_failure_count += 1
            error_detail = raw_results[0].get("send_error", "")
            if "no_bridge" in str(error_detail).lower():
                result = {"status": "error", "recoverable": False,
                          "reason": "wechat_sender_unavailable",
                          "hint": "发送通道不可用。请调用 done(status='failed')。"}
            else:
                result = {"status": "error", "recoverable": True,
                          "reason": "send_failed",
                          "hint": f"发送失败：{error_detail[:100]}。可以重试或 done。"}
            logger.info("exec_send_one: failed error=%s", error_detail[:80])
        elif send_status == "rate_limited":
            result = {"status": "error", "recoverable": True,
                      "reason": "rate_limited",
                      "hint": "发送太快被微信限流，请等待几秒后重试或调用 done(status='failed')。"}
        elif send_status == "blocked":
            result = {"status": "error", "recoverable": True,
                      "reason": "send_blocked",
                      "hint": "消息被拦截（可能为空或包含敏感内容）。请修改后重试。"}
            logger.info("exec_send_one: blocked")
        else:
            turn.send_failure_count += 1
            result = {"status": "error", "recoverable": False,
                      "reason": "send_unknown_status",
                      "hint": f"发送状态未知：{send_status}"}
            logger.info("exec_send_one: unknown status=%s", send_status)

        turn.tool_results.append({"name": "wechat_reply", "result": send_status})
        _append_result(result)
        return TurnState.APPEND_SEND_RESULT

    def _do_append_send_result(self, turn: TurnScratchpad) -> TurnState:
        """APPEND_SEND_RESULT: transition after send result appended.

        v1.6: if wechat_reply was delivered → POST_SEND_THINKING_DELAY
        (interruptible delay before next CALL_LLM to avoid rate limits).
        Otherwise → POST_SEND_INTERRUPT_CHECK (immediate re-decision).
        """
        if turn._last_send_delivered:
            turn._last_send_delivered = False
            return TurnState.POST_SEND_THINKING_DELAY
        return TurnState.POST_SEND_INTERRUPT_CHECK

    async def _do_post_send_thinking_delay(self, turn: TurnScratchpad) -> TurnState:
        """POST_SEND_THINKING_DELAY: interruptible delay between wechat_reply bubbles.

        v1.6: after a wechat_reply is delivered, pause before CALL_LLM to:
          - Reduce WeChat rate-limit risk (spacing out sends)
          - Give the user a chance to interject (natural chat feel)

        Delay is NOT written to model history or tool results.
        Interrupt during delay → END_INTERRUPTED_DURING_REPLY_SEQUENCE
          (already-sent messages are preserved).
        No interrupt → POST_SEND_INTERRUPT_CHECK → LIMIT_CHECK → CALL_LLM.
        """
        if self.test_mode:
            return TurnState.POST_SEND_INTERRUPT_CHECK

        reply_index = turn.sent_message_count  # 1-based after delivered increment
        last_text = turn._current_reply_text if turn._current_reply_text else ""
        is_narrative = len(last_text) >= POST_SEND_DELAY_NARRATIVE_THRESHOLD_CHARS

        # ── Compute delay range ────────────────────────────────────
        if is_narrative:
            min_delay, max_delay = POST_SEND_DELAY_LONG_NARRATIVE
            delay_reason = f"long_narrative ({len(last_text)} chars)"
        elif reply_index <= len(POST_SEND_DELAY_TIERS):
            min_delay, max_delay = POST_SEND_DELAY_TIERS[reply_index - 1]
            delay_reason = f"reply_index={reply_index}"
        else:
            min_delay, max_delay = POST_SEND_DELAY_TIERS[-1]
            delay_reason = f"reply_index={reply_index} (capped)"

        delay_s = min_delay + random.random() * (max_delay - min_delay)
        poll_interval_s = POST_SEND_DELAY_POLL_INTERVAL_MS / 1000.0

        logger.info(
            "post_send_thinking_delay: start delay=%.2fs reason=%s reply_index=%d narrative=%s",
            delay_s, delay_reason, reply_index, is_narrative,
        )

        # ── Interruptible wait loop ────────────────────────────────
        waited = 0.0
        interrupted = False
        while waited < delay_s:
            sleep_for = min(poll_interval_s, delay_s - waited)
            await asyncio.sleep(sleep_for)
            waited += sleep_for

            if self._is_interrupted():
                interrupted = True
                break

        # ── Record accumulated delay (excluded from turn timeout) ──
        turn._thinking_delay_accumulated_ms += waited * 1000

        if interrupted:
            turn.reply_sequence_status = "interrupted_by_user"
            turn.add_fallback_event(
                "interrupted_during_thinking_delay",
                f"sent {turn.sent_message_count} messages, waited {waited:.2f}s",
            )
            logger.info(
                "post_send_thinking_delay: interrupted_during_delay=yes "
                "waited=%.2fs reply_index=%d sent_count=%d → END_INTERRUPTED_DURING_REPLY_SEQUENCE",
                waited, reply_index, turn.sent_message_count,
            )
            return TurnState.END_INTERRUPTED_DURING_REPLY_SEQUENCE

        logger.info(
            "post_send_thinking_delay: complete waited=%.2fs reply_index=%d "
            "delay_reason=%s next_call_llm_after_delay=yes",
            waited, reply_index, delay_reason,
        )
        return TurnState.POST_SEND_INTERRUPT_CHECK

    def _do_post_send_interrupt_check(self, turn: TurnScratchpad) -> TurnState:
        """POST_SEND_INTERRUPT_CHECK: check if user interrupted mid-sequence.

        v1.5: if user sent new message after we sent a bubble but before done →
        END_INTERRUPTED_DURING_REPLY_SEQUENCE.
        Already-sent messages are preserved.
        """
        if self._is_interrupted():
            turn.reply_sequence_status = "interrupted_by_user"
            turn.add_fallback_event("interrupted_during_reply_sequence",
                f"sent {turn.sent_message_count} messages before user interruption")
            logger.info("post_send_interrupt: interrupted=True sent_count=%d → END_INTERRUPTED_DURING_REPLY_SEQUENCE",
                       turn.sent_message_count)
            return TurnState.END_INTERRUPTED_DURING_REPLY_SEQUENCE
        return TurnState.LIMIT_CHECK

    def _do_limit_check(self, turn: TurnScratchpad) -> TurnState:
        """LIMIT_CHECK: check turn-level limits after each send.

        v1.5: limit info already embedded in wechat_reply tool result
        (limit_reached=true). This state checks for exceeded limits that
        force termination.
        """
        if turn.sent_message_count >= turn.max_reply_messages_per_turn:
            # Already flagged in tool result. Allow model one more turn
            # to call done. If it calls wechat_reply again → END_MAX_LIMIT
            # (handled in CLASSIFY_ONE_TOOL_CALL).
            logger.info("limit_check: at max replies sent_count=%d → CALL_LLM (model should done)",
                       turn.sent_message_count)
            return TurnState.CALL_LLM
        if turn.send_failure_count >= turn.max_send_failures_per_turn:
            turn.add_fallback_event("max_send_failures",
                f"{turn.send_failure_count} failures")
            logger.info("limit_check: max send failures → CALL_LLM")
            return TurnState.CALL_LLM
        if turn.llm_call_count >= turn.max_llm_steps_per_turn:
            turn.add_fallback_event("max_llm_steps",
                f"{turn.llm_call_count} steps")
            logger.info("limit_check: max LLM steps → HARD_FALLBACK")
            return TurnState.HARD_FALLBACK
        return TurnState.CALL_LLM

    # ================================================================
    # v1.5: done pipeline — only normal terminal
    # ================================================================

    def _do_validate_done_args(self, turn: TurnScratchpad) -> TurnState:
        """VALIDATE_DONE_ARGS: validate done(status, note).

        v1.5: status must be replied/no_reply/failed.
        replied requires sent_count > 0.
        no_reply requires sent_count == 0.
        no_reply/failed require non-empty note.
        replied note is optional.
        """
        classified = turn._classified_single
        serialized = classified.serialized or {}
        fn = serialized.get("function", {})
        args_raw = fn.get("arguments", "")

        try:
            args = _json.loads(args_raw) if args_raw.strip() else {}
        except _json.JSONDecodeError as e:
            turn.messages.append({
                "role": "system",
                "content": f"done 参数 JSON 解析失败：{e}。请修正后重新调用 done。",
            })
            return TurnState.PROTOCOL_RETRY

        status = str(args.get("status", "") or "").strip()
        note = str(args.get("note", "") or "").strip()
        sent_count = turn.sent_message_count

        # ── status must be valid ──
        if status not in ("replied", "no_reply", "failed"):
            turn.messages.append({
                "role": "system",
                "content": f"done status 必须为 replied/no_reply/failed，收到了 '{status}'。请修正后重新调用。",
            })
            return TurnState.PROTOCOL_RETRY

        # ── status vs sent_count consistency ──
        if status == "replied" and sent_count == 0:
            turn.messages.append({
                "role": "system",
                "content": "done(status='replied') 要求本轮至少发送过一条消息，但 sent_count=0。请先调用 wechat_reply 或使用 done(status='no_reply')。",
            })
            return TurnState.PROTOCOL_RETRY
        if status == "no_reply" and sent_count > 0:
            turn.messages.append({
                "role": "system",
                "content": f"done(status='no_reply') 要求本轮未发送消息，但已发送 {sent_count} 条。请使用 done(status='replied')。",
            })
            return TurnState.PROTOCOL_RETRY

        # ── note required for no_reply / failed ──
        if status in ("no_reply", "failed") and not note:
            turn.messages.append({
                "role": "system",
                "content": f"done(status='{status}') 时 note 不能为空，请说明原因后重新调用 done。",
            })
            return TurnState.PROTOCOL_RETRY

        # ── v1.5: no_reply guard — reject if user message is a question / follow-up ──
        if status == "no_reply" and _user_message_is_question(turn.user_message):
            turn.messages.append({
                "role": "system",
                "content": (
                    "用户当前在问你问题或追问（消息包含疑问词、追问标记或社交邀约）。"
                    "不能 done(status='no_reply')。"
                    "请调用 wechat_reply 回答用户的问题，或说明你不知道。"
                ),
            })
            logger.info("validate_done_args: no_reply blocked, user message is question: %s",
                       turn.user_message[:80])
            return TurnState.PROTOCOL_RETRY

        turn.done_status = status
        turn.done_note = note

        logger.info("validate_done_args: status=%s note=%s sent_count=%d",
                   status, note[:80] if note else "(none)", sent_count)

        if status == "replied":
            turn.reply_sequence_status = "completed"
            return TurnState.END_REPLIED
        elif status == "no_reply":
            turn.reply_sequence_status = "no_reply"
            return TurnState.END_NO_REPLY
        else:
            turn.reply_sequence_status = "failed"
            return TurnState.END_FAILED

    # ================================================================
    # v1.5: Protocol retry
    # ================================================================

    def _do_protocol_retry(self, turn: TurnScratchpad) -> TurnState:
        """PROTOCOL_RETRY: model violated tool protocol.

        v1.5: if sent_count > 0 when retries exceeded → END_PROTOCOL_ERROR
        (preserve sent history, don't HARD_FALLBACK).
        If sent_count == 0 when retries exceeded → HARD_FALLBACK.
        """
        turn.protocol_retry_count += 1

        if turn.protocol_retry_count > turn.max_protocol_retries:
            turn.add_fallback_event(
                "protocol_retry_exceeded",
                f"exceeded max protocol retries ({turn.max_protocol_retries})",
            )
            logger.info("protocol_retry: exceeded sent_count=%d", turn.sent_message_count)
            if turn.sent_message_count > 0:
                # Don't lose already-sent messages → END_PROTOCOL_ERROR
                turn.reply_sequence_status = "protocol_error"
                return TurnState.END_PROTOCOL_ERROR
            return TurnState.HARD_FALLBACK

        turn.add_fallback_event(
            "protocol_retry",
            f"attempt {turn.protocol_retry_count}/{turn.max_protocol_retries}",
        )
        logger.info("protocol_retry: attempt=%d/%d",
                   turn.protocol_retry_count, turn.max_protocol_retries)

        # ── v1.5: if any assistant tool_calls message in recent history lacks
        # a matching tool result, the LLM API will reject the next request
        # ("insufficient tool messages following tool_calls message").
        # Search backward from end for unmatched assistant tool_calls.
        # (The caller may have already appended a system message before PROTOCOL_RETRY,
        # so we check the last few messages, not just messages[-1].)
        for _scan_offset in range(min(3, len(turn.messages)), 0, -1):
            _candidate = turn.messages[-_scan_offset]
            if _candidate.get("role") == "assistant" and _candidate.get("tool_calls"):
                # Check if any matching tool message already follows it
                _tcs = _candidate["tool_calls"]
                _has_match = False
                for _j in range(len(turn.messages) - _scan_offset + 1, len(turn.messages)):
                    if turn.messages[_j].get("role") == "tool":
                        _has_match = True
                        break
                if not _has_match:
                    for tc in _tcs:
                        cid = tc.get("id", "") if isinstance(tc, dict) else ""
                        turn.messages.append({
                            "role": "tool",
                            "tool_call_id": cid,
                            "content": _json.dumps({
                                "status": "error", "recoverable": True,
                                "reason": "protocol_retry",
                                "hint": "上一个工具调用因协议违规被拒绝，请按提示重新调用正确的工具。",
                            }, ensure_ascii=False),
                        })
                    break

        retry_note = "必须调用 wechat_reply 或 done。每次只能调用一个工具。"
        turn.messages.append({"role": "system", "content": retry_note})

        # v1.3: flash → pro upgrade logic
        last = turn.model_attempts[-1] if turn.model_attempts else None
        if last and last.selected_model == "flash":
            if self._routing_mode == "manual_flash" and not self._manual_flash_allow_upgrade:
                return TurnState.CALL_LLM
            return TurnState.FLASH_TO_PRO_UPGRADE

        return TurnState.CALL_LLM

    # ================================================================
    # v1.5: Hard fallback
    # ================================================================

    async def _do_hard_fallback(
        self, turn: TurnScratchpad, wechat_sender: WeChatSender | None,
    ) -> TurnState:
        """HARD_FALLBACK: system fixed fallback text.

        v1.5: only reached when sent_count == 0 (otherwise END_PROTOCOL_ERROR).
        Uses VisibleMessage for fallback tracking. Does NOT use model content.
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
        parts.append("可用工具：get_time, get_weather, wechat_reply, done")
        parts.append(
            "通过 wechat_reply 逐条发送微信。最后调用 done 结束本轮。"
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


def _user_message_is_question(text: str) -> bool:
    """v1.5: detect if user message is a question, follow-up, or social invitation.

    Returns True when the message clearly asks something or invites a reply.
    Used to block done(status='no_reply') when the user expects a response.
    """
    if not text or not text.strip():
        return False
    t = text.strip()
    # Explicit question markers
    _question_words = (
        "吗", "呢", "啥", "什么", "哪", "谁", "几", "多少",
        "为什么", "为啥", "怎么", "怎样", "如何", "干嘛", "咋", "怎么了",
        "然后", "接着", "之后", "还有", "是不是", "对不对", "行不行",
        "在哪", "什么时候", "几点", "多久", "多远",
    )
    for w in _question_words:
        if w in t:
            return True
    # Question marks
    if "？" in t or "?" in t:
        return True
    # Short follow-ups that clearly invite a reply
    _followups = ("说呀", "说啊", "讲啊", "继续", "接着说", "然后呢", "后来呢",
                  "你咋", "你怎么", "你咋看", "你怎么看")
    if t in _followups:
        return True
    return False


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
