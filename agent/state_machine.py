"""State machine types — v1.5 strict CC causal chain.

TurnState enum, TurnScratchpad (turn-local mutable state), TurnResult
(the immutable result returned to callers after the turn ends),
VisibleMessage, DeliveryStatus, ContextBuildResult.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TurnState(StrEnum):
    """v1.5 strict CC causal chain state machine.

    One tool call per LLM response.  wechat_reply = one bubble (not terminal).
    done = only normal terminal tool.
    """

    # --- lifecycle ---
    RECEIVE_EVENT = "receive_event"
    CREATE_TURN = "create_turn"
    INTERRUPT_CHECK = "interrupt_check"
    BUILD_CONTEXT = "build_context"
    CLASSIFY_CONTEXT_ERROR = "classify_context_error"
    OPTIONAL_PREPROCESS = "optional_preprocess"

    # --- LLM ---
    CALL_LLM = "call_llm"

    # --- output handling ---
    HANDLE_MODEL_OUTPUT = "handle_model_output"
    CLASSIFY_ONE_TOOL_CALL = "classify_one_tool_call"       # v1.5: exactly 1 tool call

    # --- fact tools ---
    EXEC_FACT_TOOLS = "exec_fact_tools"

    # --- wechat_reply pipeline (v1.5: action tool, NOT terminal) ---
    VALIDATE_REPLY_ARGS = "validate_reply_args"
    GUARD_OUTPUT = "guard_output"
    PRE_SEND_INTERRUPT_CHECK = "pre_send_interrupt_check"
    EXEC_SEND_ONE = "exec_send_one"                         # single bubble send
    APPEND_SEND_RESULT = "append_send_result"
    POST_SEND_THINKING_DELAY = "post_send_thinking_delay"    # v1.6: interruptible delay between sends
    POST_SEND_INTERRUPT_CHECK = "post_send_interrupt_check"
    LIMIT_CHECK = "limit_check"

    # --- done pipeline (v1.5: only normal terminal) ---
    VALIDATE_DONE_ARGS = "validate_done_args"

    # --- v3.1: skip_proactive pipeline ---
    VALIDATE_SKIP_PROACTIVE_ARGS = "validate_skip_proactive_args"
    EXEC_SKIP_PROACTIVE = "exec_skip_proactive"

    # --- protocol ---
    PROTOCOL_RETRY = "protocol_retry"

    # ── v1.3: model routing (kept) ──
    FLASH_TO_PRO_UPGRADE = "flash_to_pro_upgrade"

    # --- fallback ---
    HARD_FALLBACK = "hard_fallback"

    # --- post-turn ---
    WRITE_MODEL_HISTORY = "write_model_history"
    POST_TURN_HOOKS = "post_turn_hooks"

    # === v1.5 Terminal states ===
    END_REPLIED = "end_replied"                     # done(status="replied")
    END_NO_REPLY = "end_no_reply"                   # done(status="no_reply")
    END_FAILED = "end_failed"
    END_FALLBACK_SENT = "end_fallback_sent"
    END_CANCELLED = "end_cancelled"
    END_SUPERSEDED_BEFORE_SEND = "end_superseded_before_send"
    END_INTERRUPTED_DURING_REPLY_SEQUENCE = "end_interrupted_during_reply_sequence"
    END_MAX_LIMIT = "end_max_limit"
    END_PROTOCOL_ERROR = "end_protocol_error"
    END_ABORTED_BY_SYSTEM = "end_aborted_by_system"

    # === v3.1 Proactive-specific terminal states ===
    END_SKIPPED_PROACTIVE = "end_skipped_proactive"
    END_FAILED_NO_FALLBACK = "end_failed_no_fallback"
    END_PROACTIVE_GUARD_BLOCKED = "end_proactive_guard_blocked"
    END_PROACTIVE_MAX_REPLY_LIMIT = "end_proactive_max_reply_limit"
    END_PROACTIVE_MAX_LLM_STEPS = "end_proactive_max_llm_steps"


# v1.5 terminal states
TERMINAL_STATES: frozenset[TurnState] = frozenset({
    TurnState.END_REPLIED,
    TurnState.END_NO_REPLY,
    TurnState.END_FAILED,
    TurnState.END_FALLBACK_SENT,
    TurnState.END_CANCELLED,
    TurnState.END_SUPERSEDED_BEFORE_SEND,
    TurnState.END_INTERRUPTED_DURING_REPLY_SEQUENCE,
    TurnState.END_MAX_LIMIT,
    TurnState.END_PROTOCOL_ERROR,
    TurnState.END_ABORTED_BY_SYSTEM,
    # v3.1 proactive-specific terminal states
    TurnState.END_SKIPPED_PROACTIVE,
    TurnState.END_FAILED_NO_FALLBACK,
    TurnState.END_PROACTIVE_GUARD_BLOCKED,
    TurnState.END_PROACTIVE_MAX_REPLY_LIMIT,
    TurnState.END_PROACTIVE_MAX_LLM_STEPS,
})

# v3.1: Unified turn source constants
SOURCE_USER_MESSAGE = "user_message"
SOURCE_PROACTIVE_IMPULSE = "PROACTIVE_IMPULSE"


# ================================================================
# Delivery status (v1.2 §13.3 — five-state model + PENDING)
# ================================================================

class DeliveryStatus(StrEnum):
    """Per-message delivery status for the send path."""
    PENDING = "pending"                      # 待发送
    FAILED_BEFORE_SEND = "failed_before_send"  # 发送前失败（可重试）
    UNKNOWN_AFTER_ATTEMPT = "unknown_after_attempt"  # 发送后未知（默认不重试）
    SENT_CONFIRMED = "sent_confirmed"        # 确认已送达
    FAILED_CONFIRMED = "failed_confirmed"    # 确认失败（可重试）
    BLOCKED_BY_GUARD = "blocked_by_guard"    # 被输出保护拦截（不重试）


# ================================================================
# VisibleMessage — per-message tracking (v1.2 §13.2)
# ================================================================

@dataclass(slots=True)
class VisibleMessage:
    """One visible WeChat message tracked through the send pipeline.

    v1.5: each wechat_reply tool call produces exactly one VisibleMessage.
    """

    visible_message_id: str           # vm_{turn_id}_{reply_index}
    index: int                        # reply_index (1-based sent order)
    original_text: str = ""           # raw text from wechat_reply(text=...)
    guarded_text: str = ""            # text after GUARD_OUTPUT (this is what was sent)
    guard_status: str = ""            # PASSED / SANITIZED / BLOCKED
    delivery_status: str = ""         # DeliveryStatus value
    attempt_count: int = 0
    wechat_message_id: str = ""       # WeChat-side message id (if available)
    error_code: str = ""
    delay: float = 0.0
    # ── v1.5 ──
    tool_call_id: str = ""            # LLM tool_call_id for this bubble
    idempotency_key: str = ""         # turn_id + tool_call_id
    sent_at_timestamp: float = 0.0


# ================================================================
# v1.3: Model profile — per-model params (shared provider)
# ================================================================

@dataclass(slots=True)
class ModelProfile:
    """v1.3: flash 或 pro 的模型级参数。不含 provider/api_key/base_url。"""
    model_name: str = "deepseek-v4-flash"
    temperature: float = 0.7
    max_tokens: int = 800
    timeout_seconds: float = 20.0


# ================================================================
# v1.3: Model attempt log — one per CALL_LLM
# ================================================================

@dataclass(slots=True)
class ModelAttempt:
    """v1.3 §9: 一次模型调用尝试记录。"""
    attempt_index: int = 0
    selected_model: str = ""         # "flash" | "pro"
    routing_mode: str = ""           # "auto" | "manual_flash" | "manual_pro"
    routing_reason: str = ""         # "daily_chat" | "flash_failed" | ...
    model_name: str = ""             # 实际传给 API 的模型名
    started_at: float = 0.0
    finished_at: float = 0.0
    latency_ms: int = 0
    result_type: str = ""            # "success" | "llm_error" | "protocol_violation"
    error_type: str = ""             # "timeout" | "empty_response" | ...
    upgraded_from: str = ""          # "flash"（如果是从 flash 升级来的）


# ================================================================
# ContextBuildResult — internal to _do_build_context (v1.2 §6)
# ================================================================

@dataclass(slots=True)
class ContextBuildResult:
    """Result of BUILD_CONTEXT, consumed by CLASSIFY_CONTEXT_ERROR."""

    messages: list[dict[str, Any]]
    warnings: list[dict[str, str]]     # [{"source": "summary_loader", "detail": "..."}, ...]
    fatal_error: str = ""              # non-empty means fatal — route to HARD_FALLBACK


# ================================================================
# TurnScratchpad (v1.2 expanded)
# ================================================================

@dataclass
class TurnScratchpad:
    """Turn-local mutable state. Never persisted. Never enters model context.

    v1.5: per-bubble wechat_reply tracking, done validation, interrupt differentiation.
    """

    turn_id: str = field(default_factory=lambda: f"turn_{uuid.uuid4().hex[:12]}")
    start_time: float = field(default_factory=time.time)
    turn_timeout_seconds: float = 60.0

    # Input
    user_message: str = ""
    trigger: str = "user"  # user | admin | proactive

    # ── v1.5: Turn lifecycle metadata ──────────────────────────
    source: str = "user_message"       # SOURCE_USER_MESSAGE | SOURCE_PROACTIVE_IMPULSE
    proactive_meta: dict[str, Any] | None = None  # v3.1: proactive turn impulse metadata (see §0.3 contract)
    input_message_ids: list[str] = field(default_factory=list)
    input_first_seq: int = 0
    input_latest_seq: int = 0
    input_cutoff_time: float = 0.0
    interrupt_token: str = ""
    status: str = "created"
    terminal_state: TurnState | None = None
    context_warnings: list[dict[str, str]] = field(default_factory=list)
    _resume_state: TurnState | None = field(default=None, repr=False)

    # LLM message array (grows through the turn)
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Tool results (diagnostic only)
    tool_results: list[dict[str, Any]] = field(default_factory=list)

    # ── v1.3: Model routing ────────────────────────────────────
    model_attempts: list["ModelAttempt"] = field(default_factory=list)
    _current_attempt: "ModelAttempt | None" = field(default=None, repr=False)
    _force_model_label: str = ""
    _force_model_upgraded_from: str = ""
    max_flash_upgrade_attempts: int = 1
    flash_upgrade_count: int = 0
    route: str = ""

    # ── v1.5: Per-bubble send tracking ─────────────────────────
    visible_messages: list[VisibleMessage] = field(default_factory=list)
    sent_results: list[dict[str, Any]] = field(default_factory=list)
    _sent_idempotency_keys: set[str] = field(default_factory=set)
    _guarded_text: str = ""              # guarded text for current wechat_reply
    _current_reply_text: str = ""        # original text for current wechat_reply
    _current_tool_call_id: str = ""      # tool_call_id for current wechat_reply

    # ── v1.5: Reply sequence counters ──────────────────────────
    reply_sequence_status: str = "in_progress"  # in_progress | completed | no_reply | failed | interrupted_by_user | max_limit_reached | protocol_error
    sent_message_count: int = 0          # successfully delivered this turn
    send_failure_count: int = 0          # failed delivery attempts this turn
    done_status: str = ""                # done(status=...)
    done_note: str = ""                  # done(note=...)

    # ── v1.5: Pending wechat_reply (for interrupt differentiation) ──
    _has_pending_wechat_reply: bool = False  # model generated wechat_reply but not yet sent

    # ── v1.6: Post-send thinking delay ────────────────────────────
    _last_send_delivered: bool = False       # set by EXEC_SEND_ONE, read by APPEND_SEND_RESULT
    _thinking_delay_accumulated_ms: float = 0.0  # total time spent in POST_SEND_THINKING_DELAY

    # Counters
    iteration: int = 0
    llm_call_count: int = 0
    protocol_retry_count: int = 0
    send_attempt: int = 0

    # ── v3.1: Proactive turn counters ──────────────────────────
    proactive_attempt_count: int = 0       # model attempted wechat_reply count (LLM behavior audit)
    proactive_send_attempt_count: int = 0  # actual wechat send API calls (hard_max_wechat_reply_per_turn limits this)
    proactive_delivered_count: int = 0     # confirmed delivered count (allowed_tools phase judgment basis)
    proactive_llm_step_count: int = 0      # LLM call count for proactive (max_llm_steps_per_proactive_turn limits this)
    proactive_delivered_texts: list[str] = field(default_factory=list)  # confirmed delivered text content (only this writes to history)

    # ── v1.5 limits ────────────────────────────────────────────
    max_llm_steps_per_turn: int = 20        # v1.5
    max_reply_messages_per_turn: int = 15   # v1.5
    max_protocol_retries: int = 2           # v1.5
    max_send_failures_per_turn: int = 2     # v1.5
    proactive_soft_reply_limit: int = 0     # v1.5: set for proactive turns (0 = n/a)

    # Diagnostics
    fallback_events: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated": False,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "reasoning_tokens": 0,
    })

    # Errors
    llm_status: str = "ok"
    error_info: dict[str, Any] | None = None

    @property
    def elapsed_ms(self) -> int:
        return int((time.time() - self.start_time) * 1000)

    def add_fallback_event(self, event_type: str, detail: str = "") -> None:
        self.fallback_events.append({"type": event_type, "detail": detail[:500]})


# ================================================================
# TurnResult (v1.2 — assembled after post-terminal pipeline)
# ================================================================

@dataclass
class TurnResult:
    """Immutable result returned to main.py after the turn ends.

    Assembled AFTER WRITE_MODEL_HISTORY and POST_TURN_HOOKS complete.
    main.py reads this for transport-layer operations and HTTP response.
    """

    terminal_state: TurnState
    turn_id: str
    user_message: str

    # Messages actually sent to WeChat (VisibleMessage list or legacy dicts)
    visible_messages: list[dict[str, Any]] = field(default_factory=list)

    # Diagnostics
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    fallback_events: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    iterations: int = 0
    elapsed_ms: int = 0
    llm_status: str = "ok"

    # Outbound semantics (for history / logging)
    source: str = "user_reply"
    author: str = "assistant"
    trigger: str = "user_message"

    # Context sample (last N messages, for diagnostics)
    context_sample: list[dict[str, str]] = field(default_factory=list)

    # ── v1.5 ──
    post_terminal_completed: bool = False
    reply_sequence_status: str = ""          # v1.5
    sent_message_count: int = 0              # v1.5
    done_status: str = ""                    # v1.5
    done_note: str = ""                      # v1.5

    # ── v3.1: Proactive turn fields (for main.py callbacks) ──
    proactive_delivered_count: int = 0
    proactive_delivered_texts: list[str] = field(default_factory=list)
    proactive_attempt_count: int = 0
    proactive_send_attempt_count: int = 0

    @property
    def was_sent(self) -> bool:
        return self.terminal_state == TurnState.END_REPLIED

    @property
    def was_skipped(self) -> bool:
        return self.terminal_state == TurnState.END_NO_REPLY

    @property
    def was_fallback(self) -> bool:
        return self.terminal_state == TurnState.END_FALLBACK_SENT

    @property
    def was_failed(self) -> bool:
        return self.terminal_state in (TurnState.END_FAILED,)

    @property
    def was_cancelled(self) -> bool:
        return self.terminal_state == TurnState.END_CANCELLED

    @property
    def was_interrupted(self) -> bool:
        return self.terminal_state in (
            TurnState.END_SUPERSEDED_BEFORE_SEND,
            TurnState.END_INTERRUPTED_DURING_REPLY_SEQUENCE,
        )

    @property
    def delivered_count(self) -> int:
        return sum(
            1 for r in self.visible_messages
            if r.get("send_status") in ("delivered", "sent_confirmed")
            or r.get("delivery_status") in ("sent_confirmed",)
        )


# ================================================================
# Interrupt checker (v1.2: token-based)
# ================================================================

# Callback: (interrupt_token: str) -> bool.
# Returns True when the turn identified by interrupt_token has been
# superseded by a newer event and should be cancelled.
# Signature: Callable[[str], bool]
InterruptChecker = Callable[[str], bool]


# ================================================================
# v1.2 unified callback signatures
# ================================================================

# write_model_history callback:
#   (turn: TurnScratchpad, terminal_state: TurnState) -> Awaitable[None]
# post_turn_hook callback (single hook):
#   (turn: TurnScratchpad, terminal_state: TurnState) -> Awaitable[None]
PostTerminalCallback = Callable[["TurnScratchpad", TurnState], Awaitable[None]]


# ================================================================
# v1.5 Turn limits
# ================================================================

MAX_REPLY_MESSAGES_PER_TURN = 15
MAX_LLM_STEPS_PER_TURN = 20
MAX_PROTOCOL_RETRIES_PER_TURN = 2
MAX_SEND_FAILURES_PER_TURN = 2

# v1.6: Post-send thinking delay — interruptible pause between wechat_reply bubbles.
# Each tier covers a reply_index range. Values are (min_s, max_s) tuples.
POST_SEND_DELAY_TIERS: list[tuple[float, float]] = [
    (1.5, 2.5),   # after 1st message
    (2.5, 3.5),   # after 2nd message
    (3.5, 5.5),   # after 3rd+ message
]
POST_SEND_DELAY_LONG_NARRATIVE: tuple[float, float] = (2.5, 4.5)
POST_SEND_DELAY_NARRATIVE_THRESHOLD_CHARS: int = 120
POST_SEND_DELAY_POLL_INTERVAL_MS: int = 300  # interrupt check granularity


