"""Turn-local state and result types for ordinary text chat."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TurnState(StrEnum):
    END_REPLIED = "end_replied"
    END_FAILED = "end_failed"
    END_CANCELLED = "end_cancelled"
    END_SUPERSEDED_BEFORE_SEND = "end_superseded_before_send"
    END_INTERRUPTED_DURING_REPLY_SEQUENCE = "end_interrupted_during_reply_sequence"
    END_MAX_LIMIT = "end_max_limit"
    END_PROTOCOL_ERROR = "end_protocol_error"


SOURCE_USER_MESSAGE = "user_message"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    FAILED_BEFORE_SEND = "failed_before_send"
    UNKNOWN_AFTER_ATTEMPT = "unknown_after_attempt"
    SENT_CONFIRMED = "sent_confirmed"
    FAILED_CONFIRMED = "failed_confirmed"
    BLOCKED_BY_GUARD = "blocked_by_guard"


@dataclass(slots=True)
class VisibleMessage:
    visible_message_id: str
    index: int
    original_text: str = ""
    guarded_text: str = ""
    guard_status: str = ""
    delivery_status: str = ""
    attempt_count: int = 0
    wechat_message_id: str = ""
    error_code: str = ""
    delay: float = 0.0
    tool_call_id: str = ""
    idempotency_key: str = ""
    sent_at_timestamp: float = 0.0


@dataclass(slots=True)
class ModelProfile:
    model_name: str = "deepseek-v4-pro"
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout_seconds: float = 40.0


@dataclass(slots=True)
class ModelAttempt:
    attempt_index: int = 0
    selected_model: str = ""
    routing_mode: str = ""
    routing_reason: str = ""
    model_name: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    latency_ms: int = 0
    result_type: str = ""
    error_type: str = ""
    upgraded_from: str = ""


@dataclass
class TurnScratchpad:
    turn_id: str = field(default_factory=lambda: f"turn_{uuid.uuid4().hex[:12]}")
    start_time: float = field(default_factory=time.time)
    user_message: str = ""
    trigger: str = "user"
    source: str = SOURCE_USER_MESSAGE
    input_message_ids: list[str] = field(default_factory=list)
    input_first_seq: int = 0
    input_latest_seq: int = 0
    input_cutoff_time: float = 0.0
    route: str = ""
    interrupt_checker: Any = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    visible_messages: list[VisibleMessage] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    fallback_events: list[dict[str, Any]] = field(default_factory=list)
    model_attempts: list[ModelAttempt] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "reasoning_tokens": 0,
        "estimated": False,
    })
    llm_status: str = "ok"
    llm_call_count: int = 0
    sent_message_count: int = 0
    reply_sequence_status: str = "in_progress"
    terminal_state: TurnState | None = None
    _protocol_event_writer: Any = field(default=None, repr=False)

    @property
    def elapsed_ms(self) -> int:
        return int((time.time() - self.start_time) * 1000)

    def add_fallback_event(self, event_type: str, detail: str = "") -> None:
        self.fallback_events.append({"type": event_type, "detail": detail[:500]})

    def get_delivered_texts(self) -> list[str]:
        return [
            item.guarded_text or item.original_text
            for item in self.visible_messages
            if item.delivery_status == DeliveryStatus.SENT_CONFIRMED
        ]


@dataclass
class TurnResult:
    terminal_state: TurnState
    turn_id: str
    user_message: str
    visible_messages: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    fallback_events: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    iterations: int = 0
    elapsed_ms: int = 0
    llm_status: str = "ok"
    source: str = "user_reply"
    author: str = "assistant"
    trigger: str = "user_message"
    context_sample: list[dict[str, Any]] = field(default_factory=list)
    post_terminal_completed: bool = False
    reply_sequence_status: str = ""
    sent_message_count: int = 0

    @property
    def delivered_count(self) -> int:
        return sum(
            1
            for item in self.visible_messages
            if item.get("delivery_status") == DeliveryStatus.SENT_CONFIRMED
            or item.get("send_status") in {"delivered", DeliveryStatus.SENT_CONFIRMED}
        )


InterruptChecker = Callable[[str], bool]
PostTerminalCallback = Callable[[TurnScratchpad, TurnState], Awaitable[None]]

MAX_REPLY_MESSAGES_PER_TURN = 15
MAX_LLM_STEPS_PER_TURN = 20
MAX_PROTOCOL_RETRIES_PER_TURN = 2
