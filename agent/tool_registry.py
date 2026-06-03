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
    """Populate the module-level registries with canonical tools.

    Phase 13: All tools use canonical names only.
    Model-visible: send_text_message, finish_turn, skip_initiative,
    schedule_user_reminder, get_time, get_weather, mark_reminder_delivered,
    apply_forget_directive, generate_image_message, send_image_message,
    send_sticker.
    """

    # ── Fact tools (model-visible) ──────────────────────────────
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

    # ── Phase 13: canonical tool names only ─────────────────────────
    TOOL_REGISTRY.register(ToolDef(
        name="send_text_message",
        description="发送一条文字消息给用户。每次调用只发送一条。想发多条请多次调用。text 不能包含换行符。",
        args_schema={
            "properties": {"text": {"type": "string", "description": "要发送的消息内容。不能为空。不能包含换行符。"}},
            "required": ["text"],
        },
        side_effect_level="high",
        native_tool_call_eligible=True,
        tool_category="action_tool",
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="finish_turn",
        description="显式结束本轮对话。每轮必须以调用 finish_turn 结束。status: replied=已回复, no_reply=不需回复, failed=失败。",
        args_schema={
            "properties": {
                "status": {"type": "string", "enum": ["replied", "no_reply", "failed"], "description": "replied=已回复, no_reply=不需回复, failed=失败"},
                "note": {"type": "string", "description": "简短说明。status=no_reply/failed 时必填。"},
                "next_wake_minutes": {"type": "integer", "description": "仅主动回合：建议下一次主动唤醒的间隔分钟数。"},
            },
            "required": ["status"],
        },
        side_effect_level="none",
        native_tool_call_eligible=True,
        tool_category="terminal_tool",
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="skip_initiative",
        description="本轮主动消息不适合说出口时调用。只能在尚未发送任何消息时调用。必须在强度范围内选择 next_wake_minutes。强制开口阶段不可用。",
        args_schema={
            "properties": {
                "reason": {"type": "string", "enum": ["too_soon", "too_intrusive", "low_value", "repeated", "unsafe", "user_might_be_busy"], "description": "跳过原因"},
                "next_wake_minutes": {"type": "integer", "description": "下次主动唤醒的分钟数。必须在系统允许的范围内。"},
                "note": {"type": "string", "description": "简短补充说明"},
            },
            "required": ["reason", "next_wake_minutes"],
        },
        side_effect_level="none",
        native_tool_call_eligible=True,
        tool_category="terminal_tool",
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="schedule_user_reminder",
        description="创建一个用户授权的未来提醒任务。delay_minutes 和 trigger_at 二选一。",
        args_schema={
            "properties": {
                "message_hint": {"type": "string", "description": "提醒时给 LLM 看的备忘，描述提醒内容。最多 500 字。"},
                "delay_minutes": {"type": "integer", "description": "从现在起延迟多少分钟提醒。与 trigger_at 二选一。"},
                "trigger_at": {"type": "string", "description": "绝对提醒时间。格式 HH:mm（每天）或 ISO 时间戳。"},
                "recurring": {"type": "string", "enum": [None, "daily"], "description": "null=一次性, daily=每天重复"},
                "skip_quiet_hours": {"type": "boolean", "description": "true=勿扰期间照发"},
                "title": {"type": "string", "description": "可选短标题"},
            },
            "required": ["message_hint"],
        },
        side_effect_level="internal_message",
        native_tool_call_eligible=True,
        tool_category="state_tool",
    ))

    # ── Phase 2: new-tool-name stubs (executor wiring in later phases) ──
    TOOL_REGISTRY.register(ToolDef(
        name="mark_reminder_delivered",
        description="标记提醒已送达。提醒到期后发送完消息必须调用此工具标记完成。",
        args_schema={
            "properties": {
                "reminder_task_id": {
                    "type": "string",
                    "description": "已送达的提醒任务 ID",
                },
            },
            "required": ["reminder_task_id"],
        },
        side_effect_level="internal_message",
        native_tool_call_eligible=True,
        tool_category="state_tool",
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="apply_forget_directive",
        description="应用用户的'别记这个'指令，屏蔽相关记忆并设置遗忘窗口。",
        args_schema={
            "properties": {
                "directive_text": {
                    "type": "string",
                    "description": "用户要求忘记的内容描述",
                },
                "scope": {
                    "type": "string",
                    "enum": ["local_window", "topic"],
                    "description": "local_window=屏蔽前后几轮, topic=屏蔽整个话题类别",
                },
            },
            "required": ["directive_text"],
        },
        side_effect_level="internal_message",
        native_tool_call_eligible=True,
        tool_category="state_tool",
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="generate_image_message",
        description="根据描述生成一张图片并发送给用户。",
        args_schema={
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "图片生成提示词，描述想要的图片内容",
                },
            },
            "required": ["prompt"],
        },
        side_effect_level="high",
        native_tool_call_eligible=True,
        tool_category="action_tool",
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="send_image_message",
        description="发送一张已有图片给用户。",
        args_schema={
            "properties": {
                "image_ref": {
                    "type": "string",
                    "description": "图片引用地址或 ID",
                },
            },
            "required": ["image_ref"],
        },
        side_effect_level="high",
        native_tool_call_eligible=True,
        tool_category="action_tool",
    ))
    TOOL_REGISTRY.register(ToolDef(
        name="send_sticker",
        description="发送一个表情包给用户。",
        args_schema={
            "properties": {
                "sticker_id": {
                    "type": "string",
                    "description": "表情包 ID",
                },
            },
            "required": ["sticker_id"],
        },
        side_effect_level="high",
        native_tool_call_eligible=True,
        tool_category="action_tool",
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
