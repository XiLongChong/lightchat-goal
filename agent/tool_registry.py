"""ActionRegistry and ToolRegistry — Phase 4B tool/action boundary.

Splits the legacy "工具" concept into two clear categories:

  ActionRegistry  — internal actions interpreted by the system
                     (wechat_reply, schedule_wechat_message, skip)
  ToolRegistry    — external tools executed by the backend
                     (get_time, get_weather, search_memory, send_email, …)

This split prepares for official Tool Calls (Phase 5) where only
native_tool_call_eligible tools will be exposed to the provider.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

SideEffectLevel = str  # "none" | "internal_message" | "low" | "medium" | "high"
HandlerType = str  # "internal" | "dispatch" | "executor"


@dataclass(slots=True)
class ActionDef:
    """Definition of a system-internal action (NOT an external tool).

    These are model-requested operations that the system interprets
    and executes internally — they must NEVER be exposed as native
    function-calling tools to the provider.
    """

    name: str
    description: str
    args_schema: list[str]
    side_effect_level: SideEffectLevel = "none"
    handler_type: HandlerType = "internal"
    requires_confirmation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolDef:
    """Definition of an external tool that the backend executes.

    native_tool_call_eligible marks tools safe for future official
    Tool Calls (no side effects, no internal state mutation).

    args_schema can be either:
      - list[str]: simple arg names (backward-compatible, all typed as string)
      - dict: full JSON Schema properties + required list:
          {"properties": {"city": {"type": "string", "description": "..."}},
           "required": ["city"]}
    """

    name: str
    description: str
    args_schema: list[str] | dict[str, Any]
    side_effect_level: SideEffectLevel = "none"
    requires_confirmation: bool = False
    native_tool_call_eligible: bool = False
    # Optional: runtime check before execution (e.g. SMTP configured)
    enabled_check: Callable[[], bool] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════
# ActionRegistry
# ══════════════════════════════════════════════════════════════════

class ActionRegistry:
    """Registry of system-internal actions.

    These are NOT external tools. They are interpreted by ChatAgent
    and must not be exposed as native function-calling tools.
    """

    def __init__(self):
        self._actions: dict[str, ActionDef] = {}

    def register(self, action: ActionDef) -> None:
        if action.name in self._actions:
            logger.warning("ActionRegistry: overwriting %s", action.name)
        self._actions[action.name] = action

    def has(self, name: str) -> bool:
        return name in self._actions

    def get(self, name: str) -> ActionDef | None:
        return self._actions.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._actions.keys())

    def iter_defs(self):
        return iter(self._actions.values())

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    def __len__(self) -> int:
        return len(self._actions)


# ══════════════════════════════════════════════════════════════════
# ToolRegistry
# ══════════════════════════════════════════════════════════════════

class ToolRegistry:
    """Registry of external tools that the backend executes.

    native_tool_call_eligible tools can be exported for provider-level
    function calling (Phase 5). Others remain JSON-action-only.
    """

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        if tool.name in self._tools:
            logger.warning("ToolRegistry: overwriting %s", tool.name)
        self._tools[tool.name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools.keys())

    def iter_defs(self):
        return iter(self._tools.values())

    def get_native_tool_specs(self, eligible_only: bool = True) -> list[dict[str, Any]]:
        """Export tools as OpenAI-compatible function specs.

        Args:
            eligible_only: if True, only return tools with
                           native_tool_call_eligible=True.

        Returns a list of dicts suitable for the ``tools`` parameter
        of a chat completions request.
        """
        specs: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if eligible_only and not tool.native_tool_call_eligible:
                continue

            # Build JSON Schema parameters from the args_schema
            if isinstance(tool.args_schema, dict):
                # Full schema: {"properties": {...}, "required": [...]}
                parameters: dict[str, Any] = {
                    "type": "object",
                    "properties": tool.args_schema.get("properties", {}),
                }
                if tool.args_schema.get("required"):
                    parameters["required"] = tool.args_schema["required"]
            else:
                # Legacy list-of-strings: all typed as string, no required
                props: dict[str, Any] = {}
                for arg in tool.args_schema:
                    props[arg] = {"type": "string", "description": arg}
                parameters = {
                    "type": "object",
                    "properties": props,
                }

            specs.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": parameters,
                },
            })
        return specs

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    def __len__(self) -> int:
        return len(self._tools)


# ══════════════════════════════════════════════════════════════════
# Pre-built registry instances (module-level singletons)
# ══════════════════════════════════════════════════════════════════

ACTION_REGISTRY = ActionRegistry()
TOOL_REGISTRY = ToolRegistry()


def _build_default_registries() -> None:
    """Populate the module-level registries with the current tool set."""

    # ── Internal actions ──────────────────────────────────────────
    ACTION_REGISTRY.register(ActionDef(
        name="wechat_reply",
        description="回复微信消息。每次调用发一条消息给用户。可以多次调用来分段回复。text 是回复内容，delay 是发送前等待秒数",
        args_schema=["text", "delay"],
        side_effect_level="internal_message",
        handler_type="internal",
    ))
    ACTION_REGISTRY.register(ActionDef(
        name="schedule_wechat_message",
        description="安排未来发送的微信消息。代码会持久化任务并在到期后逐条发送，重启后仍然有效",
        args_schema=["delay_minutes", "messages"],
        side_effect_level="medium",
        handler_type="internal",
    ))
    ACTION_REGISTRY.register(ActionDef(
        name="skip",
        description="不回复当前消息，只在用户明确表示不需要回复或话题自然结束时使用",
        args_schema=["reason"],
        side_effect_level="none",
        handler_type="internal",
    ))

    # ── External tools ────────────────────────────────────────────
    TOOL_REGISTRY.register(ToolDef(
        name="get_time",
        description="获取指定时区的当前时间。默认返回中国时区 (Asia/Shanghai) 的时间",
        args_schema={
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "时区标识符，如 Asia/Shanghai, America/New_York。默认 Asia/Shanghai",
                },
            },
            "required": [],
        },
        side_effect_level="none",
        native_tool_call_eligible=True,
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="get_weather",
        description="查询指定城市的实时天气信息，包括天气状况、温度、湿度、风速",
        args_schema={
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名称，如 北京, 上海, Tokyo, London",
                },
            },
            "required": ["city"],
        },
        side_effect_level="none",
        native_tool_call_eligible=True,
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="search_memory",
        description="按需搜索长期记忆。只有需要过去事实、偏好、项目进展时调用；没有结果就不要编造",
        args_schema=["query", "top_k"],
        side_effect_level="none",
        native_tool_call_eligible=False,  # involves memory strategy — keep JSON-action
        # TODO(native-tools): evaluate search_memory only after bot-scoped
        # context injection, rate limits, and overuse guards are designed.
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="read_persona_file",
        description="读取当前人设文件原文。只能读配置允许的人设文件，不能读任意项目文件",
        args_schema=[],
        side_effect_level="low",
        native_tool_call_eligible=False,  # internal management — keep JSON-action
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="propose_persona_change",
        description="提出人设文件修改草案。不会直接写入，系统会发微信让用户确认后再改",
        args_schema=["content", "reason"],
        side_effect_level="low",
        native_tool_call_eligible=False,  # confirmation flow — keep JSON-action
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="send_email",
        description="发送邮件。需要系统已配置 SMTP。args: to, subject, body",
        args_schema=["to", "subject", "body"],
        side_effect_level="high",
        requires_confirmation=True,
        native_tool_call_eligible=False,  # high risk — must never be native tool call
        metadata={
            "todo": (
                "TODO(safety): route send_email through draft/confirmation "
                "flow before enabling broad autonomous use."
            ),
        },
    ))


# Build on import
_build_default_registries()


# ══════════════════════════════════════════════════════════════════
# Compatibility helpers — keep tools.py TOOL_WHITELIST working
# ══════════════════════════════════════════════════════════════════

def get_all_tool_names() -> frozenset[str]:
    """Return the union of all action + tool names (legacy TOOL_WHITELIST equivalent)."""
    return ACTION_REGISTRY.names() | TOOL_REGISTRY.names()


def is_action(name: str) -> bool:
    return name in ACTION_REGISTRY


def is_external_tool(name: str) -> bool:
    return name in TOOL_REGISTRY
