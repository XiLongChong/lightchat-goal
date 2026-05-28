"""MessageEnvelope — internal unified message structure (Phase 4A).

This is a compatibility layer. It does NOT change any runtime behaviour.
Future phases (Tool Calls, reasoning_content, multi-provider) will build
on this structure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Roles that are valid in OpenAI-compatible chat completions.
VALID_ROLES = frozenset({"system", "user", "assistant", "tool"})


@dataclass(slots=True)
class MessageEnvelope:
    """Internal representation of a single chat message.

    Design rules:
      - provider_private is NEVER serialised into user-visible text,
        conversation history, or memory extraction.
      - reasoning_content (inside provider_private) is NEVER shown to the user.
      - raw_message (inside provider_private) is a transient runtime reference.
    """

    role: str
    content: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    tool_result: Any = None

    # Reserved for provider-internal state that MUST NOT leak.
    provider_private: dict[str, Any] = field(default_factory=dict)

    # Lightweight metadata for internal routing (not exposed to provider).
    metadata: dict[str, Any] = field(default_factory=dict)

    # Optional timestamp and source tag from legacy history rows.
    ts: float | None = None
    source: str | None = None

    # ── factories ──────────────────────────────────────────────────

    @classmethod
    def from_legacy_history(cls, row: dict[str, Any]) -> "MessageEnvelope":
        """Convert a legacy history row into a MessageEnvelope.

        Legacy rows look like::

            {"role": "user", "content": "...", "ts": 1.2, "source": "proactive"}

        The content is kept as-is — no JSON parsing or sanitisation.
        """
        role = str(row.get("role", "user"))
        if role not in VALID_ROLES:
            role = "user"
        raw_content = row.get("content")
        content = str(raw_content) if raw_content is not None else None
        return cls(
            role=role,
            content=content,
            ts=_safe_float(row.get("ts")),
            source=str(row.get("source", "")) or None,
        )

    @classmethod
    def from_llm_response(
        cls,
        response: Any,  # LLMResponse — imported lazily to avoid circular import
        role: str = "assistant",
    ) -> "MessageEnvelope":
        """Create an assistant envelope from an LLMResponse.

        - content = response.content
        - provider_private captures provider / model / reasoning_content / raw_message
        - tool_calls = response.tool_calls
        - raw_response is NOT stored (too large for long-lived objects)
        """
        content = getattr(response, "content", None) or ""
        provider = getattr(response, "provider", "") or ""
        model = getattr(response, "model", "") or ""
        reasoning = getattr(response, "reasoning_content", "") or ""
        tool_calls = getattr(response, "tool_calls", None)

        return cls(
            role=role,
            content=content,
            tool_calls=list(tool_calls) if tool_calls else None,
            provider_private={
                "provider": provider,
                "model": model,
                "reasoning_content": reasoning,
                # raw_message intentionally omitted — use only for transient debugging
            },
        )

    # ── conversions ────────────────────────────────────────────────

    def to_openai_message(self) -> dict[str, Any]:
        """Convert to an OpenAI-compatible chat completions message dict.

        Never includes provider_private, metadata, ts, or source.
        """
        msg: dict[str, Any] = {"role": self.role}

        if self.content is not None:
            msg["content"] = self.content

        if self.name:
            msg["name"] = self.name

        if self.tool_calls is not None:
            msg["tool_calls"] = self.tool_calls

        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id

        return msg

    def to_deepseek_message(self) -> dict[str, Any]:
        """Alias for to_openai_message — DeepSeek uses the same format."""
        return self.to_openai_message()

    # ── validation ─────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Return a list of structural issues (empty = valid). Never raises."""
        errors: list[str] = []
        if self.role not in VALID_ROLES:
            errors.append(f"invalid_role: {self.role}")
        if self.role == "tool" and not self.tool_call_id:
            errors.append("tool role requires tool_call_id")
        if self.tool_call_id and self.role != "tool":
            errors.append(f"tool_call_id set but role={self.role} (expected tool)")
        return errors


# ── batch helpers ──────────────────────────────────────────────────

def message_envelopes_to_provider_messages(
    envelopes: list[MessageEnvelope],
    provider: str = "deepseek",
) -> list[dict[str, Any]]:
    """Convert a list of envelopes to provider-native message dicts."""
    if provider in ("deepseek", "openai"):
        return [e.to_openai_message() for e in envelopes]
    logger.warning("Unknown provider %s, falling back to OpenAI format", provider)
    return [e.to_openai_message() for e in envelopes]


# ── internal helpers ───────────────────────────────────────────────

def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
