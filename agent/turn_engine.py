"""TurnEngine — turn lifecycle orchestrator.

Phase 12: Coordinates ChatAgent state machine with facade delegation.
The state machine itself stays in ChatAgent; TurnEngine wires in the
facades built during Phases 2-8 (ToolExposurePolicy, ContextBuilder,
MemorySystem) so ChatAgent no longer owns tool exposure or context
assembly decisions.

Spec: docs/runtime-specs/03-turn-engine-spec.zh.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.state_machine import (
    SOURCE_PROACTIVE_IMPULSE,
    SOURCE_USER_MESSAGE,
    TurnResult,
    TurnScratchpad,
    TurnState,
)
from runtime_contracts import (
    ToolExposure as RToolExposure,
    TurnPhase,
    TurnType,
    UserIntent,
)

logger = logging.getLogger(__name__)


# ── Turn request (simplified for Phase 12 wiring) ──────────────────


@dataclass
class TurnEngineRequest:
    """Self-contained turn request with all data TurnEngine needs."""

    user_message: str | None = None
    trigger: str = "user"
    source: str = SOURCE_USER_MESSAGE
    bot_id: str = "default"
    history: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    wechat_sender: Any = None
    proactive_meta: dict[str, Any] | None = None
    input_message_ids: list[str] | None = None
    input_first_seq: int = 0
    input_latest_seq: int = 0
    input_cutoff_time: float = 0.0
    interrupt_checker: Any = None

    # Phase 12: tool exposure data
    turn_type: TurnType = TurnType.USER_CHAT
    last_decision: str | None = None
    consecutive_skip_count: int = 0


# ── TurnEngine ──────────────────────────────────────────────────────


class TurnEngine:
    """Turn lifecycle orchestrator wrapping ChatAgent + facades.

    The ChatAgent state machine is the execution engine. TurnEngine
    injects facade-derived decisions (tool exposure, context assembly)
    before each state transition that needs them.

    Phase 12: wires ToolExposurePolicy into tool decisions. ContextBuilder
    delegation is structural (the existing _do_build_context is kept but
    the TurnEngine provides the canonical path for new callers).
    """

    def __init__(
        self,
        chat_agent: Any,
        *,
        tool_exposure_policy: Any = None,
        context_builder: Any = None,
        memory_system: Any = None,
    ) -> None:
        self._agent = chat_agent
        self._tool_exposure_policy = tool_exposure_policy
        self._context_builder = context_builder
        self._memory_system = memory_system

    # ── Public API ─────────────────────────────────────────────────

    async def run_turn(self, request: TurnEngineRequest) -> TurnResult:
        """Execute a complete turn through the ChatAgent state machine.

        Injects tool exposure decisions from ToolExposurePolicy
        when available. Falls back to ChatAgent's internal logic
        when the policy facade is not provided.
        """
        # ── Resolve tool exposure ─────────────────────────────────
        allowed_tools_override: list[str] | None = None
        if self._tool_exposure_policy is not None:
            allowed_tools_override = self._resolve_allowed_tools(request)
            if request.source == SOURCE_PROACTIVE_IMPULSE:
                allowed_tools_override = self._resolve_proactive_tools(request)

        # ── Delegate to ChatAgent ─────────────────────────────────
        return await self._agent.chat(
            user_message=request.user_message,
            history=request.history,
            trigger=request.trigger,
            bot_id=request.bot_id,
            system_prompt=request.system_prompt,
            wechat_sender=request.wechat_sender,
            source=request.source,
            proactive_meta=request.proactive_meta,
            input_message_ids=request.input_message_ids,
            input_first_seq=request.input_first_seq,
            input_latest_seq=request.input_latest_seq,
            input_cutoff_time=request.input_cutoff_time,
            interrupt_checker=request.interrupt_checker,
            native_allowed_tools_override=allowed_tools_override,
        )

    # ── Tool exposure delegation ───────────────────────────────────

    def _resolve_allowed_tools(self, request: TurnEngineRequest) -> list[str]:
        """Compute allowed_tools via ToolExposurePolicy facade."""
        try:
            from agent.tool_exposure_policy import ToolExposureRequest

            exposure = self._tool_exposure_policy.resolve(ToolExposureRequest(
                turn_type=request.turn_type,
                trigger_source=request.source,
                user_intent=UserIntent.CHAT,
                last_decision=request.last_decision,
                consecutive_skip_count=request.consecutive_skip_count,
            ))
            return list(exposure.exposed_tool_names)
        except Exception:
            logger.warning("ToolExposurePolicy.resolve failed, falling back to agent defaults", exc_info=True)
            return []

    def _resolve_proactive_tools(self, request: TurnEngineRequest) -> list[str]:
        """Compute proactive turn allowed_tools via ToolExposurePolicy."""
        try:
            from agent.tool_exposure_policy import ToolExposureRequest

            exposure = self._tool_exposure_policy.resolve(ToolExposureRequest(
                turn_type=TurnType.RELATIONSHIP_PROACTIVE,
                trigger_source="scheduler",
                user_intent=UserIntent.CHAT,
                last_decision=request.last_decision,
                consecutive_skip_count=request.consecutive_skip_count,
            ))
            return list(exposure.exposed_tool_names)
        except Exception:
            logger.warning("ToolExposurePolicy.resolve proactive failed", exc_info=True)
            return ["send_text_message", "finish_turn", "get_time", "get_weather"]

    # ── Convenience: turn_type from request ────────────────────────

    @staticmethod
    def classify_turn_type(source: str, proactive_meta: dict[str, Any] | None = None) -> TurnType:
        """Determine turn type from source and metadata."""
        if source == SOURCE_PROACTIVE_IMPULSE:
            meta = proactive_meta or {}
            trigger_kind = str(meta.get("trigger_kind", ""))
            if trigger_kind == "reminder_due":
                return TurnType.USER_REMINDER_DUE
            return TurnType.RELATIONSHIP_PROACTIVE
        return TurnType.USER_CHAT


# ── Factory ─────────────────────────────────────────────────────────


def create_turn_engine(
    chat_agent: Any,
    *,
    tool_exposure_policy: Any = None,
    context_builder: Any = None,
    memory_system: Any = None,
) -> TurnEngine:
    """Factory for TurnEngine with optional facade injection."""
    return TurnEngine(
        chat_agent=chat_agent,
        tool_exposure_policy=tool_exposure_policy,
        context_builder=context_builder,
        memory_system=memory_system,
    )
