"""Build the provider system prompt and conversation transcript."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.context_builder import ORDINARY_CHAT_GLOBAL_PROTOCOL
from runtime_contracts import (
    BuildContextError,
    BuildContextRequest,
    BuiltContext,
    ContextBuildWarning,
    ConversationHistoryMessage,
    DeliveryStatus,
    ProviderMessage,
    SystemTime,
    TurnType,
)


@dataclass(slots=True)
class BuiltContextForTurn:
    messages: list[dict[str, Any]] = field(default_factory=list)
    system: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[ContextBuildWarning] = field(default_factory=list)


class ContextBuilder:
    """Single entry point for model input construction."""

    def __init__(self, persona_text: str = "", protocol_dir: str = "prompts") -> None:
        del protocol_dir
        self._persona_fallback = persona_text

    # ── public API ──────────────────────────────────────────────────

    def build(self, request: BuildContextRequest) -> BuiltContext:
        self._validate(request)
        system: list[dict[str, Any]] = []
        messages: list[ProviderMessage] = []
        warnings: list[ContextBuildWarning] = []

        for text in (
            request.fixed_system_protocol,
            request.fixed_output_protocol,
            request.fixed_tool_protocol,
            request.persona or self._persona_fallback,
            self._time_block(request),
            request.tool_exposure.tool_contract_block if request.tool_exposure else "",
        ):
            if str(text or "").strip():
                system.append({"type": "text", "text": str(text)})

        for item in request.recent_history:
            if item.role not in {"user", "assistant"}:
                continue
            if (
                item.role == "assistant"
                and str(item.delivery_status) not in {
                    DeliveryStatus.DELIVERED,
                    DeliveryStatus.DELIVERED.value,
                    "sent_confirmed",
                }
            ):
                continue
            if item.content in (None, ""):
                continue
            messages.append(ProviderMessage(
                role=item.role,
                content=item.content,
            ))

        if not messages:
            warnings.append(ContextBuildWarning(
                source="recent_history",
                detail="no prior conversation history",
                severity="info",
            ))

        return BuiltContext(
            turn_id=request.turn_id,
            call_id=request.call_id,
            provider_messages=messages,
            provider_system=system,
            exposed_tool_schemas=(
                request.tool_exposure.native_tool_schemas
                if request.tool_exposure else []
            ),
            warnings=warnings,
        )

    def build_for_chat(
        self,
        *,
        persona: str = "",
        personality: str = "",
        history: list[dict[str, Any]] | None = None,
        user_message: str = "",
        bot_id: str = "default",
        system_prompt: str = "",
        turn_id: str = "",
        tool_exposure: Any = None,
    ) -> BuiltContextForTurn:
        """Build complete model input for an ordinary chat turn.

        This is the high-level entry point.  It handles persona assembly,
        history conversion, channel wrapping, and system prompt construction
        so that ChatAgent only needs to assign ``turn.messages`` and
        ``turn._provider_system`` from the result.
        """
        history = list(history or [])

        prompt = system_prompt.strip() or "\n\n".join(
            part for part in (persona, personality) if part.strip()
        )

        timezone_info = timezone(timedelta(hours=8))
        now = datetime.now(timezone_info)

        recent_history: list[ConversationHistoryMessage] = []
        for item in history:
            role = str(item.get("role", ""))
            content = item.get("content")
            if role not in {"user", "assistant"} or content in (None, ""):
                continue
            if role == "user" and isinstance(content, str):
                content = self._channel_message(content)
            timestamp = float(item.get("ts", 0) or 0)
            created_at = datetime.fromtimestamp(timestamp, timezone_info) if timestamp else now
            recent_history.append(ConversationHistoryMessage(
                message_id=str(item.get("id", "")),
                turn_id=str(item.get("turn_id", "")),
                role=role,
                content=copy.deepcopy(content),
                source="protocol",
                delivery_status="delivered",
                created_at=created_at,
            ))

        request = BuildContextRequest(
            turn_id=turn_id or f"ctx_{id(history)}",
            call_id=f"ctx_{turn_id or id(history)}",
            turn_type=TurnType.USER_CHAT,
            user_id="default",
            bot_id=bot_id,
            persona=prompt,
            fixed_system_protocol=ORDINARY_CHAT_GLOBAL_PROTOCOL,
            system_time=SystemTime(
                dt=now,
                timezone="Asia/Shanghai",
                weekday_cn=["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
            ),
            recent_history=recent_history,
            tool_exposure=tool_exposure,
        )

        built = self.build(request)
        messages: list[dict[str, Any]] = [
            {"role": item.role, "content": copy.deepcopy(item.content)}
            for item in built.provider_messages
        ]
        messages.append({
            "role": "user",
            "content": self._channel_message(user_message),
        })

        return BuiltContextForTurn(
            messages=messages,
            system=copy.deepcopy(built.provider_system),
            warnings=built.warnings,
        )

    # ── internal helpers ────────────────────────────────────────────

    @staticmethod
    def _channel_message(text: str) -> str:
        content = str(text or "").strip()
        if content.startswith("<channel "):
            return content
        return f'<channel source="wechat">\n{content}\n</channel>'

    @staticmethod
    def _validate(request: BuildContextRequest) -> None:
        missing = [
            name
            for name in ("turn_id", "call_id", "turn_type", "user_id", "bot_id")
            if not getattr(request, name, None)
        ]
        if missing:
            raise BuildContextError(
                f"missing required fields: {', '.join(missing)}",
                missing_fields=missing,
            )

    @staticmethod
    def _time_block(request: BuildContextRequest) -> str:
        if request.system_time is None:
            return ""
        value = request.system_time
        return (
            f"Current time: {value.dt:%Y-%m-%d %H:%M:%S} "
            f"{value.weekday_cn} ({value.timezone})."
        )
