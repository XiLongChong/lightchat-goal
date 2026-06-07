"""Runtime data contract types.

All types in this module are pure data structures — no business logic, no side effects.
Each type is defined once and used across all runtime modules as the single source of truth.

Spec references:
  - docs/runtime-specs/01-context-builder-spec.zh.md
  - docs/runtime-specs/02-tool-exposure-policy-spec.zh.md
  - docs/runtime-specs/03-turn-engine-spec.zh.md
  - docs/runtime-specs/04-memory-system-spec.zh.md
  - docs/runtime-specs/05-scheduler-runtime-spec.zh.md
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════════


class TurnType(str, enum.Enum):
    USER_CHAT = "user_chat"
    USER_MEDIA_CHAT = "user_media_chat"
    USER_REMINDER_REQUEST = "user_reminder_request"
    RELATIONSHIP_PROACTIVE = "relationship_proactive"
    USER_REMINDER_DUE = "user_reminder_due"
    SCHEDULED_RITUAL = "scheduled_ritual"
    ADMIN_MANUAL_TRIGGER = "admin_manual_trigger"
    MEMORY_MANAGEMENT = "memory_management"


class TriggerSource(str, enum.Enum):
    USER = "user"
    SCHEDULER = "scheduler"
    ADMIN = "admin"
    BRIDGE = "bridge"


class UserIntent(str, enum.Enum):
    CHAT = "chat"
    REQUEST_REMINDER = "request_reminder"
    REQUEST_IMAGE = "request_image"
    REQUEST_WEATHER = "request_weather"
    FORGET_DIRECTIVE = "forget_directive"
    RELATIONSHIP_FEEDBACK = "relationship_feedback"
    TASK_FEEDBACK = "task_feedback"
    TIME_QUERY = "time_query"
    UNKNOWN = "unknown"


class MediaIntent(str, enum.Enum):
    IMAGE_REQUESTED = "image_requested"
    IMAGE_PROVIDED = "image_provided"
    VOICE_PROVIDED = "voice_provided"


class MessageKind(str, enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    FILE = "file"
    SYSTEM_EVENT = "system_event"


class DeliveryStatus(str, enum.Enum):
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RATE_LIMITED = "rate_limited"       # WeChat platform rate limit (retryable)
    BLOCKED = "blocked"                 # WeChat platform content block (retryable)
    BLOCKED_BY_GUARD = "blocked_by_guard"
    NOT_APPLICABLE = "not_applicable"


class TurnPhase(str, enum.Enum):
    NORMAL_CHAT = "normal_chat"
    SKIPPABLE = "skippable"
    FORCED_SPEAK = "forced_speak"
    AFTER_FIRST_MESSAGE = "after_first_message"
    REMINDER_DELIVERY = "reminder_delivery"
    MEMORY_MANAGEMENT = "memory_management"
    ADMIN = "admin"


class CacheClass(str, enum.Enum):
    FIXED = "fixed"
    LOW_FREQUENCY = "low_frequency"
    TURN_DYNAMIC = "turn_dynamic"


class ContextBlockKind(str, enum.Enum):
    FIXED_PROTOCOL = "fixed_protocol"
    PERSONA = "persona"
    RELATIONSHIP_STATE = "relationship_state"
    MEMORY = "memory"
    RECENT_HISTORY = "recent_history"
    TURN_LOCAL = "turn_local"
    MEDIA = "media"
    SYSTEM_TIME = "system_time"
    CURRENT_INPUT = "current_input"
    TOOL_CONTRACT = "tool_contract"
    TERMINAL_POLICY = "terminal_policy"


class ResumeType(str, enum.Enum):
    COLD_START = "cold_start"
    CONTINUOUS_CHAT = "continuous_chat"
    SHORT_RESUME = "short_resume"
    SAME_DAY_RESUME = "same_day_resume"
    NEXT_DAY_RESUME = "next_day_resume"
    LONG_GAP_RESUME = "long_gap_resume"


class ToolCategory(str, enum.Enum):
    OUTPUT = "output"
    TURN_CONTROL = "turn_control"
    STATE = "state"
    INFORMATION = "information"


class ToolSideEffect(str, enum.Enum):
    NONE = "none"
    SENDS_MESSAGE = "sends_message"
    WRITES_STATE = "writes_state"
    READS_EXTERNAL = "reads_external"


# ═══════════════════════════════════════════════════════════════════════════════
# Primitives
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class SystemTime:
    dt: datetime
    timezone: str  # e.g. "Asia/Shanghai"
    weekday_cn: str  # e.g. "周三"


@dataclass(slots=True)
class MediaAttachment:
    item_id: str
    kind: str  # "image" | "voice"
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    file_size_bytes: int | None = None
    summary: str | None = None  # vision description or STT transcript
    tone: str | None = None  # voice tone hint
    emotion_hint: str | None = None  # voice emotion hint
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DeliveryReceipt:
    delivery_receipt_id: str
    message_id: str
    turn_id: str
    status: DeliveryStatus
    delivered_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


@dataclass(slots=True)
class ContextBuildWarning:
    source: str
    detail: str
    severity: str  # "info" | "warning" | "error"
    block_affected: str | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Inbound
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class InboundMessageItem:
    item_id: str
    kind: MessageKind
    text: str | None = None
    media: MediaAttachment | None = None
    received_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InboundMessageBatch:
    batch_id: str
    user_id: str
    bot_id: str
    items: list[InboundMessageItem] = field(default_factory=list)
    source_channel: str = "wechat"
    batch_reason: str = "single_message"
    received_at: datetime | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Turn request
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class RuntimeTrigger:
    trigger_id: str
    trigger_type: str  # "heartbeat" | "due_reminder" | "scheduled_ritual" | "manual_admin" | "post_user_turn"
    reason: str
    source_task_id: str | None = None
    due_at: float | None = None
    claimed_at: float | None = None
    trigger_kind: str = "cold_start"  # "cold_start" | "wake_up" | "scheduled" | "user_requested"
    is_wake_up: bool = False
    intensity: str = "daily"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TurnClassification:
    turn_type: TurnType
    trigger_source: TriggerSource
    user_intent: UserIntent = UserIntent.UNKNOWN
    media_intent: MediaIntent | None = None
    confidence: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class TurnRequest:
    turn_id: str
    turn_type: TurnType
    user_id: str
    bot_id: str
    inbound_batch: InboundMessageBatch | None = None
    runtime_trigger: RuntimeTrigger | None = None
    classification: TurnClassification | None = None
    tool_exposure: ToolExposure | None = None  # populated after policy resolve
    terminal_policy: TerminalPolicy | None = None
    memory_selection: MemorySelection | None = None
    created_at: datetime | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Turn-local state
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class TurnLocalSentMessage:
    message_id: str
    tool_call_id: str
    text: str
    status: DeliveryStatus
    delivered_at: datetime | None = None
    delivery_receipt_id: str | None = None


@dataclass(slots=True)
class TurnBudgetState:
    model_calls_remaining: int = 20
    tool_calls_remaining: int = 40
    delivered_messages_remaining: int = 15
    send_attempts_remaining: int = 3
    elapsed_seconds: float = 0.0
    exhausted_budget_type: str | None = None


@dataclass(slots=True)
class RejectedToolCall:
    tool_call_id: str
    tool_name: str
    reason: str  # "tool_not_exposed" | "invalid_arguments" | "invalid_phase" | "budget_exceeded"
    exposed_tool_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CurrentTurnLocalState:
    turn_id: str
    model_call_count: int = 0
    tool_call_count: int = 0
    delivered_message_count: int = 0
    send_attempt_count: int = 0
    failed_send_attempt_count: int = 0
    sent_messages_this_turn: list[TurnLocalSentMessage] = field(default_factory=list)
    tool_results_this_turn: list[ToolExecutionResult] = field(default_factory=list)
    rejected_tool_calls: list[RejectedToolCall] = field(default_factory=list)
    budget_state: TurnBudgetState = field(default_factory=TurnBudgetState)
    model_call_ids: list[str] = field(default_factory=list)
    context_snapshot_ids: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# Tool exposure
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class NativeToolSchema:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    terminal: bool = False
    side_effect: ToolSideEffect = ToolSideEffect.NONE
    category: ToolCategory = ToolCategory.OUTPUT


@dataclass(slots=True)
class DeniedTool:
    tool_name: str
    reason: str
    would_expose_if: str | None = None


@dataclass(slots=True)
class ToolExposure:
    exposure_id: str
    turn_id: str
    phase: TurnPhase
    exposed_tool_names: list[str] = field(default_factory=list)
    native_tool_schemas: list[NativeToolSchema] = field(default_factory=list)
    tool_contract_block: str = ""
    denied_tools: list[DeniedTool] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════════
# Context
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class ContentPart:
    type: str  # "text" | "image" | "audio"
    text: str | None = None
    image_url: str | None = None
    audio_url: str | None = None


@dataclass(slots=True)
class ProviderMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: Any
    name: str | None = None  # tool name when role=tool
    tool_call_id: str | None = None  # required when role=tool


@dataclass(slots=True)
class ContextBlock:
    block_id: str
    kind: ContextBlockKind
    cache_class: CacheClass
    content_summary: str
    source: str
    content_ref: str | None = None
    enters_model: bool = True
    enters_snapshot: bool = True


@dataclass(slots=True)
class SystemTimeBlock:
    system_time: SystemTime | None  # None = degraded mode
    last_user_ts: float | None = None
    last_assistant_ts: float | None = None
    gap_seconds: float | None = None
    resume_type: ResumeType = ResumeType.COLD_START
    has_previous: bool = False


@dataclass(slots=True)
class MediaContextItem:
    item_id: str
    kind: str  # "image" | "voice"
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TruncationRecord:
    block_id: str
    original_estimated_tokens: int
    after_truncation_tokens: int
    strategy: str  # "head_trim" | "tail_only" | "exclude_block"
    detail: str = ""


@dataclass(slots=True)
class TokenBudgetReport:
    budget: int
    used: int
    remaining: int
    budget_exhausted: bool = False
    truncation_applied: bool = False
    truncation_details: list[TruncationRecord] = field(default_factory=list)


@dataclass(slots=True)
class ContextSnapshot:
    snapshot_id: str
    turn_id: str
    call_id: str
    created_at: datetime
    block_summaries: list[ContextBlock] = field(default_factory=list)
    exposed_tool_names: list[str] = field(default_factory=list)
    history_message_ids: list[str] = field(default_factory=list)
    memory_ids: list[str] = field(default_factory=list)
    turn_local_summary: dict[str, int] = field(default_factory=dict)
    terminal_policy: TerminalPolicy | None = None
    full_payload_ref: str | None = None
    token_budget_report: TokenBudgetReport | None = None


@dataclass(slots=True)
class BuildContextRequest:
    # Identity
    turn_id: str
    call_id: str
    turn_type: TurnType
    user_id: str
    bot_id: str
    # Current input
    inbound_batch: InboundMessageBatch | None = None
    trigger: RuntimeTrigger | None = None
    # Turn-local
    current_turn_local_state: CurrentTurnLocalState | None = None
    # Tool exposure
    tool_exposure: ToolExposure | None = None
    # Terminal policy
    terminal_policy: TerminalPolicy | None = None
    # Memory
    memory_selection: MemorySelection | None = None
    # Conversation history (completed turns only)
    recent_history: list[ConversationHistoryMessage] = field(default_factory=list)
    # Relationship
    relationship_state: Any | None = None
    # Persona / fixed prompts
    persona: str = ""
    fixed_system_protocol: str = ""
    fixed_output_protocol: str = ""
    fixed_tool_protocol: str = ""
    # System time
    system_time: SystemTime | None = None
    # Media
    media_context: list[MediaContextItem] = field(default_factory=list)
    # Config
    debug_mode: bool = False
    token_budget: int = 8000
    default_city: str = ""
    # Initiative / reminder state
    initiative_state: Any | None = None
    reminder_state: Any | None = None


@dataclass(slots=True)
class BuiltContext:
    turn_id: str
    call_id: str
    provider_messages: list[ProviderMessage] = field(default_factory=list)
    provider_system: list[dict[str, Any]] = field(default_factory=list)
    exposed_tool_schemas: list[NativeToolSchema] = field(default_factory=list)
    tool_contract_block: ProviderMessage | None = None
    context_blocks: list[ContextBlock] = field(default_factory=list)
    context_snapshot: ContextSnapshot | None = None
    token_budget_report: TokenBudgetReport | None = None
    terminal_policy: TerminalPolicy | None = None
    warnings: list[ContextBuildWarning] = field(default_factory=list)


class BuildContextError(Exception):
    def __init__(self, reason: str, missing_fields: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.missing_fields = missing_fields or []


# ═══════════════════════════════════════════════════════════════════════════════
# Terminal policy
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class TerminalPolicy:
    turn_type: TurnType
    required_terminal_tools: list[str] = field(default_factory=lambda: ["finish_turn"])
    allowed_terminal_states: list[str] = field(default_factory=list)
    requires_next_wake: bool = False
    min_delivered_messages: int = 0
    max_delivered_messages: int = 15
    allows_skip: bool = False
    forced_speak: bool = False
    allows_fact_tools: bool = True
    allows_image_tools: bool = False
    allows_reminder_tools: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# Model call
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class ModelCallResult:
    call_id: str
    turn_id: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_content: str | None = None
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    raw_response: Any | None = None


@dataclass(slots=True)
class ToolExecutionResult:
    tool_call_id: str
    turn_id: str
    tool_name: str
    status: str  # "success" | "rejected" | "failed"
    result_payload: dict[str, Any] = field(default_factory=dict)
    delivery_receipt: DeliveryReceipt | None = None
    error_code: str | None = None
    error_message: str | None = None
    visible_to_model: bool = True


# ═══════════════════════════════════════════════════════════════════════════════
# Memory (lightweight types used by ContextBuilder / TurnEngine contracts)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class MemorySelection:
    profile_memory: list[Any] = field(default_factory=list)
    relationship_memory: list[Any] = field(default_factory=list)
    boundary_memory: list[Any] = field(default_factory=list)
    episode_memory: list[Any] = field(default_factory=list)
    open_thread_memory: list[Any] = field(default_factory=list)
    sent_content_memory: list[Any] = field(default_factory=list)
    selection_reasons: list[str] = field(default_factory=list)
    total_memory_count: int = 0
    token_estimate: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# Conversation history
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class ConversationHistoryMessage:
    message_id: str
    turn_id: str
    role: str  # "user" | "assistant"
    content: Any
    source: str  # "user_message" | "assistant_reply" | "assistant_proactive" | "reminder_reply"
    delivery_status: DeliveryStatus = DeliveryStatus.NOT_APPLICABLE
    created_at: datetime | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# Turn result
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class TurnResult:
    turn_id: str
    terminal_state: str
    success: bool = False
    delivered_messages: list[TurnLocalSentMessage] = field(default_factory=list)
    skipped: bool = False
    failed: bool = False
    model_call_count: int = 0
    tool_call_count: int = 0
    delivered_message_count: int = 0
    send_attempt_count: int = 0
    failed_send_attempt_count: int = 0
    context_snapshot_ids: list[str] = field(default_factory=list)
    model_call_ids: list[str] = field(default_factory=list)
    budget_state: TurnBudgetState | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    next_wake_after: float | None = None
