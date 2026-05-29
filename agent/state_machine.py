"""State machine types — v0.2 Phase 1.

TurnState enum, TurnScratchpad (turn-local mutable state), and TurnResult
(the immutable result returned to callers after the turn ends).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TurnState(StrEnum):
    """Level-0 state machine states per v0.2 design.

    Non-terminal states:
    """

    # --- lifecycle ---
    CREATE_TURN = "create_turn"
    BUILD_CONTEXT = "build_context"
    OPTIONAL_PREPROCESS = "optional_preprocess"

    # --- LLM ---
    CALL_LLM = "call_llm"

    # --- output handling ---
    HANDLE_MODEL_OUTPUT = "handle_model_output"
    CLASSIFY_TOOL_CALLS = "classify_tool_calls"

    # --- fact / state tools ---
    EXEC_FACT_TOOLS = "exec_fact_tools"
    EXEC_STATE_TOOLS = "exec_state_tools"

    # --- protocol ---
    PROTOCOL_RETRY = "protocol_retry"

    # --- terminal action ---
    TERMINAL_ACTION_BRANCH = "terminal_action_branch"

    # --- send path ---
    PARSE_VISIBLE_BATCH = "parse_visible_batch"
    GUARD_OUTPUT = "guard_output"
    EXEC_SEND_WECHAT = "exec_send_wechat"
    SEND_RETRY_UNDELIVERED_ONLY = "send_retry_undelivered_only"
    DECIDE_SEND_TERMINAL = "decide_send_terminal"

    # --- skip path ---
    EXEC_SKIP_REPLY = "exec_skip_reply"

    # --- fallback ---
    HARD_FALLBACK = "hard_fallback"

    # --- post-turn ---
    WRITE_MODEL_HISTORY = "write_model_history"
    POST_TURN_HOOKS = "post_turn_hooks"

    # === Terminal states ===
    END_SENT = "end_sent"
    END_PARTIAL_SENT = "end_partial_sent"
    END_SKIPPED = "end_skipped"
    END_FALLBACK_SENT = "end_fallback_sent"
    END_FAILED = "end_failed"


# Set of states that terminate the turn loop
TERMINAL_STATES: frozenset[TurnState] = frozenset({
    TurnState.END_SENT,
    TurnState.END_PARTIAL_SENT,
    TurnState.END_SKIPPED,
    TurnState.END_FALLBACK_SENT,
    TurnState.END_FAILED,
})


@dataclass
class TurnScratchpad:
    """Turn-local mutable state. Never persisted. Never enters model context."""

    turn_id: str = field(default_factory=lambda: f"turn_{uuid.uuid4().hex[:12]}")
    start_time: float = field(default_factory=time.time)

    # Input
    user_message: str = ""
    trigger: str = "user"  # user | admin

    # LLM message array (grows through the turn)
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Tool results (diagnostic only)
    tool_results: list[dict[str, Any]] = field(default_factory=list)

    # Send path state
    visible_batch: list[str] = field(default_factory=list)  # raw parsed messages
    guarded_batch: list[dict[str, Any]] = field(
        default_factory=list
    )  # [{"text": str, "delay": float}, ...]
    sent_results: list[dict[str, Any]] = field(default_factory=list)

    # Counters
    iteration: int = 0          # total state transitions
    llm_call_count: int = 0     # LLM calls only
    protocol_retry_count: int = 0
    send_attempt: int = 0

    # Limits
    max_iterations: int = 5
    max_protocol_retries: int = 1
    max_send_retries: int = 2

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
    llm_status: str = "ok"  # ok | error
    error_info: dict[str, Any] | None = None

    @property
    def elapsed_ms(self) -> int:
        return int((time.time() - self.start_time) * 1000)

    def add_fallback_event(self, event_type: str, detail: str = "") -> None:
        self.fallback_events.append({"type": event_type, "detail": detail[:500]})


@dataclass
class TurnResult:
    """Immutable result returned to main.py after the turn ends.

    main.py uses this to write history, record diagnostics, and respond to bridge.
    """

    terminal_state: TurnState
    turn_id: str
    user_message: str

    # Messages actually sent to WeChat (may be empty for skip/fail)
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

    @property
    def was_sent(self) -> bool:
        return self.terminal_state in (TurnState.END_SENT, TurnState.END_PARTIAL_SENT)

    @property
    def was_skipped(self) -> bool:
        return self.terminal_state == TurnState.END_SKIPPED

    @property
    def was_fallback(self) -> bool:
        return self.terminal_state == TurnState.END_FALLBACK_SENT

    @property
    def was_failed(self) -> bool:
        return self.terminal_state in (TurnState.END_FAILED,)

    @property
    def delivered_count(self) -> int:
        return sum(
            1 for r in self.visible_messages if r.get("send_status") == "delivered"
        )
