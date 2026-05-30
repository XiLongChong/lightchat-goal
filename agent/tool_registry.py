"""ActionRegistry and ToolRegistry — Phase 4B tool/action boundary.

Splits the legacy "工具" concept into two clear categories:

  ActionRegistry  — internal actions interpreted by the system
                     (wechat_reply, skip)
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
    tool_category: str = "fact_tool"  # v0.2: fact_tool | terminal_tool | state_tool
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
    """Populate the module-level registries with the v0.2 Phase 1 tool set.

    Only tools that are actively exposed to the model are registered.
    Dead features (search_memory, send_email, read_persona_file,
    propose_persona_change, wechat_reply, skip) are removed.
    """

    # ── Internal actions ──────────────────────────────────────────
    # v0.2: wechat_reply and skip are removed.
    # ActionRegistry kept empty for future SYSTEM_HOOK needs.

    # ── Fact tools ────────────────────────────────────────────────
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
        tool_category="fact_tool",
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
        tool_category="fact_tool",
    ))

    # ── Terminal tools ────────────────────────────────────────────
    TOOL_REGISTRY.register(ToolDef(
        name="send_wechat_messages",
        description=(
            "发送一条或多条微信消息给用户。messages 数组的每个元素是一条独立的微信消息气泡。"
            "每个元素是一条完整发送的消息——不要在单个消息内用换行符分隔多条内容。"
            "如果想发多条微信，必须用多个数组元素，而不是在一个元素里换行。"
            "像真人聊天一样：简单回复可以只发 1 条，需要分段表达、吐槽、补刀、追问时可以发 2-4 条。"
            "不要为了凑数量而拆分，也不要发重复内容。"
        ),
        args_schema={
            "properties": {
                "messages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "微信消息数组，每个字符串是一条消息。最少 1 条，最多 12 条。",
                    "minItems": 1,
                    "maxItems": 12,
                },
                "mode": {
                    "type": "string",
                    "enum": ["normal", "long", "short"],
                    "description": "发送模式：normal=日常聊天, long=允许较长内容, short=短消息模式",
                },
            },
            "required": ["messages"],
        },
        side_effect_level="high",
        requires_confirmation=False,
        native_tool_call_eligible=True,
        tool_category="terminal_tool",
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="skip_reply",
        description=(
            "明确决定不回复当前消息。仅在用户消息不需要回复时调用。"
            "例如：用户说'晚安''好的''嗯''知道了'等自然结束对话的消息。"
            "调用后本轮结束，不发送任何微信消息。"
        ),
        args_schema={
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "简短说明为什么不需要回复，如'用户说晚安，对话自然结束'",
                },
            },
            "required": [],
        },
        side_effect_level="none",
        native_tool_call_eligible=True,
        tool_category="terminal_tool",
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

