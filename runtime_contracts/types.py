"""Data contracts shared by context building and tool exposure."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TurnType(StrEnum):
    USER_CHAT = "user_chat"


class UserIntent(StrEnum):
    UNKNOWN = "unknown"
    CHAT = "chat"


class TurnPhase(StrEnum):
    NORMAL_CHAT = "normal_chat"


class ToolCategory(StrEnum):
    OUTPUT = "output"
    INFORMATION = "information"
    MEMORY = "memory"


class ToolSideEffect(StrEnum):
    NONE = "none"
    SENDS_MESSAGE = "sends_message"
    READS_EXTERNAL = "reads_external"
    WRITES_STATE = "writes_state"


class DeliveryStatus(StrEnum):
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(slots=True)
class SystemTime:
    dt: datetime
    timezone: str
    weekday_cn: str


@dataclass(slots=True)
class ProviderMessage:
    role: str
    content: Any


@dataclass(slots=True)
class ConversationHistoryMessage:
    message_id: str
    turn_id: str
    role: str
    content: Any
    source: str
    delivery_status: DeliveryStatus | str = DeliveryStatus.NOT_APPLICABLE
    created_at: datetime | None = None


@dataclass(slots=True)
class NativeToolSchema:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    terminal: bool = False
    side_effect: ToolSideEffect = ToolSideEffect.NONE
    category: ToolCategory = ToolCategory.OUTPUT


@dataclass(slots=True)
class ToolExposure:
    exposure_id: str
    turn_id: str
    phase: TurnPhase
    exposed_tool_names: list[str] = field(default_factory=list)
    native_tool_schemas: list[NativeToolSchema] = field(default_factory=list)
    tool_contract_block: str = ""
    denied_tools: list[Any] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ContextBuildWarning:
    source: str
    detail: str
    severity: str = "warning"


@dataclass(slots=True)
class BuildContextRequest:
    turn_id: str
    call_id: str
    turn_type: TurnType
    user_id: str
    bot_id: str
    persona: str = ""
    fixed_system_protocol: str = ""
    fixed_output_protocol: str = ""
    fixed_tool_protocol: str = ""
    system_time: SystemTime | None = None
    recent_history: list[ConversationHistoryMessage] = field(default_factory=list)
    tool_exposure: ToolExposure | None = None
    token_budget: int = 8000


@dataclass(slots=True)
class BuiltContext:
    turn_id: str
    call_id: str
    provider_messages: list[ProviderMessage] = field(default_factory=list)
    provider_system: list[dict[str, Any]] = field(default_factory=list)
    exposed_tool_schemas: list[NativeToolSchema] = field(default_factory=list)
    warnings: list[ContextBuildWarning] = field(default_factory=list)


class BuildContextError(Exception):
    def __init__(self, reason: str, missing_fields: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.missing_fields = missing_fields or []
