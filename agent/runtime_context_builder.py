"""Build the provider system prompt and conversation transcript."""

from __future__ import annotations

from typing import Any

from runtime_contracts import (
    BuildContextError,
    BuildContextRequest,
    BuiltContext,
    ContextBuildWarning,
    DeliveryStatus,
    ProviderMessage,
)


class ContextBuilder:
    """Single entry point for model input construction."""

    def __init__(self, persona_text: str = "", protocol_dir: str = "prompts") -> None:
        del protocol_dir
        self._persona_fallback = persona_text

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
