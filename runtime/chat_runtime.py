"""Ordinary chat runtime entry point."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.state_machine import SOURCE_USER_MESSAGE, TurnResult, TurnScratchpad


@dataclass
class ChatRuntime:
    user_id: str = ""
    bot_id: str = "default"
    persona: str = ""
    personality: str = ""
    chat_agent: Any = None
    context_builder: Any = None
    tool_exposure_policy: Any = None

    async def handle_chat(
        self,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        trigger: str = "user",
        source: str = SOURCE_USER_MESSAGE,
        wechat_sender: Any = None,
        input_message_ids: list[str] | None = None,
        input_first_seq: int = 0,
        input_latest_seq: int = 0,
        input_cutoff_time: float = 0.0,
        interrupt_checker: Any = None,
        write_model_history: Any = None,
        post_turn_hooks: list | None = None,
        protocol_event_writer: Any = None,
    ) -> TurnResult:
        if self.chat_agent is None:
            raise RuntimeError("chat_agent not configured")
        return await self.chat_agent.chat(
            user_message=user_message,
            history=history or [],
            trigger=trigger,
            bot_id=self.bot_id,
            system_prompt=system_prompt,
            wechat_sender=wechat_sender,
            source=source,
            input_message_ids=input_message_ids,
            input_first_seq=input_first_seq,
            input_latest_seq=input_latest_seq,
            input_cutoff_time=input_cutoff_time,
            interrupt_checker=interrupt_checker,
            native_allowed_tools_override=[
                "send_text_message",
                "get_time",
                "get_weather",
            ],
            write_model_history=write_model_history,
            post_turn_hooks=post_turn_hooks,
            protocol_event_writer=protocol_event_writer,
        )

    def build_turn_request(self, user_message: str) -> TurnScratchpad:
        return TurnScratchpad(
            user_message=user_message,
            trigger="user",
            source=SOURCE_USER_MESSAGE,
        )

    def get_history_for_context(
        self,
        history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {"role": item["role"], "content": item["content"]}
            for item in history
            if item.get("role") in {"user", "assistant"}
            and str(item.get("content", "")).strip()
        ]


def create_chat_runtime(**kwargs: Any) -> ChatRuntime:
    return ChatRuntime(**kwargs)
