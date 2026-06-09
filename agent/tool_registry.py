"""Canonical model-visible tool registry."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolDef:
    name: str
    description: str
    args_schema: dict[str, Any]
    side_effect_level: str = "none"
    native_tool_call_eligible: bool = True
    tool_category: str = "fact_tool"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def iter_defs(self):
        return iter(self._tools.values())

    def get_native_tool_specs(
        self,
        eligible_only: bool = True,
        allowed_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        allowed = set(allowed_names) if allowed_names is not None else None
        specs: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if eligible_only and not tool.native_tool_call_eligible:
                continue
            if allowed is not None and tool.name not in allowed:
                continue
            parameters = {
                "type": "object",
                "properties": deepcopy(tool.args_schema.get("properties", {})),
                "additionalProperties": False,
            }
            required = tool.args_schema.get("required", [])
            if required:
                parameters["required"] = list(required)
            specs.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": parameters,
                    "strict": True,
                },
            })
        return specs

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    def __len__(self) -> int:
        return len(self._tools)


TOOL_REGISTRY = ToolRegistry()

TOOL_REGISTRY.register(ToolDef(
    name="send_text_message",
    description=(
        "Send exactly one visible text message to the user. "
        "Call it again only when another separate chat bubble is useful."
    ),
    args_schema={
        "properties": {
            "text": {
                "type": "string",
                "description": "The exact text to send to the user.",
                "minLength": 1,
            },
        },
        "required": ["text"],
    },
    side_effect_level="high",
    tool_category="action_tool",
))

TOOL_REGISTRY.register(ToolDef(
    name="get_time",
    description="Get the current time in an IANA timezone.",
    args_schema={
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA timezone such as Asia/Shanghai.",
            },
        },
        "required": [],
    },
))

TOOL_REGISTRY.register(ToolDef(
    name="get_weather",
    description="Get current weather for a city or region.",
    args_schema={
        "properties": {
            "location": {
                "type": "string",
                "description": "City or region name.",
            },
        },
        "required": ["location"],
    },
))


def get_all_tool_names() -> frozenset[str]:
    return TOOL_REGISTRY.names()


def is_action(name: str) -> bool:
    return name == "send_text_message"


def is_external_tool(name: str) -> bool:
    return name in {"get_time", "get_weather"}
