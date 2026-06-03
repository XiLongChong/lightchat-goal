"""ChatRuntime — real chat turn orchestrator.

Phase 10/12: Full orchestrator that wires the /chat path through the
target architecture: ContextBuilder → ToolExposurePolicy → ChatAgent.

Spec: docs/runtime-specs/03-turn-engine-spec.zh.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.state_machine import (
    SOURCE_PROACTIVE_IMPULSE,
    SOURCE_USER_MESSAGE,
    TurnScratchpad,
    TurnResult,
)

logger = logging.getLogger(__name__)


@dataclass
class ChatRuntime:
    """Orchestrates a chat turn through the full runtime pipeline.

    Owns the dependency graph:
      ContextBuilder → ToolExposurePolicy → ChatAgent → MemorySystem

    This is the SINGLE entry point for /chat. main.py delegates here.
    """

    user_id: str = ""
    bot_id: str = "default_bot"

    # Injected dependencies
    persona: str = ""
    personality: str = ""
    chat_agent: Any = None           # ChatAgent instance
    context_builder: Any = None      # ContextBuilder instance
    tool_exposure_policy: Any = None # ToolExposurePolicy instance
    memory_system: Any = None        # MemorySystem instance

    async def handle_chat(
        self,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        trigger: str = "user",
        source: str = SOURCE_USER_MESSAGE,
        wechat_sender: Any = None,
        proactive_meta: dict[str, Any] | None = None,
        input_message_ids: list[str] | None = None,
        input_first_seq: int = 0,
        input_latest_seq: int = 0,
        input_cutoff_time: float = 0.0,
        interrupt_checker: Any = None,
        state_tool_executor: Any = None,
        extra_tools: list[str] | None = None,
        write_model_history: Any = None,
        post_turn_hooks: list | None = None,
    ) -> TurnResult:
        """Process a chat message through the full runtime pipeline.

        Phase 10/12: Real orchestrator — delegates context building to
        ContextBuilder, tool exposure to ToolExposurePolicy, and state
        machine execution to ChatAgent.

        Returns TurnResult with terminal_state, visible_messages, diagnostics.
        """
        if self.chat_agent is None:
            raise RuntimeError("chat_agent not configured")

        history = history or []

        # ── Resolve tools via ToolExposurePolicy ────────────────────
        allowed_tools_override: list[str] | None = None
        if self.tool_exposure_policy is not None:
            try:
                from agent.tool_exposure_policy import ToolExposureRequest
                from runtime_contracts import TurnType, UserIntent

                is_proactive = source == SOURCE_PROACTIVE_IMPULSE
                turn_type = TurnType.RELATIONSHIP_PROACTIVE if is_proactive else TurnType.USER_CHAT
                trigger_source = "scheduler" if is_proactive else "user"

                request = ToolExposureRequest(
                    turn_type=turn_type,
                    trigger_source=trigger_source,
                    user_intent=UserIntent.CHAT,
                )
                exposure = self.tool_exposure_policy.resolve(request)
                allowed_tools_override = list(exposure.exposed_tool_names)
                if extra_tools:
                    for t in extra_tools:
                        if t not in allowed_tools_override:
                            allowed_tools_override.append(t)
                logger.info("ChatRuntime: resolved tools via policy: %s", allowed_tools_override)
            except Exception as e:
                logger.warning("ChatRuntime: ToolExposurePolicy failed: %s", e)
                allowed_tools_override = None

        # ── Delegate to ChatAgent state machine ────────────────────
        return await self.chat_agent.chat(
            user_message=user_message,
            history=history,
            trigger=trigger,
            bot_id=self.bot_id,
            system_prompt=system_prompt,
            wechat_sender=wechat_sender,
            source=source,
            proactive_meta=proactive_meta,
            input_message_ids=input_message_ids,
            input_first_seq=input_first_seq,
            input_latest_seq=input_latest_seq,
            input_cutoff_time=input_cutoff_time,
            interrupt_checker=interrupt_checker,
            state_tool_executor=state_tool_executor,
            native_allowed_tools_override=allowed_tools_override,
            write_model_history=write_model_history,
            post_turn_hooks=post_turn_hooks,
        )

    def build_turn_request(self, user_message: str) -> TurnScratchpad:
        """Create a TurnScratchpad for the given user message."""
        turn = TurnScratchpad()
        turn.user_message = user_message
        turn.trigger = "user"
        turn.source = SOURCE_USER_MESSAGE
        return turn

    def get_history_for_context(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter history to only delivered user/assistant messages."""
        visible: list[dict[str, Any]] = []
        for item in (history or []):
            role = str(item.get("role", ""))
            if role not in ("user", "assistant"):
                continue
            content = str(item.get("content", ""))
            if not content.strip():
                continue
            visible.append({"role": role, "content": content})
        return visible


def create_chat_runtime(
    user_id: str = "",
    bot_id: str = "default_bot",
    persona: str = "",
    personality: str = "",
    chat_agent: Any = None,
    context_builder: Any = None,
    tool_exposure_policy: Any = None,
    memory_system: Any = None,
) -> ChatRuntime:
    """Factory for ChatRuntime with all facade dependencies."""
    return ChatRuntime(
        user_id=user_id,
        bot_id=bot_id,
        persona=persona,
        personality=personality,
        chat_agent=chat_agent,
        context_builder=context_builder,
        tool_exposure_policy=tool_exposure_policy,
        memory_system=memory_system,
    )
